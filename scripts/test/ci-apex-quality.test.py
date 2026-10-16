#!/usr/bin/env python3
"""Infra-only export and required read-only Apex gate contracts."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tarfile
import io
import ast
import re
from types import SimpleNamespace
import unittest

ROOT=Path(__file__).resolve().parents[2]
EXPORTER=ROOT/'scripts/setup/export-ci-workflows.py'
GO_SHA='ecbadb99091a3f46e31f5f934b068b1864eafa7995211b39eaddf76996045fe5'
PG_IMAGE='postgres:17-alpine@sha256:979c4379dd698aba0b890599a6104e082035f98ef31d9b9291ec22f2b13059ca'
IMAGE='node:22.23.1-alpine3.23@sha256:4848379985144e72c7537574c1a894d4ec096704b21ce45e5eee386be9fab737'
REQUIRED={'lint','test','coverage','build','typecheck'}

def parse(file):
    p=subprocess.run(['bun','-e','console.log(JSON.stringify(Bun.YAML.parse(require("node:fs").readFileSync(process.argv[1],"utf8"))))',str(file)],capture_output=True,text=True,check=True)
    result=json.loads(p.stdout)
    # YAML block scalars are literal. Read their actual bytes rather than a
    # runtime YAML parser normalizing shell backslash escapes.
    lines=file.read_text().splitlines();blocks=[]
    for i,line in enumerate(lines):
        if line.strip()=='run: |':
            indent=len(line)-len(line.lstrip());body=[]
            for child in lines[i+1:]:
                if child.strip() and len(child)-len(child.lstrip())<=indent:break
                body.append(child[indent+2:])
            blocks.append('\n'.join(body)+'\n')
    block=iter(blocks)
    for step in result['jobs']['gates']['steps']:
        if '\n' in step.get('run',''):step['run']=next(block)
    return result

class ApexQualityCI(unittest.TestCase):
    def test_required_test_gate_executes_all_apex_verifiers_and_explicit_linux_runtime(self):
        value=parse(ROOT/'.github/workflows/quality-gates.yml');job=value['jobs']['gates']
        self.assertEqual(value['permissions'],{'contents':'read'});self.assertEqual(set(job['strategy']['matrix']['gate']),REQUIRED)
        self.assertNotIn('environment',job);self.assertNotIn('permissions',job);self.assertNotRegex(json.dumps(value),r'\$\{\{\s*secrets\.')
        case=next(x['run'] for x in job['steps'] if 'case "$GATE_NAME"' in x.get('run',''))
        for path in ['source-context.test.sh','release-manifest.test.sh','migration-inventory.test.sh','migration-plan.test.sh','migration-sql.test.py','migration-sql-runtime.test.py','image-build.test.sh','registry-policy.test.py','registry-io.test.sh','registry-https.test.py','admission.test.py','publish-batch.test.py','github-read.test.py','apex-ci-policy.test.ts','source-checks.test.ts','hosted-release.test.sh','processor-readiness.test.sh','vps-reconcile.test.sh','staging-substrate.test.sh','ci-apex-quality.test.py','vps-deploy-users-boundary.test.sh']:
            self.assertIn(path,case)
        self.assertIn('host-retained-dev.test.py',case)
        for path in ('libreoffice-recipe.test.py', 'host-backup-schedule.test.py'):
            self.assertIn(path,case)
        self.assertIn('test -race -count=1 ./...',case);self.assertIn('vet ./...',case)
        self.assertIn('provider-inputs.test.py',case)
        self.assertIn('environment-inputs.test.py',case)
        self.assertIn('registry-namespace.test.py',case)
        self.assertIn('publication-record.test.py',case)
        self.assertIn('attestation-binding.test.py',case)
        self.assertIn('native-attestation.test.py',case)
        for path in ('deploy-request.test.py','deployment-candidate.test.py','ssh-entry.test.py','deploy-admission.test.py','deploy-dispatch.test.py','host-release-state.test.py','host-compose.test.py','migration-transport.test.py','pg-container-bridge.test.py','candidate-migration.test.py','host-migrate.test.py','host-seed.test.py','host-backup.test.py','host-assets.test.py','root-executor.test.py','host-boundary.test.py'):
            self.assertIn(path,case)
        self.assertIn('auth-runtime.test.py',case)
        for path in ('host-runtime.test.py','host-supervisor.test.py','host-controls.test.py','host-smoke.test.py','host-smoke-setup.test.py'):
            self.assertIn(path,case)
        self.assertIn('auth-runtime.integration.py',case)
        for path in ('artifact-handoff.test.py','artifact-download.test.py','publish-images.test.py','build-pipeline.test.py','apex-workflow.test.py'):
            self.assertIn(path,case)
        self.assertIn('idle-adapter-unix.test.sh --runtime',case)
        self.assertNotIn('|| true',case);self.assertNotIn('continue-on-error',json.dumps(value))

    def test_security_job_scans_full_history_with_pinned_quiet_gate(self):
        p=subprocess.run(['bun','-e','console.log(JSON.stringify(Bun.YAML.parse(require("node:fs").readFileSync(process.argv[1],"utf8"))))',str(ROOT/'.github/workflows/security.yml')],capture_output=True,text=True,check=True)
        value=json.loads(p.stdout);job=value['jobs']['security']
        self.assertEqual(value['permissions'],{'contents':'read'})
        self.assertNotIn('environment',job)
        checkout=next(x for x in job['steps'] if x.get('uses','').startswith('actions/checkout@'))
        self.assertEqual(checkout['with'].get('fetch-depth'),0)
        commands='\n'.join(x.get('run','') for x in job['steps'])
        self.assertIn('bun scripts/lib/apex-ci-policy.ts',commands)
        self.assertIn('gitleaks/releases/download/v8.30.1/',commands)
        self.assertIn('551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb',commands)
        self.assertIn('scripts/security/secret_gate.py',commands)
        self.assertNotRegex(json.dumps(value),r'secrets\.|upload-artifact|continue-on-error')

    def test_required_lint_gate_enforces_the_infra_workflow_role_checker(self):
        value=parse(ROOT/'.github/workflows/quality-gates.yml')
        case=next(x['run'] for x in value['jobs']['gates']['steps'] if 'case "$GATE_NAME"' in x.get('run',''))
        lint=case.split('lint)',1)[1].split(';;',1)[0]
        self.assertIn('bun scripts/lib/apex-ci-policy.ts',lint)

    def test_infra_release_packaging_uses_allowlisted_bundle_and_never_full_tree_archive(self):
        text=(ROOT/'.github/workflows/release-provenance.yml').read_text()
        self.assertIn('python3 scripts/release/public_bundle.py --commit "$GITHUB_SHA"',text)
        self.assertNotIn('git archive --format=tar',text)

    def test_public_infra_typecheck_uses_only_owned_locked_tools_without_peer_checkout(self):
        value=parse(ROOT/'.github/workflows/quality-gates.yml');steps=value['jobs']['gates']['steps']
        self.assertTrue(all('repository' not in step.get('with', {}) for step in steps), 'public infra CI must not clone peer repositories')
        install=[x for x in steps if x.get('working-directory')=='.github/type-tools']
        self.assertEqual(len(install),1)
        self.assertEqual(install[0]['if'],"matrix.gate == 'typecheck'")
        self.assertEqual(install[0]['run'],'pnpm install --frozen-lockfile --ignore-scripts')
        command=next(x['run'] for x in steps if 'case "$GATE_NAME"' in x.get('run',''))
        self.assertIn('.github/type-tools/node_modules/.bin/tsc',command)
        self.assertNotIn('ci-type-tools/node_modules',command)

    def test_go_and_public_linux_fixture_are_pinned_prepared_only_for_test_gate(self):
        value=parse(ROOT/'.github/workflows/quality-gates.yml');job=value['jobs']['gates']
        tools=[x for x in job['steps'] if x.get('name')=='Prepare verified Apex Go and public Linux fixture']
        self.assertEqual(len(tools),1,'missing pinned Apex tool preparation')
        step=tools[0];self.assertEqual(step['if'],"matrix.gate == 'test'")
        command=step['run'];self.assertIn('https://go.dev/dl/go1.27.2.linux-amd64.tar.gz',command);self.assertIn(GO_SHA,command)
        self.assertLess(command.index('sha256sum -c -'),command.index('tar -xzf'))
        self.assertIn(IMAGE,command);self.assertIn(PG_IMAGE,command);self.assertIn('docker pull --platform linux/amd64',command)
        self.assertIn('supabase/gotrue:v2.196.0@sha256:7e813221b93fbf54b515036438550e483bfaf057b9db52fe9bc1ce91c47e817e',command)
        self.assertIn('postgres:17-alpine@sha256:5a6fcbc5d93831991d2386fa634509b3c49a1ac5ffb70c13c2322840f821d7e7',command)
        self.assertIn('linux/amd64',command);self.assertIn('GOTOOLCHAIN=local',command)
        self.assertNotIn('latest',command);self.assertNotIn('docker run',command);self.assertNotIn('docker build',command)

    def test_tampered_tool_archive_fails_before_extraction_or_fixture_pull(self):
        value=parse(ROOT/'.github/workflows/quality-gates.yml')
        steps=[x for x in value['jobs']['gates']['steps'] if x.get('name')=='Prepare verified Apex Go and public Linux fixture']
        self.assertEqual(len(steps),1,'missing pinned preparation step')
        with tempfile.TemporaryDirectory(prefix='ci-apex-tools-') as tmp:
            base=Path(tmp);binpath=base/'bin';binpath.mkdir();log=base/'calls'
            curl=binpath/'curl';curl.write_text("#!/usr/bin/env python3\nimport sys\nfrom pathlib import Path\na=sys.argv;Path(a[a.index('--output')+1]).write_text('tampered archive')\n");curl.chmod(0o755)
            for name in ['tar','docker']:
                f=binpath/name;f.write_text('#!/bin/sh\nprintf called >> "$TEST_CALLS"\nexit 9\n');f.chmod(0o755)
            env={**os.environ,'RUNNER_TEMP':tmp,'GITHUB_ENV':str(base/'env'),'TEST_CALLS':str(log),'PATH':str(binpath)+os.pathsep+os.environ['PATH']}
            p=subprocess.run(['bash','-c',steps[0]['run']],env=env,capture_output=True,text=True)
            self.assertNotEqual(p.returncode,0);self.assertFalse(log.exists());self.assertFalse((base/'env').exists())

    def test_literal_preparation_script_checks_real_fixture_hash_and_exports_lines(self):
        value=parse(ROOT/'.github/workflows/quality-gates.yml')
        step=next(x for x in value['jobs']['gates']['steps'] if x.get('name')=='Prepare verified Apex Go and public Linux fixture')
        with tempfile.TemporaryDirectory(prefix='ci-apex-good-') as tmp:
            base=Path(tmp);binpath=base/'bin';binpath.mkdir();archive=base/'fixture.tar.gz'
            data=b"#!/bin/sh\nprintf 'go version go1.27.2 linux/amd64\\n'\n"
            with tarfile.open(archive,'w:gz') as out:
                item=tarfile.TarInfo('go/bin/go');item.size=len(data);item.mode=0o755;out.addfile(item,io.BytesIO(data))
            checksum=hashlib.sha256(archive.read_bytes()).hexdigest()
            curl=binpath/'curl';curl.write_text("#!/usr/bin/env python3\nimport os,sys,shutil\na=sys.argv;shutil.copyfile(os.environ['TEST_ARCHIVE'],a[a.index('--output')+1])\n");curl.chmod(0o755)
            docker=binpath/'docker';docker.write_text("#!/bin/sh\nif [ \"$1\" = image ]; then printf 'linux/amd64\\n'; fi\n");docker.chmod(0o755)
            env={**os.environ,'RUNNER_TEMP':tmp,'GITHUB_ENV':str(base/'env'),'TEST_ARCHIVE':str(archive),'PATH':str(binpath)+os.pathsep+os.environ['PATH']}
            p=subprocess.run(['bash','-c',step['run'].replace(GO_SHA,checksum)],env=env,text=True,capture_output=True)
            self.assertEqual(p.returncode,0,p.stderr)
            exports=(base/'env').read_text().splitlines();self.assertEqual(len(exports),5)
            self.assertIn('GOTOOLCHAIN=local',exports);self.assertIn('XYNES_IDLE_FIXTURE_IMAGE='+IMAGE,exports)

    def test_selected_export_check_is_idempotent_and_preserves_sibling_mirrors(self):
        with tempfile.TemporaryDirectory(prefix='ci-apex-workspace-') as tmp:
            base=Path(tmp);repo=base/'selected';repo.mkdir()
            subprocess.run(['git','init','-q',str(repo)],check=True)
            subprocess.run(['git','-C',str(repo),'remote','add','origin','https://github.com/Xynes-Studio/xynes-infra.git'],check=True)
            siblings=base/'xynes/xynes-gateway/.github/workflows';siblings.mkdir(parents=True)
            sentinel=siblings/'quality-gates.yml';sentinel.write_text('UNCHANGED-SIBLING\n')
            before=hashlib.sha256(sentinel.read_bytes()).hexdigest()
            args=['python3',str(EXPORTER),'--workspace-root',str(base),'--repository','xynes/xynes-infra','--checkout',str(repo),'--workflow','quality-gates.yml']
            p=subprocess.run(args,capture_output=True,text=True);self.assertEqual(p.returncode,0,p.stderr)
            expected=(ROOT/'.github/workflows/quality-gates.yml').read_bytes()
            self.assertEqual((repo/'.github/workflows/quality-gates.yml').read_bytes(),expected)
            self.assertFalse((repo/'.github/workflows/release-provenance.yml').exists())
            self.assertEqual(hashlib.sha256(sentinel.read_bytes()).hexdigest(),before)
            p=subprocess.run(args+['--check'],capture_output=True,text=True);self.assertEqual(p.returncode,0,p.stderr)
            (repo/'.github/workflows/quality-gates.yml').write_text('drift\n')
            p=subprocess.run(args+['--check'],capture_output=True,text=True);self.assertNotEqual(p.returncode,0)
            self.assertEqual((repo/'.github/workflows/quality-gates.yml').read_text(),'drift\n')
            self.assertEqual(hashlib.sha256(sentinel.read_bytes()).hexdigest(),before)

    def test_all_32_noninfra_generated_mirrors_match_reviewed_baseline(self):
        profiles=json.loads((ROOT/'scripts/setup/ci-quality-profiles.json').read_text())
        pins=json.loads((ROOT/'scripts/setup/ci-action-pins.json').read_text())
        with tempfile.TemporaryDirectory(prefix='ci-apex-siblings-') as tmp:
            workspace=Path(tmp)
            for profile in profiles:
                repo=workspace/profile['repository'];repo.mkdir(parents=True)
                (repo/'package.json').write_text(json.dumps({'packageManager':'pnpm@'+('10.23.0' if profile['kind']=='lumia' else '10.33.0')}))
                (repo/'bun.lock').write_text('fixture')
            tree=ast.parse(EXPORTER.read_text())
            nodes=[node for node in tree.body if isinstance(node,ast.FunctionDef) or isinstance(node,ast.Assign) and any(isinstance(target,ast.Name) and target.id=='peers' for target in node.targets)]
            scope={'json':json,'re':re,'Path':Path,'args':SimpleNamespace(workspace_root=workspace,checkout=None),'actions':{pin['upstream_repository']:pin['uses'] for pin in pins if pin['upstream_repository']!='github/codeql-action'}}
            exec(compile(ast.Module(body=nodes,type_ignores=[]),'<workflow-renderer>','exec'),scope)
            digest=hashlib.sha256();count=0
            for profile in profiles:
                if profile['kind']=='infra':continue
                for filename,renderer in [('quality-gates.yml',scope['workflow']),('release-provenance.yml',scope['release_workflow'])]:
                    rendered='# Generated from xynes-infra scripts/setup/ci-quality-profiles.json.\n'+'\n'.join(scope['yaml'](renderer(profile)))+'\n'
                    digest.update((profile['repository']+'/'+filename+'\0'+rendered).encode());count+=1
            self.assertEqual(count,32)
            self.assertEqual(digest.hexdigest(),'cd78e2304314b33ba9106fb4ed80554856b9f115a6ee9dab5ac8e2cc8c4f04ed','unrelated sibling workflow output changed')

    def test_wrong_selected_repository_identity_is_refused_before_writes(self):
        with tempfile.TemporaryDirectory(prefix='ci-apex-foreign-') as tmp:
            repo=Path(tmp);subprocess.run(['git','init','-q',str(repo)],check=True)
            subprocess.run(['git','-C',str(repo),'remote','add','origin','https://github.com/Xynes-Studio/xynes-gateway.git'],check=True)
            p=subprocess.run(['python3',str(EXPORTER),'--repository','xynes/xynes-infra','--checkout',str(repo),'--workflow','quality-gates.yml'],capture_output=True,text=True)
            self.assertNotEqual(p.returncode,0);self.assertFalse((repo/'.github').exists())

if __name__=='__main__':unittest.main(verbosity=2)
