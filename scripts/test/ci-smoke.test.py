#!/usr/bin/env python3
"""Real OCI bytes qualify CI smoke inputs before any Docker execution."""
import copy
import contextlib
import importlib.util
import json
import hashlib
import io
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
import zipfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/release'))
import image_build
import release_manifest as records

spec = importlib.util.spec_from_file_location('ci_smoke_image_fixture', ROOT / 'scripts/test/image-build.test.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class Tests(unittest.TestCase):
    def setUp(self):
        path = ROOT / 'scripts/release/ci_smoke.py'
        self.assertTrue(path.is_file(), 'CI product-smoke input verifier is missing')
        spec = importlib.util.spec_from_file_location('ci_smoke_under_test', path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        temporary = tempfile.TemporaryDirectory(prefix='ci-smoke-inputs-')
        self.addCleanup(temporary.cleanup)
        self.archives = Path(temporary.name).resolve()
        self.manifest, self.public, self.controller = fixture.inputs()
        self.manifest.update(status='approved', approval_reference='https://github.com/Xynes-Studio/xynes-infra/pull/1')
        self.public = json.loads(json.dumps(self.public).replace('.example.invalid', '.example.com'))
        self.tasks = image_build.build_tasks(self.manifest, self.public, self.controller, 'release')
        images = []
        for task in self.tasks:
            path = self.archives / (task['id'] + '.oci.tar')
            fixture.archive(path, task['labels'])
            images.append(image_build.inspect_oci(path, task))
        self.receipt = {'schema_version': 1, 'artifact_kind': 'local-oci-build-receipts',
                        'controller': copy.deepcopy(self.controller),
                        'source_manifest_sha256': records.canonical_sha256(self.manifest),
                        'images': images, 'external_images': copy.deepcopy(self.manifest['external_images']),
                        'published': False, 'deployable': False}

    def verify(self):
        return self.module.verify_build(manifest=self.manifest, public=self.public,
                                        controller=self.controller, receipt=self.receipt, archives=self.archives)

    def test_selects_six_exact_backend_images_for_eight_service_runtime(self):
        images = self.verify()
        self.assertEqual({task['workload'] for task, _, _ in images}, {
            'xynes-gateway', 'xynes-accounts-service', 'xynes-authz-service',
            'xynes-cms-core', 'xynes-doc-service', 'xynes-telemetry-service'})
        self.assertTrue(all(task['environment'] is None for task, _, _ in images))
        self.assertTrue(all(actual == image_build.inspect_oci(path, task) for task, path, actual in images))

    def test_refuses_mismatched_controller_source_or_external_images(self):
        original = copy.deepcopy(self.receipt)
        for key, value in [('controller', {**self.controller, 'run_id': 999}),
                           ('source_manifest_sha256', 'sha256:' + 'f' * 64),
                           ('external_images', {}), ('published', True), ('deployable', True),
                           ('schema_version', True)]:
            with self.subTest(key=key), self.assertRaises(self.module.SmokeError):
                self.receipt = {**original, key: value}
                self.verify()

    def test_refuses_missing_duplicate_or_forged_image_receipts(self):
        original = copy.deepcopy(self.receipt['images'])
        for images in (original[:-1], original[:-1] + [original[0]],
                       [{**original[0], 'config_digest': 'sha256:' + 'f' * 64}] + original[1:],
                       [{**original[0], 'manifest_digest': 'sha256:' + 'f' * 64}] + original[1:],
                       [{**original[0], 'platform': 'linux/arm64'}] + original[1:]):
            with self.subTest(images=len(images)), self.assertRaises(self.module.SmokeError):
                self.receipt['images'] = images
                self.verify()

    def test_checks_unselected_frontend_archive_too(self):
        (self.archives / 'prod--xynes-auth-app.oci.tar').write_bytes(b'corrupt')
        with self.assertRaises(self.module.SmokeError):
            self.verify()

    def test_refuses_missing_backend_archive(self):
        (self.archives / 'shared--xynes-gateway.oci.tar').unlink()
        with self.assertRaises(self.module.SmokeError):
            self.verify()

    def test_refuses_extra_archive_and_symlink(self):
        extra = self.archives / 'unexpected.oci.tar'
        extra.write_bytes(b'extra')
        with self.assertRaises(self.module.SmokeError):
            self.verify()
        extra.unlink()
        path = self.archives / (self.tasks[0]['id'] + '.oci.tar')
        outside = tempfile.TemporaryDirectory(prefix='ci-smoke-symlink-target-')
        self.addCleanup(outside.cleanup)
        target = Path(outside.name).resolve() / 'owned-copy'
        path.rename(target)
        path.symlink_to(target)
        with self.assertRaises(self.module.SmokeError):
            self.verify()


class NativeGitTests(unittest.TestCase):
    def setUp(self):
        import ci_smoke
        self.module=ci_smoke
        self.controller={'repository':'Xynes-Studio/xynes-infra','commit':'a'*40,
                         'workflow':'apex-build.yml','run_id':100,'run_attempt':1}

    def test_native_tracked_git_uses_only_fixed_root_and_exact_manifest_reads(self):
        commands=[]
        def command(argv,**kwargs):
            commands.append((argv,kwargs));return b'inert'
        with patch.object(self.module.build_pipeline,'bounded_command',side_effect=command):
            tracked=self.module.tracked_git_command(self.controller)
            for suffix in (['rev-parse','HEAD'],['show','a'*40+':infra/release/source-manifest.json']):
                tracked(['git','--no-replace-objects']+suffix,env={'PATH':os.environ['PATH']},cwd=ROOT,timeout=30)
            for argv,cwd in ((['git','--no-replace-objects','fetch','origin'],ROOT),
                             (['git','-c','safe.directory=*','rev-parse','HEAD'],ROOT),
                             (['git','--no-replace-objects','show','b'*40+':infra/release/source-manifest.json'],ROOT),
                             (['git','--no-replace-objects','rev-parse','HEAD'],Path('/tmp'))):
                with self.assertRaises(self.module.SmokeError):tracked(argv,env={},cwd=cwd,timeout=30)
        self.assertEqual(len(commands),2)
        self.assertTrue(all(argv[:3]==['git','-c','safe.directory='+str(ROOT)] for argv,_ in commands))
        self.assertTrue(all('GH_TOKEN' not in kwargs['env'] for _,kwargs in commands))

    def test_real_git_different_owner_reads_with_only_fixed_root_exception(self):
        with tempfile.TemporaryDirectory(prefix='ci-smoke-git-home-') as directory:
            clean=self.module.build_pipeline.clean_environment({'PATH':os.environ['PATH']},Path(directory))
            commit=subprocess.check_output(['git','--no-replace-objects','rev-parse','HEAD'],cwd=ROOT,env=clean,stderr=subprocess.PIPE).strip().decode()
            clean['GIT_TEST_ASSUME_DIFFERENT_OWNER']='1'
            refused=subprocess.run(['git','--no-replace-objects','rev-parse','HEAD'],cwd=ROOT,env=clean,capture_output=True)
            if refused.returncode==0:self.skipTest('Git lacks different-owner test simulation')
            self.assertIn(b'dubious ownership',refused.stderr)
            self.controller['commit']=commit
            raw,manifest=self.module.build_pipeline.tracked_manifest(self.controller,clean,
                self.module.tracked_git_command(self.controller))
            self.assertEqual(raw,(ROOT/'infra/release/source-manifest.json').read_bytes())
            self.assertEqual(records.strict_json(raw.decode()),manifest)
            self.assertEqual(clean['GIT_CONFIG_GLOBAL'],'/dev/null')
            self.assertNotIn('SUDO_UID',clean);self.assertNotIn('GH_TOKEN',clean)


class EntrypointTests(unittest.TestCase):
    def setUp(self):
        import ci_smoke
        self.module = ci_smoke
        spec = importlib.util.spec_from_file_location('ci_migration_fixture', ROOT / 'scripts/test/migration-transport.test.py')
        migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
        self.fixture = migration.Tests(); self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.fixture.create()
        self.manifest = self.fixture.manifest; self.controller = self.fixture.controller
        self.raw = self.fixture.manifest_bytes
        self.public = json.loads(json.dumps(fixture.inputs()[1]).replace('.example.invalid', '.example.com'))
        self.work = self.fixture.work / 'ci'; self.work.mkdir(mode=0o700)
        (self.fixture.fixture.roots['xynes-infra']/'infra/release').mkdir(parents=True,exist_ok=True)
        catalog = records.read_json(ROOT / 'infra/release/source-check-catalog.json')
        checks = {'checks_verified':True,'approved':False,'deployable':False,'sources':[
            {'repository':source['repository'],'commit':source['commit'],'checks':[
                {'name':name,'check_id':1,'job_id':2,'run_id':3,'run_attempt':1,'workflow_id':4,
                 'workflow_path':path,'workflow_blob':'b'*40} for name,path in catalog[key].items()]}
            for key,source in self.manifest['sources'].items()]}
        self.files = {'admission':{'controller.json':self.controller,'source-manifest.json':self.raw,
            'public-inputs.json':self.public,'source-checks.json':checks},'build':{},
            'migration':{'migration-bundle.tar':self.fixture.bundle.read_bytes()}}
        images=[]
        for task in image_build.build_tasks(self.manifest,self.public,self.controller,'release'):
            path = self.fixture.work / (task['id'] + '.oci.tar'); fixture.archive(path,task['labels'])
            images.append(image_build.inspect_oci(path,task)); self.files['build']['archives/'+path.name]=path.read_bytes()
        self.files['build']['image-receipts.json']={'schema_version':1,'artifact_kind':'local-oci-build-receipts',
            'controller':self.controller,'source_manifest_sha256':records.canonical_sha256(self.manifest),
            'images':images,'external_images':self.manifest['external_images'],'published':False,'deployable':False}
        self.env={'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':'Xynes-Studio/xynes-infra',
            'GITHUB_SHA':self.controller['commit'],'GITHUB_REF':'refs/heads/main','GITHUB_REF_PROTECTED':'true',
            'GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_RUN_ID':str(self.controller['run_id']),
            'GITHUB_RUN_ATTEMPT':'1','GITHUB_JOB':'product-smoke','GITHUB_ACTOR':'archan96',
            'GITHUB_API_URL':'https://api.github.com','GITHUB_SERVER_URL':'https://github.com',
            'GITHUB_WORKFLOW_REF':'Xynes-Studio/xynes-infra/.github/workflows/apex-build.yml@refs/heads/main',
            'APEX_REGISTRY_PREFIX':'ghcr.io/xynes-studio/apex','GH_TOKEN':'ghs_INERT_DO_NOT_EXPOSE',
            'PATH':os.environ['PATH'],'RUNNER_TEMP':str(self.fixture.work)}
        self.blobs={}; self.metadata={}; self.references={}; self.refresh()
        self.calls=[]; self.stack_calls=[]
        self.root_patch=patch.object(self.module.os,'geteuid',return_value=0)
        self.root_patch.start(); self.addCleanup(self.root_patch.stop)
        self.migration_owner=patch.object(self.module.migration_transport,'EXTRACT_UID',os.getuid())
        self.migration_owner.start(); self.addCleanup(self.migration_owner.stop)

    def refresh(self):
        for identifier,(kind,files) in enumerate(self.files.items(),90):
            stream=io.BytesIO()
            with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_STORED) as archive:
                for name,value in files.items(): archive.writestr(name,value if type(value) is bytes else json.dumps(value).encode())
            blob=stream.getvalue(); reference={'id':identifier,'digest':'sha256:'+hashlib.sha256(blob).hexdigest()}
            self.blobs[identifier]=blob; self.references[kind]=reference
            url='https://api.github.com/repos/Xynes-Studio/xynes-infra/actions/artifacts/'+str(identifier)
            self.metadata[url.removeprefix('https://api.github.com')]={'id':identifier,'digest':reference['digest'],
                'name':f'apex-{kind}-{self.controller["run_id"]}-1','expired':False,'size_in_bytes':len(blob),
                'url':url,'archive_download_url':url+'/zip','workflow_run':{'id':self.controller['run_id'],
                'repository_id':1415400917,'head_repository_id':1415400917,'head_branch':'main',
                'head_sha':self.controller['commit']}}
            self.env['APEX_'+kind.upper()+'_RECEIPT']=json.dumps(reference)

    def read(self,path):
        self.calls.append(path); return copy.deepcopy(self.metadata[path])

    def download(self,identifier,path,max_bytes):
        self.assertEqual(len(self.blobs[identifier]),max_bytes); path.write_bytes(self.blobs[identifier])

    def command(self,argv,*,env,cwd,timeout):
        self.assertNotIn('GH_TOKEN',env); self.assertNotIn('DOCKER_HOST',env)
        self.assertNotIn('fetch',argv)
        if argv[-2:]==['rev-parse','HEAD']:return (self.controller['commit']+'\n').encode()
        self.assertEqual(argv[-2],'show'); return self.raw

    def stack(self,**kwargs):
        self.stack_calls.append(kwargs); self.assertNotIn('GH_TOKEN',os.environ)
        self.assertEqual(kwargs['controller'],self.controller); self.assertEqual(kwargs['manifest_bytes'],self.raw)
        self.assertEqual(len(kwargs['selected']),6)
        return {'stage':'qa','result':'passed','storage_verified':False,
            'storage_skip_reason':'policy:qa-storage-provider-deferred','steps':[], 'observed_at':1800000000}

    def invoke(self):
        with patch.object(self.module.build_pipeline,'ROOT',self.fixture.fixture.roots['xynes-infra']):
            return self.module.run(self.env,self.work,read=self.read,download=self.download,
                command=self.command,stack_run=self.stack)

    def test_current_run_three_real_artifacts_delegate_only_after_complete_verification(self):
        # Tracked manifest reader uses its real on-disk equality check.
        (self.fixture.fixture.roots['xynes-infra']/'infra/release/source-manifest.json').write_bytes(self.raw)
        with patch.dict(os.environ,{'GH_TOKEN':'CANARY-AMBIENT-TOKEN','DOCKER_HOST':'CANARY-DOCKER-ENDPOINT'}):
            result=self.invoke()
            self.assertEqual(os.environ['GH_TOKEN'],'CANARY-AMBIENT-TOKEN')
            self.assertEqual(os.environ['DOCKER_HOST'],'CANARY-DOCKER-ENDPOINT')
        self.assertEqual(result['result'],'passed'); self.assertFalse(result['deployable'])
        self.assertEqual(result['artifacts'],self.references); self.assertEqual(len(result['images']),6)
        self.assertEqual(len(self.stack_calls),1); self.assertEqual(len(self.calls),6)

    def test_native_stack_prepares_oci_engine_after_qualification_before_services(self):
        import ci_smoke_stack
        order=[]
        def prepare(env,work):
            self.assertEqual(env['GITHUB_JOB'],'product-smoke')
            self.assertTrue(work.is_dir());self.assertEqual(work.stat().st_mode&0o777,0o700)
            self.assertEqual(len(self.calls),6)
            order.append('engine');return {'containerd_store':True,'changed':True}
        def stack(**kwargs):order.append('stack');return self.stack(**kwargs)
        (self.fixture.fixture.roots['xynes-infra']/'infra/release/source-manifest.json').write_bytes(self.raw)
        with patch.dict(sys.modules,{'ci_smoke_engine':SimpleNamespace(prepare=prepare)}), \
             patch.object(ci_smoke_stack,'run',side_effect=stack), \
             patch.object(self.module.build_pipeline,'ROOT',self.fixture.fixture.roots['xynes-infra']):
            result=self.module.run(self.env,self.work,read=self.read,download=self.download,command=self.command)
        self.assertEqual(order,['engine','stack'])
        self.assertEqual(result['runner_engine'],{'containerd_store':True,'changed':True})

    def test_engine_preparation_failure_never_starts_services_or_echoes_errors(self):
        import ci_smoke_stack
        def prepare(env,work):raise ValueError('CANARY-ENGINE-PRIVATE')
        (self.fixture.fixture.roots['xynes-infra']/'infra/release/source-manifest.json').write_bytes(self.raw)
        with patch.dict(sys.modules,{'ci_smoke_engine':SimpleNamespace(prepare=prepare)}), \
             patch.object(ci_smoke_stack,'run') as stack, \
             patch.object(self.module.build_pipeline,'ROOT',self.fixture.fixture.roots['xynes-infra']):
            with self.assertRaises(self.module.SmokeError) as caught:
                self.module.run(self.env,self.work,read=self.read,download=self.download,command=self.command)
        stack.assert_not_called()
        self.assertEqual(caught.exception.safe_evidence['phase'],'engine-preparation')
        self.assertNotIn('CANARY',str(caught.exception)+json.dumps(caught.exception.safe_evidence))

    def test_wrong_job_or_uid_refuses_before_artifact_reads(self):
        for job in ('publish-images','build-images','product-smoke-evil'):
            self.env['GITHUB_JOB']=job
            with self.assertRaises(self.module.SmokeError):self.invoke()
        self.env['GITHUB_JOB']='product-smoke'
        with patch.object(self.module.os,'geteuid',return_value=501),self.assertRaises(self.module.SmokeError):self.invoke()
        self.assertEqual(self.calls,[])

    def test_artifact_native_run_mismatch_refuses_without_stack(self):
        next(iter(self.metadata.values()))['workflow_run']['head_sha']='b'*40
        with self.assertRaises(self.module.SmokeError):self.invoke()
        self.assertEqual(self.stack_calls,[])

    def test_tracked_manifest_mismatch_refuses_before_build_or_stack(self):
        (self.fixture.fixture.roots['xynes-infra']/'infra/release/source-manifest.json').write_bytes(self.raw+b' ')
        with self.assertRaises(self.module.SmokeError):self.invoke()
        self.assertEqual(len(self.calls),2); self.assertEqual(self.stack_calls,[])

    def test_wrong_migration_controller_refuses_before_stack(self):
        (self.fixture.fixture.roots['xynes-infra']/'infra/release/source-manifest.json').write_bytes(self.raw)
        alternate=self.fixture.work/'wrong-controller.tar'
        self.fixture.create(controller={**self.controller,'run_id':self.controller['run_id']+1},output=alternate)
        self.files['migration']['migration-bundle.tar']=alternate.read_bytes()
        self.refresh()
        with self.assertRaises(self.module.SmokeError) as caught:self.invoke()
        self.assertEqual(self.stack_calls,[]); self.assertNotIn('PRIVATE',str(caught.exception))

    def test_stack_failure_persists_only_allowlisted_phase_diagnostics(self):
        (self.fixture.fixture.roots['xynes-infra']/'infra/release/source-manifest.json').write_bytes(self.raw)
        def fail(**kwargs):
            error=ValueError('CANARY-PRIVATE-ERROR'); error.safe_evidence={'phase':'flow',
                'stack_phase':'flow',
                'command':{'kind':'docker-load','exit_code':1,'error_class':'command-failed'},
                'failed_flow_phase':'cms-entry-publish','steps':[{'flow':'cms-entry-publish','status':503,
                'observed_at':1800000000,'body':'CANARY-PRIVATE-BODY'}],'password':'CANARY-PRIVATE-SECRET'}
            raise error
        self.stack=fail
        with self.assertRaises(self.module.SmokeError) as caught:self.invoke()
        result=caught.exception.safe_evidence
        self.assertEqual(result['result'],'refused'); self.assertEqual(result['phase'],'flow')
        self.assertEqual(result['failed_flow_phase'],'cms-entry-publish')
        self.assertEqual(result['stack_phase'],'flow')
        self.assertEqual(result['command'],{'kind':'docker-load','exit_code':1,'error_class':'command-failed'})
        self.assertNotIn('CANARY',json.dumps(result)); self.assertNotIn('CANARY',str(caught.exception))
        path=self.module.persist_result(result,self.env)
        self.assertEqual(records.read_json(path),result)

    def test_native_failure_writes_controlled_result_and_generic_stderr(self):
        result={'schema_version':1,'artifact_kind':'ci-product-smoke','result':'refused',
                'published':False,'deployable':False,'storage_verified':False,'phase':'build-artifact'}
        def fail(env,work,**kwargs):
            self.assertEqual(work.parent,Path('/tmp').resolve())
            self.assertEqual(work.stat().st_mode&0o777,0o700)
            error=self.module.SmokeError('CANARY-DO-NOT-PRINT');error.safe_evidence=result;raise error
        stdout,stderr=io.StringIO(),io.StringIO()
        with patch.dict(os.environ,self.env,clear=True),patch.object(self.module.sys,'argv',['ci_smoke.py']),\
                patch.object(self.module,'run',side_effect=fail),contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
            self.assertEqual(self.module.main(),1)
        self.assertEqual(stdout.getvalue(),'');self.assertEqual(stderr.getvalue(),'CI product smoke refused\n')
        self.assertEqual(records.read_json(self.fixture.work/'apex-ci-smoke/result.json'),result)

    def test_output_handoff_changes_only_owned_result_and_directory(self):
        result={'artifact_kind':'ci-product-smoke','result':'refused','published':False,
                'deployable':False,'storage_verified':False}
        owner=os.getuid()+1;self.env['SUDO_UID']=str(owner)
        transferred=[]
        def transfer(descriptor,uid,gid):
            transferred.append((os.fstat(descriptor).st_ino,uid,gid))
        with patch.object(self.module.os,'fchown',side_effect=transfer),patch.object(self.module.os,'chown') as chown:
            path=self.module.persist_result(result,self.env)
        chown.assert_not_called()
        self.assertEqual(transferred,[(path.stat().st_ino,owner,-1),(path.parent.stat().st_ino,owner,-1)])
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertEqual(path.parent.stat().st_mode&0o777,0o700)


if __name__ == '__main__':
    unittest.main()
