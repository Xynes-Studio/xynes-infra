"""Black-box source-context tests with real committed, dirty and ignored Git input."""
import copy
import hashlib
import json
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/release'))
import source_context
SCRIPT = ROOT / 'scripts/release/prepare-context.sh'
SOURCES = ['xynes-gateway', 'xynes-accounts-service', 'xynes-authz-service',
           'xynes-cms-core', 'xynes-doc-service', 'xynes-telemetry-service',
           'xynes-storage-service', 'xynes-auth-app', 'xynes-cms-console-web',
           'xynes-auth-sdk', 'xynes-i18n', 'lumia-ds']
BASE = 'example.test/bun:1.4.2@sha256:' + 'a' * 64


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], stderr=subprocess.DEVNULL).decode().strip()


class AccountsSourceModuleTest(unittest.TestCase):
    PATHS = ('src/infra/secrets/index.ts', 'src/infra/secrets/SecretManagerClient.ts')

    def test_allows_only_the_two_accounts_runtime_source_paths(self):
        for path in self.PATHS:
            self.assertTrue(source_context.safe_source_file('xynes-accounts-service', path), path)

    def test_does_not_open_other_owners_aliases_or_secret_paths(self):
        self.assertIn('secrets', source_context.DENIED_PARTS)
        for owner in SOURCES + ['accounts-service', 'accounts', 'xynes-accounts-service/', 'XYNES-ACCOUNTS-SERVICE']:
            if owner == 'xynes-accounts-service':
                continue
            for path in self.PATHS:
                self.assertFalse(source_context.safe_source_file(owner, path), (owner, path))
        neighbours = ['src/infra/secrets/extra.ts', 'src/infra/secrets/private.key',
                      'src/infra/secrets/index.ts.pem', 'src/infra/secrets/index.ts.key',
                      'src/infra/secrets/.env', 'src/infra/secrets/.env.local',
                      'src/infra/secrets/index.test.ts', 'src/infra/secrets/subdir/index.ts',
                      'secrets/index.ts', 'scripts/secrets/index.ts', 'src/secrets/index.ts',
                      'src/infra/secrets/SecretManagerClient.ts.bak',
                      'src/infra/secrets/SecretManagerClient.ts/extra.ts',
                      'src//infra/secrets/index.ts', 'src/infra/secrets/INDEX.ts']
        for path in neighbours:
            self.assertFalse(source_context.safe_source_file('xynes-accounts-service', path), path)

    def fixture(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        repo = Path(temp.name)
        git(repo, 'init', '-q'); git(repo, 'config', 'commit.gpgsign', 'false')
        git(repo, 'config', 'user.name', 'Fixture'); git(repo, 'config', 'user.email', 'fixture@example.invalid')
        (repo / 'package.json').write_text('{}')
        contents = {path: ('export const fixture' + str(i) + ' = true;\n').encode()
                    for i, path in enumerate(self.PATHS)}
        for path, raw in contents.items():
            target = repo / path; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(raw)
        git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'Public runtime source fixture')
        return repo, git(repo, 'rev-parse', 'HEAD'), contents

    def test_tracked_export_keeps_exact_committed_source_bytes(self):
        repo, commit, contents = self.fixture()
        (repo / self.PATHS[0]).write_text('DIRTY_PRIVATE_PAYLOAD')
        (repo / 'src/infra/secrets/untracked.ts').write_text('UNTRACKED_PRIVATE_PAYLOAD')
        exported = {path: (mode, raw) for path, mode, raw in
                    source_context.tracked_files(repo, commit, 'xynes-accounts-service')}
        for path, raw in contents.items():
            self.assertIn(path, exported)
            self.assertEqual(exported[path], ('100644', raw))
        self.assertNotIn('src/infra/secrets/untracked.ts', exported)

    def test_selected_runtime_module_symlinks_are_still_refused(self):
        for path in self.PATHS:
            repo, _, _ = self.fixture(); target = repo / path
            target.unlink(); target.symlink_to('../../outside')
            git(repo, 'add', path); git(repo, 'commit', '-qm', 'Hostile selected symlink')
            with self.assertRaises(source_context.ContextError):
                list(source_context.tracked_files(repo, git(repo, 'rev-parse', 'HEAD'), 'xynes-accounts-service'))


class ContextTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.manifest = {'schema_version': 1, 'status': 'planning', 'approval_reference': None,
                         'sources': {}, 'recipes': {}, 'base_images': [BASE],
                         'processors': {name: {'source': 'xynes-storage-service',
                            'context_prefix': 'sidecars/' + name,
                            'recipe': 'infra/images/' + name + '.Dockerfile',
                            'sha256': hashlib.sha256((ROOT / ('infra/images/' + name + '.Dockerfile')).read_bytes()).hexdigest() if (ROOT / ('infra/images/' + name + '.Dockerfile')).exists() else '0' * 64,
                            'target': 'prod', 'platform': 'linux/amd64'}
                            for name in ('archive-scanner', 'libreoffice')},
                         'external_images': {
                           'clamav-freshclam': {'image': 'clamav/clamav:1.5.2-debian@sha256:' + 'b' * 64, 'platform': 'linux/amd64'},
                           'supabase-db': {'image': 'postgres:17-alpine@sha256:' + 'c' * 64, 'platform': 'linux/amd64'},
                           'supabase-auth': {'image': 'supabase/gotrue:v2.196.0@sha256:' + 'd' * 64, 'platform': 'linux/amd64'}},
                         'tools': {'pnpm': '10.33.0', 'lumia_pnpm': '10.23.0'}}
        for name in SOURCES:
            category = 'xynes' if name in SOURCES[:7] else 'xynes-front-end'
            repo = self.workspace / category / name
            repo.mkdir(parents=True)
            git(repo, 'init', '-q')
            git(repo, 'config', 'commit.gpgsign', 'false')
            git(repo, 'config', 'user.name', 'Fixture')
            git(repo, 'config', 'user.email', 'fixture@example.invalid')
            files = {'package.json': '{}', 'src/index.ts': 'export const original = true;\n',
                     'bun.lock': 'lock', 'pnpm-lock.yaml': 'lock',
                     '.env.example': 'FAKE_TOKEN=excluded', 'docs/junk.md': 'excluded',
                     'src/index.test.ts': 'excluded', 'src/private.key': 'excluded',
                     '.gitignore': '.env.local\nnode_modules\n'}
            if name == 'xynes-storage-service':
                files.update({'sidecars/archive-scanner/go.mod': 'module fixture\n\ngo 1.24\n',
                              'sidecars/archive-scanner/coverage-gate.py': '# fixture coverage checker\n',
                              'sidecars/libreoffice/package.json': '{}',
                              'sidecars/libreoffice/bun.lock': '{}',
                              'sidecars/libreoffice/tsconfig.json': '{}',
                              'sidecars/libreoffice/.eslintrc.cjs': 'module.exports = {};',
                              'sidecars/archive-scanner/archive.go': 'package main\n',
                              'sidecars/archive-scanner/server.go': 'package main\nfunc main() {}\n',
                              'sidecars/archive-scanner/archive_test.go': 'package main\n',
                              'sidecars/archive-scanner/gateway_test.go': 'package main\n',
                              'sidecars/archive-scanner/more_test.go': 'package main\n',
                              'sidecars/archive-scanner/server_test.go': 'package main\n',
                              'sidecars/archive-scanner/private.go': 'excluded',
                              'sidecars/archive-scanner/.env.private': 'excluded',
                              'sidecars/libreoffice/src/shim.ts': 'export const pinned = true;\n',
                              'sidecars/libreoffice/src/convert.ts': 'export const convert = true;\n',
                              'sidecars/libreoffice/src/errors.ts': 'export {};\n',
                              'sidecars/libreoffice/src/health.ts': 'export {};\n',
                              'sidecars/libreoffice/src/parse-request.ts': 'export {};\n',
                              'sidecars/libreoffice/src/safe-mime.ts': 'export {};\n',
                              'sidecars/libreoffice/src/soffice-runner.ts': 'export {};\n',
                              'sidecars/libreoffice/src/private.ts': 'excluded',
                              'sidecars/unrelated/source.ts': 'excluded'})
                for test in ('_test-helpers.ts', 'config.test.ts', 'convert.test.ts', 'errors.test.ts', 'health.test.ts', 'parse-request.test.ts', 'safe-mime.test.ts', 'shim-boot.test.ts', 'shim.test.ts', 'soffice-runner.test.ts'):
                    files['sidecars/libreoffice/tests/' + test] = 'export {};\n'
            if name == 'lumia-ds':
                files.update({'packages/forms/package.json': '{}',
                              'packages/forms/src/index.ts': 'export const form = true;',
                              'packages/forms/semantic.css': 'body{}',
                              'packages/forms/node_modules/private.txt': 'excluded',
                              'apps/docs/private.txt': 'excluded'})
            recipe = 'FROM ' + BASE + ' AS prod\nCOPY src ./src\nUSER bun\nCMD ["bun","src/index.ts"]\n'
            if name in SOURCES[:9]:
                files['Dockerfile'] = recipe
                self.manifest['recipes'][name] = {'source': name, 'path': 'Dockerfile',
                                                  'sha256': hashlib.sha256(recipe.encode()).hexdigest(), 'target': 'prod'}
            for rel, contents in files.items():
                path = repo / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(contents)
            git(repo, 'add', '.')
            git(repo, 'commit', '-qm', 'Fixture source')
            sha = git(repo, 'rev-parse', 'HEAD')
            self.manifest['sources'][name] = {'repository': 'Xynes-Studio/' + name, 'commit': sha}
            (repo / 'src/index.ts').write_text('DIRTY_PRIVATE_PAYLOAD')
            (repo / '.env.local').write_text('UNTRACKED_PRIVATE_PAYLOAD')
            (repo / 'src/untracked.ts').write_text('UNTRACKED_PRIVATE_PAYLOAD')
        actual_bases = json.loads((ROOT / 'infra/release/source-manifest.json').read_text())['base_images']
        self.manifest['base_images'] += [x for x in actual_bases if x not in self.manifest['base_images']]
        storage = self.workspace / 'xynes/xynes-storage-service'
        for name, processor in self.manifest['processors'].items():
            prefix = 'sidecars/' + name + '/'
            processor['files'] = sorted([{'path': path[len(prefix):], 'mode': mode,
                'sha256': hashlib.sha256(contents).hexdigest()} for path, mode, contents in
                source_context.tracked_files(storage, self.manifest['sources']['xynes-storage-service']['commit'], 'xynes-storage-service', name)], key=lambda file: file['path'])
            processor['base_images'] = source_context.validate_recipe((ROOT / processor['recipe']).read_bytes(), processor, self.manifest['base_images'])
        self.inputs = {
            'xynes-auth-app': {
                'NEXT_PUBLIC_SUPABASE_URL': 'https://qa-supabase.example.invalid',
                'NEXT_PUBLIC_SUPABASE_ANON_KEY': 'fixture-public-key',
                'NEXT_PUBLIC_API_URL': 'https://qa-api.example.invalid',
                'NEXT_PUBLIC_APP_URL': 'https://qa-auth.example.invalid',
                'NEXT_PUBLIC_AUTH_APP_URL': 'https://qa-auth.example.invalid',
                'NEXT_PUBLIC_CONSOLE_URL': 'https://qa-cms.example.invalid',
                'NEXT_PUBLIC_CMS_CONSOLE_URL': 'https://qa-cms.example.invalid',
                'NEXT_PUBLIC_ALLOWED_REDIRECT_DOMAINS': 'qa-cms.example.invalid,qa-auth.example.invalid'},
            'xynes-cms-console-web': {
                'NEXT_PUBLIC_SUPABASE_URL': 'https://qa-supabase.example.invalid',
                'NEXT_PUBLIC_SUPABASE_ANON_KEY': 'fixture-public-key',
                'NEXT_PUBLIC_API_URL': 'https://qa-api.example.invalid',
                'NEXT_PUBLIC_APP_URL': 'https://qa-cms.example.invalid',
                'NEXT_PUBLIC_AUTH_APP_URL': 'https://qa-auth.example.invalid',
                'NEXT_PUBLIC_ALLOWED_REDIRECT_DOMAINS': 'qa-cms.example.invalid,qa-auth.example.invalid'}}

    def run_context(self, manifest=None, inputs=None, extra=()):
        manifest_path = self.root / 'manifest.json'
        manifest_path.write_text(json.dumps(manifest or self.manifest))
        inputs_path = self.root / 'public.json'
        inputs_path.write_text(json.dumps(inputs or self.inputs))
        self.output = self.root / 'prepared'
        return subprocess.run(['bash', str(SCRIPT), '--manifest', str(manifest_path),
                               '--workspace-root', str(self.workspace), '--output', str(self.output),
                               '--environment', 'qa', '--public-inputs', str(inputs_path), *extra],
                              capture_output=True, text=True)

    def test_replace_commit_and_blob_refs_cannot_substitute_pinned_bytes(self):
        repo = self.workspace / 'xynes/xynes-gateway'
        original = self.manifest['sources']['xynes-gateway']['commit']
        original_blob = git(repo, 'rev-parse', original + ':src/index.ts')
        (repo / 'src/index.ts').write_text('export const hostileReplacement = true;\n')
        git(repo, 'add', 'src/index.ts'); git(repo, 'commit', '-qm', 'Replacement fixture')
        replacement = git(repo, 'rev-parse', 'HEAD')
        replacement_blob = git(repo, 'rev-parse', 'HEAD:src/index.ts')
        for kind, old, new in [('commit', original, replacement), ('blob', original_blob, replacement_blob)]:
            git(repo, 'replace', old, new)
            result = self.run_context()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((self.output / 'backends/xynes-gateway/src/index.ts').read_text(), 'export const original = true;\n', kind)
            shutil.rmtree(self.output)
            git(repo, 'replace', '-d', old)

    def test_accounts_runtime_source_files_survive_preparation_and_remain_scanned(self):
        for marker in (False, True):
            repo = self.workspace / 'xynes/xynes-accounts-service'
            for index, path in enumerate(AccountsSourceModuleTest.PATHS):
                target = repo / path; target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('-----BEGIN PRIVATE KEY-----fixture' if marker else
                                  'export const publicSource' + str(index) + ' = true;\n')
            git(repo, 'add', 'src/infra/secrets'); git(repo, 'commit', '-qm', 'Runtime source fixture')
            manifest = copy.deepcopy(self.manifest)
            manifest['sources']['xynes-accounts-service']['commit'] = git(repo, 'rev-parse', 'HEAD')
            result = self.run_context(manifest)
            if marker:
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.output.exists())
                self.assertNotIn('PRIVATE KEY', result.stdout + result.stderr)
            else:
                self.assertEqual(result.returncode, 0, result.stderr)
                record = json.loads((self.output / 'source-record.json').read_text())
                for path in AccountsSourceModuleTest.PATHS:
                    exported = self.output / 'backends/xynes-accounts-service' / path
                    self.assertTrue(exported.is_file())
                    self.assertEqual(exported.read_bytes(), (repo / path).read_bytes())
                    self.assertEqual(record['files']['backends/xynes-accounts-service/' + path],
                                     hashlib.sha256(exported.read_bytes()).hexdigest())
                shutil.rmtree(self.output)

    def test_reads_original_committed_files_and_excludes_private_noise(self):
        result = self.run_context()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.output / 'backends/xynes-gateway/src/index.ts').read_text(),
                         'export const original = true;\n')
        paths = [str(p.relative_to(self.output)) for p in self.output.rglob('*')]
        self.assertFalse(any(x in path for path in paths for x in ['.env', '.git/', 'private.key',
                              'untracked.ts', 'index.test.ts', 'node_modules', 'junk.md']))
        self.assertNotIn('PRIVATE_PAYLOAD', ''.join(p.read_text() for p in self.output.rglob('*') if p.is_file()))

    def test_frontend_layout_includes_linked_package_source_and_original_recipe(self):
        result = self.run_context()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.output / 'frontend/lumia-ds/packages/forms/src/index.ts').exists())
        self.assertTrue((self.output / 'frontend/xynes-auth-sdk/src/index.ts').exists())
        self.assertTrue((self.output / 'frontend/xynes-i18n/src/index.ts').exists())
        self.assertTrue((self.output / 'frontend/xynes-cms-console-web/Dockerfile').exists())
        self.assertFalse((self.output / 'frontend/lumia-ds/apps').exists())
        record = json.loads((self.output / 'source-record.json').read_text())
        self.assertFalse(record['deployable'])
        self.assertEqual(record['sources'], self.manifest['sources'])
        self.assertGreater(len(record['files']), 5)

    def test_central_backend_recipes_preserve_immutable_source_and_closed_owner(self):
        manifest=copy.deepcopy(self.manifest)
        for name in SOURCES[:6]:
            recipe_path='infra/images/'+name+'.Dockerfile'
            path=ROOT/recipe_path
            digest=hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else '0'*64
            manifest['recipes'][name]={'source':'xynes-infra','path':recipe_path,'sha256':digest,'target':'prod'}
        result=self.run_context(manifest)
        self.assertEqual(result.returncode,0,result.stderr)
        for name in SOURCES[:6]:
            prepared=self.output/'backends'/name/'Dockerfile'
            self.assertEqual(prepared.read_bytes(),(ROOT/manifest['recipes'][name]['path']).read_bytes())
            self.assertEqual((self.output/'backends'/name/'src/index.ts').read_text(),'export const original = true;\n')
        record=json.loads((self.output/'source-record.json').read_text())
        self.assertEqual(record['recipes'],manifest['recipes'])
        self.assertFalse(record['deployable'])

    def test_coordinated_backend_runtime_and_available_ssl_packages_are_pinned(self):
        manifest=json.loads((ROOT/'infra/release/source-manifest.json').read_text())
        for name in SOURCES[:6]:
            recipe=manifest['recipes'][name]
            self.assertEqual(recipe['source'],'xynes-infra',name)
            contents=(ROOT/recipe['path']).read_bytes()
            self.assertEqual(hashlib.sha256(contents).hexdigest(),recipe['sha256'],name)
            self.assertIn(b'oven/bun:1.4.2-alpine@sha256:d888c0ae6c86d7866ff10c5aafdd9077b36aee6455b33dd270fb93c0dd5cef6f',contents,name)
            self.assertIn(b'libcrypto3=3.5.9-r0 libssl3=3.5.9-r0',contents,name)
            self.assertNotIn(b'--allow-untrusted',contents,name)

    def test_refuses_foreign_backend_recipe_path_and_mismatched_owner(self):
        for path in ['infra/images/xynes-accounts-service.Dockerfile','infra/images/archive-scanner.Dockerfile','Dockerfile','../Dockerfile']:
            manifest=copy.deepcopy(self.manifest)
            manifest['recipes']['xynes-gateway'].update(source='xynes-infra',path=path)
            result=self.run_context(manifest)
            self.assertNotEqual(result.returncode,0,path)
            self.assertFalse(self.output.exists())

    def test_central_cms_runtime_preserves_original_app_source_and_standalone_contract(self):
        manifest=copy.deepcopy(self.manifest)
        recipe_path='infra/images/cms-console-web.Dockerfile'
        recipe=ROOT/recipe_path
        digest=hashlib.sha256(recipe.read_bytes()).hexdigest() if recipe.exists() else '0'*64
        manifest['recipes']['xynes-cms-console-web']={'source':'xynes-infra','path':recipe_path,'sha256':digest,'target':'prod'}
        result=self.run_context(manifest)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((self.output/'frontend/cms-console-web.Dockerfile').read_bytes(),recipe.read_bytes())
        self.assertEqual((self.output/'frontend/xynes-cms-console-web/src/index.ts').read_text(),'export const original = true;\n')
        contents=recipe.read_text()
        self.assertIn('node:22.23.1-alpine3.23@sha256:8516dce0483394d5708d4b2ee6cacb79fb1d617ea4e2787c2120bcca92ce372e',contents)
        self.assertNotIn('node:20-',contents)
        self.assertIn('libcrypto3=3.5.9-r0 libssl3=3.5.9-r0',contents)
        self.assertIn('.next/standalone',contents)
        self.assertIn('exec node server.js',contents)
        self.assertIn('cp -a /tmp/icons-generated',contents)
        self.assertNotIn('DIRTY_PRIVATE_PAYLOAD',(self.output/'frontend/xynes-cms-console-web/src/index.ts').read_text())

    def test_frontend_recipes_select_declared_lumia_package_manager(self):
        for name in ['auth-app','cms-console-web']:
            contents=(ROOT/('infra/images/'+name+'.Dockerfile')).read_text()
            self.assertIn('cd /app/lumia-ds &&',contents,name)
            self.assertIn('test "$(pnpm --version)" = 10.23.0',contents,name)
            self.assertNotIn('pnpm --dir lumia-ds',contents,name)

    def test_rejects_moving_ref_before_output(self):
        manifest = copy.deepcopy(self.manifest)
        manifest['sources']['xynes-gateway']['commit'] = 'develop'
        result = self.run_context(manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_lumia_icon_build_retains_reviewed_cli_svg_import_helper(self):
        repo = self.workspace / 'xynes-front-end/lumia-ds'
        helper = repo / 'packages/cli/bin/lib/svg-import.js'
        helper.parent.mkdir(parents=True)
        helper.write_text('module.exports = { readSvgDirectory: () => [] };\n')
        svg = repo / 'packages/icons/svg/IconFixture.svg'
        svg.parent.mkdir(parents=True)
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>')
        config = repo / 'packages/icons/svgr.config.js'
        config.write_text('module.exports = {};\n')
        git(repo, 'add', str(helper.relative_to(repo)))
        git(repo, 'add', str(svg.relative_to(repo)), str(config.relative_to(repo)))
        git(repo, 'commit', '-qm', 'Reviewed icon build dependency')
        self.manifest['sources']['lumia-ds']['commit'] = git(repo, 'rev-parse', 'HEAD')
        result = self.run_context()
        self.assertEqual(result.returncode, 0, result.stderr)
        prepared = self.output / 'frontend/lumia-ds/packages/cli/bin/lib/svg-import.js'
        self.assertTrue(prepared.exists(), 'icon build dependency was incorrectly excluded')
        self.assertEqual(prepared.read_text(), helper.read_text())
        self.assertTrue((self.output / 'frontend/lumia-ds/packages/icons/svg/IconFixture.svg').is_file())
        self.assertTrue((self.output / 'frontend/lumia-ds/packages/icons/svgr.config.js').is_file())

    def test_rejects_unknown_source_and_unknown_commit(self):
        for change in ['unknown-source', 'unknown-commit']:
            with self.subTest(change=change):
                manifest = copy.deepcopy(self.manifest)
                if change == 'unknown-source':
                    manifest['sources']['attacker'] = manifest['sources']['xynes-gateway']
                else:
                    manifest['sources']['xynes-gateway']['commit'] = '0' * 40
                result = self.run_context(manifest)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.output.exists())

    def test_rejects_missing_base_digest_and_recipe_tampering(self):
        for change in ['base', 'recipe']:
            with self.subTest(change=change):
                manifest = copy.deepcopy(self.manifest)
                if change == 'base': manifest['base_images'] = ['example.test/bun:latest']
                else: manifest['recipes']['xynes-gateway']['sha256'] = '0' * 64
                result = self.run_context(manifest)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.output.exists())

    def test_rejects_planning_release_and_missing_approval(self):
        result = self.run_context(extra=('--purpose', 'release'))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())
        manifest = copy.deepcopy(self.manifest)
        manifest['status'] = 'approved'
        result = self.run_context(manifest, extra=('--purpose', 'release'))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_rejects_unknown_environment_and_secret_build_fields(self):
        for extra, inputs in [(('--environment', 'dev'), self.inputs),
                              ((), {**self.inputs, 'DATABASE_URL': 'do-not-print-this-secret'})]:
            result = self.run_context(inputs=inputs, extra=extra)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('do-not-print-this-secret', result.stdout + result.stderr)
            self.assertFalse(self.output.exists())

    def test_rejects_url_credentials_and_service_role_key(self):
        for key, value in [('NEXT_PUBLIC_API_URL', 'https://username:do-not-print-this-secret@api.example.invalid'),
                           ('NEXT_PUBLIC_SUPABASE_ANON_KEY', 'eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.fake')]:
            inputs = copy.deepcopy(self.inputs)
            inputs['xynes-auth-app'][key] = value
            result = self.run_context(inputs=inputs)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('do-not-print-this-secret', result.stdout + result.stderr)
            self.assertFalse(self.output.exists())

    def test_rejects_recipe_base_missing_from_declared_inventory(self):
        manifest = copy.deepcopy(self.manifest)
        manifest['base_images'] = ['example.test/bun:1.4.2@sha256:' + 'b' * 64]
        result = self.run_context(manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_refuses_tracked_secret_material_inside_allowed_source(self):
        repo = self.workspace / 'xynes/xynes-gateway'
        (repo / 'src/private.ts').write_text('const privateKey = "-----BEGIN PRIVATE KEY-----secret";')
        git(repo, 'add', 'src/private.ts')
        git(repo, 'commit', '-qm', 'Hostile committed secret')
        manifest = copy.deepcopy(self.manifest)
        manifest['sources']['xynes-gateway']['commit'] = git(repo, 'rev-parse', 'HEAD')
        result = self.run_context(manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_rejects_different_frontend_api_and_auth_origins(self):
        inputs = copy.deepcopy(self.inputs)
        inputs['xynes-cms-console-web']['NEXT_PUBLIC_AUTH_APP_URL'] = 'https://prod-auth.example.invalid'
        result = self.run_context(inputs=inputs)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_refuses_existing_output_without_overwriting(self):
        (self.root / 'prepared').mkdir()
        marker = self.root / 'prepared/keep'
        marker.write_text('keep')
        result = self.run_context()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(marker.read_text(), 'keep')

    def test_rejects_tracked_symlinks_in_selected_source(self):
        repo = self.workspace / 'xynes/xynes-gateway'
        (repo / 'src/escape.ts').symlink_to('../../outside')
        git(repo, 'add', 'src/escape.ts')
        git(repo, 'commit', '-qm', 'Hostile symlink')
        manifest = copy.deepcopy(self.manifest)
        manifest['sources']['xynes-gateway']['commit'] = git(repo, 'rev-parse', 'HEAD')
        result = self.run_context(manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())


    def test_processor_context_is_byte_identical_closed_and_records_freshclam(self):
        repo = self.workspace / 'xynes/xynes-storage-service'
        (repo / 'sidecars/archive-scanner/archive.go').write_text('DIRTY_PRIVATE_PAYLOAD')
        (repo / 'sidecars/libreoffice/src/shim.ts').write_text('DIRTY_PRIVATE_PAYLOAD')
        result = self.run_context()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.output / 'processors/archive-scanner/archive.go').read_text(), 'package main\n')
        self.assertEqual((self.output / 'processors/libreoffice/src/shim.ts').read_text(), 'export const pinned = true;\n')
        self.assertTrue((self.output / 'processors/archive-scanner/archive_test.go').is_file())
        self.assertFalse((self.output / 'processors/archive-scanner/private.go').exists())
        self.assertFalse((self.output / 'processors/libreoffice/src/private.ts').exists())
        self.assertFalse((self.output / 'backends/xynes-storage-service/sidecars').exists())
        record = json.loads((self.output / 'source-record.json').read_text())
        self.assertEqual(record['processors'], self.manifest['processors'])
        self.assertEqual(record['external_images'], self.manifest['external_images'])
        self.assertFalse(record['deployable'])

    def test_rejects_missing_unknown_or_mutable_processor_inputs(self):
        mutations = [lambda m: m['processors'].pop('archive-scanner'),
                     lambda m: m['processors'].update(attacker=m['processors']['archive-scanner']),
                     lambda m: m['processors']['archive-scanner'].update(source='xynes-gateway'),
                     lambda m: m['processors']['archive-scanner'].update(context_prefix='sidecars/../private'),
                     lambda m: m['processors']['archive-scanner'].update(recipe='infra/images/auth-app.Dockerfile'),
                     lambda m: m['processors']['archive-scanner'].update(sha256='develop'),
                     lambda m: m['processors']['archive-scanner'].update(platform='linux/arm64'),
                     lambda m: m['external_images']['clamav-freshclam'].update(image='clamav/clamav:latest'),
                     lambda m: m['external_images'].update(attacker=m['external_images']['clamav-freshclam'])]
        for mutate in mutations:
            manifest = copy.deepcopy(self.manifest)
            mutate(manifest)
            result = self.run_context(manifest)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(self.output.exists())

    def test_rejects_selected_processor_symlinks_and_missing_test_source(self):
        repo = self.workspace / 'xynes/xynes-storage-service'
        path = repo / 'sidecars/archive-scanner/archive.go'
        path.unlink()
        path.symlink_to('../../outside')
        git(repo, 'add', str(path.relative_to(repo)))
        git(repo, 'commit', '-qm', 'Hostile processor symlink')
        self.manifest['sources']['xynes-storage-service']['commit'] = git(repo, 'rev-parse', 'HEAD')
        result = self.run_context()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_rejects_processor_recipe_hash_mismatch(self):
        manifest = copy.deepcopy(self.manifest)
        manifest['processors']['libreoffice']['sha256'] = '1' * 64
        result = self.run_context(manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_rejects_unparsed_from_syntax_that_hides_mutable_base(self):
        repo = self.workspace / 'xynes/xynes-gateway'
        data = 'FROM --platform=linux/amd64 attacker.invalid/tool:latest AS build\nFROM ' + BASE + ' AS prod\n'
        (repo / 'Dockerfile').write_text(data)
        git(repo, 'add', 'Dockerfile')
        git(repo, 'commit', '-qm', 'Malformed base declaration')
        self.manifest['sources']['xynes-gateway']['commit'] = git(repo, 'rev-parse', 'HEAD')
        self.manifest['recipes']['xynes-gateway']['sha256'] = hashlib.sha256(data.encode()).hexdigest()
        result = self.run_context()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_rejects_omitted_reviewed_scanner_test(self):
        repo = self.workspace / 'xynes/xynes-storage-service'
        git(repo, 'rm', 'sidecars/archive-scanner/more_test.go')
        git(repo, 'commit', '-qm', 'Omitted reviewed scanner test')
        self.manifest['sources']['xynes-storage-service']['commit'] = git(repo, 'rev-parse', 'HEAD')
        result = self.run_context()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())


    def test_processor_inventory_must_match_exact_git_bytes_and_modes(self):
        for change in ('bytes', 'mode', 'missing', 'duplicate', 'extra_base'):
            manifest = copy.deepcopy(self.manifest)
            processor = manifest['processors']['archive-scanner']
            if change == 'bytes': processor['files'][0]['sha256'] = '7' * 64
            if change == 'mode': processor['files'][0]['mode'] = '100755'
            if change == 'missing': processor['files'].pop()
            if change == 'duplicate': processor['files'][-1] = processor['files'][0]
            if change == 'extra_base': processor['base_images'] = sorted(processor['base_images'] + [BASE])
            result = self.run_context(manifest=manifest)
            self.assertEqual(result.returncode, 1, change)
            self.assertFalse(self.output.exists(), change)

    def test_unrelated_storage_commit_preserves_exact_processor_identity(self):
        import release_manifest
        old = {name: release_manifest.processor_content_identity(self.manifest, name) for name in self.manifest['processors']}
        repo = self.workspace / 'xynes/xynes-storage-service'
        (repo / 'src/index.ts').write_text('export const nextApprovedAppVersion = true;\n')
        git(repo, 'add', 'src/index.ts'); git(repo, 'commit', '-qm', 'Unrelated application source')
        self.manifest['sources']['xynes-storage-service']['commit'] = git(repo, 'rev-parse', 'HEAD')
        self.assertEqual(old, {name: release_manifest.processor_content_identity(self.manifest, name) for name in self.manifest['processors']})
        result = self.run_context(); self.assertEqual(result.returncode, 0, result.stderr)



