#!/usr/bin/env python3
"""Real artifact ZIPs/OCI archives and native metadata fixtures; zero network."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts/release'))
import image_build,registry_io,release_manifest as records
def fixture(name):
 spec=importlib.util.spec_from_file_location(name,ROOT/('scripts/test/'+name+'.test.py'));module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
IMAGE=fixture('image-build');ADMISSION=fixture('admission')

class Tests(unittest.TestCase):
 def setUp(self):
  path=ROOT/'scripts/release/publish_images.py';self.assertTrue(path.exists(),'protected image publisher entrypoint missing')
  spec=importlib.util.spec_from_file_location('publish_images',path);self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve();self.root.chmod(0o700)
  self.manifest,self.public,_=IMAGE.inputs();self.manifest.update(status='approved',approval_reference='https://github.com/Xynes-Studio/xynes-infra/pull/1')
  self.controller=dict(repository='Xynes-Studio/xynes-infra',commit='a'*40,workflow='apex-build.yml',run_id=100,run_attempt=1)
  self.data=ADMISSION.fixtures();self.data[ADMISSION.P+'/actions/runs/100/attempts/1/jobs?per_page=100&page=1']={'total_count':1,'jobs':[self.data[ADMISSION.P+'/actions/jobs/200']]}
  catalog=json.loads((ROOT/'infra/release/source-check-catalog.json').read_text())
  self.source_checks={'checks_verified':True,'approved':False,'deployable':False,'sources':[{'repository':s['repository'],'commit':s['commit'],'checks':[{'name':name,'check_id':800+i,'job_id':900+i,'run_id':1000+i,'run_attempt':1,'workflow_id':1100+i,'workflow_path':path,'workflow_blob':'b'*40} for i,(name,path) in enumerate(catalog[key].items())]} for key,s in self.manifest['sources'].items()]}
  self.admission_files={'controller.json':self.controller,'source-manifest.json':self.manifest,'public-inputs.json':self.public,'source-checks.json':self.source_checks}
  self.build_files={};images=[]
  for task in image_build.build_tasks(self.manifest,self.public,self.controller,'release'):
   path=self.root/(task['id']+'.oci.tar');IMAGE.archive(path,task['labels']);images.append(image_build.inspect_oci(path,task));self.build_files['archives/'+path.name]=path.read_bytes();path.unlink()
  self.build_files['image-receipts.json']={'schema_version':1,'artifact_kind':'local-oci-build-receipts','controller':self.controller,'source_manifest_sha256':records.canonical_sha256(self.manifest),'images':images,'external_images':self.manifest['external_images'],'published':False,'deployable':False}
  self.blobs={};self.refs={};self.refresh()
  self.env={'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':'Xynes-Studio/xynes-infra','GITHUB_SHA':'a'*40,'GITHUB_REF':'refs/heads/main','GITHUB_REF_PROTECTED':'true','GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_RUN_ID':'100','GITHUB_RUN_ATTEMPT':'1','GITHUB_JOB':'publish-images','GITHUB_ACTOR':'archan96','GITHUB_API_URL':'https://api.github.com','GITHUB_SERVER_URL':'https://github.com','GITHUB_WORKFLOW_REF':'Xynes-Studio/xynes-infra/.github/workflows/apex-build.yml@refs/heads/main','APEX_REGISTRY_PREFIX':'ghcr.io/xynes-studio/apex','GH_TOKEN':'ghs_INERT_DO_NOT_PRINT_JOB_TOKEN','GH_POLICY_READ_TOKEN':'inert-policy-token'}
  for kind in ('admission','build'):self.env['APEX_'+kind.upper()+'_RECEIPT']=json.dumps(self.refs[kind])
  self.writes=[]
  self.events_output=io.StringIO();self.stdout=io.StringIO()
  for manager in (contextlib.redirect_stderr(self.events_output),contextlib.redirect_stdout(self.stdout)):
   manager.__enter__();self.addCleanup(manager.__exit__,None,None,None)
 def events(self):return [(value['phase'],value['status']) for value in map(json.loads,self.events_output.getvalue().splitlines())]
 def assert_safe_events(self):
  self.assertEqual(self.stdout.getvalue(),'')
  for value in map(json.loads,self.events_output.getvalue().splitlines()):
   self.assertEqual(set(value),{'artifact_kind','phase','status'});self.assertEqual(value['artifact_kind'],'apex-build-diagnostic')
  for secret in (self.env['GH_TOKEN'],self.env['GH_POLICY_READ_TOKEN'],'CANARY-private-error','https://private.invalid/credential'):
   self.assertNotIn(secret,self.events_output.getvalue())
 def test_publisher_diagnostics_preserve_success_order_and_stdout_contract(self):
  result=self.invoke();out=self.root/'outputs';out.write_text('');out.chmod(0o600)
  self.module.persist_publication(result,{**self.env,'RUNNER_TEMP':str(self.root),'GITHUB_OUTPUT':str(out)})
  expected=[(phase,status) for phase in ('initial-policy','admission-artifact','source-qualification','build-artifact') for status in ('start','success')]
  expected += [('image-publication-batch','start'),('namespace-qualification','start'),('namespace-qualification','success'),('image-publication-batch','success'),('publication-receipt','start'),('publication-receipt','success')]
  self.assertEqual(self.events(),expected);self.assert_safe_events()
 def test_cli_stdout_remains_one_machine_readable_result_without_diagnostics(self):
  result=self.invoke()
  with patch.dict(os.environ,self.env,clear=True),patch.object(self.module.sys,'argv',['publish_images.py']),patch.object(self.module,'run',return_value=result),patch.object(self.module,'persist_publication'):
   self.assertEqual(self.module.main(),0)
  self.assertEqual(len(self.stdout.getvalue().splitlines()),1)
  self.assertEqual(json.loads(self.stdout.getvalue()),{'published':True,'images':15,'receipt_sha256':result['receipt_sha256'],'attested':False,'deployable':False})
  for secret in (self.env['GH_TOKEN'],self.env['GH_POLICY_READ_TOKEN']):self.assertNotIn(secret,self.stdout.getvalue()+self.events_output.getvalue())
 def test_unvalidated_input_failure_only_emits_fixed_policy_phase(self):
  self.env['GITHUB_REPOSITORY']='https://private.invalid/credential';self.refused()
  self.assertEqual(self.events(),[('initial-policy','start'),('initial-policy','failure')]);self.assert_safe_events()
 def test_failed_artifact_download_reports_phase_without_private_error(self):
  def download(*args):raise RuntimeError('CANARY-private-error https://private.invalid/credential '+self.env['GH_TOKEN'])
  self.download=download;self.refused()
  self.assertEqual(self.events()[-2:],[('admission-artifact','start'),('admission-artifact','failure')]);self.assertEqual(self.writes,[]);self.assert_safe_events()
 def test_failed_source_qualification_reports_phase_before_build_download(self):
  self.source_checks['checks_verified']=False;self.refresh();self.env['APEX_ADMISSION_RECEIPT']=json.dumps(self.refs['admission']);self.refused()
  self.assertEqual(self.events()[-2:],[('source-qualification','start'),('source-qualification','failure')]);self.assertEqual(self.writes,[]);self.assert_safe_events()
 def test_failed_namespace_reports_nested_phase_and_no_publication_receipt(self):
  def read(path):
   if path.startswith('/orgs/'):raise RuntimeError('CANARY-private-error '+self.env['GH_POLICY_READ_TOKEN'])
   return self.read(path)
  with self.assertRaises(self.module.PublisherError):self.module.run(self.env,self.root,read=read,read_policy=self.read,read_graphql=lambda _:None,download=self.download,transport=type('Transport',(),{'request':lambda *a,**k:None})(),publish=self.publish)
  self.assertEqual(self.events()[-4:],[('image-publication-batch','start'),('namespace-qualification','start'),('namespace-qualification','failure'),('image-publication-batch','failure')])
  self.assertEqual(self.writes,[]);self.assertFalse((self.root/'apex-publications').exists());self.assert_safe_events()
 def test_invalid_publication_receipt_reports_fixed_failure(self):
  with self.assertRaises(self.module.PublisherError):self.module.persist_publication({'CANARY-private-error':'https://private.invalid/credential'},self.env)
  self.assertEqual(self.events(),[('publication-receipt','start'),('publication-receipt','failure')]);self.assert_safe_events()
 def refresh(self):
  for identifier,kind,files in [(98,'admission',self.admission_files),(99,'build',self.build_files)]:
   stream=io.BytesIO()
   with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_STORED) as archive:
    for name,value in files.items():archive.writestr(name,value if isinstance(value,bytes) else json.dumps(value).encode())
   body=stream.getvalue();self.blobs[identifier]=body;digest='sha256:'+hashlib.sha256(body).hexdigest();url='https://api.github.com'+ADMISSION.P+f'/actions/artifacts/{identifier}'
   self.refs[kind]={'id':identifier,'digest':digest}
   self.data[ADMISSION.P+f'/actions/artifacts/{identifier}']={'id':identifier,'digest':digest,'name':f'apex-{kind}-100-1','expired':False,'size_in_bytes':len(body),'url':url,'archive_download_url':url+'/zip','workflow_run':{'id':100,'repository_id':1415400917,'head_repository_id':1415400917,'head_branch':'main','head_sha':'a'*40}}
 def read(self,path):
  if path.startswith('/orgs/Xynes-Studio/packages/container/apex%2F'):
   name=path.split('%2F')[-1];return {'id':1000+list(registry_io.WORKLOADS).index(name),'name':'apex/'+name,'package_type':'container','visibility':'private','owner':{'login':'Xynes-Studio','type':'Organization'},'repository':{'id':1415400917,'full_name':'Xynes-Studio/xynes-infra'}}
  return copy.deepcopy(self.data[path])
 def download(self,identifier,path,max_bytes):
  self.assertEqual(len(self.blobs[identifier]),max_bytes);path.write_bytes(self.blobs[identifier])
 def publish(self,path,task,image,**kwargs):
  self.writes.append(image);return {**image_build.inspect_oci(path,task),'image':image,'published':True,'registry_content_verified':True,'visibility_verified':False,'attested':False,'trust_verified':False,'deployable':False}
 def test_new_private_namespaces_are_qualified_with_inert_bytes_before_app_publication(self):
  existing={};inert=[];app=[];inert_images=[]
  def native_package(workload):return copy.deepcopy(existing.get(workload))
  def reading(path):
   if path.startswith('/orgs/Xynes-Studio/packages/container/apex%2F'):
    return native_package(path.split('%2F')[-1])
   return self.read(path)
  def publishing(path,task,image,**kwargs):
   if task['id'].startswith('namespace--'):
    import tarfile
    with tarfile.open(path) as archive:
     config=json.loads(archive.extractfile('blobs/sha256/'+image_build.inspect_oci(path,task)['config_digest'][7:]).read())
     self.assertEqual(config['rootfs']['diff_ids'],[])
     self.assertEqual(config['config']['Labels']['com.xynes.apex.artifact-kind'],'namespace-qualification-only')
    inert.append(task['workload']);inert_images.append(image);existing[task['workload']]=self.read('/orgs/Xynes-Studio/packages/container/apex%2F'+task['workload'])
   else:
    self.assertEqual(len(existing),11);app.append(image)
   return self.publish(path,task,image,**kwargs)
  result=self.module.run(self.env,self.root,read=reading,read_policy=self.read,read_graphql=lambda _:None,
    read_namespace=native_package,download=self.download,transport=type('Transport',(),{'request':lambda *a,**k:None})(),publish=publishing)
  self.assertEqual(len(inert),11);self.assertEqual(len(app),15);self.assertEqual(len(result['images']),15)
  self.assertTrue(set(inert_images).isdisjoint(image['image'] for image in result['images']))
 def invoke(self):
  return self.module.run(self.env,self.root,read=self.read,read_policy=self.read,read_graphql=lambda _:None,download=self.download,transport=type('Transport',(),{'request':lambda *a,**k:None})(),publish=self.publish)
 def refused(self):
  with self.assertRaises(self.module.PublisherError) as caught:self.invoke()
  self.assertEqual(str(caught.exception),'Protected image publisher refused')
  for private in ('ghs_INERT_DO_NOT_PRINT_JOB_TOKEN','inert-policy-token'):self.assertNotIn(private,str(caught.exception))
 def test_actual_handoff_native_admission_and_full_batch_are_connected(self):
  result=self.invoke();self.assertEqual(len(self.writes),15);self.assertTrue(result['published']);self.assertFalse(result['attested']);self.assertFalse(result['deployable'])
 def test_full_receipt_and_verified_subjects_are_retained_for_signer_and_deployer(self):
  result=self.invoke();out=self.root/'github-output';out.write_text('');out.chmod(0o600)
  saved=self.module.persist_publication(result,{**self.env,'RUNNER_TEMP':str(self.root),'GITHUB_OUTPUT':str(out)})
  self.assertEqual(json.loads(saved.read_text()),result);self.assertEqual(saved.stat().st_mode&0o777,0o600)
  outputs=dict(line.split('=',1) for line in out.read_text().splitlines())
  subjects=json.loads(outputs['subjects']);self.assertEqual(len(subjects),15)
  self.assertEqual({s['name']+'@'+s['digest'] for s in subjects},{i['image'] for i in result['images']})
  self.assertNotIn('GH_TOKEN',saved.read_text());self.assertNotIn(self.env['GH_TOKEN'],out.read_text())
  original=saved.read_bytes()
  with self.assertRaises(self.module.PublisherError):self.module.persist_publication(result,{**self.env,'RUNNER_TEMP':str(self.root),'GITHUB_OUTPUT':str(out)})
  self.assertEqual(saved.read_bytes(),original)
 def test_untrusted_environment_cannot_download_or_publish(self):
  for key,value in [('GITHUB_EVENT_NAME','pull_request'),('GITHUB_REF_PROTECTED','false'),('GITHUB_RUN_ATTEMPT','2'),('GITHUB_JOB','other'),('APEX_REGISTRY_PREFIX','ghcr.io/foreign/apex'),('GITHUB_REPOSITORY','Other/repo'),('GH_TOKEN','inert-read-token')]:
   original=self.env[key];self.env[key]=value;self.refused();self.assertEqual(self.writes,[]);self.env[key]=original
 def test_removed_native_approval_and_foreign_artifact_never_write(self):
  approvals=ADMISSION.P+'/actions/runs/100/approvals';saved=self.data[approvals];self.data[approvals]=[];self.refused();self.assertEqual(self.writes,[]);self.data[approvals]=saved
  self.data[ADMISSION.P+'/actions/artifacts/99']['workflow_run']['id']=101;self.refused();self.assertEqual(self.writes,[])
 def test_last_archive_corruption_fails_before_registry_write(self):
  name=list(self.build_files)[-2];self.build_files[name]=b'corrupt-final-image';self.refresh();self.env['APEX_BUILD_RECEIPT']=json.dumps(self.refs['build']);self.refused();self.assertEqual(self.writes,[])
 def test_invalid_batch_does_not_even_create_missing_namespaces(self):
  name=list(self.build_files)[-2];self.build_files[name]=b'corrupt-final-image';self.refresh();self.env['APEX_BUILD_RECEIPT']=json.dumps(self.refs['build'])
  with self.assertRaises(self.module.PublisherError):
   self.module.run(self.env,self.root,read=self.read,read_policy=self.read,read_graphql=lambda _:None,
     read_namespace=lambda _:None,download=self.download,
     transport=type('Transport',(),{'request':lambda *a,**k:None})(),publish=self.publish)
  self.assertEqual(self.writes,[])
 def test_mismatched_controller_and_unqualified_source_receipts_refuse(self):
  self.admission_files['controller.json']={**self.controller,'run_id':999};self.refresh();self.env['APEX_ADMISSION_RECEIPT']=json.dumps(self.refs['admission']);self.refused();self.assertEqual(self.writes,[])
 def test_unqualified_source_or_wrong_producer_is_not_authorization(self):
  original=copy.deepcopy(self.source_checks)
  for mutation in ('missing-source','wrong-workflow','approved-boolean','failed-checks'):
   value=copy.deepcopy(original)
   if mutation=='missing-source':value['sources'].pop()
   elif mutation=='wrong-workflow':value['sources'][-1]['checks'][0]['workflow_path']='.github/workflows/other.yml'
   elif mutation=='approved-boolean':value['approved']=True
   else:value['checks_verified']=False
   self.admission_files['source-checks.json']=value;self.refresh();self.env['APEX_ADMISSION_RECEIPT']=json.dumps(self.refs['admission']);self.refused();self.assertEqual(self.writes,[])

if __name__=='__main__':unittest.main()
