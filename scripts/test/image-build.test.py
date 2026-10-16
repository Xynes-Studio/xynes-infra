#!/usr/bin/env python3
"""Real OCI tar bytes exercise build-task and image-content refusal boundaries."""
import copy
import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import subprocess

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/release'))
SCRIPT=ROOT/'scripts/release/image_build.py'
MODULE=None

def tool():
    global MODULE
    if MODULE is None:
        assert SCRIPT.exists(),'Central OCI image coordinator is missing'
        spec=importlib.util.spec_from_file_location('image_build',SCRIPT)
        MODULE=importlib.util.module_from_spec(spec);spec.loader.exec_module(MODULE)
    return MODULE


def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()
def sha(data):return hashlib.sha256(data).hexdigest()
def descriptor(data,kind):return {'mediaType':kind,'digest':'sha256:'+sha(data),'size':len(data)}


def inputs():
    manifest=json.loads((ROOT/'infra/release/source-manifest.json').read_text())
    public={}
    for stage in ['qa','staging','prod']:
        common={'NEXT_PUBLIC_AUTH_APP_URL':f'https://auth.{stage}.example.invalid','NEXT_PUBLIC_API_URL':f'https://api.{stage}.example.invalid','NEXT_PUBLIC_SUPABASE_URL':f'https://supabase.{stage}.example.invalid','NEXT_PUBLIC_SUPABASE_ANON_KEY':'sb_publishable_'+'a'*24,'NEXT_PUBLIC_ALLOWED_REDIRECT_DOMAINS':f'auth.{stage}.example.invalid,cms.{stage}.example.invalid'}
        public[stage]={'xynes-auth-app':{**common,'NEXT_PUBLIC_APP_URL':common['NEXT_PUBLIC_AUTH_APP_URL'],'NEXT_PUBLIC_CONSOLE_URL':f'https://cms.{stage}.example.invalid','NEXT_PUBLIC_CMS_CONSOLE_URL':f'https://cms.{stage}.example.invalid'},'xynes-cms-console-web':{**common,'NEXT_PUBLIC_APP_URL':f'https://cms.{stage}.example.invalid'}}
    controller={'repository':'Xynes-Studio/xynes-infra','commit':'1'*40,'workflow':'apex-build.yml','run_id':123,'run_attempt':1}
    return manifest,public,controller


def archive(path, labels, architecture='amd64', mutations=None, index_wrapper=False):
    raw=io.BytesIO()
    with tarfile.open(fileobj=raw,mode='w'):pass
    layer=gzip.compress(raw.getvalue(),mtime=0)
    config={'architecture':architecture,'os':'linux','config':{'User':'1001','Labels':copy.deepcopy(labels)},'rootfs':{'type':'layers','diff_ids':['sha256:'+sha(raw.getvalue())]}}
    if mutations:mutations(config)
    config_bytes=encoded(config)
    manifest={'schemaVersion':2,'mediaType':'application/vnd.oci.image.manifest.v1+json','config':descriptor(config_bytes,'application/vnd.oci.image.config.v1+json'),'layers':[descriptor(layer,'application/vnd.oci.image.layer.v1.tar+gzip')]}
    manifest_bytes=encoded(manifest)
    entry=descriptor(manifest_bytes,'application/vnd.oci.image.manifest.v1+json')
    blobs={sha(layer):layer,sha(config_bytes):config_bytes,sha(manifest_bytes):manifest_bytes}
    if index_wrapper:
        inner=encoded({'schemaVersion':2,'mediaType':'application/vnd.oci.image.index.v1+json','manifests':[entry]})
        blobs[sha(inner)]=inner;entry=descriptor(inner,'application/vnd.oci.image.index.v1+json')
    files={'oci-layout':encoded({'imageLayoutVersion':'1.0.0'}),'index.json':encoded({'schemaVersion':2,'manifests':[entry]}),**{'blobs/sha256/'+name:data for name,data in blobs.items()}}
    write_tar(path,files)
    return files,'sha256:'+sha(manifest_bytes)


def write_tar(path,files,extra=None):
    with tarfile.open(path,'w') as out:
        for name,data in files.items():
            member=tarfile.TarInfo(name);member.size=len(data);out.addfile(member,io.BytesIO(data))
        if extra:out.addfile(extra)


class ImageBuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='apex-image-build-test-');self.base=Path(self.tmp.name).resolve()
        self.manifest,self.public,self.controller=inputs()
    def tearDown(self):self.tmp.cleanup()
    def tasks(self):return tool().build_tasks(self.manifest,self.public,self.controller,'planning')
    def test_fifteen_tasks_build_shared_once_and_six_public_variants(self):
        tasks=self.tasks();self.assertEqual(len(tasks),15)
        shared=[t for t in tasks if t['environment'] is None]
        self.assertEqual({t['workload'] for t in shared},set(tool().SHARED_BUILD))
        self.assertNotIn('clamav-freshclam',[t['workload'] for t in tasks])
        for stage in ['qa','staging','prod']:
            self.assertEqual({t['workload'] for t in tasks if t['environment']==stage},{'xynes-auth-app','xynes-cms-console-web'})
    def test_processor_oci_labels_ignore_new_controller_and_unrelated_source(self):
        previous={t['workload']:t for t in self.tasks() if t['workload'] in ('archive-scanner','libreoffice')}
        manifest=copy.deepcopy(self.manifest);manifest['sources']['xynes-auth-app']['commit']='9'*40
        manifest['sources']['xynes-storage-service']['commit']='8'*40
        controller={**self.controller,'commit':'7'*40,'run_id':124}
        current=tool().build_tasks(manifest,self.public,controller,'planning')
        for task in current:
            if task['workload'] in previous:
                self.assertEqual(task['labels'],previous[task['workload']]['labels'])
                self.assertEqual(task['build_args'],previous[task['workload']]['build_args'])
                self.assertNotEqual(task['build_inputs_sha256'],previous[task['workload']]['build_inputs_sha256'])
    def test_processor_content_changes_invalidate_labels_and_epoch(self):
        original={t['workload']:t for t in self.tasks()}
        for change in ('bytes','mode','recipe','base'):
            manifest=copy.deepcopy(self.manifest);processor=manifest['processors']['archive-scanner']
            if change=='bytes':processor['files'][0]['sha256']='6'*64
            if change=='mode':processor['files'][0]['mode']='100755'
            if change=='recipe':processor['sha256']='6'*64
            if change=='base':
                base='example.invalid/base:1@sha256:'+'6'*64
                manifest['base_images'].append(base);processor['base_images'].append(base);processor['base_images'].sort()
            task=next(t for t in tool().build_tasks(manifest,self.public,self.controller,'planning') if t['workload']=='archive-scanner')
            self.assertNotEqual(task['labels'],original['archive-scanner']['labels'],change)
            self.assertNotEqual(task['build_args'],original['archive-scanner']['build_args'],change)
    def test_processor_exporter_rewrites_timestamps_and_does_not_reuse_host_epoch(self):
        for task in self.tasks():
            if task['workload'] in ('archive-scanner','libreoffice'):
                command=tool().build_argv(task,self.base,self.base/'out.tar','fixture-builder',self.base/'docker-config')
                exporter=command[command.index('--output')+1]
                self.assertIn('rewrite-timestamp=true',exporter)
                self.assertIn('compatibility-version=30',exporter)
                epoch=int(task['build_args']['SOURCE_DATE_EPOCH'])
                self.assertGreaterEqual(epoch,946684800);self.assertLess(epoch,1262044800)
    def test_fresh_processor_oci_keeps_digest_across_distinct_release_provenance(self):
        first=next(t for t in self.tasks() if t['workload']=='archive-scanner')
        manifest=copy.deepcopy(self.manifest);manifest['sources']['xynes-storage-service']['commit']='8'*40
        second=next(t for t in tool().build_tasks(manifest,self.public,{**self.controller,'run_id':124},'planning') if t['workload']=='archive-scanner')
        one=self.base/'one.tar';two=self.base/'two.tar';archive(one,first['labels']);archive(two,second['labels'])
        old=tool().inspect_oci(one,first);new=tool().inspect_oci(two,second)
        self.assertEqual(old['config_digest'],new['config_digest']);self.assertEqual(old['manifest_digest'],new['manifest_digest'])
        self.assertNotEqual(old['source_commit'],new['source_commit']);self.assertNotEqual(old['build_inputs_sha256'],new['build_inputs_sha256'])
    def test_processor_archive_refuses_baked_release_metadata(self):
        task=next(t for t in self.tasks() if t['workload']=='archive-scanner')
        path=self.base/'processor.tar'
        archive(path,{**task['labels'],'com.xynes.apex.run-id':'123'})
        with self.assertRaises(ValueError):tool().inspect_oci(path,task)
    def test_namespace_exception_requires_exact_internal_recipe_and_target(self):
        import registry_namespace
        task=registry_namespace._task(self.controller,'archive-scanner')
        self.assertTrue(tool().namespace_task(task))
        for field,value in [('id','shared--archive-scanner'),('target','dev'),('platform','linux/arm64'),('environment','prod'),('recipe_sha256','7'*64)]:
            changed=copy.deepcopy(task);changed[field]=value
            self.assertFalse(tool().namespace_task(changed),field)
    def test_release_builds_reject_shared_public_api_and_auth_origins(self):
        manifest=copy.deepcopy(self.manifest);manifest['status']='approved';manifest['approval_reference']='https://github.com/Xynes-Studio/xynes-infra/issues/1'
        self.assertEqual(len(tool().build_tasks(manifest,self.public,self.controller,'release')),15)
        for key in ['NEXT_PUBLIC_API_URL','NEXT_PUBLIC_SUPABASE_URL','NEXT_PUBLIC_AUTH_APP_URL']:
            public=copy.deepcopy(self.public)
            for values in public['qa'].values():values[key]=public['prod']['xynes-auth-app'][key]
            if key=='NEXT_PUBLIC_AUTH_APP_URL':public['qa']['xynes-auth-app']['NEXT_PUBLIC_APP_URL']=public['qa']['xynes-auth-app'][key]
            with self.assertRaises(ValueError):tool().build_tasks(manifest,public,self.controller,'release')

    def test_actual_git_export_resolves_every_task_recipe_path(self):
        spec=importlib.util.spec_from_file_location('context_fixture',ROOT/'scripts/test/source-context.test.py')
        fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
        case=fixture.ContextTest();case.setUp()
        try:
            result=case.run_context();self.assertEqual(result.returncode,0,result.stderr)
            tasks=tool().build_tasks(case.manifest,{stage:copy.deepcopy(case.inputs) for stage in ['qa','staging','prod']},self.controller,'planning')
            for task in tasks:
                if task['environment'] in [None,'qa']:
                    relative=Path(task['dockerfile']).relative_to('contexts/qa')
                    path=case.output/relative
                    self.assertTrue(path.is_file(),task['id'])
                    self.assertEqual(sha(path.read_bytes()),task['recipe_sha256'])
        finally:case.tearDown();case.doCleanups()

    def test_native_sources_and_closed_targets_match_the_manifest(self):
        for t in self.tasks():
            self.assertEqual(t['platform'],'linux/amd64');self.assertEqual(t['target'],'prod')
            self.assertEqual(t['labels'].get('org.opencontainers.image.source'),'https://github.com/Xynes-Studio/xynes-infra')
            if t['workload'] in ['archive-scanner','libreoffice']:
                self.assertEqual(t['source_commit'],self.manifest['sources']['xynes-storage-service']['commit'])
    def test_argv_never_publishes_loads_or_accepts_secrets(self):
        t=self.tasks()[0]
        command=tool().build_argv(t,self.base,self.base/'out.tar','fixture-builder',self.base/'docker-config')
        self.assertNotIn('--push',command);self.assertNotIn('--load',command);self.assertNotIn('--secret',command);self.assertNotIn('--ssh',command)
        self.assertIn('type=oci',','.join(command));self.assertIn('--provenance=false',command);self.assertIn('--sbom=false',command)
        self.assertNotIn('GH_TOKEN',str(command));self.assertIn('linux/amd64',command)
        with self.assertRaises(ValueError):tool().build_argv(t,self.base,self.base/'out.tar','--push',self.base/'docker-config')
    def test_frontend_public_hashes_and_args_do_not_cross_stages(self):
        tasks=self.tasks()
        for t in tasks:
            if t['environment']:
                expected=self.public[t['environment']][t['workload']]
                self.assertEqual({k:v for k,v in t['build_args'].items() if k.startswith('NEXT_PUBLIC_')},expected)
        auth=[t for t in tasks if t['workload']=='xynes-auth-app']
        self.assertEqual(len({t['build_inputs_sha256'] for t in auth}),3)
    def test_planning_and_invalid_inputs_cannot_become_release_authorization(self):
        planning=copy.deepcopy(self.manifest)
        planning.update(status='planning',approval_reference=None)
        with self.assertRaises(ValueError):tool().build_tasks(planning,self.public,self.controller,'release')
        for mutate in [lambda m:m['sources']['xynes-gateway'].update(commit='develop'),lambda m:m['recipes']['xynes-auth-app'].update(target='dev')]:
            m=copy.deepcopy(self.manifest);mutate(m)
            with self.assertRaises(ValueError):tool().build_tasks(m,self.public,self.controller,'planning')
        p=copy.deepcopy(self.public);p['qa']['xynes-auth-app']['TOKEN']='DO-NOT-EXPOSE'
        with self.assertRaises(ValueError):tool().build_tasks(self.manifest,p,self.controller,'planning')
    def valid(self,**kwargs):
        task=self.tasks()[0];path=self.base/'image.tar';files,digest=archive(path,task['labels'],**kwargs)
        return task,path,files,digest
    def test_valid_oci_binds_actual_manifest_platform_and_labels(self):
        task,path,files,digest=self.valid();v=tool().inspect_oci(path,task)
        self.assertEqual(v['manifest_digest'],digest);self.assertEqual(v['platform'],'linux/amd64')
        self.assertEqual(v['archive_sha256'],sha(path.read_bytes()));self.assertFalse(v['published']);self.assertFalse(v['deployable'])
        self.assertEqual(v['build_inputs_sha256'],task['build_inputs_sha256'])
    def test_valid_single_platform_nested_index(self):
        task,path,files,digest=self.valid(index_wrapper=True);self.assertEqual(tool().inspect_oci(path,task)['manifest_digest'],digest)
    def test_wrong_architecture_and_label_are_refused(self):
        for kwargs in [{'architecture':'arm64'},{'mutations':lambda c:c['config']['Labels'].update({'com.xynes.apex.build-inputs-sha256':'a'*64})}]:
            task,path,_,_=self.valid(**kwargs)
            with self.assertRaises(ValueError):tool().inspect_oci(path,task)
    def test_tampered_blob_and_missing_blob_refused(self):
        for change in ['tamper','remove']:
            task,path,files,_=self.valid();name=next(p for p in files if p.startswith('blobs/'))
            if change=='tamper':files[name]=b'tampered'
            else:files.pop(name)
            write_tar(path,files)
            with self.assertRaises(ValueError):tool().inspect_oci(path,task)
    def test_unsafe_tar_members_refused_without_extraction(self):
        for name,kind in [('../escape',tarfile.REGTYPE),('/etc/unsafe',tarfile.REGTYPE),('blobs/sha256/link',tarfile.SYMTYPE),('unsafe-device',tarfile.CHRTYPE)]:
            task,path,files,_=self.valid();extra=tarfile.TarInfo(name);extra.type=kind;extra.linkname='/etc/passwd';write_tar(path,files,extra)
            with self.assertRaises(ValueError):tool().inspect_oci(path,task)
            self.assertFalse((self.base.parent/'escape').exists())
    def test_duplicate_members_and_multi_platform_index_refused(self):
        task,path,files,_=self.valid();extra=tarfile.TarInfo('index.json');write_tar(path,files,extra)
        with self.assertRaises(ValueError):tool().inspect_oci(path,task)
        task,path,files,_=self.valid();index=json.loads(files['index.json']);index['manifests']*=2;files['index.json']=encoded(index);write_tar(path,files)
        with self.assertRaises(ValueError):tool().inspect_oci(path,task)
    def test_archive_symlinks_hardlinks_and_oversize_refused(self):
        task,path,_,_=self.valid();link=self.base/'link';link.symlink_to(path)
        with self.assertRaises(ValueError):tool().inspect_oci(link,task)
        hard=self.base/'hard';os.link(path,hard)
        with self.assertRaises(ValueError):tool().inspect_oci(hard,task)
        os.unlink(hard)
        with path.open('wb') as out:out.truncate(tool().MAX_ARCHIVE+1)
        with self.assertRaises(ValueError):tool().inspect_oci(path,task)
    def test_oci_output_option_cannot_be_injected_by_destination_path(self):
        task=self.tasks()[0]
        for name in ['bad,push=true.tar','bad\nname.tar']:
            with self.assertRaises(ValueError):tool().build_argv(task,self.base,self.base/name,'fixture-builder',self.base/'config')
    def test_layer_diff_ids_and_expansion_bound_are_verified(self):
        task,path,_,_=self.valid(mutations=lambda c:c['rootfs'].update(diff_ids=['sha256:'+'0'*64]))
        with self.assertRaises(ValueError):tool().inspect_oci(path,task)
        task,path,_,_=self.valid()
        with patch.object(tool(),'MAX_EXPANDED',1024),self.assertRaises(ValueError):tool().inspect_oci(path,task)
    def test_unreferenced_blob_and_wrong_manifest_schema_are_refused(self):
        task,path,files,_=self.valid();data=b'unreviewed';files['blobs/sha256/'+sha(data)]=data;write_tar(path,files)
        with self.assertRaises(ValueError):tool().inspect_oci(path,task)
        task,path,files,_=self.valid();files['index.json']=b'[]';write_tar(path,files)
        with self.assertRaises(ValueError):tool().inspect_oci(path,task)
    def run_fixture(self,fail_at=None,tamper=False):
        output=self.base/'output';config=self.base/'docker-config';config.mkdir()
        recipe_bytes=b'FROM fixture:1 AS prod\n'
        for section in ['recipes','processors']:
            for recipe in self.manifest[section].values():recipe['sha256']=sha(recipe_bytes)
        tasks={task['id']:task for task in self.tasks()};calls=[];preparations=[]
        def prepare(args):
            preparations.append(args.environment);args.output.mkdir()
            print('Prepared immutable contexts')
            for task in tasks.values():
                if (task['environment'] or 'qa')==args.environment:
                    path=output/task['dockerfile'];path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(recipe_bytes)
        def builder(command,**kwargs):
            self.assertEqual(set(kwargs['env']) & {'GH_TOKEN','GITHUB_TOKEN','DOCKER_AUTH_CONFIG','SSH_AUTH_SOCK'},set())
            self.assertNotIn('--push',command);self.assertNotIn('--load',command)
            artifact=Path(command[command.index('--output')+1].split('dest=',1)[1])
            task=tasks[artifact.name.removesuffix('.oci.tar')];calls.append(task['id'])
            if fail_at==len(calls):raise subprocess.CalledProcessError(1,command)
            archive(artifact,task['labels'],architecture='arm64' if tamper else 'amd64')
            return subprocess.CompletedProcess(command,0)
        captured=io.StringIO()
        with contextlib.redirect_stdout(captured),patch.object(tool().source_context,'prepare',prepare),patch.object(tool().subprocess,'run',builder),patch.dict(os.environ,{'GH_TOKEN':'DO-NOT-EXPOSE','GITHUB_TOKEN':'DO-NOT-EXPOSE','DOCKER_AUTH_CONFIG':'DO-NOT-EXPOSE'}):
            if fail_at or tamper:
                with self.assertRaises((ValueError,subprocess.SubprocessError)):
                    tool().execute(self.manifest,self.public,self.controller,'planning',self.base,output,'fixture-builder',config)
                self.assertFalse((output/'image-receipts.json').exists())
            else:
                result=tool().execute(self.manifest,self.public,self.controller,'planning',self.base,output,'fixture-builder',config)
                self.assertEqual(len(result['images']),15);self.assertEqual(preparations,['qa','staging','prod'])
                self.assertFalse(result['published']);self.assertFalse(result['deployable'])
                self.assertEqual(json.loads((output/'image-receipts.json').read_text()),result)
                self.assertEqual(len(list((output/'archives').glob('*.oci.tar'))),15)
                self.assertNotIn('DO-NOT-EXPOSE',(output/'image-receipts.json').read_text())
        self.assertEqual(captured.getvalue(),'')
        self.assertEqual((output/'logs/source-preparation.log').read_text().count('Prepared immutable contexts'),3)
        return calls
    def test_executor_builds_exactly_fifteen_archives_without_credentials(self):
        self.assertEqual(len(self.run_fixture()),15)
    def test_partial_build_does_not_publish_a_complete_receipt(self):
        self.assertEqual(len(self.run_fixture(fail_at=3)),3)
    def test_wrong_platform_build_stops_before_next_task(self):
        self.assertEqual(len(self.run_fixture(tamper=True)),1)
    def test_registry_credentials_are_refused_before_any_context_export(self):
        config=self.base/'config';config.mkdir();(config/'config.json').write_text('{"auths":{"registry.example.invalid":{"auth":"DO-NOT-EXPOSE"}}}')
        with patch.object(tool().source_context,'prepare') as prepared,self.assertRaises(ValueError):
            tool().execute(self.manifest,self.public,self.controller,'planning',self.base,self.base/'output','fixture-builder',config)
        prepared.assert_not_called();self.assertFalse((self.base/'output').exists())

    def test_configuration_id_is_never_reported_as_manifest_digest(self):
        task,path,files,digest=self.valid();v=tool().inspect_oci(path,task)
        self.assertNotEqual(v['config_digest'],v['manifest_digest'])
        self.assertEqual(v['manifest_digest'],digest)


if __name__=='__main__':unittest.main()
