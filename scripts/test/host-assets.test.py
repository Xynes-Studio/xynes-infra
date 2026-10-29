#!/usr/bin/env python3
"""Actual private release file persistence; no native approval or host deploy."""
import copy,hashlib,importlib.util,json,os,subprocess,sys,tempfile,time,unittest
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
  repo=cls.fixture.fixture.roots['xynes-infra']
  subprocess.run(['git','-C',str(repo),'-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false','commit','--allow-empty','-qm','successor controller only'],check=True)
  cls.fixture.controller={**cls.fixture.controller,'commit':subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD']).decode().strip()}
  bundle=cls.fixture.work/'successor.tar';receipt=cls.fixture.create(output=bundle)
  receiver=cls.fixture.work/'successor';receiver.mkdir(mode=0o700)
  cls.successor=cls.fixture.extract(receipt,bundle=bundle,work=receiver)
  # Genuine fresh builds of the successor commit with new run ids. (The transport
  # itself already refuses any run_attempt other than 1.)
  successor_controller=cls.fixture.controller;cls.rebuilds={}
  for name,changes in (('retry',{'run_id':successor_controller['run_id']+77}),('second',{'run_id':successor_controller['run_id']+78}),
                       ('proof',{'run_id':successor_controller['run_id']+79})):
   cls.fixture.controller={**successor_controller,**changes}
   bundle=cls.fixture.work/(name+'.tar');receipt=cls.fixture.create(output=bundle)
   receiver=cls.fixture.work/name;receiver.mkdir(mode=0o700)
   cls.rebuilds[name]=cls.fixture.extract(receipt,bundle=bundle,work=receiver)
  cls.fixture.controller=successor_controller
  subprocess.run(['git','-C',str(repo),'-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false','commit','--allow-empty','-qm','later accepted upgrade'],check=True)
  cls.fixture.controller={**cls.fixture.controller,'commit':subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD']).decode().strip()}
  bundle=cls.fixture.work/'later.tar';receipt=cls.fixture.create(output=bundle)
  receiver=cls.fixture.work/'later';receiver.mkdir(mode=0o700)
  cls.later=cls.fixture.extract(receipt,bundle=bundle,work=receiver)
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
  self.state.accept(self.token,smoke={'storage_verified':True,'stage':'qa','publication':self.request['publication'],'source_sha256':'sha256:'+self.verified.source_manifest_sha256,'result':'passed','observed_at':int(time.time())},accepted_at=int(time.time()))
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
 def initial_fixture(self):
  self.state.fail(self.token,reason='interrupted',failed_at=int(time.time()))
  failed=copy.deepcopy(self.state.data['attempts'][-1])
  root=self.root/'initial';root.mkdir(mode=0o700)
  for patcher in (patch.object(self.m,'INITIAL_BASELINE_ROOT',root),
    patch.object(self.m,'initial_database',return_value={'container_id':'a'*64,'data_inode':[7,11],'data_uid':70,'data_mode':448})):
   patcher.start();self.addCleanup(patcher.stop)
  catalog={};self.store.migration(root,'origin',self.verified,catalog)
  publication={'id':457,'digest':'sha256:'+'c'*64}
  import host_migrate
  units,_=self.m.migration_transport.application_inputs(self.verified)
  capsule={'schema_version':1,'artifact_kind':'qa-failed-initial-baseline','stage':'qa',
    'failed_record_sha256':hashlib.sha256(self.m.encode(failed)).hexdigest(),
    'state_sha256':hashlib.sha256(self.m.host_release_state.encode(self.state.data)).hexdigest(),
    'origin_controller':self.verified.controller,'origin_source_manifest_sha256':self.verified.source_manifest_sha256,
    'baseline_sha256':host_migrate.baseline(units),'database':self.m.initial_database(),
    'native_proof_sha256':'d'*64,
    'successor':{'controller':self.successor.controller,'publication':publication,'source_manifest_sha256':self.successor.source_manifest_sha256},'files':catalog}
  def seal():
   raw=self.m.encode(capsule)
   for name,data in [('capsule.json',raw),('capsule.sha256',(hashlib.sha256(raw).hexdigest()+'\n').encode())]:
    path=root/name;path.write_bytes(data);path.chmod(0o600)
  for name,value in [('INITIAL_ORIGIN_COMMIT',self.verified.controller['commit']),
    ('INITIAL_ORIGIN_MANIFEST',self.verified.source_manifest_sha256),('INITIAL_ORIGIN_BASELINE',capsule['baseline_sha256']),
    ('INITIAL_FAILED_DEPLOYMENT',self.request['deployment']),('INITIAL_FAILED_PUBLICATION',self.request['publication'])]:
   patcher=patch.object(self.m,name,value);patcher.start();self.addCleanup(patcher.stop)
  seal()
  return root,capsule,publication,seal
 def test_failed_initial_capsule_authorizes_only_exact_successor_and_preserves_old_rows(self):
  root,capsule,publication,seal=self.initial_fixture()
  result=self.store.initial_origin(self.state,self.successor,publication)
  self.assertEqual(result[0].controller,self.verified.controller)
  self.assertEqual(result[1]['capsule_sha256'],hashlib.sha256((root/'capsule.json').read_bytes()).hexdigest())
  self.assertEqual(self.state.read_accepted(),{'current':None,'previous':None})
  request={**self.request,'controller':self.successor.controller,'publication':publication,'deployment':{'run_id':790,'run_attempt':1,'job_id':902}}
  token=self.state.begin(request,source_sha256='sha256:'+self.successor.source_manifest_sha256,started_at=int(time.time()))
  self.assertEqual(self.store.initial_origin(self.state,self.successor,publication)[0].controller,self.verified.controller)
  assets=self.m.replace(self.assets,migration=self.successor,controller=self.successor.controller,
    publication=publication,manifest=json.loads((self.successor.directory/'source-manifest.json').read_bytes()),origin=result[0])
  restored=self.store.persist(assets,token=token,proofs={},state=self.state)
  meta=json.loads((self.base/token/'asset-record.json').read_bytes())
  self.assertEqual(meta['proofs']['initial_baseline_origin'],result[1])
  self.state.accept(token,smoke={'storage_verified':True,'stage':'qa','publication':publication,'source_sha256':'sha256:'+self.successor.source_manifest_sha256,'result':'passed','observed_at':int(time.time())},accepted_at=int(time.time()))
  # A real accepted successor is the anchor; the failed origin is never accepted.
  self.assertEqual(len([r for r in self.state.data['attempts'] if r['status']=='accepted']),1)
  with patch.object(self.m,'initial_database',side_effect=AssertionError('historical CID must not constrain a genuinely accepted upgrade')):
   self.assertEqual(self.store.initial_origin(self.state,self.later,{'id':458,'digest':'sha256:'+'e'*64},expected_prior=restored.origin)[0].controller,self.verified.controller)
  laterpub={'id':458,'digest':'sha256:'+'e'*64}
  request={**request,'controller':self.later.controller,'publication':laterpub,'deployment':{'run_id':791,'run_attempt':1,'job_id':903}}
  nexttoken=self.state.begin(request,source_sha256='sha256:'+self.later.source_manifest_sha256,started_at=int(time.time()))
  laterassets=self.m.replace(assets,migration=self.later,controller=self.later.controller,publication=laterpub,manifest=json.loads((self.later.directory/'source-manifest.json').read_bytes()))
  self.store.persist(laterassets,token=nexttoken,proofs={},state=self.state)
  self.state.accept(nexttoken,smoke={'storage_verified':True,'stage':'qa','publication':laterpub,'source_sha256':'sha256:'+self.later.source_manifest_sha256,'result':'passed','observed_at':int(time.time())},accepted_at=int(time.time()))
  self.assertEqual(self.store.load_release('qa',self.state.read_accepted()['current']).origin.controller,self.verified.controller)
  self.assertEqual(len([r for r in self.state.data['attempts'] if r['status']=='accepted']),2)
  (root/'capsule.json').write_bytes(b'{}')
  with self.assertRaises(self.m.AssetError):self.store.load_release('qa',self.state.read_accepted()['current'])
 def test_initial_capsule_denies_tampering_wrong_successor_db_history_and_stage(self):
  root,capsule,publication,seal=self.initial_fixture()
  good=copy.deepcopy(capsule)
  # state_sha256 is no longer compared with the live ledger (see the relaxed
  # initial-origin tests below); it must still be present and hex-shaped.
  mutations=[lambda c:c.update(stage='prod'),lambda c:c.update(state_sha256='not-a-ledger-digest'),
    lambda c:c.pop('state_sha256'),
    lambda c:c.update(failed_record_sha256='f'*64),lambda c:c.update(baseline_sha256='f'*64),
    # successor publication/run id are informational now; commit and manifest bind.
    lambda c:c['successor']['controller'].update(commit='f'*40),lambda c:c['successor'].update(source_manifest_sha256='f'*64),
    lambda c:c['successor']['publication'].update(id='999'),lambda c:c['successor'].pop('publication'),
    lambda c:c['successor']['controller'].update(commit=c['origin_controller']['commit']),
    lambda c:c['database'].update(container_id='b'*64),
    lambda c:c.update(origin_source_manifest_sha256='f'*64)]
  for mutate in mutations:
   capsule.clear();capsule.update(copy.deepcopy(good));mutate(capsule);seal()
   with self.assertRaises(self.m.AssetError):self.store.initial_origin(self.state,self.successor,publication)
  # The failed origin build can never be presented as its own successor.
  capsule.clear();capsule.update(copy.deepcopy(good))
  capsule['successor'].update(controller=copy.deepcopy(capsule['origin_controller']),source_manifest_sha256=self.verified.source_manifest_sha256);seal()
  with self.assertRaises(self.m.AssetError):self.store.initial_origin(self.state,self.verified,publication)
  capsule.clear();capsule.update(good);seal();(root/'capsule.sha256').write_text('f'*64+'\n')
  with self.assertRaises(self.m.AssetError):self.store.initial_origin(self.state,self.successor,publication)
  seal();(root/'capsule.json').chmod(0o640)
  with self.assertRaises(self.m.AssetError):self.store.initial_origin(self.state,self.successor,publication)
  (root/'capsule.json').chmod(0o600)
  for stage in ('staging','prod'):
   self.assertIsNone(self.m.Store(stage,base=self.base).initial_origin(self.state,self.successor,publication))
 def test_initial_resume_executes_only_native_ledger_query_and_keeps_pending(self):
  root,capsule,publication,seal=self.initial_fixture()
  origin=self.store.initial_origin(self.state,self.successor,publication)[0]
  request={**self.request,'controller':self.successor.controller,'publication':publication,'deployment':{'run_id':790,'run_attempt':1,'job_id':902}}
  token=self.state.begin(request,source_sha256='sha256:'+self.successor.source_manifest_sha256,started_at=int(time.time()))
  import host_migrate
  units,_=self.m.migration_transport.application_inputs(origin)
  ledger={'database':'postgres','user':'postgres','in_recovery':False,'migration_count':40,
    'bootstrap':[{'environment':'qa','baseline_sha256':capsule['baseline_sha256'],'completed':True}],'migrations':units}
  with patch.object(host_migrate,'ROOT_UID',os.getuid()),patch.object(host_migrate.pg_container_bridge,'stage_environment',return_value={'XYNES_ENV':'qa','POSTGRES_PASSWORD':'inert'}),patch.object(host_migrate.pg_container_bridge,'inspected_container',return_value='a'*64),patch.object(host_migrate,'_execute',return_value=json.dumps(ledger).encode()) as execute:
   result=host_migrate.apply(self.successor,self.state,prior_verified=origin)
   self.assertFalse(result['schema_sql_applied']);self.assertEqual(result['migration_count'],40)
   execute.assert_called_once_with('a'*64,'inert')
  self.assertEqual(self.state.read_pending()['token'],token)
  self.assertIsNone(self.state.read_accepted()['current'])
  # The coordinator selects exactly the same retained Verified origin.
  selected=self.m.root_executor.origin_bundle(None,self.state,self.successor,publication)
  self.assertEqual(selected.controller,origin.controller)
 def test_extant_qa_capsule_does_not_block_other_stages_fresh_or_accepted(self):
  root,capsule,publication,seal=self.initial_fixture()
  for stage in ('staging','prod'):
   path=self.root/stage;path.mkdir(mode=0o700)
   with self.m.host_release_state.ReleaseState(path,stage=stage,owner_uid=os.getuid()) as state:
    self.assertIsNone(self.m.root_executor.origin_bundle(None,state,self.successor,publication))
    token=state.begin(self.request,source_sha256='sha256:'+self.verified.source_manifest_sha256,started_at=100)
    state.accept(token,smoke={'storage_verified':True,'stage':stage,'publication':self.request['publication'],'source_sha256':'sha256:'+self.verified.source_manifest_sha256,'result':'passed','observed_at':101},accepted_at=102)
    self.assertEqual(self.m.root_executor.origin_bundle(self.assets,state,self.successor,publication).controller,self.verified.controller)
 def test_known_failed_origin_with_missing_capsule_refuses_coordinator_before_begin(self):
  root,capsule,publication,seal=self.initial_fixture();root.rename(root.with_name('retained'))
  with self.assertRaises(self.m.AssetError):self.m.root_executor.origin_bundle(None,self.state,self.successor,publication)
  self.assertIsNone(self.state.read_pending())
 def test_unrelated_accepted_release_cannot_anchor_failed_origin(self):
  root,capsule,publication,seal=self.initial_fixture()
  request={**self.request,'controller':self.later.controller,'publication':publication,'deployment':{'run_id':790,'run_attempt':1,'job_id':902}}
  token=self.state.begin(request,source_sha256='sha256:'+self.later.source_manifest_sha256,started_at=100)
  self.state.accept(token,smoke={'storage_verified':True,'stage':'qa','publication':publication,'source_sha256':'sha256:'+self.later.source_manifest_sha256,'result':'passed','observed_at':101},accepted_at=102)
  with self.assertRaises(self.m.AssetError):self.store.initial_origin(self.state,self.successor,publication)
 def test_capsule_complete_origin_and_private_regular_files_are_required(self):
  root,capsule,publication,seal=self.initial_fixture()
  with self.assertRaises(self.m.AssetError):self.store.initial_origin(self.state,self.later,publication)
  file=root/next(iter(capsule['files']));raw=file.read_bytes()
  file.write_bytes(raw+b' ')
  with self.assertRaises(self.m.AssetError):self.store.initial_origin(self.state,self.successor,publication)
  file.write_bytes(raw);file.chmod(0o600)
  alias=self.root/'alias';os.link(file,alias)
  with self.assertRaises(self.m.AssetError):self.store.initial_origin(self.state,self.successor,publication)
  alias.unlink();extra=root/'unknown';extra.write_text('retained')
  with self.assertRaises(self.m.AssetError):self.store.initial_origin(self.state,self.successor,publication)
  self.assertEqual(extra.read_text(),'retained');extra.unlink()
  outside=self.root/'outside';outside.write_bytes(raw);outside.chmod(0o600);file.unlink();file.symlink_to(outside)
  with self.assertRaises(self.m.AssetError):self.store.initial_origin(self.state,self.successor,publication)
 def other_attempt(self,run,*,controller=None,source=None,publication=None):
  request={**self.request,'controller':controller or self.later.controller,
    'publication':publication or {'id':run,'digest':'sha256:'+'e'*64},'deployment':{'run_id':run,'run_attempt':1,'job_id':run+1000}}
  return request,self.state.begin(request,source_sha256='sha256:'+(source or self.later.source_manifest_sha256),started_at=int(time.time()))
 def successor_request(self,publication,run=790):
  return {**self.request,'controller':self.successor.controller,'publication':publication,'deployment':{'run_id':run,'run_attempt':1,'job_id':run+112}}
 def test_capsule_from_older_ledger_authorizes_after_only_failed_attempts_were_appended(self):
  root,capsule,publication,seal=self.initial_fixture()
  # Two later failed attempts (another controller, and the successor build under
  # another publication; the ledger never reuses a publication id) change the
  # ledger hash that this capsule recorded.
  _,token=self.other_attempt(800);self.state.fail(token,reason='runtime',failed_at=int(time.time()))
  _,token=self.other_attempt(795,controller=self.successor.controller,source=self.successor.source_manifest_sha256)
  self.state.fail(token,reason='migration',failed_at=int(time.time()))
  self.assertNotEqual(hashlib.sha256(self.m.host_release_state.encode(self.state.data)).hexdigest(),capsule['state_sha256'])
  self.assertEqual(self.store.initial_origin(self.state,self.successor,publication)[0].controller,self.verified.controller)
  self.assertEqual(self.m.root_executor.origin_bundle(None,self.state,self.successor,publication).controller,self.verified.controller)
  # The current request may be pending, as during migration and persistence.
  self.state.begin(self.successor_request(publication,796),source_sha256='sha256:'+self.successor.source_manifest_sha256,started_at=int(time.time()))
  self.assertEqual(self.store.initial_origin(self.state,self.successor,publication)[0].controller,self.verified.controller)
 def test_relaxed_initial_origin_refuses_accepted_foreign_pending_and_identity_drift(self):
  root,capsule,publication,seal=self.initial_fixture()
  def refused(successor=None,pub=None):
   with self.assertRaises(self.m.AssetError):self.store.initial_origin(self.state,successor or self.successor,pub or publication)
  # A pending attempt for any other request is not the current one.
  for controller,source,pub in ((self.later.controller,self.later.source_manifest_sha256,None),
      (self.successor.controller,self.successor.source_manifest_sha256,{'id':991,'digest':'sha256:'+'c'*64})):
   _,token=self.other_attempt(801 if pub is None else 802,controller=controller,source=source,publication=pub);refused()
   self.state.fail(token,reason='interrupted',failed_at=int(time.time()))
  self.assertIsNotNone(self.store.initial_origin(self.state,self.successor,publication))
  # Successor, publication and DB identity bindings are unchanged.
  refused(self.later)
  # With no pending attempt the publication is no longer bound (a fresh build has a new one).
  self.assertIsNotNone(self.store.initial_origin(self.state,self.successor,{'id':999,'digest':'sha256:'+'c'*64}))
  with patch.object(self.m,'initial_database',return_value={'container_id':'b'*64,'data_inode':[7,11],'data_uid':70,'data_mode':448}):refused()
  # Any accepted attempt ends the no-anchor bridge (an unrelated one cannot anchor).
  request,token=self.other_attempt(803)
  self.state.accept(token,smoke={'storage_verified':True,'stage':'qa','publication':request['publication'],'source_sha256':'sha256:'+self.later.source_manifest_sha256,'result':'passed','observed_at':int(time.time())},accepted_at=int(time.time()))
  refused()
 def test_relaxed_initial_origin_requires_the_exact_origin_failure_once_and_empty_pointers(self):
  root,capsule,publication,seal=self.initial_fixture()
  good=copy.deepcopy(self.state.data)
  def refused(data):
   self.state.data=data
   with self.assertRaises(self.m.AssetError):self.store.initial_origin(self.state,self.successor,publication)
  with patch.object(self.state,'_read',side_effect=lambda *args:self.state.data):
   missing=copy.deepcopy(good);missing['attempts']=[];refused(missing)
   changed=copy.deepcopy(good);changed['attempts'][0]['failure_reason']='smoke';refused(changed)
   duplicate=copy.deepcopy(good);duplicate['attempts'].append(copy.deepcopy(good['attempts'][0]));refused(duplicate)
   for key in ('current','previous'):
    pointer=copy.deepcopy(good);pointer[key]=good['attempts'][0]['token'];refused(pointer)
   self.state.data=copy.deepcopy(good)
   self.assertIsNotNone(self.store.initial_origin(self.state,self.successor,publication))
 def rebuilt(self,name):
  # Same commit/manifest, new build run: only informational controller fields differ.
  build=self.rebuilds[name]
  self.assertEqual(build.controller['commit'],self.successor.controller['commit'])
  self.assertEqual(build.source_manifest_sha256,self.successor.source_manifest_sha256)
  return build
 def test_fresh_build_of_same_successor_commit_is_authorized_after_failed_successor_attempt(self):
  root,capsule,publication,seal=self.initial_fixture()
  token=self.state.begin(self.successor_request(publication),source_sha256='sha256:'+self.successor.source_manifest_sha256,started_at=int(time.time()))
  self.state.fail(token,reason='runtime',failed_at=int(time.time()))
  retry=self.rebuilt('retry');retrypub={'id':460,'digest':'sha256:'+'9'*64}
  self.assertEqual(self.store.initial_origin(self.state,retry,retrypub)[0].controller,self.verified.controller)
  self.assertEqual(self.m.root_executor.origin_bundle(None,self.state,retry,retrypub).controller,self.verified.controller)
  request={**self.successor_request(retrypub,797),'controller':retry.controller}
  token=self.state.begin(request,source_sha256='sha256:'+retry.source_manifest_sha256,started_at=int(time.time()))
  result=self.store.initial_origin(self.state,retry,retrypub)
  self.assertEqual(result[1]['capsule_sha256'],hashlib.sha256((root/'capsule.json').read_bytes()).hexdigest())
  # Pending binding is still exact: another publication or run of the same commit is not this request.
  with self.assertRaises(self.m.AssetError):self.store.initial_origin(self.state,retry,publication)
  with self.assertRaises(self.m.AssetError):self.store.initial_origin(self.state,self.rebuilt('second'),retrypub)
  assets=self.m.replace(self.assets,migration=retry,controller=retry.controller,publication=retrypub,
    manifest=json.loads((self.successor.directory/'source-manifest.json').read_bytes()),origin=result[0])
  restored=self.store.persist(assets,token=token,proofs={},state=self.state)
  self.assertEqual(json.loads((self.base/token/'asset-record.json').read_bytes())['proofs']['initial_baseline_origin'],result[1])
  self.state.accept(token,smoke={'storage_verified':True,'stage':'qa','publication':retrypub,'source_sha256':'sha256:'+retry.source_manifest_sha256,'result':'passed','observed_at':int(time.time())},accepted_at=int(time.time()))
  # The accepted retry is the single anchor for the later lineage.
  with patch.object(self.m,'initial_database',side_effect=AssertionError('anchored lineage must not reread the DB')):
   self.assertEqual(self.store.initial_origin(self.state,self.later,{'id':461,'digest':'sha256:'+'8'*64},expected_prior=restored.origin)[0].controller,self.verified.controller)
  # A second accepted build of the successor commit does not lock QA out;
  # the earliest accepted one stays the anchor.
  second=self.rebuilt('second');secondpub={'id':462,'digest':'sha256:'+'7'*64}
  request={**self.successor_request(secondpub,798),'controller':second.controller}
  secondtoken=self.state.begin(request,source_sha256='sha256:'+second.source_manifest_sha256,started_at=int(time.time()))
  self.store.persist(self.m.replace(assets,migration=second,controller=second.controller,publication=secondpub),token=secondtoken,proofs={},state=self.state)
  self.state.accept(secondtoken,smoke={'storage_verified':True,'stage':'qa','publication':secondpub,'source_sha256':'sha256:'+second.source_manifest_sha256,'result':'passed','observed_at':int(time.time())},accepted_at=int(time.time()))
  used=[];native_original=self.store.original
  def original(record):used.append(record['token']);return native_original(record)
  with patch.object(self.store,'original',side_effect=original):
   self.assertEqual(self.store.initial_origin(self.state,self.later,{'id':463,'digest':'sha256:'+'6'*64},expected_prior=restored.origin)[0].controller,self.verified.controller)
  self.assertEqual(used,[token])
  # A bad earliest anchor refuses even though the later accepted build is valid: no fallback.
  record=self.base/token/'asset-record.json';good=record.read_bytes()
  for change in (lambda m:m['proofs'].pop('initial_baseline_origin'),lambda m:m['proofs']['initial_baseline_origin'].update(capsule_sha256='0'*64)):
   meta=json.loads(good);change(meta);record.write_bytes(self.m.encode(meta))
   with self.assertRaises(self.m.AssetError):self.store.initial_origin(self.state,self.later,{'id':463,'digest':'sha256:'+'6'*64},expected_prior=restored.origin)
  record.write_bytes(good)
  self.assertIsNotNone(self.store.load_release('qa',self.state.read_accepted()['current']))
 def test_accepted_anchor_must_carry_the_capsule_proof(self):
  root,capsule,publication,seal=self.initial_fixture()
  retry=self.rebuilt('proof');retrypub={'id':464,'digest':'sha256:'+'5'*64}
  request={**self.successor_request(retrypub,799),'controller':retry.controller}
  token=self.state.begin(request,source_sha256='sha256:'+retry.source_manifest_sha256,started_at=int(time.time()))
  origin=self.store.initial_origin(self.state,retry,retrypub)[0]
  assets=self.m.replace(self.assets,migration=retry,controller=retry.controller,publication=retrypub,
    manifest=json.loads((self.successor.directory/'source-manifest.json').read_bytes()),origin=origin)
  restored=self.store.persist(assets,token=token,proofs={},state=self.state)
  self.state.accept(token,smoke={'storage_verified':True,'stage':'qa','publication':retrypub,'source_sha256':'sha256:'+retry.source_manifest_sha256,'result':'passed','observed_at':int(time.time())},accepted_at=int(time.time()))
  record=self.base/token/'asset-record.json';good=record.read_bytes()
  # A changed proof (also refused by _load) or an absent one (only the anchor check refuses it).
  for change in (lambda m:m['proofs']['initial_baseline_origin'].update(capsule_sha256='0'*64),lambda m:m['proofs'].pop('initial_baseline_origin')):
   meta=json.loads(good);change(meta);record.write_bytes(self.m.encode(meta))
   with self.assertRaises(self.m.AssetError):self.store.initial_origin(self.state,self.later,{'id':465,'digest':'sha256:'+'4'*64},expected_prior=restored.origin)
  record.write_bytes(good)
  self.assertIsNotNone(self.store.initial_origin(self.state,self.later,{'id':465,'digest':'sha256:'+'4'*64},expected_prior=restored.origin))
 def test_accepted_release_without_matching_successor_anchor_refuses(self):
  root,capsule,publication,seal=self.initial_fixture()
  request,token=self.other_attempt(805)
  self.state.accept(token,smoke={'storage_verified':True,'stage':'qa','publication':request['publication'],'source_sha256':'sha256:'+self.later.source_manifest_sha256,'result':'passed','observed_at':int(time.time())},accepted_at=int(time.time()))
  for current,pub in ((self.successor,publication),(self.later,{'id':806,'digest':'sha256:'+'3'*64}),(self.rebuilds['retry'],{'id':807,'digest':'sha256:'+'2'*64})):
   with self.assertRaises(self.m.AssetError):self.store.initial_origin(self.state,current,pub)
   with self.assertRaises(self.m.AssetError):self.store.initial_origin(self.state,current,pub,expected_prior=self.verified)
 def test_successor_identity_is_repository_workflow_commit_first_attempt_and_manifest(self):
  root,capsule,publication,seal=self.initial_fixture()
  successor=capsule['successor'];controller=successor['controller'];source=successor['source_manifest_sha256']
  build=self.m.successor_build
  self.assertTrue(build({**controller,'run_id':controller['run_id']+5},source,successor))
  for change in ({'commit':'f'*40},{'repository':'Xynes-Studio/other'},{'workflow':'other.yml'},{'run_attempt':2},
                 {'run_attempt':True},{'commit':None}):
   self.assertFalse(build({**controller,**change},source,successor),change)
  self.assertFalse(build(controller,'f'*64,successor));self.assertFalse(build(None,source,successor))
  # Integrated: a different commit or a different source manifest of the same commit refuses.
  with self.assertRaises(self.m.AssetError):self.store.initial_origin(self.state,self.later,publication)
  with patch.object(self.m,'successor_build',side_effect=lambda c,s,x:build(c,'f'*64,x)):
   with self.assertRaises(self.m.AssetError):self.store.initial_origin(self.state,self.successor,publication)
  # run_attempt 2 is refused above at unit level; the transport and the ledger's
  # request parser already refuse it, so no integrated Verified/record can carry it.
  with self.assertRaises(self.m.host_release_state.StateError):
   self.state.begin({**self.successor_request(publication,804),'controller':{**self.successor.controller,'run_attempt':2}},
                    source_sha256='sha256:'+self.successor.source_manifest_sha256,started_at=int(time.time()))
 def test_missing_capsule_never_adopts_unaccepted_origin(self):
  root=self.root/'absent';patcher=patch.object(self.m,'INITIAL_BASELINE_ROOT',root);patcher.start();self.addCleanup(patcher.stop)
  self.assertIsNone(self.store.initial_origin(self.state,self.successor,{'id':457,'digest':'sha256:'+'c'*64}))
if __name__=='__main__':unittest.main()
