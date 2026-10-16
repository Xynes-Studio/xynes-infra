#!/usr/bin/env python3
"""Real inert OCI archives; injected publication spies never contact a provider."""
import copy
import importlib.util
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/release'))
import image_build
import registry_io
import release_manifest as records
SPEC=importlib.util.spec_from_file_location('image_fixture',ROOT/'scripts/test/image-build.test.py')
FIXTURE=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(FIXTURE)

class Tests(unittest.TestCase):
 def setUp(self):
  path=ROOT/'scripts/release/publish_batch.py';self.assertTrue(path.is_file(),'complete-batch publication orchestration is missing')
  spec=importlib.util.spec_from_file_location('publish_batch',path);self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
  self.tmp=tempfile.TemporaryDirectory(prefix='apex-publish-batch-');self.addCleanup(self.tmp.cleanup);self.archives=Path(self.tmp.name).resolve()
  self.manifest,self.public,self.controller=FIXTURE.inputs();self.manifest.update(status='approved',approval_reference='https://github.com/Xynes-Studio/xynes-infra/pull/1')
  self.tasks=image_build.build_tasks(self.manifest,self.public,self.controller,'release');images=[]
  for task in self.tasks:
   archive=self.archives/(task['id']+'.oci.tar');FIXTURE.archive(archive,task['labels']);images.append(image_build.inspect_oci(archive,task))
  self.build=dict(schema_version=1,artifact_kind='local-oci-build-receipts',controller=copy.deepcopy(self.controller),source_manifest_sha256=records.canonical_sha256(self.manifest),images=images,external_images=self.manifest['external_images'],published=False,deployable=False)
  self.writes=[];self.gates=[];self.proofs=[];self.events=[]
  self.credential=registry_io.GitHubPublisherToken('fixture-actor','ghs_INERT_FIXTURE_NOT_A_REAL_TOKEN_123456789')
  self.transport=SimpleNamespace(request=lambda *args,**kwargs: (_ for _ in ()).throw(AssertionError('network adapter must not be called by publication spy')))
 def admission(self,controller):
  self.gates.append(copy.deepcopy(controller));self.events.append('admission')
  result={**controller,'admitted':True,'repository_id':1415400917,'job_id':200,'workflow_blob':'a'*40,'environment_id':300,'reviewer_id':8,'checks':[dict(name=n,check_id=600+i,run_id=400+i,job_id=500+i,run_attempt=1,workflow_id=700+i) for i,n in enumerate(['lint','test','coverage','build','typecheck','security'])]}
  result['source_approval']=dict(approval_reference='https://github.com/Xynes-Studio/xynes-infra/pull/1',pull_number=1,head_commit='b'*40,merge_commit=controller['commit'],tree_sha='c'*40,reviewer_id=8,review_id=901,source_manifest_sha256=records.canonical_sha256(self.manifest))
  result['evidence_sha256']=records.canonical_sha256(result);return result
 def package(self,workload):
  self.proofs.append(workload);self.events.append('package:'+workload)
  return dict(id=1+list(dict.fromkeys(t['workload'] for t in self.tasks)).index(workload),name='apex/'+workload,package_type='container',visibility='private',owner=dict(login='Xynes-Studio',type='Organization'),repository=dict(id=1415400917,full_name='Xynes-Studio/xynes-infra'))
 def publish(self,path,task,image,**kwargs):
  self.assertIs(kwargs['credential'],self.credential);self.assertIs(kwargs['transport'],self.transport)
  self.writes.append(task['id']);self.events.append('write:'+task['id'])
  return {**image_build.inspect_oci(path,task),'image':image,'published':True,'registry_content_verified':True,'visibility_verified':False,'attested':False,'trust_verified':False,'deployable':False}
 def run_batch(self,**overrides):
  arguments=dict(manifest=self.manifest,public=self.public,controller=self.controller,build_receipt=self.build,archives=self.archives,admission_check=self.admission,package_proof=self.package,credential=self.credential,transport=self.transport,publish=self.publish)
  arguments.update(overrides);return self.module.publish_batch(**arguments)
 def test_source_approval_must_bind_actual_manifest_before_any_upload(self):
  for mode in ('missing','hash','extra'):
   def gate(controller):
    value=self.admission(controller)
    if mode=='missing':del value['source_approval']
    elif mode=='hash':value['source_approval']['source_manifest_sha256']='0'*64
    else:value['source_approval']['approved']=True
    value['evidence_sha256']=records.canonical_sha256({k:v for k,v in value.items() if k!='evidence_sha256'});return value
   with self.subTest(mode=mode):
    with self.assertRaises(self.module.PublishError):self.run_batch(admission_check=gate)
    self.assertEqual(self.writes,[])
 def test_complete_real_archive_batch_rechecks_gates_per_write(self):
  result=self.run_batch();self.assertEqual(self.writes,[t['id'] for t in self.tasks]);self.assertEqual(len(result['images']),15)
  self.assertTrue(result['published']);self.assertFalse(result['attested']);self.assertFalse(result['trust_verified']);self.assertFalse(result['deployable'])
  for index,event in enumerate(self.events):
   if event.startswith('write:'):
    self.assertTrue(self.events[index-1].startswith('admission'));self.assertTrue(self.events[index-2].startswith('package:'))
 def test_invalid_complete_batches_never_write_even_when_error_is_last_image(self):
  original=copy.deepcopy(self.build)
  for mode in ['missing','duplicate','controller','manifest_hash','extra','published','planning','public','archive_name','tamper']:
   with self.subTest(mode=mode):
    self.build=copy.deepcopy(original);manifest=copy.deepcopy(self.manifest);public=copy.deepcopy(self.public)
    renamed=None;last=self.archives/(self.tasks[-1]['id']+'.oci.tar');saved=last.read_bytes()
    if mode=='missing':self.build['images'].pop()
    elif mode=='duplicate':self.build['images'][-1]=copy.deepcopy(self.build['images'][0])
    elif mode=='controller':self.build['controller']['run_id']=999
    elif mode=='manifest_hash':self.build['source_manifest_sha256']='0'*64
    elif mode=='extra':self.build['images'][-1]['approved']=True
    elif mode=='published':self.build['published']=True
    elif mode=='planning':manifest.update(status='planning',approval_reference=None)
    elif mode=='public':public['prod']['xynes-auth-app']['NEXT_PUBLIC_APP_URL']='https://wrong.example.invalid'
    elif mode=='archive_name':renamed=last.with_name('unexpected.oci.tar');last.rename(renamed)
    elif mode=='tamper':last.write_bytes(b'tampered-last-archive')
    try:
     with self.assertRaises(self.module.PublishError):self.run_batch(manifest=manifest,public=public)
     self.assertEqual(self.writes,[])
    finally:
     if renamed:renamed.rename(last)
     elif mode=='tamper':last.write_bytes(saved)
 def test_missing_or_public_foreign_namespace_packages_never_receive_appbytes(self):
  for mode in ['missing','public','foreignrepo','foreignworkload','owner','extra']:
   with self.subTest(mode=mode):
    def proof(workload):
     value=self.package(workload)
     if workload==self.tasks[-1]['workload']:
      if mode=='missing':return None
      if mode=='public':value['visibility']='public'
      elif mode=='foreignrepo':value['repository']['id']=1
      elif mode=='foreignworkload':value['name']='apex/other'
      elif mode=='owner':value['owner']['login']='other'
      elif mode=='extra':value['approved']=True
     return value
    with self.assertRaises(self.module.PublishError):self.run_batch(package_proof=proof)
    self.assertEqual(self.writes,[])
 def test_revoked_admission_stops_next_write_and_returns_no_complete_receipt(self):
  def admission(controller):
   if len(self.writes)==1:raise RuntimeError('no longer admitted')
   return self.admission(controller)
  with self.assertRaises(self.module.PublishError):self.run_batch(admission_check=admission)
  self.assertEqual(self.writes,[self.tasks[0]['id']])
 def test_mid_batch_private_package_revocation_stops_next_image(self):
  def package(workload):
   value=self.package(workload)
   if self.writes and workload==self.tasks[1]['workload']:value['visibility']='public'
   return value
  with self.assertRaises(self.module.PublishError):self.run_batch(package_proof=package)
  self.assertEqual(self.writes,[self.tasks[0]['id']])
 def test_approval_boolean_or_wrong_controller_receipt_is_not_authorization(self):
  for mode in ['bool','wrongcontroller','tampered_hash']:
   with self.subTest(mode=mode):
    def check(controller):
     if mode=='bool':return {'approved':True}
     value=self.admission(controller)
     if mode=='wrongcontroller':value['commit']='b'*40;value['evidence_sha256']=records.canonical_sha256({k:v for k,v in value.items() if k!='evidence_sha256'})
     else:value['evidence_sha256']='0'*64
     return value
    with self.assertRaises(self.module.PublishError):self.run_batch(admission_check=check)
    self.assertEqual(self.writes,[])
 def test_partial_content_verification_never_emits_complete_receipt(self):
  def publish(*args,**kwargs):
   result=self.publish(*args,**kwargs)
   if len(self.writes)==2:result['registry_content_verified']=False
   return result
  with self.assertRaises(self.module.PublishError):self.run_batch(publish=publish)
  self.assertEqual(len(self.writes),2)

if __name__=='__main__':unittest.main()
