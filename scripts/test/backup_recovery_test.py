"""Inert executable fixtures; no real database, provider or encryption claim."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('backup_recovery', ROOT / 'scripts/lib/backup_recovery.py')
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)

FIXTURE = r'''#!/usr/bin/env python3
import hashlib, json, os, pathlib, sys, time
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
if os.environ.get('FIXTURE_FAIL') == name:
    print('INERT_SECRET_SENTINEL_DO_NOT_LOG', file=sys.stderr)
    sys.exit(1)
def query(sql):
    if 'pg_export_snapshot' in sql: return '00000003-0000001B-1'
    if 'json_agg(n.nspname' in sql: return json.dumps(['platform.workspaces', 'supabase_migrations.schema_migrations'])
    if 'json_agg(nspname' in sql: return json.dumps(['platform', 'public', 'supabase_migrations'])
    if "'ordinal'" in sql: return json.dumps([{'table': 'platform.workspaces', 'column': 'id', 'ordinal': 1, 'type': 'uuid', 'not_null': True, 'identity': '', 'generated': '', 'default': None}])
    if "'row_md5'" in sql: return json.dumps({'rows': 3 if os.environ.get('FIXTURE_DIFFERENT') else 2, 'row_md5': hashlib.md5(b'inert-diagnostic').hexdigest()})
    if 'count(*) FROM pg_class' in sql: return os.environ.get('FIXTURE_TARGET_COUNT', '0')
    return 'unexpected'
if name == 'psql':
    if '--command' in args:
        print(query(args[args.index('--command') + 1]))
    else:
        for line in sys.stdin:
            if os.environ.get('FIXTURE_SNAPSHOT_SLOW'): time.sleep(2)
            print(query(line), flush=True)
elif name == 'pg_dump':
    if '--version' in args: print('pg_dump (PostgreSQL) inert-fixture')
    else: sys.stdout.buffer.write(b'PGDMP-inert-fixture-only')
elif name == 'pg_restore':
    if '--schema-only' in args:
        if '--file=-' not in args: sys.exit(1)  # Real PostgreSQL requires explicit output.
        print('CREATE SCHEMA platform; -- inert fixture')
    else:
        pathlib.Path(os.environ['FIXTURE_RESTORED']).write_text(json.dumps(args))
elif name == 'age':
    data = pathlib.Path(args[-1]).read_bytes()
    if '--decrypt' in args:
        if not data.startswith(b'INERT-AGE-FIXTURE:'): sys.exit(1)
        data = data[len(b'INERT-AGE-FIXTURE:'):]
    else: data = b'INERT-AGE-FIXTURE:' + data
    sys.stdout.buffer.write(data)
elif name == 'b2':
    path, remote = pathlib.Path(args[-2]), args[-1]
    sha1 = hashlib.sha1(path.read_bytes()).hexdigest()
    if os.environ.get('FIXTURE_RECEIPT_FAIL') and path.name == 'record.json': sys.exit(1)
    info = {'action': 'upload', 'fileId': 'inert-file-id', 'fileName': remote, 'size': path.stat().st_size, 'contentSha1': sha1}
    if os.environ.get('FIXTURE_BAD_SHA1'): info['contentSha1'] = 'bad'
    if os.environ.get('FIXTURE_BAD_LENGTH'): info['size'] += 1
    if os.environ.get('FIXTURE_MULTIPART'):
        info['contentSha1'] = 'none'; info['fileInfo'] = {'large_file_sha1': sha1}
    # Matches the upstream v4 upload command and FileVersion.as_dict output.
    print('URL by file name: https://download.example.invalid/' + remote)
    print('URL by fileId: https://download.example.invalid/inert-file-id')
    print(json.dumps(info))
'''

class BackupRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.umask = os.umask(0o077)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        for name in ['psql', 'pg_dump', 'pg_restore', 'age', 'b2']:
            p = self.bin / name
            p.write_text(FIXTURE)
            p.chmod(0o700)
        self.output = self.root / 'backups'
        self.report = self.root / 'reports'
        self.recipient = self.root / 'recipient.txt'
        self.recipient.write_text('age1' + 'q' * 58)  # Invalid checksum; executable fixture only.
        self.identity = self.root / 'identity.txt'
        self.identity.write_text('AGE-SECRET-KEY-1' + 'Q' * 58)  # Invalid checksum; executable fixture only.
        self.marker = self.root / 'restore-marker'
        self.environment = patch.dict(os.environ, {'PATH': str(self.bin) + ':' + os.environ['PATH'], 'PGHOST': '127.0.0.1', 'PGPORT': '5499', 'PGUSER': 'fixture-operator', 'PGDATABASE': 'xynes_restore_fixture', 'FIXTURE_RESTORED': str(self.marker), 'XYNES_BACKUP_EXECUTABLE_FIXTURES': 'inert-fixtures-only', 'XYNES_TEST_AGE_BIN': os.environ.get('XYNES_TEST_AGE_BIN', '')}, clear=True)
        self.environment.start()
    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()
        os.umask(self.umask)
    def args(self, operation, *extra, environment='dev'):
        common = ['--environment', environment, '--output-dir', str(self.output)] if operation != 'restore' else []
        return recovery.parser().parse_args([operation, *common, *extra])
    def create_backup(self, upload=False, environment='dev'):
        args = self.args('backup', '--recipient-file', str(self.recipient), '--verify-table', 'platform.workspaces', *(['--upload', '--bucket', 'inert-backups'] if upload else []), environment=environment)
        result = recovery.backup(args)
        return result, self.output / result['job']
    def restore_args(self, result, job):
        return self.args('restore', '--backup-file', str(job / 'backup.tar.age'), '--expected-sha256', result['sha256'], '--identity-file', str(self.identity), '--report-dir', str(self.report), '--approved-isolated-target', '--confirm-target', '127.0.0.1:5499/xynes_restore_fixture')
    def test_fixture_backup_restore_evidence_and_private_outputs(self):
        result, job = self.create_backup()
        self.assertFalse(result['uploaded'])
        self.assertEqual(set(p.name for p in job.iterdir()), {'backup.tar.age', 'record.json'})
        self.assertEqual(job.stat().st_mode & 0o777, 0o700)
        self.assertEqual((job / 'record.json').stat().st_mode & 0o777, 0o600)
        restored = recovery.restore(self.restore_args(result, job))
        self.assertTrue(restored['within_rpo_rto'])
        self.assertEqual(restored['verified_table_count'], 2)
        argv = json.loads(self.marker.read_text())
        self.assertIn('--single-transaction', argv)
        self.assertNotIn('--clean', argv)
        self.assertEqual(len(list(self.report.glob('restore-*.json'))), 1)
        self.assertFalse(list(self.report.glob('.restore-*')))
    def test_closed_release_binding_is_encrypted_and_persisted_in_uploaded_receipt(self):
        binding={'accepted_token':'a'*64,'accepted_source_sha256':'sha256:'+'b'*64,'database_container_id':'c'*64}
        args=self.args('backup','--recipient-file',str(self.recipient),'--verify-table','platform.workspaces','--upload','--bucket','inert-backups')
        uploaded=[];original=recovery.upload
        def capture(path,bucket,name):
            if path.name=='record.json':uploaded.append(json.loads(path.read_text()))
            return original(path,bucket,name)
        with patch.object(recovery,'upload',capture):result=recovery.backup(args,release_binding=binding)
        job=self.output/result['job']
        self.assertEqual(recovery.read_json(job/'record.json')['release_binding'],binding)
        self.assertEqual(uploaded[0]['release_binding'],binding)
        import io
        raw=(job/'backup.tar.age').read_bytes().removeprefix(b'INERT-AGE-FIXTURE:')
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            self.assertEqual(json.load(archive.extractfile('manifest.json'))['release_binding'],binding)
        self.assertEqual(recovery.monitor(self.args('monitor'))['status'],'backup-monitor-healthy')
    def test_release_binding_rejects_arbitrary_metadata_before_external_commands(self):
        args=self.args('backup','--recipient-file',str(self.recipient),'--verify-table','platform.workspaces')
        valid={'accepted_token':'a'*64,'accepted_source_sha256':'sha256:'+'b'*64,'database_container_id':'c'*64}
        for value in ({},True,{**valid,'secret':'do not retain'},{**valid,'accepted_token':'bad'},{**valid,'accepted_source_sha256':'b'*64},{**valid,'database_container_id':'short'}):
            with self.subTest(value=value),patch.object(recovery,'command',side_effect=AssertionError('no external call')),self.assertRaises(recovery.RecoveryError):
                recovery.backup(args,release_binding=value)

    def test_operator_bash_entry_points_work_from_another_directory(self):
        import subprocess
        output = subprocess.check_output(['bash', str(ROOT / 'scripts/backup-pg.sh'), '--environment', 'dev', '--output-dir', str(self.output), '--recipient-file', str(self.recipient), '--verify-table', 'platform.workspaces'], cwd=self.root, text=True)
        record = json.loads(output)
        args = self.restore_args(record, self.output / record['job'])
        output = subprocess.check_output(['bash', str(ROOT / 'scripts/restore-pg.sh'), '--backup-file', str(args.backup_file), '--expected-sha256', args.expected_sha256, '--identity-file', str(args.identity_file), '--report-dir', str(args.report_dir), '--approved-isolated-target', '--confirm-target', args.confirm_target], cwd=self.root, text=True)
        self.assertEqual(json.loads(output)['status'], 'restore-verified')
        self.assertTrue(self.marker.exists())
    def test_old_recoverable_backup_does_not_pass_rpo_signoff(self):
        result, job = self.create_backup()
        source = self.root / 'old-source'; source.mkdir()
        raw = self.root / 'old.tar'
        raw.write_bytes((job / 'backup.tar.age').read_bytes()[len(b'INERT-AGE-FIXTURE:'):])
        recovery.unpack(raw, source, 100000)
        manifest = recovery.read_json(source / 'manifest.json')
        manifest['snapshot_at'] = datetime.fromtimestamp(time.time() - 2*86400, timezone.utc).isoformat()
        recovery.write_json(source / 'manifest.json', manifest)
        archive = self.archive([(p, (source / p).read_bytes(), tarfile.REGTYPE) for p in recovery.MEMBERS])
        cipher = job / 'backup.tar.age'; cipher.write_bytes(b'INERT-AGE-FIXTURE:' + archive.read_bytes())
        argv = ['restore', '--backup-file', str(cipher), '--expected-sha256', recovery.digest(cipher), '--identity-file', str(self.identity), '--report-dir', str(self.report), '--approved-isolated-target', '--confirm-target', '127.0.0.1:5499/xynes_restore_fixture']
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout): self.assertEqual(recovery.main(argv), 1)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report['status'], 'restore-verified')
        self.assertFalse(report['within_rpo_rto'])
        self.assertGreater(report['rpo_seconds'], 86400)
    def test_upload_monitor_and_multipart_checksum(self):
        with patch.dict(os.environ, {'FIXTURE_MULTIPART': '1'}):
            result, _ = self.create_backup(upload=True)
        self.assertTrue(result['uploaded'])
        self.assertEqual(recovery.monitor(self.args('monitor'))['status'], 'backup-monitor-healthy')
    def test_failed_upload_never_advertises_success(self):
        for variable, value in [('FIXTURE_FAIL', 'b2'), ('FIXTURE_RECEIPT_FAIL', '1'), ('FIXTURE_BAD_SHA1', '1'), ('FIXTURE_BAD_LENGTH', '1')]:
            with self.subTest(variable=variable), patch.dict(os.environ, {variable: value}):
                with self.assertRaises(recovery.RecoveryError): self.create_backup(upload=True)
            records = list(self.output.glob('dev-*/record.json'))
            self.assertTrue(records)
            self.assertTrue(all(not json.loads(p.read_text())['uploaded'] for p in records))
            with self.assertRaises(recovery.RecoveryError): recovery.monitor(self.args('monitor'))
    def test_upload_rejects_non_json_and_wrong_receipt_types(self):
        file = self.root / 'cipher.age'; file.write_bytes(b'inert-fixture')
        receipt = {'action': 'upload', 'fileName': 'dev/fixture', 'size': len(b'inert-fixture'), 'fileId': 'inert-id', 'contentSha1': recovery.digest(file, 'sha1')}
        for raw in [b'INERT_SECRET_SENTINEL', b'[]', b'\xff', json.dumps({**receipt, 'action': 'hide'}).encode(), json.dumps({**receipt, 'size': True}).encode(), json.dumps({**receipt, 'fileId': ''}).encode(), json.dumps({**receipt, 'fileName': 'wrong'}).encode()]:
            with self.subTest(raw=raw), patch.object(recovery, 'command', return_value=raw), self.assertRaises(recovery.RecoveryError): recovery.upload(file, 'inert-backups', 'dev/fixture')
        with patch.object(recovery, 'command', return_value=json.dumps(receipt).encode()):
            self.assertEqual(recovery.upload(file, 'inert-backups', 'dev/fixture')['file_id'], 'inert-id')
    def test_ciphertext_and_target_checks_precede_db_writes(self):
        result, job = self.create_backup()
        for field, value in [('approved_isolated_target', False), ('expected_sha256', '0' * 64), ('confirm_target', 'wrong')]:
            args = self.restore_args(result, job)
            setattr(args, field, value)
            with self.subTest(field=field), self.assertRaises(recovery.RecoveryError): recovery.restore(args)
            self.assertFalse(self.marker.exists())
        for changes in [{'PGHOST': 'hosted.example.invalid'}, {'PGPORT': '5432'}, {'PGPORT': '54320'}, {'PGDATABASE': 'postgres'}, {'PGHOSTADDR': '192.0.2.1'}, {'PGSERVICE': 'prod'}, {'PGSERVICEFILE': '/inert'}, {'PGPORT': ''}, {'PGDATABASE': 'postgresql://not-allowed'}]:
            with self.subTest(changes=changes), patch.dict(os.environ, changes), self.assertRaises(recovery.RecoveryError): recovery.restore(self.restore_args(result, job))
            self.assertFalse(self.marker.exists())
        with patch.dict(os.environ, {'FIXTURE_TARGET_COUNT': '1'}), self.assertRaises(recovery.RecoveryError): recovery.restore(self.restore_args(result, job))
        self.assertFalse(self.marker.exists())
    def test_restored_data_mismatch_fails_and_cleans_plaintext(self):
        result, job = self.create_backup()
        with patch.dict(os.environ, {'FIXTURE_DIFFERENT': '1'}), self.assertRaisesRegex(recovery.RecoveryError, 'restored-evidence-mismatch'):
            recovery.restore(self.restore_args(result, job))
        self.assertTrue(self.marker.exists())  # Approved scratch target requires operator inspection/recreation.
        self.assertFalse(list(self.report.glob('.restore-*')))
        self.assertFalse(list(self.report.glob('restore-*.json')))
    def test_source_failures_limits_and_missing_tables_preserve_no_plaintext(self):
        for name in ['psql', 'pg_dump', 'pg_restore', 'age']:
            with self.subTest(name=name), patch.dict(os.environ, {'FIXTURE_FAIL': name}), self.assertRaises(recovery.RecoveryError): self.create_backup()
            self.assertFalse(list(self.output.iterdir()))
        args = self.args('backup', '--recipient-file', str(self.recipient), '--verify-table', 'platform.missing')
        with self.assertRaises(recovery.RecoveryError): recovery.backup(args)
        args.verify_table = ['platform.workspaces']
        args.max_bytes = 1
        with self.assertRaises(recovery.RecoveryError): recovery.backup(args)
        self.assertFalse(list(self.output.iterdir()))
        args.upload, args.bucket = True, None
        with self.assertRaises(recovery.RecoveryError): recovery.backup(args)
    def test_secret_permissions_symlinks_and_file_limits(self):
        result, job = self.create_backup()
        self.identity.write_text('AGE-PLUGIN-INERT')
        with self.assertRaisesRegex(recovery.RecoveryError, 'native-age-identity-required'): recovery.restore(self.restore_args(result, job))
        self.identity.chmod(0o644)
        with self.assertRaises(recovery.RecoveryError): recovery.regular(self.identity, 1024, secret=True)
        link = self.root / 'link'
        link.symlink_to(self.identity)
        with self.assertRaises(recovery.RecoveryError): recovery.regular(link, 1024)
        with self.assertRaises(recovery.RecoveryError): recovery.regular(self.identity, 1)
        self.output.mkdir(mode=0o755, exist_ok=True)
        self.output.chmod(0o755)
        with self.assertRaises(recovery.RecoveryError): recovery.private_dir(self.output)
        alias = self.root / 'alias'
        alias.symlink_to(self.output)
        with self.assertRaises(recovery.RecoveryError): recovery.private_dir(alias)
        with self.assertRaises(recovery.RecoveryError): recovery.private_dir(self.identity)
    def archive(self, entries):
        archive = self.root / ('archive-' + str(time.time_ns()) + '.tar')
        with tarfile.open(archive, 'w') as bundle:
            for name, data, kind in entries:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.size = len(data)
                bundle.addfile(member, io.BytesIO(data))
        return archive
    def test_archive_traversal_symlink_duplicates_missing_and_bound(self):
        for entries in [[('../escape', b'x', tarfile.REGTYPE)], [('database.dump', b'x', tarfile.SYMTYPE)], [('database.dump', b'x', tarfile.REGTYPE)] * 2, []]:
            dest = self.root / str(time.time_ns()); dest.mkdir()
            with self.subTest(entries=entries), self.assertRaises(recovery.RecoveryError): recovery.unpack(self.archive(entries), dest, 100000)
        dest = self.root / str(time.time_ns()); dest.mkdir()
        with self.assertRaises(recovery.RecoveryError): recovery.unpack(self.archive([('database.dump', b'xx', tarfile.REGTYPE)]), dest, 1)
        self.assertFalse((self.root / 'escape').exists())
    def test_manifest_tampering_is_rejected(self):
        result, job = self.create_backup()
        source = self.root / 'source'; source.mkdir()
        raw = self.root / 'bundle.tar'
        raw.write_bytes((job / 'backup.tar.age').read_bytes()[len(b'INERT-AGE-FIXTURE:'):])
        recovery.unpack(raw, source, 100000)
        original = recovery.read_json(source / 'manifest.json')
        mutations = [{**original, 'version': 2}, {**original, 'version': True}, {**original, 'files': {}}, {**original, 'environment': 'unknown'}, {**original, 'snapshot_at': '2026-10-05'}, {**original, 'files': {**original['files'], 'database.dump': {'bytes': 1, 'sha256': '0' * 64}}}]
        for value in mutations:
            recovery.write_json(source / 'manifest.json', value)
            archive = self.archive([(p, (source / p).read_bytes(), tarfile.REGTYPE) for p in recovery.MEMBERS])
            dest = self.root / str(time.time_ns()); dest.mkdir()
            with self.subTest(value=value), self.assertRaises(recovery.RecoveryError): recovery.unpack(archive, dest, 100000)
    def test_monitor_stale_corrupt_unknown_and_record_validation(self):
        with self.assertRaises(recovery.RecoveryError): recovery.monitor(self.args('monitor'))
        result, job = self.create_backup(upload=True)
        path = job / 'record.json'
        original = recovery.read_json(path)
        for changes in [{'snapshot_at': datetime.fromtimestamp(time.time() - 90000, timezone.utc).isoformat()}, {'snapshot_at': datetime.fromtimestamp(time.time() + 1000, timezone.utc).isoformat()}, {'sha256': '0' * 64}, {'bytes': 1}, {'uploaded': 1}, {'version': 2}, {'environment': 'prod'}, {'sha256': 'bad'}]:
            recovery.write_json(path, {**original, **changes})
            with self.subTest(changes=changes), self.assertRaises(recovery.RecoveryError): recovery.monitor(self.args('monitor'))
        recovery.write_json(path, original)
        (self.output / 'unrelated.txt').write_text('preserve')
        self.assertEqual(len(recovery.jobs(self.output, 'dev')), 1)
    def test_retention_keeps_newest_unuploaded_and_unrelated(self):
        newest, newest_job = self.create_backup(upload=True)
        old, old_job = self.create_backup(upload=True)
        pending, pending_job = self.create_backup()
        record = recovery.read_json(old_job / 'record.json')
        record['snapshot_at'] = datetime.fromtimestamp(time.time() - 9 * 86400, timezone.utc).isoformat()
        recovery.write_json(old_job / 'record.json', record)
        (self.output / 'unrelated').mkdir()
        with self.assertRaises(recovery.RecoveryError): recovery.prune(self.args('prune'))
        with self.assertRaises(recovery.RecoveryError): recovery.prune(self.args('prune', '--confirm-prune', '--days', '1'))
        (old_job / 'unexpected').write_text('preserve')
        with self.assertRaises(recovery.RecoveryError): recovery.prune(self.args('prune', '--confirm-prune'))
        (old_job / 'unexpected').unlink()
        self.assertEqual(recovery.prune(self.args('prune', '--confirm-prune'))['removed'], 1)
        self.assertTrue(newest_job.exists())
        self.assertTrue(pending_job.exists())
        self.assertTrue((self.output / 'unrelated').exists())
    def test_error_logs_never_expose_external_diagnostics_or_credentials(self):
        stderr, stdout = io.StringIO(), io.StringIO()
        argv = ['backup', '--environment', 'dev', '--output-dir', str(self.output), '--recipient-file', str(self.recipient), '--verify-table', 'platform.workspaces']
        with patch.dict(os.environ, {'FIXTURE_FAIL': 'pg_dump', 'PGPASSWORD': 'INERT_SECRET_SENTINEL_DO_NOT_LOG'}), contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            self.assertEqual(recovery.main(argv), 1)
        self.assertNotIn('INERT_SECRET_SENTINEL', stderr.getvalue() + stdout.getvalue())
        self.assertIn('external-command-failed', stderr.getvalue())
        with contextlib.redirect_stderr(stderr): self.assertEqual(recovery.main([*argv, '--max-bytes', '0']), 1)
    def test_subprocess_credentials_are_scoped_to_their_tool(self):
        with patch.dict(os.environ, {'PGPASSWORD': 'inert-db-password', 'B2_APPLICATION_KEY': 'inert-provider-key', 'UNRELATED_APP_SECRET': 'inert-app-secret'}):
            pg = recovery.tool_environment('psql')
            b2 = recovery.tool_environment('b2')
            age = recovery.tool_environment('age')
            self.assertIn('PGPASSWORD', pg); self.assertNotIn('B2_APPLICATION_KEY', pg)
            self.assertIn('B2_APPLICATION_KEY', b2); self.assertNotIn('PGPASSWORD', b2)
            self.assertNotIn('PGPASSWORD', age); self.assertNotIn('B2_APPLICATION_KEY', age)
            for env in (pg, b2, age): self.assertNotIn('UNRELATED_APP_SECRET', env)
        with patch.dict(os.environ, {'PGUSER': ''}), self.assertRaises(recovery.RecoveryError): recovery.pg_connection()
    def test_command_bounds_timeouts_and_success_output(self):
        self.assertEqual(recovery.command([sys.executable, '-c', "print('fixture')"]), b'fixture\n')
        with self.assertRaises(recovery.RecoveryError): recovery.command([sys.executable, '-c', "print('too long')"], limit=1)
        with patch.object(recovery, 'TIMEOUT', 0.01), self.assertRaises(recovery.RecoveryError): recovery.command([sys.executable, '-c', 'import time; time.sleep(2)'])
        with patch.object(recovery, 'TIMEOUT', 0.01), patch.dict(os.environ, {'FIXTURE_SNAPSHOT_SLOW': '1'}), self.assertRaises(recovery.RecoveryError): self.create_backup()
        self.assertFalse(list(self.output.iterdir()))
    def test_metadata_shapes_and_invalid_table_name(self):
        value = self.root / 'value.json'; value.write_text('[]')
        with self.assertRaises(recovery.RecoveryError): recovery.read_json(value)
        value.write_bytes(b'')
        with self.assertRaises(recovery.RecoveryError): recovery.regular(value, 100)
        for table in ['platform.workspaces; DROP DATABASE fixture', 'missing', '../escape']:
            with self.assertRaises(recovery.RecoveryError): recovery.table_sql(table)
        with self.assertRaises(recovery.RecoveryError): recovery.evidence(lambda _: '{}', ['platform.workspaces'])
        with patch.dict(os.environ, {'PGPORT': '99999'}), self.assertRaises(recovery.RecoveryError): recovery.pg_connection()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), patch.object(recovery, 'monitor', side_effect=ValueError('inert-sensitive-diagnostic')):
            self.assertEqual(recovery.main(['monitor', '--environment', 'dev', '--output-dir', str(self.output)]), 1)
    def test_evidence_types_and_recipient_policy_fail_closed(self):
        valid = recovery.evidence(lambda query: json.dumps(['platform.workspaces']) if query == recovery.TABLES_SQL else json.dumps(['platform']) if query == recovery.SCHEMAS_SQL else '[]' if query == recovery.COLUMNS_SQL else json.dumps({'rows': 1, 'row_md5': 'a' * 32}), ['platform.workspaces'])
        for patch_value in [{'schemas': {}}, {'tables': ['platform.workspaces', 'platform.workspaces']}, {'columns': [1]}, {'verified_tables': {}}, {'migration_tables': ['platform.missing']}, {'verified_tables': {'platform.workspaces': {'rows': True, 'row_md5': 'a' * 32}}}, {'verified_tables': {'platform.workspaces': {'rows': -1, 'row_md5': 'a' * 32}}}, {'verified_tables': {'platform.workspaces': {'rows': 1, 'row_md5': 'bad'}}}]:
            with self.subTest(patch_value=patch_value), self.assertRaises(recovery.RecoveryError): recovery.validate_evidence({**valid, **patch_value})
        with self.assertRaises(recovery.RecoveryError): recovery.validate_evidence({})
        for value in ['', 'age1plugin-inert', 'ssh-ed25519 inert']:
            self.recipient.write_text(value or '# no recipients')
            with self.assertRaises(recovery.RecoveryError): self.create_backup()
        self.recipient.write_text('age1' + 'q' * 58)
        self.recipient.chmod(0o666)
        with self.assertRaises(recovery.RecoveryError): self.create_backup()
    def test_staging_cli_backup_metadata_restore_and_no_implicit_restore(self):
        for operation in ('backup', 'monitor', 'prune'):
            extra = ['--recipient-file', str(self.recipient), '--verify-table', 'platform.workspaces'] if operation == 'backup' else []
            self.assertEqual(self.args(operation, *extra, environment='staging').environment, 'staging')
        result, job = self.create_backup(upload=True, environment='staging')
        self.assertTrue(result['job'].startswith('staging-'))
        self.assertEqual(result['environment'], 'staging')
        self.assertEqual(recovery.read_json(job/'record.json')['environment'], 'staging')
        self.assertTrue(result['upload']['name'].startswith('staging/staging-'))
        self.assertFalse(self.marker.exists(), 'backup must never invoke target restore')
        monitor = recovery.monitor(self.args('monitor', environment='staging'))
        self.assertEqual(monitor['environment'], 'staging')
        raw = self.root/'staging.tar'
        raw.write_bytes((job/'backup.tar.age').read_bytes()[len(b'INERT-AGE-FIXTURE:'):])
        extracted = self.root/'staging-source'; extracted.mkdir()
        self.assertEqual(recovery.unpack(raw, extracted, 100000)['environment'], 'staging')
        args = self.restore_args(result, job)
        args.approved_isolated_target = False
        with self.assertRaisesRegex(recovery.RecoveryError, 'approved-isolated-target-required'):
            recovery.restore(args)
        self.assertFalse(self.marker.exists())
        args.approved_isolated_target = True
        restored = recovery.restore(args)
        self.assertEqual(restored['environment'], 'staging')
        self.assertTrue(restored['within_rpo_rto'])
        self.assertLessEqual(restored['rpo_seconds'], 86400)
        self.assertLessEqual(restored['rto_seconds'], 7200)

    def test_staging_retention_preserves_other_environments_and_pending_copies(self):
        newest, newest_job = self.create_backup(upload=True, environment='staging')
        old, old_job = self.create_backup(upload=True, environment='staging')
        pending, pending_job = self.create_backup(environment='staging')
        dev, dev_job = self.create_backup(upload=True)
        prod, prod_job = self.create_backup(upload=True, environment='prod')
        for path in (old_job, dev_job, prod_job):
            record = recovery.read_json(path/'record.json')
            record['snapshot_at'] = datetime.fromtimestamp(time.time()-9*86400, timezone.utc).isoformat()
            recovery.write_json(path/'record.json', record)
        with self.assertRaises(recovery.RecoveryError):
            recovery.prune(self.args('prune', environment='staging'))
        self.assertTrue(old_job.exists())
        (self.output/'unrelated').mkdir()
        result = recovery.prune(self.args('prune', '--confirm-prune', environment='staging'))
        self.assertEqual(result['removed'], 1)
        self.assertFalse(old_job.exists())
        for path in (newest_job, pending_job, dev_job, prod_job, self.output/'unrelated'):
            self.assertTrue(path.exists())
        record = recovery.read_json(newest_job/'record.json')
        record['snapshot_at'] = datetime.fromtimestamp(time.time()-90000, timezone.utc).isoformat()
        recovery.write_json(newest_job/'record.json', record)
        with self.assertRaisesRegex(recovery.RecoveryError, 'backup-stale-or-corrupt'):
            recovery.monitor(self.args('monitor', environment='staging'))
        with self.assertRaisesRegex(recovery.RecoveryError, 'backup-stale-or-corrupt'):
            recovery.prune(self.args('prune', '--confirm-prune', environment='staging'))
        self.assertTrue(pending_job.exists())

    def test_direct_environment_inputs_reject_unknown_values_before_commands(self):
        args = self.args('backup', '--recipient-file', str(self.recipient), '--verify-table', 'platform.workspaces')
        for environment in ('../prod', 'unknown', 'STAGING', '', None, True, ['qa']):
            args.environment = environment
            with self.subTest(environment=environment), patch.object(recovery, 'command') as command:
                with self.assertRaisesRegex(recovery.RecoveryError, 'invalid-environment'):
                    recovery.backup(args)
                command.assert_not_called()
                with self.assertRaisesRegex(recovery.RecoveryError, 'invalid-environment'):
                    recovery.jobs(self.root, environment)
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(os.environ.get('XYNES_TEST_AGE_BIN'), 'explicit verified age test binary not supplied')
    def test_real_age_roundtrip_wrong_key_and_authenticated_tampering(self):
        import subprocess
        age_dir = Path(os.environ['XYNES_TEST_AGE_BIN'])
        (self.bin / 'age').unlink()
        (self.bin / 'age').symlink_to(age_dir / 'age')
        self.identity.unlink()
        subprocess.run([str(age_dir / 'age-keygen'), '-o', str(self.identity)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.identity.chmod(0o600)
        public = subprocess.check_output([str(age_dir / 'age-keygen'), '-y', str(self.identity)])
        self.recipient.write_bytes(public)
        result, job = self.create_backup()
        cipher = job / 'backup.tar.age'
        self.assertTrue(cipher.read_bytes().startswith(b'age-encryption.org/v1'))
        self.assertNotIn(b'PGDMP-inert', cipher.read_bytes())
        self.assertTrue(recovery.restore(self.restore_args(result, job))['within_rpo_rto'])
        self.marker.unlink()
        wrong = self.root / 'wrong-key.txt'
        subprocess.run([str(age_dir / 'age-keygen'), '-o', str(wrong)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wrong.chmod(0o600)
        args = self.restore_args(result, job); args.identity_file = wrong
        with self.assertRaises(recovery.RecoveryError): recovery.restore(args)
        self.assertFalse(self.marker.exists())
        data = bytearray(cipher.read_bytes()); data[-1] ^= 1; cipher.write_bytes(data)
        args = self.restore_args(result, job); args.expected_sha256 = recovery.digest(cipher)
        with self.assertRaises(recovery.RecoveryError): recovery.restore(args)
        self.assertFalse(self.marker.exists())
        self.assertFalse(list(self.report.glob('.restore-*')))

if __name__ == '__main__':
    # Explicit suite also works under stdlib trace/runpy, whose __main__ module
    # contains the runner rather than these test classes.
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(BackupRecoveryTests)
    result = unittest.TextTestRunner().run(suite)
    if not result.wasSuccessful():
        # trace's CLI swallows SystemExit; a real exception keeps this gate red.
        raise RuntimeError('Backup recovery regression tests failed')
