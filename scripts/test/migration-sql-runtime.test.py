#!/usr/bin/env python3
"""Real, local, disposable PostgreSQL checks. No host targets or image overrides.

Requires the pinned official image already present. Pulling/preparing the image
belongs to the integration owner/CI setup, never this harness. Synthetic source
identities below confer no release admission or hosted database acceptance.
"""
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import subprocess
import sys
import time
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/release'))
import migration_plan
import migration_sql

IMAGE = 'postgres:17-alpine@sha256:979c4379dd698aba0b890599a6104e082035f98ef31d9b9291ec22f2b13059ca'
INDEX_DIGEST = IMAGE.split('@')[1]
MEMORY = 512 * 1024 * 1024
TMPFS = {
    '/var/lib/postgresql/data': 'rw,noexec,nosuid,nodev,size=256m,uid=70,gid=70,mode=0700',
    '/var/run/postgresql': 'rw,noexec,nosuid,nodev,size=16m,uid=70,gid=70,mode=0700',
    '/tmp': 'rw,noexec,nosuid,nodev,size=16m,uid=70,gid=70,mode=0700',
}
BODY_PATH = 'supabase/migrations/20261012090000_gateway_route_body_limits.sql'
LABEL_SCOPE = 'io.xynes.apex.scope'
LABEL_NONCE = 'io.xynes.apex.nonce'
SCOPE = 'migration-sql-runtime'


class FixtureError(ValueError):
    """Only fixed diagnostics; never echo Docker metadata, SQL, env or errors."""


def require(value, message='isolated PostgreSQL fixture refused'):
    if not value:
        raise FixtureError(message)


def fixture_units(texts):
    units, payloads = [], {}
    for index, text in enumerate(texts):
        path = f'supabase/migrations/2000010100{index:04d}_synthetic_fixture.sql'
        body = text.encode('utf8')
        unit = {'owner': 'xynes-infra', 'path': path,
                'sha256': hashlib.sha256(body).hexdigest(), 'source_commit': 'a' * 40}
        units.append(unit);payloads[(unit['owner'], path)] = body
    return units, payloads


def good_units():
    return fixture_units([
        'CREATE SCHEMA platform; CREATE TABLE platform.first(id integer PRIMARY KEY); INSERT INTO platform.first VALUES(1);',
        'CREATE TABLE platform.last(id integer);',
    ])


