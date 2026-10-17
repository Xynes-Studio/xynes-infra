#!/usr/bin/env python3
"""Isolated native artifact ZIP/OCI fixtures exercise the build job boundary."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts/release'))
import artifact_handoff,image_build,release_manifest as records
spec=importlib.util.spec_from_file_location('image_fixture',ROOT/'scripts/test/image-build.test.py');IMAGE=importlib.util.module_from_spec(spec);spec.loader.exec_module(IMAGE)

class Tests(unittest.TestCase):
 def setUp(self):
  path=ROOT/'scripts/release/build_pipeline.py';self.assertTrue(path.exists(),'build pipeline entrypoint missing')
  spec=importlib.util.spec_from_file_location('build_pipeline',path);self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name).resolve();self.base.chmod(0o700)
  self.root=self.base/'infra';(self.root/'infra/release').mkdir(parents=True);self.module.ROOT=self.root
  self.manifest,self.public,self.controller=IMAGE.inputs();self.manifest.update(status='approved',approval_reference='https://github.com/Xynes-Studio/xynes-infra/pull/1')
  self.public=json.loads(json.dumps(self.public).replace('.example.invalid','.example.com'));self.controller.update(commit='a'*40,run_id=100)
  self.manifest_bytes=json.dumps(self.manifest).encode();(self.root/'infra/release/source-manifest.json').write_bytes(self.manifest_bytes)
  catalog=json.loads((ROOT/'infra/release/source-check-catalog.json').read_text())
  self.checks={'checks_verified':True,'approved':False,'deployable':False,'sources':[{'repository':s['repository'],'commit':s['commit'],'checks':[{'name':name,'check_id':800+i,'job_id':900+i,'run_id':1000+i,'run_attempt':1,'workflow_id':1100+i,'workflow_path':path,'workflow_blob':'b'*40} for i,(name,path) in enumerate(catalog[key].items())]} for key,s in self.manifest['sources'].items()]}
  self.env={'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':'Xynes-Studio/xynes-infra','GITHUB_SHA':'a'*40,'GITHUB_REF':'refs/heads/main','GITHUB_REF_PROTECTED':'true','GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_RUN_ID':'100','GITHUB_RUN_ATTEMPT':'1','GITHUB_JOB':'admission','GITHUB_ACTOR':'archan96','GITHUB_API_URL':'https://api.github.com','GITHUB_SERVER_URL':'https://github.com','GITHUB_WORKFLOW_REF':'Xynes-Studio/xynes-infra/.github/workflows/apex-build.yml@refs/heads/main','APEX_REGISTRY_PREFIX':'ghcr.io/xynes-studio/apex','GH_TOKEN':'ghs_INERT_DO_NOT_PRINT_JOB_TOKEN','RUNNER_TEMP':str(self.base),'PATH':os.environ['PATH'],'GH_POLICY_READ_TOKEN':'private-capability','SSH_AUTH_SOCK':'/secret/agent','DOCKER_HOST':'tcp://untrusted:2375','GIT_CONFIG_COUNT':'1','HOME':'/foreign/home'}
  for stage in records.STAGES:self.env['APEX_PUBLIC_'+stage.upper()]=json.dumps(self.public[stage])
  self.commands=[];self.fail_build=False;self.fail_create=False;self.corrupt_last=False;self.migration_calls=[];self.fail_migration=False
  self.refresh()
  self.captured=io.StringIO();manager=contextlib.redirect_stderr(self.captured);manager.__enter__();self.addCleanup(manager.__exit__,None,None,None)
 def refresh(self):
  files={'controller.json':self.controller,'source-manifest.json':self.manifest,'public-inputs.json':self.public,'source-checks.json':self.checks}
  stream=io.BytesIO()
  with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_STORED) as archive:
   for name,value in files.items():archive.writestr(name,json.dumps(value))
  self.zip=stream.getvalue();self.ref={'id':98,'digest':'sha256:'+hashlib.sha256(self.zip).hexdigest()};url='https://api.github.com'+artifact_handoff.PREFIX+'98'
  self.metadata={'id':98,'digest':self.ref['digest'],'name':'apex-admission-100-1','expired':False,'size_in_bytes':len(self.zip),'url':url,'archive_download_url':url+'/zip','workflow_run':{'id':100,'repository_id':1415400917,'head_repository_id':1415400917,'head_branch':'main','head_sha':'a'*40}}
  self.env['APEX_ADMISSION_RECEIPT']=json.dumps(self.ref)
 def command(self,argv,*,env,cwd,timeout):
  self.commands.append((list(argv),dict(env),Path(cwd),timeout))
  if argv[0]=='bun':return json.dumps(self.checks).encode()
  if 'init' in argv:(Path(cwd)/'.git').mkdir()
  if 'show' in argv:return self.manifest_bytes
  if 'rev-parse' in argv:
   if Path(cwd)==self.root:return b'a'*40+b'\n'
   return self.manifest['sources'][Path(cwd).name]['commit'].encode()+b'\n'
  if 'create' in argv and self.fail_create:raise RuntimeError('private-capability')
  return b''
 def download(self,identifier,path,size):
  self.assertEqual(identifier,98);self.assertEqual(size,len(self.zip));path.write_bytes(self.zip)
 def execute(self,manifest,public,controller,purpose,workspace,output,builder,docker_config):
  self.assertEqual(purpose,'release');self.assertEqual(len(list(workspace.glob('*/*/.git'))),12)
  self.assertNotIn('GH_TOKEN',os.environ);self.assertNotIn('SSH_AUTH_SOCK',os.environ);self.assertNotIn('DOCKER_HOST',os.environ)
  if self.fail_build:raise RuntimeError('private-capability')
  output.mkdir(mode=0o700);(output/'archives').mkdir(mode=0o700);images=[]
  for task in image_build.build_tasks(manifest,public,controller,'release'):
   path=output/'archives'/(task['id']+'.oci.tar');IMAGE.archive(path,task['labels']);images.append(image_build.inspect_oci(path,task))
  result={'schema_version':1,'artifact_kind':'local-oci-build-receipts','controller':controller,'source_manifest_sha256':records.canonical_sha256(manifest),'images':images,'external_images':manifest['external_images'],'published':False,'deployable':False}
  (output/'image-receipts.json').write_text(json.dumps(result))
  if self.corrupt_last:path.write_bytes(b'corrupt')
  return result
 def migration_writer(self,*,manifest_bytes,controller,repositories,output):
  self.migration_calls.append((manifest_bytes,controller,repositories,output,dict(os.environ)))
  if self.fail_migration:raise RuntimeError('private-capability')
  output.write_bytes(b'inert migration transport bytes')
  os.chmod(output,0o600)
  return {'artifact_kind':'immutable-migration-bundle-receipt','controller':controller,
          'bundle_sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'bundle_size':output.stat().st_size,
          'source_manifest_sha256':records.canonical_sha256(self.manifest),
          'source_manifest_bytes_sha256':hashlib.sha256(manifest_bytes).hexdigest(),
          'approved':False,'deployable':False}
 def diagnostics(self):return [json.loads(line) for line in self.captured.getvalue().splitlines()]
 def test_build_phase_success_order_exposes_only_fixed_enums(self):
  self.env['GITHUB_JOB']='build-images'
  with patch.dict(os.environ,self.env,clear=True):self.invoke()
  phases=('controller','admission-intake','input-qualification','tracked-source-manifest','source-materialize','migration-export','migration-qualification','builder-create','image-batch','image-qualification','builder-cleanup')
  self.assertEqual([(v['phase'],v['status']) for v in self.diagnostics()],[(phase,status) for phase in phases for status in ('start','success')])
  for private in ('private-capability',self.env['GH_TOKEN'],self.env['SSH_AUTH_SOCK'],self.env['DOCKER_HOST']):self.assertNotIn(private,self.captured.getvalue())
 def test_primary_failure_is_preserved_when_cleanup_also_fails(self):
  self.env['GITHUB_JOB']='build-images';self.fail_build=True;original=self.command
  def command(argv,**options):
   if 'rm' in argv:raise RuntimeError('private-capability')
   return original(argv,**options)
  with self.assertRaises(self.module.PipelineError) as error:self.module.build(self.env,read=lambda _:copy.deepcopy(self.metadata),download=self.download,command=command,execute=self.execute,migration_writer=self.migration_writer)
  self.assertEqual(str(error.exception),'Protected build pipeline refused')
  self.assertEqual([(v['phase'],v['status']) for v in self.diagnostics()[-4:]],[('image-batch','start'),('image-batch','failure'),('builder-cleanup','start'),('builder-cleanup','failure')])
  self.assertNotIn('private-capability',self.captured.getvalue())
 def cleanup_command(self,elapsed):
  original=self.command
  def command(argv,**options):
   result=original(argv,**options)
   if 'rm' in argv:
    self.assertEqual(argv,['docker','--config',str(self.base/'apex-docker'),'buildx','rm','--force','apex-build-100-1'])
    self.assertEqual(options['cwd'],self.root)
    for key in ('GH_TOKEN','GH_POLICY_READ_TOKEN','SSH_AUTH_SOCK','DOCKER_HOST'):self.assertNotIn(key,options['env'])
    if elapsed>options['timeout']:raise subprocess.TimeoutExpired(argv,options['timeout'])
   return result
  return command
 def test_qualified_images_survive_owned_cleanup_taking_longer_than_sixty_seconds(self):
  self.env['GITHUB_JOB']='build-images'
  with patch.dict(os.environ,self.env,clear=True):
   result=self.module.build(self.env,read=lambda _:copy.deepcopy(self.metadata),download=self.download,command=self.cleanup_command(61),execute=self.execute,migration_writer=self.migration_writer)
  self.assertEqual(len(list((result/'archives').iterdir())),15)
  self.assertEqual([(v['phase'],v['status']) for v in self.diagnostics()[-2:]],[('builder-cleanup','start'),('builder-cleanup','success')])
 def test_cleanup_exceeding_five_minutes_still_refuses_qualified_output(self):
  self.env['GITHUB_JOB']='build-images'
  with patch.dict(os.environ,self.env,clear=True),self.assertRaises(self.module.PipelineError):
   self.module.build(self.env,read=lambda _:copy.deepcopy(self.metadata),download=self.download,command=self.cleanup_command(301),execute=self.execute,migration_writer=self.migration_writer)
  self.assertEqual([(v['phase'],v['status']) for v in self.diagnostics()[-2:]],[('builder-cleanup','start'),('builder-cleanup','failure')])
 def test_slow_successful_cleanup_cannot_mask_a_failed_build(self):
  self.env['GITHUB_JOB']='build-images';self.fail_build=True
  with patch.dict(os.environ,self.env,clear=True),self.assertRaises(self.module.PipelineError):
   self.module.build(self.env,read=lambda _:copy.deepcopy(self.metadata),download=self.download,command=self.cleanup_command(61),execute=self.execute,migration_writer=self.migration_writer)
  self.assertEqual([(v['phase'],v['status']) for v in self.diagnostics()[-4:]],[('image-batch','start'),('image-batch','failure'),('builder-cleanup','start'),('builder-cleanup','success')])
 def test_migration_failure_has_phase_without_raw_error(self):
  self.env['GITHUB_JOB']='build-images';self.fail_migration=True;self.refuse()
  self.assertEqual([(v['phase'],v['status']) for v in self.diagnostics()[-2:]],[('migration-export','start'),('migration-export','failure')])
  self.assertNotIn('private-capability',self.captured.getvalue())
 def test_unvalidated_controller_never_echoes_input_value(self):
  self.env['GITHUB_JOB']='build-images';self.env['GITHUB_REPOSITORY']='private-capability';self.refuse()
  self.assertEqual([(v['phase'],v['status']) for v in self.diagnostics()],[('controller','start'),('controller','failure')]);self.assertNotIn('private-capability',self.captured.getvalue())
 def test_build_emits_separate_source_bound_migration_artifact_without_credentials(self):
  self.env['GITHUB_JOB']='build-images'
  with patch.dict(os.environ,self.env,clear=True):self.invoke()
  self.assertEqual(len(self.migration_calls),1)
  raw,controller,repositories,path,environment=self.migration_calls[0]
  self.assertEqual(raw,self.manifest_bytes);self.assertEqual(controller,self.controller)
  self.assertEqual(set(repositories),{'xynes-infra',*self.module.source_context.BACKENDS})
  self.assertEqual(repositories['xynes-infra'],self.root)
  self.assertEqual(path,self.base/'apex-migrations/migration-bundle.tar')
  self.assertEqual({p.name for p in path.parent.iterdir()},{'migration-bundle.tar'})
  self.assertEqual(path.parent.stat().st_mode&0o777,0o700)
  for key in ('GH_TOKEN','GH_POLICY_READ_TOKEN','SSH_AUTH_SOCK','DOCKER_HOST'):self.assertNotIn(key,environment)
 def test_migration_transport_failure_prevents_builder_creation(self):
  self.env['GITHUB_JOB']='build-images';self.fail_migration=True
  self.refuse();self.assertFalse(any('create' in entry[0] for entry in self.commands))
 def invoke(self):
  if self.env['GITHUB_JOB']=='admission':return self.module.admit(self.env,command=self.command)
  return self.module.build(self.env,read=lambda _:copy.deepcopy(self.metadata),download=self.download,command=self.command,execute=self.execute,migration_writer=self.migration_writer)
 def refuse(self):
  with self.assertRaises(self.module.PipelineError) as caught:self.invoke()
  self.assertEqual(str(caught.exception),'Protected build pipeline refused')
 def test_admission_persists_only_four_private_data_files(self):
  result=self.invoke();self.assertEqual(set(p.name for p in result.iterdir()),set(artifact_handoff.ADMISSION_FILES))
  self.assertEqual((result/'source-manifest.json').read_bytes(),self.manifest_bytes)
  self.assertEqual(result.stat().st_mode&0o777,0o700)
  for path in result.iterdir():self.assertEqual(path.stat().st_mode&0o777,0o600)
  bun=[entry for entry in self.commands if entry[0][0]=='bun'];self.assertEqual(len(bun),1)
  self.assertEqual(bun[0][1]['GH_TOKEN'],self.env['GH_TOKEN']);self.assertNotIn('GH_POLICY_READ_TOKEN',bun[0][1]);self.assertNotIn('SSH_AUTH_SOCK',bun[0][1])
 def test_wrong_controller_and_planning_snapshot_fail_before_source_calls(self):
  for key,value in [('GITHUB_EVENT_NAME','pull_request'),('GITHUB_REF_PROTECTED','false'),('GITHUB_RUN_ATTEMPT','2'),('GITHUB_JOB','foreign'),('GITHUB_SHA','short'),('GH_TOKEN','private-token')]:
   original=self.env[key];self.env[key]=value;self.refuse();self.assertEqual(self.commands,[]);self.env[key]=original
  self.manifest.update(status='planning',approval_reference=None);self.manifest_bytes=json.dumps(self.manifest).encode();(self.root/'infra/release/source-manifest.json').write_bytes(self.manifest_bytes);self.refuse();self.assertFalse(any(c[0][0]=='bun' for c in self.commands))
 def test_untracked_manifest_and_unqualified_source_check_refuse(self):
  (self.root/'infra/release/source-manifest.json').write_bytes(self.manifest_bytes+b' ');self.refuse()
  (self.root/'infra/release/source-manifest.json').write_bytes(self.manifest_bytes);self.checks['sources'].pop();self.refuse();self.assertFalse((self.base/'apex-admission').exists())
 def test_build_intakes_native_artifact_and_qualifies_all_fifteen_archives(self):
  self.env['GITHUB_JOB']='build-images'
  with patch.dict(os.environ,self.env,clear=True):result=self.invoke()
  self.assertEqual(len(list((result/'archives').iterdir())),15);self.assertEqual(json.loads((result/'image-receipts.json').read_text())['controller'],self.controller)
  clones=[c for c in self.commands if 'fetch' in c[0]];self.assertEqual(len(clones),12)
  for argv,env,cwd,_ in clones:
   self.assertIn('--depth=1',argv);self.assertIn('https://github.com/Xynes-Studio/'+cwd.name+'.git',argv)
   self.assertEqual(argv[-1],self.manifest['sources'][cwd.name]['commit'])
   for key in ('GH_TOKEN','GH_POLICY_READ_TOKEN','SSH_AUTH_SOCK','GIT_CONFIG_COUNT','DOCKER_HOST'):self.assertNotIn(key,env)
  creates=[c for c in self.commands if 'create' in c[0]];self.assertEqual(len(creates),1);self.assertIn(self.module.BUILDKIT,creates[0][0])
  removes=[c for c in self.commands if 'rm' in c[0]];self.assertEqual(len(removes),1);self.assertEqual(removes[0][0][-1],'apex-build-100-1')
  self.assertEqual(json.loads((self.base/'apex-docker/config.json').read_text()),{})
 def test_foreign_or_corrupt_admission_has_no_clone_or_builder(self):
  self.env['GITHUB_JOB']='build-images';self.metadata['workflow_run']['head_sha']='b'*40;self.refuse();self.assertEqual(self.commands,[])
 def test_mismatched_exact_checkout_refuses_before_builder_creation(self):
  self.env['GITHUB_JOB']='build-images';original=self.command
  def command(argv,**options):
   result=original(argv,**options)
   return b'c'*40+b'\n' if 'rev-parse' in argv and options['cwd']!=self.root else result
  with self.assertRaises(self.module.PipelineError):self.module.build(self.env,read=lambda _:copy.deepcopy(self.metadata),download=self.download,command=command,execute=self.execute,migration_writer=self.migration_writer)
  self.assertFalse(any('create' in c[0] for c in self.commands))
 def test_failed_builder_creation_never_removes_unowned_builder(self):
  self.env['GITHUB_JOB']='build-images';self.fail_create=True;self.refuse();self.assertFalse(any('rm' in c[0] for c in self.commands))
 def test_failed_or_corrupt_build_cleans_only_its_builder(self):
  self.env['GITHUB_JOB']='build-images';self.fail_build=True;self.refuse();self.assertEqual(len([c for c in self.commands if 'rm' in c[0]]),1)
  other=self.base/'other';other.mkdir(mode=0o700);self.env['RUNNER_TEMP']=str(other);self.fail_build=False;self.corrupt_last=True;self.refuse();self.assertEqual(len([c for c in self.commands if 'rm' in c[0]]),2)
 def test_reused_runner_directory_is_not_overwritten(self):
  occupied=self.base/'apex-admission';occupied.mkdir(mode=0o700);sentinel=occupied/'keep';sentinel.write_text('preserve');self.refuse();self.assertEqual(sentinel.read_text(),'preserve')
 def test_actual_command_output_is_bounded_and_errors_are_redacted(self):
  with self.assertRaises(self.module.PipelineError):self.module.bounded_command([sys.executable,'-c','import sys;sys.stdout.write("x"*5000000)'],env={'PATH':os.environ['PATH']},cwd=self.base,timeout=5)
  with self.assertRaises(self.module.PipelineError):self.module.bounded_command([sys.executable,'-c','import sys;print("private-capability",file=sys.stderr);sys.exit(1)'],env={'PATH':os.environ['PATH']},cwd=self.base,timeout=5)

 def test_normal_reaped_subprocess_never_signals_a_reusable_process_group(self):
  actual_killpg=self.module.os.killpg
  with patch.object(self.module.os,'killpg',wraps=actual_killpg) as killpg:
   result=self.module.bounded_command([sys.executable,'-c','print(123)'],env={'PATH':os.environ['PATH']},cwd=self.base,timeout=5)
  self.assertEqual(result,b'123\n');killpg.assert_not_called()
 def test_timeout_signals_only_the_held_unreaped_child_before_wait(self):
  actual_popen=self.module.subprocess.Popen;actual_killpg=self.module.os.killpg;children=[];signals=[]
  def start(*args,**kwargs):
   child=actual_popen(*args,**kwargs);children.append(child);return child
  def signal_group(pid,signal):
   self.assertEqual(len(children),1);self.assertEqual(pid,children[0].pid);self.assertIsNone(children[0].returncode)
   signals.append((pid,signal));return actual_killpg(pid,signal)
  with patch.object(self.module.subprocess,'Popen',side_effect=start),patch.object(self.module.os,'killpg',side_effect=signal_group):
   with self.assertRaises(self.module.PipelineError):self.module.bounded_command([sys.executable,'-c','import time;time.sleep(30)'],env={'PATH':os.environ['PATH']},cwd=self.base,timeout=.1)
  self.assertEqual(signals,[(children[0].pid,self.module.signal.SIGKILL)])
  self.assertEqual(children[0].returncode,-self.module.signal.SIGKILL);self.assertTrue(children[0].stdout.closed);self.assertTrue(children[0].stderr.closed)
 def test_successful_subprocess_cannot_leave_a_pipe_holding_descendant(self):
  marker=self.base/'child-alive'
  child='import time;from pathlib import Path;time.sleep(.6);Path('+repr(str(marker))+').write_text("alive")'
  parent='import subprocess,sys;subprocess.Popen([sys.executable,"-c",'+repr(child)+'])'
  with self.assertRaises(self.module.PipelineError):self.module.bounded_command([sys.executable,'-c',parent],env={'PATH':os.environ['PATH']},cwd=self.base,timeout=.15)
  import time;time.sleep(.7);self.assertFalse(marker.exists(),'bounded command left its background child running')

if __name__=='__main__':unittest.main()