class AuthRecipeTest(unittest.TestCase):
    def test_auth_uses_next_start_and_separate_production_dependency_stage(self):
        path = ROOT / 'infra/images/auth-app.Dockerfile'
        self.assertTrue(path.exists(), 'Missing infra-owned Auth production recipe')
        recipe = path.read_text()
        self.assertIn('--ignore-workspace', recipe)
        self.assertIn('--prod --frozen-lockfile', recipe)
        self.assertIn('AS production-dependencies', recipe)
        self.assertIn('COPY --from=production-dependencies', recipe)
        self.assertIn('USER nextjs', recipe)
        self.assertIn('node_modules/next/dist/bin/next', recipe)
        self.assertIn('"start"', recipe)
        self.assertNotIn('standalone', recipe)
        self.assertNotIn('sed ', recipe)
        self.assertIn('/api/health', recipe)
        self.assertIn('@sha256:', recipe)

    def test_auth_context_ignores_all_but_pinned_frontend_source(self):
        path = ROOT / 'infra/images/auth-app.Dockerfile.dockerignore'
        self.assertTrue(path.exists(), 'Missing deny-by-default Auth context rules')
        rules = path.read_text().splitlines()
        self.assertEqual(rules[0], '**')
        for rule in ['!xynes-auth-app/**', '!xynes-auth-sdk/**', '!xynes-i18n/**',
                     '!lumia-ds/packages/**', '**/.env*', '**/node_modules', '**/.git']:
            self.assertIn(rule, rules)



