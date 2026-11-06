"""Owned smoke provider fixture tests; no provider credentials or network."""
import copy,json,pathlib,sys,unittest,uuid
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/release'))
import host_smoke_setup as m
class Store:
 def __init__(self,journal):self.journal=journal;self.reads=[]
 def __enter__(self):return self
 def __exit__(self,*a):pass
 def read(self,name):self.reads.append(name);return self.journal
class Tests(unittest.TestCase):
 def setUp(self):
  self.stage='staging';self.j=m.new_journal(self.stage)
  for r in m.ROLES:self.j['accounts'][r].update(user_id=str(uuid.uuid4()),workspace_id=str(uuid.uuid4()),user_intent=True,workspace_intent=True)
  a=self.j['accounts'];self.setup={'stage':self.stage,'configured':True,'user_id':a['primary']['user_id'],'workspace_id':a['primary']['workspace_id'],'forbidden_workspace_id':a['secondary']['workspace_id']}
  self.values={'R2_ACCOUNT_ID':'a'*32,'R2_BUCKET':'test-stage-bucket','R2_REGION':'auto'};self.store=Store(self.j);self.calls=[]
 def execute(self,stage,sql):self.calls.append((stage,sql));return (json.dumps({'stage':stage,'storage_provider_ready':True})+'\n').encode()
 def run_it(self,**kwargs):return m.ensure_storage_provider(self.stage,self.setup,store=self.store,values_loader=lambda stage:self.values,executor=self.execute,**kwargs)
 def test_success_emits_only_ready_flag(self):
  before=copy.deepcopy(self.j);self.assertEqual(self.run_it(),{'stage':'staging','storage_provider_ready':True});self.assertEqual(self.j,before);self.assertEqual(len(self.calls),1)
 def test_qa_has_no_store_env_or_db_effect(self):
  def fail(*a):self.fail('QA provider hook performed an effect')
  with patch.object(m,'Store',side_effect=fail):self.assertEqual(m.ensure_storage_provider('qa',{},values_loader=fail,executor=fail),{'stage':'qa','storage_provider_ready':False})
 def test_foreign_setup_workspace_refused_before_execute(self):
  self.setup['workspace_id']=self.j['accounts']['secondary']['workspace_id']
  with self.assertRaises(m.SetupError):self.run_it()
  self.assertEqual(self.calls,[])
 def test_unowned_or_malformed_journal_refused_before_execute(self):
  self.j['accounts']['primary']['workspace_intent']=False
  with self.assertRaises(m.SetupError):self.run_it()
  self.assertEqual(self.calls,[])
 def test_bad_metadata_and_extra_secret_field_refused_before_execute(self):
  for change in [{'R2_ACCOUNT_ID':'account/not-valid'},{'R2_BUCKET':"bucket'; DROP TABLE x;--"},{'R2_REGION':'other'},{'STORAGE_SECRET_ACCESS_KEY':'INERT-DO-NOT-LEAK'}]:
   old=self.values;self.values={**old,**change}
   with self.assertRaises(m.SetupError):self.run_it()
   self.values=old
  self.assertEqual(self.calls,[])
 def test_db_failure_is_redacted(self):
  def execute(*a):raise ValueError('INERT-PRIVATE-DB-ERROR')
  with self.assertRaises(m.SetupError) as error:m.ensure_storage_provider(self.stage,self.setup,store=self.store,values_loader=lambda s:self.values,executor=execute)
  self.assertNotIn('INERT-PRIVATE',str(error.exception))
 def test_false_db_readback_cannot_claim_ready(self):
  def execute(stage,sql):return b'{"stage":"staging","storage_provider_ready":false}'
  with self.assertRaises(m.SetupError):m.ensure_storage_provider(self.stage,self.setup,store=self.store,values_loader=lambda s:self.values,executor=execute)
class RuntimeTests(unittest.TestCase):
 def runtime(self,stage,provider_effect=None):
  import host_runtime,host_smoke,host_seed
  from types import SimpleNamespace
  events=[];assets=SimpleNamespace(prepared=SimpleNamespace(stage=stage),migration='inert-immutable-migration')
  patches=[patch.object(host_runtime.host_controls,'Controls'),patch.object(host_runtime.host_assets,'Store'),patch.object(m,'Setup'),patch.object(m,'ensure_storage_provider'),patch.object(host_smoke,'Smoke'),patch.object(host_seed,'Driver')]
  controls,store,setup,provider,smoke,seeder=[p.start() for p in patches]
  for p in patches:self.addCleanup(p.stop)
  result={'stage':stage,'configured':True}
  setup.return_value.run.side_effect=lambda:events.append('setup') or result
  provider.side_effect=provider_effect or (lambda *a:events.append('provider') or {'stage':stage,'storage_provider_ready':True})
  seeder.return_value.run.side_effect=lambda *a,**k:events.append('seed') or {'stage':stage,'seeds_applied':True}
  smoke.return_value.run.side_effect=lambda *a,**k:events.append('smoke') or {'fixture':True}
  return host_runtime.Runtime(stage).operations(),assets,events,provider,result,host_runtime
 def test_staging_and_prod_require_provider_after_setup_before_product(self):
  for stage in ('staging','prod'):
   ops,assets,events,provider,setup,runtime=self.runtime(stage)
   for phase in ('current','rollback'):
    events.clear();self.assertEqual(ops.product_smoke(assets,phase=phase),{'fixture':True});self.assertEqual(events,['setup','provider','seed','smoke']);provider.assert_called_with(stage,setup)
 def test_failed_provider_prevents_seed_and_product(self):
  for effect in [lambda *a: {'stage':'staging','storage_provider_ready':False},lambda *a: (_ for _ in ()).throw(ValueError('INERT-PRIVATE-ERROR'))]:
   ops,assets,events,provider,setup,runtime=self.runtime('staging',effect)
   with self.assertRaises(runtime.RuntimeError) as error:ops.product_smoke(assets,phase='current')
   self.assertEqual(events,['setup']);self.assertNotIn('INERT-PRIVATE',str(error.exception))
 def test_qa_never_calls_provider_hook(self):
  ops,assets,events,provider,setup,runtime=self.runtime('qa');ops.product_smoke(assets,phase='current');provider.assert_not_called();self.assertEqual(events,['setup','seed','smoke'])
if __name__=='__main__':unittest.main()
