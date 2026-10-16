#!/usr/bin/env python3
"""SEC-007. No env sourcing, shell execution, printed credentials, or implicit restore."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from uuid import uuid4

ENVIRONMENTS = ('dev', 'qa', 'staging', 'prod')

MEMBERS = ('database.dump', 'schema.sql', 'database-evidence.json', 'manifest.json')
MAX_METADATA = 1024 * 1024
TIMEOUT = 3600

class RecoveryError(Exception):
    """Closed error category; subprocess diagnostics may contain secrets."""

def validate_environment(value) -> None:
    if type(value) is not str or value not in ENVIRONMENTS:
        raise RecoveryError('invalid-environment')

def utc() -> str:
    return datetime.now(timezone.utc).isoformat()

def epoch(value: str) -> float:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise RecoveryError('invalid-timestamp')
    return parsed.timestamp()

def digest(path: Path, algorithm: str = 'sha256') -> str:
    result = hashlib.new(algorithm)
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(65536), b''):
            result.update(chunk)
    return result.hexdigest()

def private_dir(path: Path) -> Path:
    if path.is_symlink():
        raise RecoveryError('unsafe-directory')
    if not path.exists():
        path.mkdir(mode=0o700, parents=False)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise RecoveryError('unsafe-directory')
    return path.resolve()

def regular(path: Path, limit: int, secret: bool = False) -> Path:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit or info.st_size == 0:
        raise RecoveryError('unsafe-file')
    if secret and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise RecoveryError('unsafe-secret-file')
    return path

def read_json(path: Path) -> dict:
    regular(path, MAX_METADATA)
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise RecoveryError('invalid-metadata')
    return value

def write_json(path: Path, value: dict) -> None:
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        try:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

def tool_environment(program: str) -> dict[str, str]:
    common = {'PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SSL_CERT_DIR'}
    result = {k: v for k, v in os.environ.items() if k in common or k.startswith('LC_')}
    name = Path(program).name
    prefix = 'PG' if name in ('psql', 'initdb', 'createdb') or name.startswith('pg_') else 'B2_' if name == 'b2' else None
    if prefix:
        result.update({k: v for k, v in os.environ.items() if k.startswith(prefix)})
    if os.environ.get('XYNES_BACKUP_EXECUTABLE_FIXTURES') == 'inert-fixtures-only':
        result.update({k: v for k, v in os.environ.items() if k.startswith('FIXTURE_')})
    return result

def command(argv: list[str], output: Path | None = None, limit: int = MAX_METADATA) -> bytes:
    # All diagnostics stay private and are discarded, never sent to shared logs.
    with tempfile.TemporaryFile() as errors, tempfile.TemporaryFile() as captured:
        with output.open('xb') if output else captured as destination:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors, env=tool_environment(argv[0]))
            selector = selectors.DefaultSelector()
            assert process.stdout is not None
            selector.register(process.stdout, selectors.EVENT_READ)
            started, size = time.monotonic(), 0
            try:
                while selector.get_map():
                    if time.monotonic() - started > TIMEOUT:
                        raise RecoveryError('command-timeout')
                    for key, _ in selector.select(timeout=1):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        size += len(chunk)
                        if size > limit:
                            raise RecoveryError('command-output-limit')
                        destination.write(chunk)
                if process.wait(timeout=10) != 0:
                    raise RecoveryError('external-command-failed')
                destination.flush()
                os.fsync(destination.fileno())
                if not output:
                    destination.seek(0)
                    return destination.read()
                return b''
            finally:
                selector.close()
                process.stdout.close()
                if process.poll() is None:
                    process.kill()
                    process.wait()

def pg_connection() -> tuple[str, int, str]:
    host, port, database = (os.environ.get(k, '') for k in ('PGHOST', 'PGPORT', 'PGDATABASE'))
    if not host or not re.fullmatch(r'[0-9]{1,5}', port) or not 1 <= int(port) <= 65535 or not database:
        raise RecoveryError('explicit-pg-connection-required')
    if not os.environ.get('PGUSER'):
        raise RecoveryError('explicit-pg-user-required')
    # Reject connection-string interpretation; secrets belong in libpq env/passfile.
    if any(c in database for c in '=:/\n\r'):
        raise RecoveryError('invalid-database-name')
    if os.environ.get('PGSERVICE') or os.environ.get('PGSERVICEFILE') or os.environ.get('PGHOSTADDR'):
        raise RecoveryError('ambiguous-pg-service-config')
    return host, int(port), database

def sql(query: str) -> str:
    settings = "SET search_path=pg_catalog; SET TIME ZONE 'UTC'; SET DateStyle='ISO'; SET extra_float_digits=3; SET statement_timeout=1800000; "
    return command(['psql', '-X', '-qAt', '--no-password', '--set=ON_ERROR_STOP=1', '--command', settings + query]).decode().strip()

class Snapshot:
    def __enter__(self):
        self.errors = tempfile.TemporaryFile()
        self.process = subprocess.Popen(['psql', '-X', '-qAt', '--no-password', '--set=ON_ERROR_STOP=1'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errors, env=tool_environment('psql'))
        try:
            value = self.query("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SET LOCAL search_path=pg_catalog; SET LOCAL TIME ZONE 'UTC'; SET LOCAL DateStyle='ISO'; SET LOCAL extra_float_digits=3; SET LOCAL statement_timeout=1800000; SELECT pg_export_snapshot();")
            if not re.fullmatch(r'[0-9A-Fa-f-]{1,80}', value):
                raise RecoveryError('invalid-snapshot')
            self.identifier = value
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def query(self, query: str) -> str:
        assert self.process.stdin is not None and self.process.stdout is not None
        self.process.stdin.write((query + '\n').encode())
        self.process.stdin.flush()
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            started, result = time.monotonic(), bytearray()
            while not result.endswith(b'\n'):
                if time.monotonic() - started > TIMEOUT:
                    raise RecoveryError('snapshot-timeout')
                if not selector.select(timeout=1):
                    continue
                chunk = os.read(self.process.stdout.fileno(), 65536)
                result.extend(chunk)
                if not chunk or len(result) > MAX_METADATA:
                    raise RecoveryError('snapshot-query-failed')
        return result.decode().strip()

    def __exit__(self, *_):
        if self.process.poll() is None:
            self.process.kill()  # Rolls back the read-only snapshot; no source mutation.
        self.process.wait()
        if self.process.stdin:
            self.process.stdin.close()
        if self.process.stdout:
            self.process.stdout.close()
        self.errors.close()

TABLES_SQL = "SELECT coalesce(json_agg(n.nspname || '.' || c.relname ORDER BY n.nspname, c.relname), '[]') FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p') AND n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema';"
SCHEMAS_SQL = "SELECT coalesce(json_agg(nspname ORDER BY nspname), '[]') FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema';"
COLUMNS_SQL = "SELECT coalesce(json_agg(json_build_object('table', n.nspname || '.' || c.relname, 'column', a.attname, 'ordinal', a.attnum, 'type', format_type(a.atttypid, a.atttypmod), 'not_null', a.attnotnull, 'identity', a.attidentity, 'generated', a.attgenerated, 'default', pg_get_expr(d.adbin,d.adrelid)) ORDER BY n.nspname,c.relname,a.attnum), '[]') FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum WHERE a.attnum>0 AND NOT a.attisdropped AND c.relkind IN ('r','p') AND n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema';"

def table_sql(table: str) -> str:
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*', table):
        raise RecoveryError('invalid-verification-table')
    schema, name = table.split('.')
    # Only explicitly selected representative/migration tables: aggregate can be expensive.
    return f'''SELECT json_build_object('rows', count(*), 'row_md5', md5(coalesce(string_agg(md5(to_jsonb(t)::text), '' ORDER BY md5(to_jsonb(t)::text)), ''))) FROM "{schema}"."{name}" t;'''

def evidence(query, requested: list[str]) -> dict:
    inventory = json.loads(query(TABLES_SQL))
    if not isinstance(inventory, list) or not all(isinstance(t, str) for t in inventory):
        raise RecoveryError('invalid-table-inventory')
    migrations = [t for t in inventory if t.split('.')[-1] in ('schema_migrations', '__drizzle_migrations', 'migrations')]
    chosen = sorted(set(requested + migrations))
    if not requested or any(t not in inventory for t in chosen):
        raise RecoveryError('verification-table-missing')
    result = {'schemas': json.loads(query(SCHEMAS_SQL)), 'columns': json.loads(query(COLUMNS_SQL)), 'tables': inventory, 'migration_tables': migrations, 'verified_tables': {t: json.loads(query(table_sql(t))) for t in chosen}}
    validate_evidence(result)
    return result

def validate_evidence(value: dict) -> None:
    if set(value) != {'schemas', 'columns', 'tables', 'migration_tables', 'verified_tables'}:
        raise RecoveryError('invalid-database-evidence')
    for field in ('schemas', 'tables', 'migration_tables'):
        entries = value[field]
        if not isinstance(entries, list) or not all(isinstance(v, str) and v for v in entries) or len(entries) != len(set(entries)):
            raise RecoveryError('invalid-database-evidence')
    if not isinstance(value['columns'], list) or not all(isinstance(v, dict) and isinstance(v.get('table'), str) and v['table'] in value['tables'] for v in value['columns']):
        raise RecoveryError('invalid-database-evidence')
    verified = value['verified_tables']
    if not isinstance(verified, dict) or not verified or any(t not in value['tables'] for t in verified) or any(t not in verified for t in value['migration_tables']):
        raise RecoveryError('invalid-database-evidence')
    for table, row in verified.items():
        table_sql(table)
        if not isinstance(row, dict) or set(row) != {'rows', 'row_md5'} or type(row['rows']) is not int or row['rows'] < 0 or not isinstance(row['row_md5'], str) or not re.fullmatch(r'[a-f0-9]{32}', row['row_md5']):
            raise RecoveryError('invalid-database-evidence')

def upload(path: Path, bucket: str, name: str) -> dict:
    sha1 = digest(path, 'sha1')
    raw = command(['b2', 'file', 'upload', '--no-progress', '--sha1', sha1, bucket, str(path), name])
    try:
        lines = raw.decode().splitlines()
        # v4 emits two documented URL lines before the SDK JSON receipt.
        if len(lines) >= 3 and lines[0].startswith('URL by file name: ') and lines[1].startswith('URL by fileId: '):
            lines = lines[2:]
        response = json.loads('\n'.join(lines))
    except (ValueError, UnicodeError):
        raise RecoveryError('upload-receipt-invalid') from None
    if not isinstance(response, dict) or response.get('action') != 'upload' or response.get('fileName') != name or type(response.get('size')) is not int or response['size'] != path.stat().st_size or not isinstance(response.get('fileId'), str) or not response['fileId'].strip():
        raise RecoveryError('upload-receipt-invalid')
    info = response.get('fileInfo', {})
    returned = response.get('contentSha1')
    if returned != sha1 and not (returned == 'none' and isinstance(info, dict) and info.get('large_file_sha1') == sha1):
        raise RecoveryError('upload-checksum-mismatch')
    return {'file_id': response['fileId'], 'name': name, 'sha1': sha1}

def validated_release_binding(value: dict) -> dict:
    """Closed optional coordinator provenance; never caller-selected metadata."""
    expected={'accepted_token':r'[a-f0-9]{64}',
              'accepted_source_sha256':r'sha256:[a-f0-9]{64}',
              'database_container_id':r'[a-f0-9]{64}'}
    if type(value) is not dict or set(value)!=set(expected) or any(
            type(value[key]) is not str or not re.fullmatch(pattern,value[key])
            for key,pattern in expected.items()):
        raise RecoveryError('invalid-release-binding')
    return value.copy()

def backup(args, *, release_binding: dict | None = None) -> dict:
    binding=validated_release_binding(release_binding) if release_binding is not None else None
    validate_environment(args.environment)
    pg_connection()
    output = private_dir(args.output_dir)
    regular(args.recipient_file, 4096)
    info = args.recipient_file.stat()
    recipients = [line.strip() for line in args.recipient_file.read_text().splitlines() if line.strip() and not line.strip().startswith('#')]
    if info.st_uid != os.geteuid() or info.st_mode & 0o022 or not recipients or not all(re.fullmatch(r'age1[a-z0-9]{58}', line) for line in recipients):
        raise RecoveryError('native-age-recipient-required')
    if args.upload and not re.fullmatch(r'[A-Za-z0-9-]{6,63}', args.bucket or ''):
        raise RecoveryError('explicit-b2-bucket-required')
    started = utc()
    name = f'{args.environment}-{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}-{uuid4().hex}'
    final = output / name
    with tempfile.TemporaryDirectory(prefix='.backup-', dir=output) as temporary:
        stage = Path(temporary)
        recipient_snapshot = stage / 'recipients.txt'
        recipient_snapshot.write_text('\n'.join(recipients) + '\n')
        with Snapshot() as snapshot:
            source = evidence(snapshot.query, args.verify_table)
            command(['pg_dump', '--no-password', '--format=custom', '--no-owner', '--no-acl', '--snapshot', snapshot.identifier], stage / 'database.dump', args.max_bytes)
        command(['pg_restore', '--schema-only', '--no-owner', '--no-acl', '--file=-', str(stage / 'database.dump')], stage / 'schema.sql', args.max_bytes)
        write_json(stage / 'database-evidence.json', source)
        manifest = {'version': 1, 'environment': args.environment, 'snapshot_at': started, 'pg_dump_version': command(['pg_dump', '--version']).decode().strip(), 'files': {p: {'sha256': digest(stage / p), 'bytes': (stage / p).stat().st_size} for p in MEMBERS[:-1]}}
        if binding is not None:manifest['release_binding']=binding
        write_json(stage / 'manifest.json', manifest)
        archive = stage / 'bundle.tar'
        with tarfile.open(archive, 'w') as bundle:
            for p in MEMBERS:
                bundle.add(stage / p, arcname=p, recursive=False)
        regular(archive, args.max_bytes)
        encrypted = stage / 'backup.tar.age'
        command(['age', '--recipients-file', str(recipient_snapshot), str(archive)], encrypted, args.max_bytes)
        regular(encrypted, args.max_bytes)
        record = {'version': 1, 'environment': args.environment, 'snapshot_at': started, 'completed_at': utc(), 'sha256': digest(encrypted), 'bytes': encrypted.stat().st_size, 'uploaded': False}
        if binding is not None:record['release_binding']=binding
        for path in [archive, recipient_snapshot, *(stage / p for p in MEMBERS)]:
            path.unlink()
        write_json(stage / 'record.json', record)
        os.rename(stage, final)  # Only ciphertext and non-sensitive receipt are published.
    if args.upload:
        receipt = upload(final / 'backup.tar.age', args.bucket, f'{args.environment}/{name}/backup.tar.age')
        ready = {**record, 'upload': receipt, 'uploaded': True, 'completed_at': utc()}
        with tempfile.TemporaryDirectory(prefix='.receipt-', dir=output) as temporary:
            pending = Path(temporary) / 'record.json'
            write_json(pending, ready)
            upload(pending, args.bucket, f'{args.environment}/{name}/record.json')
        record = ready
        write_json(final / 'record.json', record)
    return {'status': 'backup-created', 'job': name, **record}

def unpack(archive: Path, destination: Path, limit: int) -> dict:
    seen, total = set(), 0
    with tarfile.open(archive, 'r:') as bundle:
        for member in bundle:
            total += member.size
            if member.name not in MEMBERS or member.name in seen or not member.isfile() or member.size <= 0 or total > limit:
                raise RecoveryError('unsafe-archive')
            seen.add(member.name)
            content = bundle.extractfile(member)
            if content is None:
                raise RecoveryError('unsafe-archive')
            with content, (destination / member.name).open('xb') as target:
                shutil.copyfileobj(content, target, 65536)
    if seen != set(MEMBERS):
        raise RecoveryError('incomplete-archive')
    manifest = read_json(destination / 'manifest.json')
    if type(manifest.get('version')) is not int or manifest['version'] != 1 or not isinstance(manifest.get('files'), dict) or set(manifest['files']) != set(MEMBERS[:-1]):
        raise RecoveryError('invalid-manifest')
    for name, expected in manifest['files'].items():
        path = destination / name
        if not isinstance(expected, dict) or expected.get('sha256') != digest(path) or type(expected.get('bytes')) is not int or expected['bytes'] != path.stat().st_size:
            raise RecoveryError('bundle-checksum-mismatch')
    if manifest.get('environment') not in ENVIRONMENTS:
        raise RecoveryError('invalid-manifest')
    epoch(manifest['snapshot_at'])
    if 'release_binding' in manifest:validated_release_binding(manifest['release_binding'])
    return manifest

def target_guard(confirmation: str | None) -> None:
    host, port, database = pg_connection()
    if host not in ('127.0.0.1', '::1') or port in (5432, 54320) or not re.fullmatch(r'xynes_restore_[a-z0-9_]{1,50}', database) or confirmation != f'{host}:{port}/{database}':
        raise RecoveryError('approved-isolated-target-required')
    # No --clean, --create, DROP, or restore into an existing app database.
    count = sql("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema' AND c.relkind IN ('r','p','v','m','S','f');")
    if count != '0':
        raise RecoveryError('restore-target-not-empty')

def restore(args) -> dict:
    if not args.approved_isolated_target:
        raise RecoveryError('approved-isolated-target-required')
    output = private_dir(args.report_dir)
    regular(args.identity_file, 65536, secret=True)
    identities = [line.strip() for line in args.identity_file.read_text().splitlines() if line.strip() and not line.strip().startswith('#')]
    if not identities or not all(re.fullmatch(r'AGE-SECRET-KEY-1[0-9A-Z]+', line) for line in identities):
        raise RecoveryError('native-age-identity-required')
    regular(args.backup_file, args.max_bytes)
    if not re.fullmatch(r'[a-f0-9]{64}', args.expected_sha256) or digest(args.backup_file) != args.expected_sha256:
        raise RecoveryError('ciphertext-checksum-mismatch')
    target_guard(args.confirm_target)
    started, started_at = time.monotonic(), utc()
    with tempfile.TemporaryDirectory(prefix='.restore-', dir=output) as temporary:
        stage = Path(temporary)
        identity_snapshot = stage / 'identity.txt'
        identity_snapshot.write_text('\n'.join(identities) + '\n')
        archive = stage / 'bundle.tar'
        command(['age', '--decrypt', '--identity', str(identity_snapshot), str(args.backup_file)], archive, args.max_bytes)
        manifest = unpack(archive, stage, args.max_bytes)
        source = read_json(stage / 'database-evidence.json')
        validate_evidence(source)
        for table in source['verified_tables']:
            table_sql(table)  # Validate all names before any DB writes.
        target_guard(args.confirm_target)
        command(['pg_restore', '--no-password', '--exit-on-error', '--single-transaction', '--no-owner', '--no-acl', '--dbname', os.environ['PGDATABASE'], str(stage / 'database.dump')])
        actual = evidence(sql, list(source['verified_tables']))
        if actual != source:
            raise RecoveryError('restored-evidence-mismatch')
    rpo = epoch(started_at) - epoch(manifest['snapshot_at'])
    rto = time.monotonic() - started
    result = {'status': 'restore-verified', 'version': 1, 'environment': manifest['environment'], 'started_at': started_at, 'completed_at': utc(), 'backup_sha256': args.expected_sha256, 'rpo_seconds': rpo, 'rto_seconds': rto, 'within_rpo_rto': 0 <= rpo <= 86400 and rto <= 7200, 'verified_table_count': len(actual['verified_tables'])}
    write_json(output / f'restore-{uuid4().hex}.json', result)
    return result

def jobs(output: Path, environment: str) -> list[tuple[Path, dict]]:
    validate_environment(environment)
    result = []
    for path in output.iterdir():
        if not re.fullmatch(re.escape(environment) + r'-\d{8}T\d{6}Z-[a-f0-9]{32}', path.name):
            continue
        private_dir(path)
        record = read_json(path / 'record.json')
        if type(record.get('version')) is not int or record['version'] != 1 or record.get('environment') != environment or type(record.get('uploaded')) is not bool or type(record.get('bytes')) is not int or not isinstance(record.get('sha256'), str) or not re.fullmatch(r'[a-f0-9]{64}', record['sha256']):
            raise RecoveryError('invalid-backup-record')
        if 'release_binding' in record:validated_release_binding(record['release_binding'])
        regular(path / 'backup.tar.age', record['bytes'])
        if (path / 'backup.tar.age').stat().st_size != record['bytes'] or epoch(record['snapshot_at']) > time.time() + 300:
            raise RecoveryError('invalid-backup-record')
        result.append((path, record))
    return sorted(result, key=lambda item: epoch(item[1]['snapshot_at']), reverse=True)

def monitor(args) -> dict:
    validate_environment(args.environment)
    candidates = [(p, r) for p, r in jobs(private_dir(args.output_dir), args.environment) if r['uploaded']]
    if not candidates:
        raise RecoveryError('no-successful-offsite-backup')
    path, record = candidates[0]
    age = time.time() - epoch(record['snapshot_at'])
    if not 0 <= age <= 86400 or digest(path / 'backup.tar.age') != record['sha256']:
        raise RecoveryError('backup-stale-or-corrupt')
    return {'status': 'backup-monitor-healthy', 'environment': args.environment, 'checked_at': utc(), 'snapshot_at': record['snapshot_at'], 'sha256': record['sha256']}

def prune(args) -> dict:
    validate_environment(args.environment)
    if not args.confirm_prune or not 7 <= args.days <= 365:
        raise RecoveryError('retention-confirmation-required')
    monitor(args)  # Preserve old copies when the newest offsite backup is unhealthy.
    candidates = [(p, r) for p, r in jobs(private_dir(args.output_dir), args.environment) if r['uploaded']]
    removed = 0
    for path, record in candidates[1:]:  # Never delete the newest successful backup.
        if time.time() - epoch(record['snapshot_at']) > args.days * 86400:
            if set(p.name for p in path.iterdir()) != {'record.json', 'backup.tar.age'}:
                raise RecoveryError('unexpected-retention-files')
            (path / 'backup.tar.age').unlink()
            (path / 'record.json').unlink()
            path.rmdir()
            removed += 1
    return {'status': 'retention-complete', 'removed': removed}

def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest='operation', required=True)
    for name in ('backup', 'restore', 'monitor', 'prune'):
        p = sub.add_parser(name)
        if name != 'restore':
            p.add_argument('--environment', choices=ENVIRONMENTS, required=True)
            p.add_argument('--output-dir', type=Path, required=True)
        if name in ('backup', 'restore'):
            p.add_argument('--max-bytes', type=int, default=10 * 1024**3)
        if name == 'backup':
            p.add_argument('--recipient-file', type=Path, required=True)
            p.add_argument('--verify-table', action='append', default=[], required=True)
            p.add_argument('--upload', action='store_true')
            p.add_argument('--bucket')
        if name == 'restore':
            p.add_argument('--backup-file', type=Path, required=True)
            p.add_argument('--expected-sha256', required=True)
            p.add_argument('--identity-file', type=Path, required=True)
            p.add_argument('--report-dir', type=Path, required=True)
            p.add_argument('--approved-isolated-target', action='store_true')
            p.add_argument('--confirm-target')
        if name == 'prune':
            p.add_argument('--days', type=int, default=7)
            p.add_argument('--confirm-prune', action='store_true')
    return result

def main(argv=None) -> int:
    os.umask(0o077)
    args = parser().parse_args(argv)
    try:
        if hasattr(args, 'max_bytes') and args.max_bytes <= 0:
            raise RecoveryError('invalid-resource-limit')
        value = {'backup': backup, 'restore': restore, 'monitor': monitor, 'prune': prune}[args.operation](args)
        print(json.dumps(value, sort_keys=True))
        return 0 if value.get('within_rpo_rto', True) else 1
    except RecoveryError as error:
        print('RECOVERY_FAIL ' + str(error), file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, subprocess.SubprocessError):
        print('RECOVERY_FAIL operation-failed', file=sys.stderr)
    return 1

if __name__ == '__main__':
    raise SystemExit(main())