class NativeRecipeTest(unittest.TestCase):
    def test_scanner_build_coverage_gate_accepts_high_and_refuses_low_per_file(self):
        import re
        data = (ROOT / 'infra/images/archive-scanner.Dockerfile').read_text()
        gate = re.search(r"&& awk '([^']+)'", data).group(1)
        for profile, expected in [
            ('mode: set\npkg/archive.go:1.1,2.1 20 1\npkg/archive.go:3.1,4.1 1 0\n', 0),
            ('mode: set\npkg/archive.go:1.1,2.1 1 1\npkg/server.go:1.1,2.1 1 0\n', 1),
            ('mode: set\n', 1),
        ]:
            result = subprocess.run(['awk', gate], input=profile, text=True, capture_output=True)
            self.assertEqual(result.returncode, expected, result.stderr)

    def test_native_recipes_have_pinned_bases_named_prod_and_byte_source_copy(self):
        for name, user in [('archive-scanner', '1000:1000'), ('libreoffice', '10001:10001')]:
            recipe = ROOT / ('infra/images/' + name + '.Dockerfile')
            self.assertTrue(recipe.exists(), 'Missing native recipe: ' + name)
            data = recipe.read_text()
            self.assertIn(' AS prod', data)
            self.assertIn('@sha256:', data)
            self.assertIn('USER ' + user, data)
            self.assertNotIn('sed -i src', data)
        scanner = (ROOT / 'infra/images/archive-scanner.Dockerfile').read_text()
        self.assertIn('golang:1.27.2-bookworm@sha256:', scanner)
        self.assertIn('go test', scanner)
        self.assertIn('archive_test.go', scanner)
        self.assertIn('HEALTHCHECK NONE', scanner)
        office = (ROOT / 'infra/images/libreoffice.Dockerfile').read_text()
        self.assertIn('snapshot.debian.org', office)
        self.assertIn('Signed-By:', office)
        self.assertIn('Check-Valid-Until: no', office)
        self.assertIn('SAL_DISABLE_MACROS=1', office)
        self.assertNotIn('trusted=yes', office)
        self.assertNotIn('--allow-unauthenticated', office)

    def test_schema_processor_and_external_inventory_are_closed(self):
        schema = json.loads((ROOT / 'infra/release/source-manifest.schema.json').read_text())
        for key, expected in [('processors', {'archive-scanner', 'libreoffice'}),
                              ('external_images', {'clamav-freshclam', 'supabase-db', 'supabase-auth'})]:
            self.assertIn(key, schema['required'])
            child = schema['properties'][key]
            self.assertFalse(child['additionalProperties'])
            self.assertEqual(set(child['required']), expected)



if __name__ == '__main__':
    unittest.main()
