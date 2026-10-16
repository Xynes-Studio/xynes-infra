#!/usr/bin/env python3
"""Private runtime composition fixtures; no hosted or native release acceptance."""
import importlib.util,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/release'))
MODULE=ROOT/'scripts/release/host_runtime.py'

class Tests(unittest.TestCase):
 def setUp(self):
  self.assertTrue(MODULE.is_file(),'concrete host runtime is missing')
  spec=importlib.util.spec_from_file_location('host_runtime',MODULE)
  self.m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=self.m;spec.loader.exec_module(self.m)
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name).resolve();self.root.chmod(0o700)
  self.config=self.m.Configuration(root=self.root,owner_uid=os.geteuid())
  for name in ('/etc/xynes/deploy','/var/lib/xynes/deploy/state','/var/lib/xynes/deploy/work'):
   path=self.config.path(name);path.mkdir(parents=True,mode=0o700)
  self.file=self.config.path('/etc/xynes/deploy/controller.env')
  self.file.write_text('RELEASE_POLICY_READ_TOKEN=github_pat_'+'a'*64+'\nGHCR_READ_TOKEN=ghp_'+'b'*40+'\n');self.file.chmod(0o600)
  self.request={'schema_version':1,'artifact_kind':'apex-deployment-request','controller':{'repository':'Xynes-Studio/xynes-infra','commit':'a'*40,'workflow':'apex-build.yml','run_id':123,'run_attempt':1},'publication':{'id':456,'digest':'sha256:'+'b'*64},'deployment':{'run_id':789,'run_attempt':1,'job_id':901}}
  self.raw=json.dumps(self.request).encode()

 def test_credentials_are_fixed_private_exact_and_never_repr_values(self):
  credentials=self.m.credentials(self.config)
  self.assertEqual(set(credentials.values),{'RELEASE_POLICY_READ_TOKEN','GHCR_READ_TOKEN'})
  self.assertNotIn('a'*64,repr(credentials));self.assertNotIn('b'*40,repr(credentials))
  self.file.chmod(0o640)
  with self.assertRaises(self.m.RuntimeError):self.m.credentials(self.config)
  self.file.chmod(0o600);self.file.write_text(self.file.read_text()+'UNRELATED_SECRET=sentinel\n')
  with self.assertRaises(self.m.RuntimeError):self.m.credentials(self.config)

 def test_duplicate_symlink_hardlink_and_untrusted_ancestor_refuse(self):
  original=self.file.read_text();self.file.write_text(original+'GHCR_READ_TOKEN=ghp_'+'c'*40+'\n')
  with self.assertRaises(self.m.RuntimeError):self.m.credentials(self.config)
  self.file.write_text(original);alias=self.root/'alias';os.link(self.file,alias)
  with self.assertRaises(self.m.RuntimeError):self.m.credentials(self.config)
  alias.unlink();self.file.parent.chmod(0o777)
  with self.assertRaises(self.m.RuntimeError):self.m.credentials(self.config)
  self.file.parent.chmod(0o700);self.file.unlink();alias.write_text(original);alias.chmod(0o600);self.file.symlink_to(alias)
  with self.assertRaises(self.m.RuntimeError):self.m.credentials(self.config)

 def test_only_package_metadata_uses_registry_credential(self):
  calls=[]
  class Reader:
   def __init__(self,token):self.token=token
   def read(self,path):calls.append((self.token,path));return {'fixture':True}
   def read_graphql(self,query):calls.append((self.token,query));return {}
  class Download:
   def __init__(self,token):self.token=token
   def download(self,*args):calls.append((self.token,args))
  with patch.object(self.m.github_read,'GitHubReadAdapter',Reader),patch.object(self.m.artifact_download,'ArtifactDownloader',Download),patch.object(self.m.native_attestation,'verify',return_value={'fixture':True}) as verify:
   native=self.m.native(self.config,self.m.credentials(self.config))
   native.read('/repos/Xynes-Studio/xynes-infra');native.read('/orgs/Xynes-Studio/packages/container/apex%2Fxynes-gateway')
   native.download(123,Path('/inert'),64);native.read_graphql('fixed')
   native.verify(image='inert',controller={'fixture':True})
   self.assertTrue(calls[0][0].startswith('github_pat_'));self.assertTrue(calls[1][0].startswith('ghp_'))
   self.assertTrue(calls[2][0].startswith('github_pat_'));self.assertTrue(calls[3][0].startswith('github_pat_'))
   self.assertEqual(verify.call_args.kwargs['binary'],self.config.path('/usr/local/lib/xynes/tools/gh/2.102.0/gh'))
   self.assertNotEqual(verify.call_args.kwargs['api_token'],verify.call_args.kwargs['registry_token'])

 def test_workflow_parser_refuses_duplicate_keys_and_non_json_data(self):
  self.assertEqual(self.m.workflow(b'{"jobs":{"deploy":{}}}'),{'jobs':{'deploy':{}}})
  for raw in (b'{"jobs":{},"jobs":{}}',b'jobs: {}',b'[]',b'{"value":NaN}'):
   with self.assertRaises(self.m.RuntimeError):self.m.workflow(raw)

 def test_attempt_uses_private_work_and_retains_failure_without_success_ack(self):
  sentinel='INERT_SECRET_SENTINEL';observed=[]
  runtime=self.m.Runtime('qa',configuration=self.config)
  def coordinator(raw,**kwargs):
   observed.append(kwargs['work']);self.assertEqual(raw,self.raw)
   self.assertEqual(kwargs['state_directory'],self.config.path('/var/lib/xynes/deploy/state'))
   self.assertEqual(kwargs['work'].stat().st_mode&0o777,0o700)
   self.assertEqual(os.environ,dict(self.m.CLEAN_ENV));self.assertEqual(os.umask(0o077),0o077)
   raise ValueError(sentinel)
  old=dict(os.environ);mask=os.umask(0o077);os.umask(mask)
  with patch.object(self.m,'native',return_value='fixture'),patch.object(runtime,'operations',return_value='fixture'),patch.object(self.m.root_executor,'run',coordinator):
   with self.assertRaises(self.m.RuntimeError) as error:runtime.execute(self.raw)
  self.assertNotIn(sentinel,str(error.exception));self.assertEqual(dict(os.environ),old)
  previous=os.umask(mask);self.assertEqual(previous,mask)
  self.assertEqual(len(observed),1);self.assertTrue(observed[0].is_dir())

 def test_success_requires_exact_binding_and_preserves_incomplete_work(self):
  runtime=self.m.Runtime('qa',configuration=self.config);result={'artifact_kind':'root-deployment-result','stage':'qa','token':'c'*64,'controller':self.request['controller'],'publication':self.request['publication'],'source_manifest_sha256':'d'*64,'deployment_accepted':True}
  with patch.object(self.m,'native',return_value='fixture'),patch.object(runtime,'operations',return_value='fixture'),patch.object(self.m.root_executor,'run',return_value=result):
   self.assertEqual(runtime.execute(self.raw),result)
  result={**result,'stage':'prod'}
  with patch.object(self.m,'native',return_value='fixture'),patch.object(runtime,'operations',return_value='fixture'),patch.object(self.m.root_executor,'run',return_value=result):
   with self.assertRaises(self.m.RuntimeError):runtime.execute(self.raw)
  self.assertEqual(len(list(self.config.path('/var/lib/xynes/deploy/work').iterdir())),2)

 def test_invalid_input_is_refused_before_configuration_or_work_creation(self):
  runtime=self.m.Runtime('qa',configuration=self.config)
  for raw in (b'{}',self.raw+b' '+b'x'*4096):
   with patch.object(self.m,'credentials') as reader:
    with self.assertRaises(self.m.RuntimeError):runtime.execute(raw)
    reader.assert_not_called()
  self.assertEqual(list(self.config.path('/var/lib/xynes/deploy/work').iterdir()),[])

 def test_production_operations_bind_real_modules_and_held_backup_state(self):
  import host_smoke
  runtime=self.m.Runtime('qa')
  with patch.object(self.m.host_controls,'Controls') as controls,patch.object(self.m.host_assets,'Store') as store,patch.object(host_smoke,'Smoke') as smoke,patch.object(self.m.host_backup,'create',return_value={'fixture':True}) as backup:
   operations=runtime.operations()
   self.assertEqual(operations.target_audit,controls.return_value.target_audit)
   self.assertEqual(operations.warm,controls.return_value.warm)
   self.assertEqual(operations.seed_counts,controls.return_value.seed_counts)
   self.assertEqual(operations.persist,store.return_value.persist)
   self.assertEqual(operations.load_release,store.return_value.load_release)
   self.assertTrue(callable(operations.product_smoke))
   from types import SimpleNamespace
   state=SimpleNamespace(stage='qa');assets=SimpleNamespace(prepared=SimpleNamespace(stage='qa'))
   self.assertEqual(operations.backup('qa',phase='before-sql',current=assets,previous=None,state=state,token='inert'),{'fixture':True})
   backup.assert_called_once_with(state,'inert')
   with self.assertRaises(self.m.RuntimeError):operations.backup('prod',phase='before-sql',current=assets,previous=None,state=state,token='inert')

 def test_dedicated_fixture_setup_precedes_source_seeds_and_product_acceptance(self):
  import host_smoke,host_smoke_setup
  from types import SimpleNamespace
  events=[];assets=SimpleNamespace(prepared=SimpleNamespace(stage='qa'),migration='inert-immutable-migration')
  with patch.object(self.m.host_controls,'Controls'),patch.object(self.m.host_assets,'Store'),patch.object(host_smoke_setup,'Setup') as setup,patch.object(host_smoke,'Smoke') as smoke,patch.object(self.m.root_executor.host_seed,'Driver') as seeder:
   setup.return_value.run.side_effect=lambda:events.append('setup') or {'stage':'qa','configured':True}
   seeder.return_value.run.side_effect=lambda *args,**kwargs:events.append('seed') or {'stage':'qa','seeds_applied':True}
   smoke.return_value.run.side_effect=lambda *args,**kwargs:events.append('smoke') or {'fixture':'product observation'}
   operations=self.m.Runtime('qa').operations()
   for phase in ('current','rollback'):
    events.clear();self.assertEqual(operations.product_smoke(assets,phase=phase),{'fixture':'product observation'})
    self.assertEqual(events,['setup','seed','smoke'])
    seeder.return_value.run.assert_called_with(assets.prepared,assets.migration,work=Path('/var/lib/xynes/deploy/work'))
   setup.return_value.run.side_effect=ValueError('INERT_SECRET_SENTINEL');events.clear()
   with self.assertRaises(self.m.RuntimeError) as error:operations.product_smoke(assets,phase='current')
   self.assertNotIn('INERT_SECRET_SENTINEL',str(error.exception));self.assertEqual(events,[])

if __name__=='__main__':unittest.main()
