#!/usr/bin/env python3
"""Actual private release file persistence; no native approval or host deploy."""
import copy,hashlib,importlib.util,json,os,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts/release'))
def load(name,path):
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);sys.modules[name]=m;s.loader.exec_module(m);return m
class Tests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.fixture=load('asset_transport_fixture',ROOT/'scripts/test/migration-transport.test.py').Tests();cls.fixture.setUp();cls.addClassCleanup(cls.fixture.doCleanups)
  cls.fixture.receipt=cls.fixture.create();cls.verified=cls.fixture.extract()
 def setUp(self):
  self.assertTrue((ROOT/'scripts/release/host_assets.py').is_file(),'durable release store is missing')
  self.m=load('host_assets',ROOT/'scripts/release/host_assets.py');self.uid=patch.object(self.m,'ROOT_UID',os.getuid());self.uid.start();self.addCleanup(self.uid.stop)
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve();self.root.chmod(0o700)
  self.base=self.root/'releases';self.base.mkdir(mode=0o700);self.store=self.m.Store('qa',base=self.base)
  self.state_root=self.root/'state';self.state_root.mkdir(mode=0o700)
  self.state=self.m.host_release_state.ReleaseState(self.state_root,stage='qa',owner_uid=os.getuid());self.state.__enter__();self.addCleanup(self.state.__exit__,None,None,None)
  self.request={'schema_version':1,'artifact_kind':'apex-deployment-request','controller':self.verified.controller,'publication':{'id':456,'digest':'sha256:'+'b'*64},'deployment':{'run_id':789,'run_attempt':1,'job_id':901}}
  self.token=self.state.begin(self.request,source_sha256='sha256:'+self.verified.source_manifest_sha256,started_at=int(time.time())-1)
  self.generation='d'*64
  self.compose=self.root/'compose.json';self.compose.write_bytes(self.config(self.generation));self.compose.chmod(0o600)
  prepared=self.m.host_compose.Prepared('qa',self.compose,hashlib.sha256(self.compose.read_bytes()).hexdigest(),{'fixture':'data-only'},storage_generation=self.generation)
  self.assets=self.m.root_executor.ReleaseAssets(prepared,self.verified,self.verified.controller,self.request['publication'],self.fixture.manifest,{}, {'fixture':'not a qualified publisher receipt'},self.verified)
 def config(self,generation):
  # Meaningful inert binding fragment; no native service/approval is fabricated.
  return self.m.encode({'services':{'storage-service':{'image':'inert-storage-digest',
    'labels':{'com.xynes.storage-generation':generation,'retained':'unchanged'},
    'environment':{'FIXTURE_ONLY':'inert-private-value'},'volumes':[
      {'type':'bind','source':'/run/xynes/storage/qa/'+generation[:32],
       'target':'/run/xynes/storage-control','read_only':False,'bind':{'create_host_path':False}},
      {'type':'bind','source':'/etc/xynes/storage-control/qa/control.token',
       'target':'/run/secrets/storage-quiesce-token','read_only':True,'bind':{'create_host_path':False}}]}}})
 def rollback_fixture(self,generation='e'*64):
  self.persist();accepted=self.accept();previous=self.store.load_release('qa',accepted)
  request=copy.deepcopy(self.request);request['controller']['run_id']+=1000
  request['publication']['id']+=1000;request['deployment']['run_id']+=1000;request['deployment']['job_id']+=1000
  token=self.state.begin(request,source_sha256='sha256:'+self.verified.source_manifest_sha256,started_at=int(time.time()))
  return accepted,previous,token,self.rebound(generation,previous)
 def rebound(self,generation,previous):
  file=self.root/('rebound-'+generation+'.json');file.write_bytes(self.config(generation));file.chmod(0o600)
  return self.m.host_compose.Prepared('qa',file,hashlib.sha256(file.read_bytes()).hexdigest(),
                                    copy.deepcopy(previous.prepared.images),storage_generation=generation)
 def recover(self,previous,rebound,token,**changes):
  self.assertTrue(hasattr(self.store,'persist_rollback'),'operational rollback persistence is missing')
  arguments=dict(state=self.state,token=token);arguments.update(changes)
  return self.store.persist_rollback(previous,rebound,**arguments)
 def persist(self,**changes):
  kw=dict(token=self.token,proofs={'local_fixture':True},state=self.state);kw.update(changes);return self.store.persist(self.assets,**kw)
 def accept(self):
  self.state.accept(self.token,smoke={'stage':'qa','publication':self.request['publication'],'source_sha256':'sha256:'+self.verified.source_manifest_sha256,'result':'passed','observed_at':int(time.time())},accepted_at=int(time.time()))
  return self.state.read_accepted()['current']
 def test_all_assets_and_origin_are_durable_and_load_only_after_accept(self):
  result=self.persist();self.assertNotEqual(result.prepared.file,self.compose)
  self.assertTrue(result.migration.directory.is_relative_to(self.base));self.assertTrue(result.origin.directory.is_relative_to(self.base))
  with self.assertRaises(self.m.AssetError):self.store.load_release('qa',self.state.read_pending())
  record=self.accept();restored=self.store.load_release('qa',record)
  self.assertEqual(restored.prepared.sha256,result.prepared.sha256);self.assertEqual(restored.controller,self.assets.controller)
  self.assertEqual(self.m.migration_transport.application_inputs(restored.migration),self.m.migration_transport.application_inputs(self.verified))
  self.assertEqual(restored.origin.controller,self.verified.controller)
  self.assertEqual(restored.prepared.storage_generation,self.generation)
  for p in (self.base/self.token).rglob('*'):self.assertEqual(p.stat().st_mode&0o777,0o700 if p.is_dir() else 0o600)
 def test_replay_or_request_binding_mismatch_never_overwrites(self):
  self.persist();original=(self.base/self.token/'asset-record.json').read_bytes()
  with self.assertRaises(self.m.AssetError):self.persist()
  self.assertEqual((self.base/self.token/'asset-record.json').read_bytes(),original)
  with self.assertRaises(self.m.AssetError):self.persist(token='c'*64)
 def test_corrupt_symlink_extra_or_foreign_metadata_refuse_load(self):
  result=self.persist();record=self.accept();file=result.prepared.file;original=file.read_bytes()
  file.write_bytes(b'tampered');
  with self.assertRaises(self.m.AssetError):self.store.load_release('qa',record)
  file.write_bytes(original);file.chmod(0o600);outside=self.root/'outside';outside.write_bytes(original);outside.chmod(0o600);file.unlink();file.symlink_to(outside)
  with self.assertRaises(self.m.AssetError):self.store.load_release('qa',record)
  file.unlink();file.write_bytes(original);file.chmod(0o600)
  extra=file.parent/'unknown';extra.write_text('preserve')
  with self.assertRaises(self.m.AssetError):self.store.load_release('qa',record)
  self.assertEqual(extra.read_text(),'preserve')
 def test_unknown_empty_directory_and_hardlinked_file_refuse_load(self):
  result=self.persist();record=self.accept();unknown=self.base/self.token/'unknown';unknown.mkdir(mode=0o700)
  with self.assertRaises(self.m.AssetError):self.store.load_release('qa',record)
  unknown.rmdir();alias=self.root/'alias';os.link(result.prepared.file,alias)
  with self.assertRaises(self.m.AssetError):self.store.load_release('qa',record)
  self.assertTrue(alias.exists())
 def test_wrong_stage_and_nonprivate_store_refuse(self):
  self.base.chmod(0o755)
  with self.assertRaises(self.m.AssetError):self.persist()
  self.base.chmod(0o700);self.persist();record=self.accept()
  with self.assertRaises(self.m.AssetError):self.store.load_release('prod',record)
 def test_file_write_failure_leaves_pending_and_preserves_source(self):
  original=self.compose.read_bytes()
  with patch.object(self.m.os,'fsync',side_effect=OSError('inert private error')):
   with self.assertRaises(self.m.AssetError):self.persist()
  self.assertEqual(self.compose.read_bytes(),original);self.assertIsNotNone(self.state.read_pending())
  self.assertFalse(self.state.read_accepted()['current'])
 def test_rollback_persistence_restores_operational_generation_without_changing_accepted_assets(self):
  accepted,previous,token,rebound=self.rollback_fixture()
  meta=(self.base/accepted['token']/'asset-record.json').read_bytes()
  old_compose=(self.base/accepted['token']/'compose.json').read_bytes()
  result=self.recover(previous,rebound,token)
  self.assertNotEqual(result.prepared.file,rebound.file)
  self.assertTrue(result.prepared.file.is_relative_to(self.base/'recovery'))
  self.assertEqual(result.prepared.storage_generation,rebound.storage_generation)
  self.assertEqual(result.controller,previous.controller);self.assertEqual(result.publication,previous.publication)
  self.assertEqual(result.origin.directory,previous.origin.directory)
  self.assertEqual((self.base/accepted['token']/'asset-record.json').read_bytes(),meta)
  self.assertEqual((self.base/accepted['token']/'compose.json').read_bytes(),old_compose)
  self.state.fail(token,reason='smoke',failed_at=int(time.time()))
  restored=self.store.load_release('qa',self.state.read_accepted()['current'])
  self.assertEqual(restored.prepared.sha256,rebound.sha256)
  self.assertEqual(self.state.read_accepted()['current']['token'],accepted['token'])
 def test_rollback_changes_outside_exact_generation_binding_refuse(self):
  accepted,previous,token,rebound=self.rollback_fixture()
  original=rebound.file.read_bytes()
  mutations=(lambda v:v['services']['storage-service']['environment'].update(FIXTURE_ONLY='changed'),
             lambda v:v['services']['storage-service']['labels'].update(retained='changed'),
             lambda v:v['services']['storage-service']['volumes'][1].update(source='/other/token'),
             lambda v:v['services']['storage-service']['volumes'][0].update(read_only=True),
             lambda v:v['services']['storage-service'].update(command=['sh','-c','anything']))
  for mutate in mutations:
   value=json.loads(original);mutate(value);raw=self.m.encode(value);rebound.file.write_bytes(raw)
   changed=self.m.replace(rebound,sha256=hashlib.sha256(raw).hexdigest())
   with self.assertRaises(self.m.AssetError):self.recover(previous,changed,token)
  self.assertFalse((self.base/'recovery').exists())
 def test_stale_wrong_stage_source_images_or_pending_token_refuse(self):
  accepted,previous,token,rebound=self.rollback_fixture()
  attacks=[(previous,previous.prepared,token),
           (self.m.replace(previous,publication={'id':999,'digest':'sha256:'+'a'*64}),rebound,token),
           (previous,self.m.replace(rebound,stage='prod'),token),
           (previous,self.m.replace(rebound,images={'fixture':'changed'}),token),
           (previous,rebound,'f'*64)]
  alias='d'*32+'e'*32;alias_prepared=self.rebound(alias,previous);attacks.append((previous,alias_prepared,token))
  for old,new,attempt in attacks:
   with self.assertRaises(self.m.AssetError):self.recover(old,new,attempt)
  self.assertEqual(self.state.read_pending()['token'],token)
 def test_unknown_incomplete_or_unreferenced_recovery_never_falls_back(self):
  accepted,previous,token,rebound=self.rollback_fixture();self.recover(previous,rebound,token)
  namespace=self.base/'recovery'/accepted['token'];unknown=namespace/'unknown';unknown.mkdir(mode=0o700)
  with self.assertRaises(self.m.AssetError):self.store.load_release('qa',accepted)
  unknown.rmdir();orphan=namespace/('f'*64);orphan.mkdir(mode=0o700)
  with self.assertRaises(self.m.AssetError):self.store.load_release('qa',accepted)
  self.assertTrue(orphan.exists())
 def test_rollback_replay_tamper_symlink_and_unsafe_permissions_refuse(self):
  accepted,previous,token,rebound=self.rollback_fixture();result=self.recover(previous,rebound,token)
  with self.assertRaises(self.m.AssetError):self.recover(previous,rebound,token)
  original=result.prepared.file.read_bytes();result.prepared.file.write_bytes(b'tampered')
  with self.assertRaises(self.m.AssetError):self.store.load_release('qa',accepted)
  result.prepared.file.write_bytes(original);result.prepared.file.chmod(0o644)
  with self.assertRaises(self.m.AssetError):self.store.load_release('qa',accepted)
  result.prepared.file.chmod(0o600);result.prepared.file.unlink();result.prepared.file.symlink_to(rebound.file)
  with self.assertRaises(self.m.AssetError):self.store.load_release('qa',accepted)
 def test_interrupted_recovery_write_preserves_pending_and_refuses_stale_load(self):
  accepted,previous,token,rebound=self.rollback_fixture()
  with patch.object(self.m.os,'fsync',side_effect=OSError('inert durability failure')):
   with self.assertRaises(self.m.AssetError):self.recover(previous,rebound,token)
  self.assertEqual(self.state.read_pending()['token'],token)
  with self.assertRaises(self.m.AssetError):self.store.load_release('qa',accepted)
  self.assertEqual(self.state.read_accepted()['current']['token'],accepted['token'])
 def test_multiple_failed_attempts_chain_operational_recovery_without_reusing_generation(self):
  accepted,previous,token,rebound=self.rollback_fixture();self.recover(previous,rebound,token)
  self.state.fail(token,reason='smoke',failed_at=int(time.time()))
  previous=self.store.load_release('qa',accepted)
  request=copy.deepcopy(self.request);request['controller']['run_id']+=2000;request['publication']['id']+=2000
  request['deployment']['run_id']+=2000;request['deployment']['job_id']+=2000
  token=self.state.begin(request,source_sha256='sha256:'+self.verified.source_manifest_sha256,started_at=int(time.time()))
  fresh=self.rebound('f'*64,previous);self.recover(previous,fresh,token)
  result=self.store.load_release('qa',accepted);self.assertEqual(result.prepared.storage_generation,'f'*64)
  self.assertEqual(result.origin.directory,previous.origin.directory)
  old=self.base/'recovery'/accepted['token']
  first=next(p/'recovery-record.json' for p in old.iterdir() if p.is_dir() and p.name!=token)
  meta=json.loads(first.read_bytes());meta['pending_request_sha256']='a'*64;first.write_bytes(self.m.encode(meta))
  with self.assertRaises(self.m.AssetError):self.store.load_release('qa',accepted)
 def test_interrupted_head_commit_preserves_incomplete_evidence_and_refuses_load(self):
  accepted,previous,token,rebound=self.rollback_fixture()
  with patch.object(self.m.os,'replace',side_effect=OSError('inert head commit failure')):
   with self.assertRaises(self.m.AssetError):self.recover(previous,rebound,token)
  namespace=self.base/'recovery'/accepted['token']
  self.assertTrue((namespace/token/'recovery-record.json').is_file())
  self.assertTrue((namespace/('head-'+token+'.json')).is_file())
  with self.assertRaises(self.m.AssetError):self.store.load_release('qa',accepted)
  self.assertEqual(self.state.read_pending()['token'],token)
 def test_changed_completed_head_is_never_blindly_overwritten(self):
  accepted,previous,token,rebound=self.rollback_fixture();self.recover(previous,rebound,token)
  self.state.fail(token,reason='smoke',failed_at=int(time.time()))
  previous=self.store.load_release('qa',accepted)
  request=copy.deepcopy(self.request);request['controller']['run_id']+=2000;request['publication']['id']+=2000
  request['deployment']['run_id']+=2000;request['deployment']['job_id']+=2000
  token=self.state.begin(request,source_sha256='sha256:'+self.verified.source_manifest_sha256,started_at=int(time.time()))
  head=self.base/'recovery'/accepted['token']/'latest.json';native_write=self.store.write
  def changed(destination,name,raw,catalog):
   result=native_write(destination,name,raw,catalog)
   if name=='recovery-record.json':head.write_bytes(b'operator divergence\n')
   return result
  with patch.object(self.store,'write',side_effect=changed):
   with self.assertRaises(self.m.AssetError):self.recover(previous,self.rebound('f'*64,previous),token)
  self.assertEqual(head.read_bytes(),b'operator divergence\n')
  with self.assertRaises(self.m.AssetError):self.store.load_release('qa',accepted)
 def test_later_soak_data_does_not_change_accepted_source_binding(self):
  accepted,previous,token,rebound=self.rollback_fixture();result=self.recover(previous,rebound,token)
  later=copy.deepcopy(accepted);later['soak']={'fixture':'does not claim native soak'};later['soak_recorded_at']=int(time.time())
  loaded=self.store.load_release('qa',later)
  self.assertEqual(loaded.prepared.sha256,result.prepared.sha256)
 def test_empty_head_cannot_authorize_fallback_to_historical_generation(self):
  accepted,previous,token,rebound=self.rollback_fixture()
  namespace=self.base/'recovery'/accepted['token'];namespace.mkdir(parents=True,mode=0o700)
  namespace.parent.chmod(0o700)
  head=namespace/'latest.json';head.write_bytes(self.m.encode({'schema_version':1,
    'artifact_kind':'rollback-operational-head','stage':'qa','accepted_token':accepted['token'],
    'latest_token':None,'record_sha256':'a'*64}));head.chmod(0o600)
  with self.assertRaises(self.m.AssetError):self.store.load_release('qa',accepted)
if __name__=='__main__':unittest.main()
