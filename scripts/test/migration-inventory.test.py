#!/usr/bin/env python3
"""Real disposable Git objects prove deterministic inventory and refusal boundaries."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/release'))
import release_manifest as records

TOOL = ROOT / 'scripts/release/migration_inventory.py'
BACKENDS = records.BACKENDS
TREES = {'xynes-accounts-service': 'drizzle', 'xynes-authz-service': 'src/db/migrations',
         'xynes-cms-core': 'drizzle', 'xynes-doc-service': 'drizzle',
         'xynes-telemetry-service': 'src/db/migrations'}
SCHEMAS = {name: ('src/db/schema.ts' if name == 'xynes-authz-service' else
                 'src/db/schema/index.ts' if name == 'xynes-telemetry-service' else
                 'src/infra/db/schema.ts') for name in BACKENDS if name != 'xynes-gateway'}


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='apex-migration-test-')
        self.base = Path(self.tmp.name)
        self.manifest = json.loads((ROOT / 'infra/release/source-manifest.json').read_text())
        self.repos = {}
        for owner in ('xynes-infra',) + BACKENDS:
            repo = self.base / owner if owner == 'xynes-infra' else self.base / 'xynes' / owner
            repo.mkdir(parents=True)
            git(repo, 'init', '-q')
            git(repo, 'config', 'user.name', 'Inventory Fixture')
            git(repo, 'config', 'user.email', 'fixture@example.invalid')
            git(repo, 'config', 'commit.gpgsign', 'false')
            self.repos[owner] = repo
            if owner == 'xynes-infra':
                self.write(owner, 'supabase/migrations/20260101000000_platform.sql', 'CREATE SCHEMA platform;\n')
                self.write(owner, 'docs/DATABASE.md', 'Fixture ownership reference, never release approval.\n')
            else:
                self.write(owner, 'package.json', json.dumps({'scripts': {'migrate': 'drizzle-kit migrate'}}))
                if owner in SCHEMAS:
                    self.write(owner, SCHEMAS[owner], '// schema contract only\n')
                    self.write(owner, 'drizzle.config.ts', '// config contract only\n')
                if owner in TREES:
                    tree = TREES[owner]
                    self.write(owner, tree + '/0000_init.sql', 'CREATE SCHEMA fixture;\n')
                    self.write(owner, tree + '/meta/_journal.json', json.dumps(self.journal()))
                    self.write(owner, tree + '/meta/0000_snapshot.json', '{"id":"fixture","prevId":"zero"}')
                if owner == 'xynes-cms-core':
                    self.write(owner, 'src/infra/db/migrate.ts', '// runner never executed\n')
                if owner == 'xynes-authz-service':
                    for p in ('src/scripts/seed.ts', 'src/db/seed/authz.seed.ts', 'src/db/seed/permissions.config.ts',
                              'src/db/seed/index.ts', 'src/infra/ensure-seeded.ts'):
                        self.write(owner, p, '// seed contract never executed\n')
                if owner == 'xynes-cms-core':
                    for p in ('src/infra/db/seed.ts', 'src/infra/db/seeders.ts'):
                        self.write(owner, p, '// seed contract never executed\n')
                if owner == 'xynes-gateway':
                    for p in ('src/infra/db.ts', 'src/infra/bodyLimitSetup.ts', 'src/infra/rateLimitSetup.ts',
                              'src/data/postgresRouteRepository.ts', 'src/data/postgresWorkspaceApiKeyRepository.ts'):
                        self.write(owner, p, '// read-only database prerequisite\n')
                if owner == 'xynes-storage-service':
                    self.write(owner, 'src/infra/db/client.ts', '// production driver prerequisite\n')
            self.commit(owner)
        self.module = None

    def tearDown(self):
        self.tmp.cleanup()

    def journal(self):
        return {'version': '7', 'dialect': 'postgresql', 'entries': [
            {'idx': 0, 'version': '7', 'when': 1, 'tag': '0000_init', 'breakpoints': True}]}

    def write(self, owner, path, data):
        p = self.repos[owner] / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data)

    def commit(self, owner):
        repo = self.repos[owner]
        git(repo, 'add', '.')
        git(repo, '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'fixture')
        commit = git(repo, 'rev-parse', 'HEAD')
        if owner == 'xynes-infra':
            self.infra_commit = commit
        else:
            self.manifest['sources'][owner]['commit'] = commit
        return commit

    def load(self):
        self.assertTrue(TOOL.is_file(), 'immutable migration inventory exporter is missing')
        if not self.module:
            spec = importlib.util.spec_from_file_location('migration_inventory', TOOL)
            self.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.module)
        return self.module

    def export(self):
        return self.load().inventory(self.manifest, self.repos, self.infra_commit)

    def refuse(self, owner, path, data):
        self.write(owner, path, data)
        self.commit(owner)
        with self.assertRaises(ValueError):
            self.export()

    def test_deterministic_exact_blob_hashes_and_closed_record_compatibility(self):
        first = self.export()
        self.assertEqual(first, self.export())
        self.assertEqual(set(first['migrations']), set(('xynes-infra',) + BACKENDS))
        p = 'src/infra/bodyLimitSetup.ts'
        row = next(x for x in first['migrations']['xynes-gateway'] if x['path'] == p)
        self.assertEqual(row['sha256'], hashlib.sha256(b'// read-only database prerequisite\n').hexdigest())
        self.assertEqual(first['source_manifest_sha256'], records.canonical_sha256(self.manifest))
        self.assertEqual(first['infra_commit'], self.infra_commit)
        records.validate_artifacts({'bundle_sha256': 'a'*64, 'binaries': {
            'xynes-idle': {'platform': 'linux/amd64', 'sha256': 'b'*64}}}, first['migrations'])
        self.assertEqual(first['owners']['xynes-gateway']['sql_order'], [])
        self.assertEqual(first['owners']['xynes-storage-service']['sql_order'], [])
        self.assertNotIn('deployable', first)
        self.assertNotIn('approved', first)

    def test_dirty_working_tree_is_never_inventory_source(self):
        first = self.export()
        self.write('xynes-doc-service', 'drizzle/0000_init.sql', 'DROP DATABASE unsafe;')
        self.write('xynes-infra', 'supabase/migrations/20260101000000_platform.sql', 'DROP SCHEMA platform;')
        self.assertEqual(first, self.export())

    def test_both_drizzle_and_src_db_migration_owners_bound(self):
        result = self.export()
        for owner, tree in TREES.items():
            self.assertEqual(result['owners'][owner]['sql_order'], [tree + '/0000_init.sql'])
            self.assertIn(tree + '/meta/_journal.json', [x['path'] for x in result['migrations'][owner]])

    def test_unjournaled_sql_is_bound_and_blocks_execution_plan(self):
        self.write('xynes-doc-service', 'drizzle/0001_pending.sql', 'SELECT 1;')
        self.commit('xynes-doc-service')
        result = self.export()
        self.assertIn('drizzle/0001_pending.sql', [x['path'] for x in result['migrations']['xynes-doc-service']])
        self.assertIn({'owner': 'xynes-doc-service', 'code': 'unjournaled-sql'}, result['blockers'])

    def test_missing_journal_sql_refused(self):
        self.refuse('xynes-doc-service', 'drizzle/meta/_journal.json', json.dumps({**self.journal(), 'entries': [
            {**self.journal()['entries'][0], 'tag': '0000_missing'}]}))

    def test_duplicate_json_keys_refused(self):
        self.refuse('xynes-authz-service', 'src/db/migrations/meta/_journal.json', '{"version":"7","version":"7"}')

    def test_journal_tag_path_traversal_refused(self):
        j = self.journal(); j['entries'][0]['tag'] = '../escape'
        self.refuse('xynes-cms-core', 'drizzle/meta/_journal.json', json.dumps(j))

    def test_duplicate_journal_tags_refused(self):
        j = self.journal(); j['entries'].append({**j['entries'][0], 'idx': 1, 'when': 2})
        self.refuse('xynes-doc-service', 'drizzle/meta/_journal.json', json.dumps(j))

    def test_non_monotonic_times_refused(self):
        self.write('xynes-doc-service', 'drizzle/0001_second.sql', 'SELECT 1;')
        j = self.journal(); j['entries'].append({**j['entries'][0], 'idx': 1, 'tag': '0001_second'})
        self.refuse('xynes-doc-service', 'drizzle/meta/_journal.json', json.dumps(j))

    def test_wrong_index_version_or_boolean_time_refused(self):
        for field, value in [('idx', 4), ('version', '6'), ('when', True), ('breakpoints', 'true')]:
            with self.subTest(field=field):
                j = self.journal();j['entries'][0][field] = value
                self.refuse('xynes-doc-service', 'drizzle/meta/_journal.json', json.dumps(j))

    def test_unknown_journal_field_refused(self):
        self.refuse('xynes-doc-service', 'drizzle/meta/_journal.json', json.dumps({**self.journal(), 'secret': 'hidden'}))

    def test_malformed_snapshot_refused(self):
        self.refuse('xynes-doc-service', 'drizzle/meta/0000_snapshot.json', '{bad')

    def test_excluded_names_inside_production_migration_root_cannot_hide_sql(self):
        for directory in ['docs', 'tests', 'secrets']:
            with self.subTest(directory=directory):
                path='drizzle/' + directory + '/0001_pending.sql'
                self.write('xynes-doc-service',path,'SELECT 1;')
                self.commit('xynes-doc-service')
                with self.assertRaises(ValueError):self.export()
                (self.repos['xynes-doc-service']/path).unlink()
                self.commit('xynes-doc-service')

    def test_unknown_backend_sql_path_refused(self):
        self.refuse('xynes-gateway', 'src/db/migrations/0000_unreviewed.sql', 'SELECT 1;')

    def test_storage_migration_cannot_silently_gain_owner(self):
        self.refuse('xynes-storage-service', 'drizzle/0000_unreviewed.sql', 'SELECT 1;')

    def test_test_fixture_sql_is_not_production_migration(self):
        self.write('xynes-storage-service', 'tests/integration/schema.sql', 'DROP SCHEMA platform;')
        self.commit('xynes-storage-service')
        self.assertNotIn('tests/integration/schema.sql', [x['path'] for x in self.export()['migrations']['xynes-storage-service']])

    def test_unknown_infra_file_in_migration_directory_refused(self):
        self.refuse('xynes-infra', 'supabase/migrations/secret.txt', 'sensitive')

    def test_sql_symlink_refused(self):
        p = self.repos['xynes-doc-service'] / 'drizzle/0000_init.sql';p.unlink();p.symlink_to('/etc/passwd')
        self.commit('xynes-doc-service')
        with self.assertRaises(ValueError):self.export()

    def test_infra_sql_symlink_refused(self):
        p = self.repos['xynes-infra'] / 'supabase/migrations/20260101000000_platform.sql';p.unlink();p.symlink_to('/etc/passwd')
        self.commit('xynes-infra')
        with self.assertRaises(ValueError):self.export()

    def test_missing_required_production_prerequisite_refused(self):
        (self.repos['xynes-storage-service'] / SCHEMAS['xynes-storage-service']).unlink()
        self.commit('xynes-storage-service')
        with self.assertRaises(ValueError):self.export()

    def test_missing_ownership_reference_refused(self):
        (self.repos['xynes-infra'] / 'docs/DATABASE.md').unlink();self.commit('xynes-infra')
        with self.assertRaises(ValueError):self.export()

    def test_foreign_owner_or_mutable_commit_refused(self):
        self.load()
        for key, value in [('repo', {**self.repos, 'foreign': self.base}), ('commit', 'HEAD')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.module.inventory(self.manifest, value if key == 'repo' else self.repos,
                                      value if key == 'commit' else self.infra_commit)

    def test_dev_only_infra_seed_explicit_blocker(self):
        self.write('xynes-infra', 'supabase/migrations/20260516090000_seed_workspace_storage_providers_r2_dev.sql', 'SELECT 1;')
        self.commit('xynes-infra')
        self.assertIn({'owner': 'xynes-infra', 'code': 'dev-only-provider-seed'}, self.export()['blockers'])

    def test_malformed_package_script_shape_refused(self):
        self.refuse('xynes-telemetry-service', 'package.json', '{"scripts":["drizzle-kit push"]}')

    def test_seed_contracts_are_explicit_source_prerequisites(self):
        result = self.export()
        expected = {
            'xynes-authz-service': ['src/scripts/seed.ts', 'src/db/seed/authz.seed.ts',
                                   'src/db/seed/permissions.config.ts', 'src/infra/ensure-seeded.ts'],
            'xynes-cms-core': ['src/infra/db/seed.ts', 'src/infra/db/seeders.ts'],
        }
        for owner, paths in expected.items():
            for path in paths:
                self.assertIn(path, result['owners'][owner]['prerequisites'])
                self.assertIn(path, [x['path'] for x in result['migrations'][owner]])

    def test_cli_matches_api_and_never_executes_prerequisite_typescript(self):
        self.load()
        self.write('xynes-cms-core', 'src/infra/db/seed.ts', 'throw new Error("execution is forbidden");')
        self.commit('xynes-cms-core')
        p = self.base / 'source-manifest.json';p.write_text(json.dumps(self.manifest))
        r = subprocess.run([sys.executable, str(TOOL), '--source-manifest', str(p),
                            '--infra-repo', str(self.repos['xynes-infra']), '--infra-commit', self.infra_commit,
                            '--workspace-root', str(self.base)], text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), self.export())
        self.assertEqual(r.stderr, '')

    def test_cli_invalid_input_fails_without_stdout_or_source_values(self):
        self.load()
        p = self.base / 'bad.json';p.write_text('{"sensitive-value":"unapproved"}')
        r = subprocess.run([sys.executable, str(TOOL), '--source-manifest', str(p),
                            '--infra-repo', str(self.repos['xynes-infra']), '--infra-commit', self.infra_commit,
                            '--workspace-root', str(self.base)], text=True, capture_output=True)
        self.assertNotEqual(r.returncode, 0);self.assertEqual(r.stdout, '')
        self.assertNotIn('sensitive-value', r.stderr)


if __name__ == '__main__':
    unittest.main()