class PostgresFixture:
    def __init__(self):
        account_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        socket = account_home / '.docker/run/docker.sock' if sys.platform == 'darwin' else Path('/var/run/docker.sock')
        require(sys.platform == 'darwin' or sys.platform.startswith('linux'), 'local Unix Docker fixture is required')
        self.docker = ['docker', '--host', 'unix://' + str(socket)]
        # Explicit local socket wins over saved contexts; no Docker overrides,
        # provider tokens, registry credentials or app env enter child processes.
        self.environment = {key: os.environ[key] for key in ('PATH', 'HOME') if key in os.environ}
        self.nonce = secrets.token_hex(16)
        self.name = 'apex-migration-runtime-' + secrets.token_hex(8)
        self.cid = None;self.image_id = None;self.launched = False;self.counter = 0

    def command(self, args, *, input=None, timeout=25):
        try:
            return subprocess.run(self.docker + args, input=input, capture_output=True,
                                  text=True, timeout=timeout, env=self.environment)
        except (OSError, subprocess.SubprocessError):
            raise FixtureError('bounded local fixture command failed') from None

    def start(self):
        # image inspect never downloads an unavailable image.
        result = self.command(['image', 'inspect', IMAGE])
        require(result.returncode == 0, 'preload the fixed official PostgreSQL image before this test')
        try:
            images = json.loads(result.stdout)
            require(isinstance(images, list) and len(images) == 1)
            image = images[0]
            require(image['Os'] == 'linux' and image['Architecture'] in ('amd64', 'arm64'))
            self.image_id = image['Id']
            require(re.fullmatch(r'sha256:[a-f0-9]{64}', self.image_id))
            official = {'postgres@' + INDEX_DIGEST, 'docker.io/library/postgres@' + INDEX_DIGEST,
                        'library/postgres@' + INDEX_DIGEST}
            require(bool(official.intersection(image.get('RepoDigests') or [])), 'local image must match the fixed official index digest')
        except (KeyError, TypeError, ValueError):
            raise FixtureError('local pinned image metadata refused') from None
        args = ['run', '-d', '--pull=never', '--name', self.name,
                '--label', LABEL_SCOPE + '=' + SCOPE, '--label', LABEL_NONCE + '=' + self.nonce,
                '--user', '70:70', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges:true', '--memory', str(MEMORY), '--pids-limit', '128',
                '--env', 'POSTGRES_HOST_AUTH_METHOD=trust', '--env', 'PGDATA=/var/lib/postgresql/data']
        for path, options in TMPFS.items():
            args += ['--tmpfs', path + ':' + options]
        args.append(self.image_id)  # exact local ID, never a tag resolution/pull
        self.launched = True
        result = self.command(args)
        require(result.returncode == 0, 'isolated PostgreSQL launch failed')
        self.cid = result.stdout.strip()
        require(re.fullmatch(r'[a-f0-9]{64}', self.cid))
        self.verify_owned()
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            ready = self.command(['exec', '--user', '70:70', self.cid,
                                  'pg_isready', '-h', '/var/run/postgresql', '-U', 'postgres', '-d', 'postgres'], timeout=5)
            if ready.returncode == 0:
                # isready may observe initdb's temporary server; require SQL too.
                probe = self.sql('postgres', 'SELECT 1;', timeout=5)
                if probe.returncode == 0 and probe.stdout.strip() == '1':
                    return self
            time.sleep(.2)
        raise FixtureError('bounded PostgreSQL readiness failed')

    def verify_owned(self):
        selector = self.cid if self.cid is not None else self.name
        result = self.command(['inspect', selector])
        require(result.returncode == 0, 'fixture identity unavailable; preserve it for integration-owner inspection')
        try:
            rows = json.loads(result.stdout)
            require(isinstance(rows, list) and len(rows) == 1)
            info = rows[0];host = info['HostConfig'];config = info['Config']
            require(re.fullmatch(r'[a-f0-9]{64}', info['Id']))
            require((self.cid is None or info['Id'] == self.cid) and info['Image'] == self.image_id
                    and info['Name'] == '/' + self.name)
            labels = config.get('Labels') or {}
            require(labels.get(LABEL_SCOPE) == SCOPE and labels.get(LABEL_NONCE) == self.nonce)
            require(config.get('User') == '70:70' and host.get('NetworkMode') == 'none'
                    and host.get('ReadonlyRootfs') is True and host.get('Privileged') is False
                    and host.get('Memory') == MEMORY and host.get('PidsLimit') == 128)
            require(host.get('CapDrop') == ['ALL'] and not host.get('CapAdd')
                    and host.get('SecurityOpt') == ['no-new-privileges:true'])
            require(not host.get('Binds') and not host.get('Mounts') and not host.get('VolumesFrom')
                    and not host.get('PortBindings') and host.get('Tmpfs') == TMPFS)
            require(set(config.get('Volumes') or {}) <= {'/var/lib/postgresql/data'})
            # Engines may expose tmpfs through HostConfig only or also Mounts.
            mounts = info.get('Mounts') or []
            require(all(mount.get('Type') == 'tmpfs' and mount.get('Destination') in TMPFS
                        and not mount.get('Source') for mount in mounts))
            require(set((info.get('NetworkSettings') or {}).get('Networks') or {}) <= {'none'})
            self.cid = info['Id']
            return info
        except (KeyError, TypeError, ValueError):
            raise FixtureError('fixture preservation check failed; no cleanup mutation permitted') from None

    def psql_args(self, database, stop=True, application=None):
        require(self.cid is not None and re.fullmatch(r'apex_[a-z_0-9]{1,48}|postgres', database))
        args = ['exec', '-i', '--user', '70:70']
        if application is not None:
            require(re.fullmatch(r'apex_lock_[a-f0-9]{16}', application))
            args += ['--env', 'PGAPPNAME=' + application]
        return args + [self.cid, 'psql', '-X', '-At', '-v', 'ON_ERROR_STOP=' + ('1' if stop else '0'),
                       '-h', '/var/run/postgresql', '-U', 'postgres', '-d', database]

    def sql(self, database, text, stop=True, timeout=20):
        require(isinstance(text, str) and len(text) < 256 * 1024)
        return self.command(self.psql_args(database, stop), input=text, timeout=timeout)

    def database(self, kind):
        require(re.fullmatch(r'[a-z_]{1,24}', kind))
        self.counter += 1
        database = f'apex_{kind}_{self.nonce[:8]}_{self.counter}'
        require(self.sql('postgres', 'CREATE DATABASE ' + database + ';').returncode == 0,
                'fixture database creation failed')
        return database

    def close(self):
        if not self.launched:
            return
        # If the client failed before returning its CID, recover only by the
        # exact generated name, then require all the same nonce/image invariants.
        if self.cid is None:
            result = self.command(['inspect', self.name])
            if result.returncode != 0:
                return
        self.verify_owned()
        require(self.command(['rm', '-f', self.cid]).returncode == 0, 'owned fixture cleanup failed')
        self.launched = False


