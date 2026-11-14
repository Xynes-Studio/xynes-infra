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
    # Legacy fixtures build fresh OCI bytes; reuse has its own explicit cases.
    manifest['processors']['libreoffice'].pop('reuse',None)
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
    def run_fixture(self,fail_at=None,tamper=False,failure_program=None,failure_timeout=None,reuse=False):
        real_run=subprocess.run
        output=self.base/'output';config=self.base/'docker-config';config.mkdir()
        recipe_bytes=b'FROM fixture:1 AS prod\n'
        for section in ['recipes','processors']:
            for recipe in self.manifest[section].values():recipe['sha256']=sha(recipe_bytes)
        tasks={task['id']:task for task in self.tasks()};calls=[];preparations=[];reuse_calls=[]
        if reuse:
            prior=self.base/'approved-prior.oci.tar';old_task=tasks['shared--libreoffice'];_,digest=archive(prior,old_task['labels'])
            self.manifest['processors']['libreoffice']['reuse']={'image':'ghcr.io/xynes-studio/apex/libreoffice@'+digest,
                'content_sha256':tool().records.processor_content_identity(self.manifest,'libreoffice')}
            tasks={task['id']:task for task in self.tasks()}
        def import_oci(artifact,task,*,credential=None):
            reuse_calls.append(task['id']);self.assertIsNone(credential);artifact.write_bytes(prior.read_bytes())

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
            if fail_at==len(calls):
                if failure_program is not None:
                    options=dict(kwargs)
                    if failure_timeout is not None:options['timeout']=failure_timeout
                    return real_run([sys.executable,'-c',failure_program],**options)
                raise subprocess.CalledProcessError(1,command)
            archive(artifact,task['labels'],architecture='arm64' if tamper else 'amd64')
            return subprocess.CompletedProcess(command,0)
        captured=io.StringIO();stdout=io.StringIO()
        with contextlib.redirect_stderr(captured),contextlib.redirect_stdout(stdout),patch.object(tool().source_context,'prepare',prepare),patch.object(tool().subprocess,'run',builder),patch.object(tool(),'reuse_oci',import_oci),patch.dict(os.environ,{'GH_TOKEN':'DO-NOT-EXPOSE','GITHUB_TOKEN':'DO-NOT-EXPOSE','DOCKER_AUTH_CONFIG':'DO-NOT-EXPOSE'}):
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
        self.assertEqual(stdout.getvalue(),'')
        self.diagnostics=[json.loads(line) for line in captured.getvalue().splitlines()]
        self.assertNotIn('DO-NOT-EXPOSE',captured.getvalue())
        self.assertEqual((output/'logs/source-preparation.log').read_text().count('Prepared immutable contexts'),3)
        self.reuse_calls=reuse_calls
        return calls
    def test_reuse_skips_libreoffice_build_subprocess_and_records_closed_diagnostic(self):
        calls=self.run_fixture(reuse=True)
        self.assertEqual(len(calls),14);self.assertNotIn('shared--libreoffice',calls);self.assertEqual(self.reuse_calls,['shared--libreoffice'])
        self.assertEqual([v['status'] for v in self.diagnostics if v['phase']=='image-reuse'],['start','success'])
    def test_diagnostics_report_success_order_and_fixed_image_ids(self):
        calls=self.run_fixture()
        expected=[]
        for phase in ('image-tasks','image-output','context-qa','context-staging','context-prod','image-recipes'):
            expected.extend([(phase,'start',None),(phase,'success',None)])
        for task in calls:
            for phase in ('image-build','image-inspect'):expected.extend([(phase,'start',task),(phase,'success',task)])
        expected.extend([('image-receipts','start',None),('image-receipts','success',None)])
        self.assertEqual([(v['phase'],v['status'],v.get('task')) for v in self.diagnostics],expected)
        for event in self.diagnostics:
            self.assertEqual(event['artifact_kind'],'apex-build-diagnostic')
            if event['phase']=='image-build' and event['status']=='start':self.assertIs(type(event['output_free_bytes']),int)
    def test_failed_build_and_failed_inspection_have_distinct_fixed_events(self):
        self.run_fixture(fail_at=3)
        self.assertEqual([(v['phase'],v['status'],v.get('task')) for v in self.diagnostics[-2:]],
                         [('image-build','start','shared--xynes-authz-service'),('image-build','failure','shared--xynes-authz-service')])
        self.assertIs(type(self.diagnostics[-1]['output_free_bytes']),int)
    def test_failed_inspection_reports_the_correct_fixed_task(self):
        self.run_fixture(tamper=True)
        self.assertEqual([(v['phase'],v['status'],v.get('task')) for v in self.diagnostics[-2:]],
                         [('image-inspect','start','shared--xynes-gateway'),('image-inspect','failure','shared--xynes-gateway')])
    def test_unknown_diagnostic_inputs_never_echo(self):
        self.assertTrue(hasattr(tool(),'emit_event'),'fixed enum diagnostic emitter missing')
        for phase,status,task in [('DO-NOT-EXPOSE','start',None),('image-build','DO-NOT-EXPOSE',None),('image-build','start','DO-NOT-EXPOSE')]:
            captured=io.StringIO()
            with contextlib.redirect_stderr(captured),self.assertRaises(ValueError):tool().emit_event(phase,status,task)
            self.assertEqual(captured.getvalue(),'')
    def test_failed_disk_measurement_never_masks_primary_error_or_echoes_values(self):
        self.assertTrue(hasattr(tool(),'diagnostic'),'fixed enum diagnostic context missing')
        captured=io.StringIO()
        with patch.object(tool().shutil,'disk_usage',side_effect=OSError('DO-NOT-EXPOSE')),contextlib.redirect_stderr(captured):
            with self.assertRaisesRegex(RuntimeError,'primary'):
                with tool().diagnostic('image-build','shared--xynes-gateway',self.base):raise RuntimeError('primary')
        events=[json.loads(line) for line in captured.getvalue().splitlines()]
        self.assertEqual([v['status'] for v in events],['start','failure']);self.assertTrue(all('output_free_bytes' not in v for v in events))
        self.assertNotIn('DO-NOT-EXPOSE',captured.getvalue());self.assertNotIn('primary',captured.getvalue())
        for value in ('DO-NOT-EXPOSE',True,-1):
            captured=io.StringIO()
            with patch.object(tool().shutil,'disk_usage',return_value=type('Disk',(),{'free':value})()),contextlib.redirect_stderr(captured):tool().emit_event('image-build','start','shared--xynes-gateway',self.base)
            self.assertNotIn('output_free_bytes',captured.getvalue());self.assertNotIn('DO-NOT-EXPOSE',captured.getvalue())
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

    def test_real_failed_subprocess_emits_closed_facts_without_log_bytes(self):
        program="import sys; print('no space left on device DO-NOT-EXPOSE https://private.invalid/path?token=DO-NOT-EXPOSE',flush=True); print('did not complete successfully: exit code: 4',flush=True); sys.exit(17)"
        self.run_fixture(fail_at=1,failure_program=program)
        event=self.diagnostics[-1];self.assertIn('error_kind',event)
        self.assertEqual(event['error_kind'],'subprocess-exit');self.assertEqual(event['returncode'],17)
        self.assertTrue(event['log_tail_available']);self.assertIn('no-space-text',event['observed_log_signals']);self.assertEqual(event['observed_step_exit_code'],4)
        for text in ('DO-NOT-EXPOSE','private.invalid','exit code: 4'):self.assertNotIn(text,json.dumps(event))
    def test_real_coordinator_timeout_emits_typed_fact_and_keeps_failure(self):
        self.run_fixture(fail_at=1,failure_program="import time; print('DO-NOT-EXPOSE',flush=True); time.sleep(5)",failure_timeout=.05)
        event=self.diagnostics[-1];self.assertIn('error_kind',event)
        self.assertEqual(event['error_kind'],'coordinator-timeout');self.assertIsNone(event['returncode']);self.assertFalse((self.base/'output/image-receipts.json').exists())
    def test_failure_log_tail_is_bounded_and_prefix_not_scanned(self):
        self.assertTrue(hasattr(tool(),'build_failure_details'),'bounded failure detail reader missing')
        path=self.base/'owned.log'
        with path.open('xb') as log:
            log.write(b'no space left on device DO-NOT-EXPOSE\n'+b'x'*131072);log.flush()
            real_pread=os.pread
            with patch.object(tool().os,'pread',wraps=real_pread) as read:
                value=tool().build_failure_details(subprocess.CalledProcessError(1,['DO-NOT-EXPOSE']),log)
            self.assertTrue(value['log_tail_available']);self.assertEqual(value['observed_log_signals'],[])
            self.assertEqual(len(read.call_args_list),1);self.assertLessEqual(read.call_args.args[1],65536);self.assertGreater(read.call_args.args[2],0)
    def test_failure_tail_refuses_symlink_and_hardlink_without_reading(self):
        self.assertTrue(hasattr(tool(),'build_failure_details'),'bounded failure detail reader missing')
        for kind in ('symlink','hardlink'):
            path=self.base/(kind+'.log');other=self.base/(kind+'.other')
            with path.open('xb') as log:
                log.write(b'no space left on device');log.flush()
                if kind=='symlink':path.unlink();other.write_text('DO-NOT-EXPOSE');path.symlink_to(other)
                else:os.link(path,other)
                with patch.object(tool().os,'pread',side_effect=AssertionError('unsafe log read')):
                    value=tool().build_failure_details(subprocess.CalledProcessError(1,['x']),log)
                self.assertFalse(value['log_tail_available']);self.assertEqual(value['observed_log_signals'],[])
    def test_failure_detail_errors_cannot_mask_original_subprocess_failure(self):
        self.assertTrue(hasattr(tool(),'build_failure_details'),'bounded failure detail reader missing')
        with patch.object(tool(),'build_failure_details',side_effect=OSError('DO-NOT-EXPOSE')):self.run_fixture(fail_at=1)
        self.assertEqual(self.diagnostics[-1]['status'],'failure');self.assertFalse((self.base/'output/image-receipts.json').exists())
    def test_subprocess_metadata_never_serializes_exception_or_unknown_signals(self):
        self.assertTrue(hasattr(tool(),'build_failure_details'),'bounded failure detail reader missing')
        path=self.base/'owned.log'
        with path.open('xb') as log:
            log.write(b'unknown DO-NOT-EXPOSE');log.flush();error=subprocess.CalledProcessError('DO-NOT-EXPOSE',['DO-NOT-EXPOSE'],output='DO-NOT-EXPOSE')
            value=tool().build_failure_details(error,log)
        self.assertIsNone(value['returncode']);self.assertNotIn('DO-NOT-EXPOSE',json.dumps(value))

    def test_configuration_id_is_never_reported_as_manifest_digest(self):
        task,path,files,digest=self.valid();v=tool().inspect_oci(path,task)
        self.assertNotEqual(v['config_digest'],v['manifest_digest'])
        self.assertEqual(v['manifest_digest'],digest)


class ProcessorReuseTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name).resolve()
        self.manifest,self.public,self.controller=inputs()
        self.original=next(t for t in tool().build_tasks(self.manifest,self.public,self.controller,'planning') if t['workload']=='libreoffice')
        self.files,self.digest=archive(self.base/'prior.tar',self.original['labels'])
        self.reuse={'image':'ghcr.io/xynes-studio/apex/libreoffice@'+self.digest,
                    'content_sha256':tool().records.processor_content_identity(self.manifest,'libreoffice')}
        self.manifest['processors']['libreoffice']['reuse']=copy.deepcopy(self.reuse)
    def task(self):
        try:return next(t for t in tool().build_tasks(self.manifest,self.public,self.controller,'planning') if t['workload']=='libreoffice')
        except ValueError:self.fail('Valid immutable processor reuse declaration was rejected')
    def test_reuse_explicitly_binds_current_provenance_without_changing_content_labels(self):
        task=self.task();self.assertEqual(task['reuse'],self.reuse);self.assertEqual(task['labels'],self.original['labels'])
        current=tool().records.build_inputs(self.controller,self.manifest,'libreoffice',None,self.public)
        self.assertEqual(current['reuse'],self.reuse);self.assertEqual(current['controller'],self.controller)
        self.assertEqual(current['source_manifest_sha256'],tool().records.canonical_sha256(self.manifest))
        self.assertNotEqual(task['build_inputs_sha256'],self.original['build_inputs_sha256'])
    def test_old_content_pin_refuses_recipe_file_mode_base_and_tool_changes(self):
        for field in ('recipe','file','mode','base','tool'):
            manifest=copy.deepcopy(self.manifest);p=manifest['processors']['libreoffice']
            if field=='recipe':p['sha256']='b'*64
            if field=='file':p['files'][0]['sha256']='b'*64
            if field=='mode':p['files'][0]['mode']='100755'
            if field=='base':
                base='example.invalid/base:1@sha256:'+'b'*64;manifest['base_images'].append(base);p['base_images'].append(base);p['base_images'].sort()
            if field=='tool':manifest['tools']['pnpm']='99.0.0'
            with self.subTest(field=field),self.assertRaises(ValueError):tool().build_tasks(manifest,self.public,self.controller,'planning')
    def test_changed_exporter_identity_refuses_existing_pin(self):
        with patch.object(tool().records,'processor_content_identity',return_value='b'*64),self.assertRaises(ValueError):
            tool().build_tasks(self.manifest,self.public,self.controller,'planning')
    def test_wrong_namespace_mutable_reference_or_extra_declaration_refuses(self):
        for value in ('ghcr.io/other/libreoffice@'+self.digest,'ghcr.io/xynes-studio/apex/archive-scanner@'+self.digest,'ghcr.io/xynes-studio/apex/libreoffice:latest','https://ghcr.io/xynes-studio/apex/libreoffice@'+self.digest):
            manifest=copy.deepcopy(self.manifest);manifest['processors']['libreoffice']['reuse']['image']=value
            with self.subTest(value=value),self.assertRaises(ValueError):tool().source_context.validate_manifest(manifest,'planning')
        self.manifest['processors']['libreoffice']['reuse']['unexpected']=True
        with self.assertRaises(ValueError):tool().source_context.validate_manifest(self.manifest,'planning')
    def test_release_record_cannot_substitute_a_new_subject_for_reuse_pin(self):
        task=self.task();image={'image':'ghcr.io/xynes-studio/apex/libreoffice@sha256:'+'0'*64,'platform':'linux/amd64',
            'source_commit':task['source_commit'],'recipe_sha256':task['recipe_sha256'],'build_inputs_sha256':task['build_inputs_sha256']}
        with self.assertRaises(ValueError):tool().records.validate_image(image,'libreoffice',self.controller,self.manifest,'ghcr.io/xynes-studio/apex')

    def test_reuse_of_other_processors_is_refused(self):
        manifest=copy.deepcopy(self.manifest);manifest['processors']['archive-scanner']['reuse']=manifest['processors']['libreoffice'].pop('reuse')
        with self.assertRaises(ValueError):tool().source_context.validate_manifest(manifest,'planning')
    def test_reconstructed_archive_preserves_exact_subject_config_layers(self):
        self.assertTrue(callable(getattr(tool(),'reuse_oci',None)),'Read-only OCI reuse importer is missing')
        task=self.task();path=self.base/'new.tar';calls=[]
        import registry_io
        manifest=self.files['blobs/sha256/'+self.digest.split(':')[1]]
        class Transport:
            def request(inner,method,url,**kw):
                calls.append((method,url,kw));self.assertFalse(kw['follow_redirects']);self.assertEqual(method,'GET');self.assertEqual(kw['body'],b'')
                if '/token?' in url:return registry_io.Response(200,{'Content-Type':'application/json'},encoded({'token':'A'*32}))
                if not kw['headers'].get('Authorization'):
                    return registry_io.Response(401,{'WWW-Authenticate':'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:xynes-studio/apex/libreoffice:pull"'})
                return registry_io.Response(200,{'Content-Type':tool().OCI_MANIFEST,'Docker-Content-Digest':self.digest},manifest)
        def fetch_blob(desc,dest,bearer,deadline):
            self.assertEqual(bearer,'A'*32);dest.write_bytes(self.files['blobs/sha256/'+desc['digest'].split(':')[1]])
        tool().reuse_oci(path,task,transport=Transport(),blob_fetcher=fetch_blob)
        result=tool().inspect_oci(path,task);self.assertEqual(result['manifest_digest'],self.digest)
        prior=tool().inspect_oci(self.base/'prior.tar',self.original);self.assertEqual(result['config_digest'],prior['config_digest'])
        self.assertNotEqual(result['build_inputs_sha256'],prior['build_inputs_sha256'])
        self.assertTrue(any('scope=repository%3Axynes-studio%2Fapex%2Flibreoffice%3Apull' in x[1] for x in calls))
    def test_explicit_pull_credential_is_hidden_and_sent_only_to_exact_token_endpoint(self):
        self.assertTrue(callable(getattr(tool(),'GitHubPullCredential',None)),'Explicit read-only pull credential type is missing')
        credential=tool().GitHubPullCredential('fixture-actor','DO_NOT_EXPOSE_READ_TOKEN_'+'A'*32)
        self.assertNotIn(credential.token,repr(credential));task=self.task();calls=[]
        import registry_io
        manifest=self.files['blobs/sha256/'+self.digest.split(':')[1]]
        class Transport:
            def request(inner,method,url,**kw):
                calls.append((url,kw['headers']))
                if '/token?' in url:
                    import base64
                    self.assertEqual(kw['headers'],{'Authorization':'Basic '+base64.b64encode(('fixture-actor:'+credential.token).encode()).decode()})
                    return registry_io.Response(200,{'Content-Type':'application/json'},encoded({'token':'B'*32}))
                if not kw['headers'].get('Authorization'):return registry_io.Response(401,{'WWW-Authenticate':'Bearer realm="https://ghcr.io/token",service="ghcr.io"'})
                return registry_io.Response(200,{'Content-Type':tool().OCI_MANIFEST,'Docker-Content-Digest':self.digest},manifest)
        def blob(desc,dest,bearer,deadline):
            self.assertEqual(bearer,'B'*32);dest.write_bytes(self.files['blobs/sha256/'+desc['digest'].split(':')[1]])
        tool().reuse_oci(self.base/'private.tar',task,credential=credential,transport=Transport(),blob_fetcher=blob)
        self.assertEqual(sum(h.get('Authorization','').startswith('Basic ') for _,h in calls),1)

    def test_corrupt_reused_blob_or_mismatched_subject_refuses(self):
        self.assertTrue(callable(getattr(tool(),'reuse_oci',None)),'Read-only OCI reuse importer is missing')
        task=self.task()
        import registry_io
        manifest=self.files['blobs/sha256/'+self.digest.split(':')[1]]
        class Transport:
            def request(inner,method,url,**kw):
                if '/token?' in url:return registry_io.Response(200,{'Content-Type':'application/json'},encoded({'token':'A'*32}))
                if not kw['headers'].get('Authorization'):return registry_io.Response(401,{'WWW-Authenticate':'Bearer realm="https://ghcr.io/token",service="ghcr.io"'})
                return registry_io.Response(200,{'Content-Type':tool().OCI_MANIFEST,'Docker-Content-Digest':self.digest},manifest)
        def corrupt(desc,dest,bearer,deadline):dest.write_bytes(b'corrupt')
        with self.assertRaises(ValueError):tool().reuse_oci(self.base/'corrupt.tar',task,transport=Transport(),blob_fetcher=corrupt)
        task['reuse']['image']='ghcr.io/xynes-studio/apex/libreoffice@sha256:'+'0'*64
        with self.assertRaises(ValueError):tool().reuse_oci(self.base/'wrong.tar',task,transport=Transport(),blob_fetcher=corrupt)


class ReuseBlobTransportTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name).resolve()/'blob'
        self.body=b'approved opaque OCI blob';self.desc={'digest':'sha256:'+sha(self.body),'size':len(self.body)};self.calls=[]
    def fetch(self,responses):
        import time
        queue=list(responses);outer=self
        class Response:
            def __init__(inner,row):inner.status,inner.metadata,body=row;inner.body=io.BytesIO(body)
            def getheaders(inner):return list(inner.metadata.items()) if isinstance(inner.metadata,dict) else inner.metadata
            def read(inner,n):return inner.body.read(n)
        class Connection:
            sock=None
            def __init__(inner,host,**kw):
                self.assertTrue(kw['context'].check_hostname);self.assertNotEqual(kw['context'].verify_mode,0)
                inner.host=host;inner.response=Response(queue.pop(0))
            def connect(inner):pass
            def request(inner,method,target,headers):outer.calls.append((inner.host,method,target,dict(headers)))
            def getresponse(inner):return inner.response
            def close(inner):pass
        with patch.object(tool().http.client,'HTTPSConnection',Connection):
            tool().fetch_reuse_blob(self.desc,self.path,'A'*32,time.monotonic()+60)
    def test_sole_cdn_hop_strips_authorization_and_retains_exact_bytes(self):
        self.fetch([(307,{'Location':'https://pkg-containers.githubusercontent.com/ghcr1/blobs/sha256:fixture?sig=private'},b''),(200,{'Content-Length':str(len(self.body))},self.body)])
        self.assertEqual(self.path.read_bytes(),self.body);self.assertEqual(self.calls[0][3],{'Authorization':'Bearer '+'A'*32});self.assertEqual(self.calls[1][3],{})
        self.assertEqual([x[0] for x in self.calls],['ghcr.io','pkg-containers.githubusercontent.com'])
    def test_unsafe_redirects_never_receive_a_token_or_create_blob(self):
        targets=['http://pkg-containers.githubusercontent.com/x','https://evil.invalid/x','https://user@pkg-containers.githubusercontent.com/x','https://pkg-containers.githubusercontent.com:443/x','https://pkg-containers.githubusercontent.com//x','https://pkg-containers.githubusercontent.com/x?sig=%0dprivate','https://pkg-containers.githubusercontent.com/x#private']
        for target in targets:
            self.calls=[]
            with self.subTest(target=target),self.assertRaises(ValueError) as caught:self.fetch([(307,{'Location':target},b'')])
            self.assertEqual(len(self.calls),1);self.assertFalse(self.path.exists());self.assertNotIn(target,str(caught.exception))
    def test_second_redirect_or_unapproved_status_refuses(self):
        with self.assertRaises(ValueError):self.fetch([(307,{'Location':'https://pkg-containers.githubusercontent.com/x'},b''),(307,{'Location':'https://pkg-containers.githubusercontent.com/y'},b'')])
        self.assertFalse(self.path.exists())
        with self.assertRaises(ValueError):self.fetch([(302,{'Location':'https://pkg-containers.githubusercontent.com/x'},b'')])
    def test_corruption_oversize_and_truncation_refuse(self):
        for body in (b'wrong',self.body+b'extra',self.body[:-1]):
            if self.path.exists():self.path.unlink()
            with self.subTest(length=len(body)),self.assertRaises(ValueError):self.fetch([(200,{},body)])
    def test_digest_header_mismatch_and_duplicate_header_refuse(self):
        for headers in ({'Docker-Content-Digest':'sha256:'+'0'*64},[('Content-Length',str(len(self.body))),('Content-Length',str(len(self.body)))]):
            with self.subTest(headers=headers),self.assertRaises(ValueError):self.fetch([(200,headers,self.body)])
            self.assertFalse(self.path.exists())
    def test_ambiguous_transfer_framing_refuses(self):
        with self.assertRaises(ValueError):self.fetch([(200,{'Transfer-Encoding':'chunked','Content-Length':str(len(self.body))},self.body)])
    def test_existing_destination_is_never_overwritten(self):
        self.path.write_bytes(b'owned earlier attempt')
        with self.assertRaises(ValueError):self.fetch([(200,{},self.body)])
        self.assertEqual(self.path.read_bytes(),b'owned earlier attempt')