class MigrationSQLRuntimeTests(unittest.TestCase):
    fixture = None

    def ok(self, result):
        self.assertEqual(result.returncode, 0, 'isolated fixture SQL failed')

    def refused(self, result):
        self.assertNotEqual(result.returncode, 0, 'unsafe batch was accepted')

    def value(self, database, sql):
        result = self.fixture.sql(database, sql)
        self.ok(result)
        return result.stdout.strip()

    def test_atomic_failure_resume_and_replay_even_with_on_error_stop_disabled(self):
        db = self.fixture.database('resume')
        units, payloads = fixture_units([
            'CREATE SCHEMA platform; CREATE TABLE platform.first(id integer PRIMARY KEY); INSERT INTO platform.first VALUES(1);',
            'CREATE TABLE platform.atomic_failure(id integer); SELECT transient_fixture_prerequisite();',
            'CREATE TABLE platform.last(id integer);',
        ])
        batch = migration_sql.render('qa', db, units, payloads)
        self.fixture.sql(db, batch, stop=False)  # deliberately adversarial client setting
        self.assertEqual(self.value(db, 'SELECT completed FROM xynes_release.bootstrap;'), 'f')
        self.assertEqual(self.value(db, "SELECT to_regclass('platform.atomic_failure') IS NULL;"), 't')
        self.assertEqual(self.value(db, "SELECT count(*) FROM xynes_release.migrations WHERE path='" + units[1]['path'] + "';"), '0')
        self.assertEqual(self.value(db, 'SELECT count(*) FROM platform.first;'), '1')
        self.ok(self.fixture.sql(db, "CREATE FUNCTION public.transient_fixture_prerequisite() RETURNS integer LANGUAGE sql AS 'SELECT 1';"))
        self.ok(self.fixture.sql(db, batch))
        self.assertEqual(self.value(db, 'SELECT count(*) FROM xynes_release.migrations;'), '3')
        self.assertEqual(self.value(db, 'SELECT completed FROM xynes_release.bootstrap;'), 't')
        self.ok(self.fixture.sql(db, batch))
        self.assertEqual(self.value(db, 'SELECT count(*) FROM platform.first;'), '1')
        self.assertEqual(self.value(db, 'SELECT count(*) FROM xynes_release.migrations;'), '3')

    def test_cross_stage_and_changed_baseline_refused_without_replay_writes(self):
        db = self.fixture.database('baseline');units, payloads = good_units()
        self.ok(self.fixture.sql(db, migration_sql.render('qa', db, units, payloads)))
        self.refused(self.fixture.sql(db, migration_sql.render('staging', db, units, payloads)))
        changed, new_payloads = fixture_units([
            'CREATE SCHEMA platform; CREATE TABLE platform.first(id integer PRIMARY KEY); INSERT INTO platform.first VALUES(2);',
            'CREATE TABLE platform.last(id integer);',
        ])
        self.refused(self.fixture.sql(db, migration_sql.render('qa', db, changed, new_payloads)))
        self.assertEqual(self.value(db, 'SELECT id FROM platform.first;'), '1')
        self.assertEqual(self.value(db, 'SELECT environment FROM xynes_release.bootstrap;'), 'qa')
        self.assertEqual(self.value(db, 'SELECT count(*) FROM xynes_release.migrations;'), '2')

    def test_ledger_hash_and_unknown_entry_tampering_refused(self):
        units, payloads = good_units()
        for tamper in ['hash', 'extra']:
            db = self.fixture.database('ledger')
            batch = migration_sql.render('qa', db, units, payloads)
            self.ok(self.fixture.sql(db, batch))
            if tamper == 'hash':
                text = "UPDATE xynes_release.migrations SET sha256='" + '0' * 64 + "' WHERE path='" + units[0]['path'] + "';"
            else:
                text = "INSERT INTO xynes_release.migrations(owner,path,sha256,source_commit) VALUES('xynes-infra','supabase/migrations/20000101000099_foreign.sql','" + '0' * 64 + "','" + 'a' * 40 + "');"
            self.ok(self.fixture.sql(db, text))
            self.refused(self.fixture.sql(db, batch))
            self.assertEqual(self.value(db, 'SELECT count(*) FROM platform.first;'), '1')

    def test_preexisting_comments_or_other_application_schema_requires_adoption(self):
        units, payloads = good_units()
        for schema in ['comments', 'platform']:
            db = self.fixture.database('adoption')
            self.ok(self.fixture.sql(db, 'CREATE SCHEMA ' + schema + ';'))
            self.refused(self.fixture.sql(db, migration_sql.render('qa', db, units, payloads)))
            self.assertEqual(self.value(db, "SELECT to_regnamespace('xynes_release') IS NULL;"), 't')

    def test_legacy_public_objects_and_partial_ledger_require_adoption(self):
        units, payloads = good_units()
        for text in ['CREATE TABLE public.roles(id integer);', 'CREATE SCHEMA xynes_release;']:
            db = self.fixture.database('legacy')
            self.ok(self.fixture.sql(db, text))
            self.refused(self.fixture.sql(db, migration_sql.render('qa', db, units, payloads)))
            self.assertEqual(self.value(db, "SELECT to_regclass('platform.first') IS NULL;"), 't')

    def test_foreign_database_identity_does_not_initialize_ledger(self):
        db = self.fixture.database('identity');units, payloads = good_units()
        self.refused(self.fixture.sql(db, migration_sql.render('qa', 'apex_different_database', units, payloads)))
        self.assertEqual(self.value(db, "SELECT to_regnamespace('xynes_release') IS NULL;"), 't')
        self.assertEqual(self.value(db, "SELECT to_regnamespace('platform') IS NULL;"), 't')

    def test_advisory_lock_contention_is_bounded_and_refused(self):
        db = self.fixture.database('lock');units, payloads = good_units()
        application = 'apex_lock_' + self.fixture.nonce[:16]
        holder = subprocess.Popen(self.fixture.docker + self.fixture.psql_args(db, application=application),
                                  stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  text=True, env=self.fixture.environment)
        try:
            holder.stdin.write(f'SELECT pg_advisory_lock({migration_sql.LOCK_KEY}); SELECT pg_sleep(45);\n')
            holder.stdin.close()
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                held = self.value(db, "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid WHERE l.locktype='advisory' AND l.granted AND a.application_name='" + application + "';")
                if held == '1':
                    break
                time.sleep(.1)
            else:
                self.fail('fixture advisory lock was not acquired')
            start = time.monotonic()
            self.refused(self.fixture.sql(db, migration_sql.render('qa', db, units, payloads), timeout=15))
            self.assertLess(time.monotonic() - start, 15)
            self.assertEqual(self.value(db, "SELECT to_regnamespace('xynes_release') IS NULL;"), 't')
        finally:
            # Terminate only this connection inside this owned, networkless DB.
            self.fixture.sql(db, "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name='" + application + "' AND datname='" + db + "' AND pid<>pg_backend_pid();")
            try:
                holder.wait(timeout=5)
            except subprocess.TimeoutExpired:
                holder.kill();holder.wait(timeout=5)

    def test_actual_body_limit_sql_is_additive_idempotent_and_renderer_compatible(self):
        actual = migration_plan.read_owned(ROOT, BODY_PATH)
        db = self.fixture.database('body')
        self.ok(self.fixture.sql(db, 'CREATE SCHEMA platform; CREATE TABLE platform.routes(id integer PRIMARY KEY);'))
        self.ok(self.fixture.sql(db, actual.decode('utf8')))
        self.ok(self.fixture.sql(db, actual.decode('utf8')))
        self.assertEqual(self.value(db, "SELECT data_type||':'||is_nullable||':'||(column_default IS NULL)::text FROM information_schema.columns WHERE table_schema='platform' AND table_name='routes' AND column_name='max_body_bytes';"), 'integer:YES:true')
        self.assertEqual(self.value(db, "SELECT count(*) FROM pg_constraint WHERE conrelid='platform.routes'::regclass AND conname='max_body_bytes_non_negative' AND contype='c';"), '1')
        self.ok(self.fixture.sql(db, 'INSERT INTO platform.routes VALUES(1,NULL),(2,0),(3,1048576);'))
        self.refused(self.fixture.sql(db, 'INSERT INTO platform.routes VALUES(4,-1);'))
        self.assertEqual(self.value(db, 'SELECT count(*) FROM platform.routes;'), '3')
        db = self.fixture.database('body_batch')
        units, payloads = fixture_units(['CREATE SCHEMA platform; CREATE TABLE platform.routes(id integer PRIMARY KEY);'])
        unit = {'owner': 'xynes-infra', 'path': BODY_PATH, 'sha256': hashlib.sha256(actual).hexdigest(), 'source_commit': 'b' * 40}
        units.append(unit);payloads[(unit['owner'], unit['path'])] = actual
        batch = migration_sql.render('qa', db, units, payloads)
        self.ok(self.fixture.sql(db, batch));self.ok(self.fixture.sql(db, batch))
        self.assertEqual(self.value(db, 'SELECT count(*) FROM xynes_release.migrations;'), '2')
        self.assertEqual(self.value(db, 'SELECT completed FROM xynes_release.bootstrap;'), 't')


def main():
    if len(sys.argv) != 1:
        print('Runtime fixture accepts no target, image or configuration arguments.', file=sys.stderr)
        return 2
    fixture = PostgresFixture()
    result = None;cleanup = False
    try:
        fixture.start();MigrationSQLRuntimeTests.fixture = fixture
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(MigrationSQLRuntimeTests)
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    except FixtureError:
        print('Pinned local PostgreSQL fixture unavailable or unsafe; no runtime acceptance.', file=sys.stderr)
    finally:
        try:
            fixture.close();cleanup = True
        except FixtureError:
            print('Fixture cleanup refused; preserve it for integration-owner inspection.', file=sys.stderr)
    if result is None:
        return 1
    print(json.dumps({'artifact_kind': 'local-postgresql-migration-runtime-fixture',
                      'image_index': INDEX_DIGEST, 'tests_run': result.testsRun,
                      'failures': len(result.failures), 'errors': len(result.errors),
                      'owned_fixture_cleanup_verified': cleanup, 'hosted_acceptance': False,
                      'source_admission_verified': False, 'deployable': False}, sort_keys=True))
    return 0 if result.wasSuccessful() and cleanup else 1


if __name__ == '__main__':
    sys.exit(main())