class ReuseDeadlineTest(unittest.TestCase):
    def test_slow_headers_body_and_chunk_framing_respect_shared_absolute_deadline(self):
        for stage in ('headers','body','chunk-framing'):
            with self.subTest(stage=stage):
                now=[0.0];body=b'DATA'
                header=(b'HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\n' if stage!='chunk-framing' else b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n')
                wire=header+(body if stage!='chunk-framing' else b'4;extension=slow\r\nDATA\r\n0\r\n\r\n');start=0 if stage=='headers' else len(header)
                class Drip(io.BytesIO):
                    def read1(inner,n):
                        if inner.tell()>=start:now[0]+=0.25
                        return inner.read(min(n,1))
                class Socket:
                    def settimeout(inner,value):pass
                    def makefile(inner,*a,**kw):return Drip(wire)
                class Connection:
                    def __init__(inner,*a,**kw):inner.sock=Socket()
                    def connect(inner):pass
                    def request(inner,*a,**kw):pass
                    def getresponse(inner):
                        response=inner.response_class(inner.sock,method='GET');response.begin();return response
                    def close(inner):pass
                with patch.object(tool().http.client,'HTTPSConnection',Connection),patch.object(tool().time,'monotonic',lambda:now[0]),self.assertRaises(ValueError):
                    tool().reuse_get('https://ghcr.io/token?fixture',{},1.0)
                self.assertLessEqual(now[0],1.0)


if __name__=='__main__':unittest.main()
