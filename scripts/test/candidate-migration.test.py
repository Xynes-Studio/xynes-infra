#!/usr/bin/env python3
"""Native data artifact -> real source-bound migration plan; no host SQL."""
import base64,copy,hashlib,importlib.util,io,json,os,sys,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/release'))
import deployment_candidate as candidate,migration_transport,artifact_handoff,admission

def load(name,path):
 spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module);return module

class Tests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.fixture=load('candidate_migration_fixture',ROOT/'scripts/test/migration-transport.test.py').Tests()
  cls.fixture.setUp();cls.manifest=cls.fixture.manifest_bytes;cls.controller=cls.fixture.controller
  cls.receipt=cls.fixture.create();cls.tar=cls.fixture.bundle.read_bytes()
  out=io.BytesIO()
  with zipfile.ZipFile(out,'w',compression=zipfile.ZIP_STORED) as archive:archive.writestr('migration-bundle.tar',cls.tar)
  cls.zip=out.getvalue()
 @classmethod
 def tearDownClass(cls):cls.fixture.doCleanups()
 def setUp(self):
  self.assertTrue(hasattr(candidate,'acquire_migration'),'native migration intake is missing')
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.work=Path(self.tmp.name).resolve();self.work.chmod(0o700)
  self.reference={'id':999,'digest':'sha256:'+hashlib.sha256(self.zip).hexdigest()}
  url='https://api.github.com'+artifact_handoff.PREFIX+'999'
  self.metadata={'id':999,'digest':self.reference['digest'],'name':f'apex-migration-{self.controller["run_id"]}-1','expired':False,'size_in_bytes':len(self.zip),'url':url,'archive_download_url':url+'/zip','workflow_run':{'id':self.controller['run_id'],'repository_id':admission.REPOSITORY_ID,'head_repository_id':admission.REPOSITORY_ID,'head_branch':'main','head_sha':self.controller['commit']}}
  repo={'id':admission.REPOSITORY_ID,'full_name':admission.REPOSITORY,'private':False,'fork':False,'default_branch':'main'}
  self.rows={admission.PREFIX:repo,admission.PREFIX+'/branches/main':{'name':'main','protected':True,'commit':{'sha':self.controller['commit']}},admission.PREFIX+f'/actions/runs/{self.controller["run_id"]}':{'repository':repo,'head_repository':repo,'id':self.controller['run_id'],'run_attempt':1,'path':admission.WORKFLOW,'event':'workflow_dispatch','head_sha':self.controller['commit'],'head_branch':'main','status':'completed','conclusion':'success','workflow_id':42},admission.PREFIX+'/actions/workflows/42':{'id':42,'path':admission.WORKFLOW,'state':'active'}}
  self.rows[admission.PREFIX+'/contents/'+candidate.SOURCE_PATH+'?ref='+self.controller['commit']]={'type':'file','path':candidate.SOURCE_PATH,'encoding':'base64','size':len(self.manifest),'sha':hashlib.sha1(f'blob {len(self.manifest)}\0'.encode()+self.manifest).hexdigest(),'content':base64.b64encode(self.manifest).decode()}
  self.rows[admission.PREFIX+f'/actions/runs/{self.controller["run_id"]}/artifacts?per_page=100&page=1']={'total_count':1,'artifacts':[self.metadata]}
  self.rows[artifact_handoff.PREFIX+'999']=self.metadata;self.downloaded=0
 def read(self,path):return copy.deepcopy(self.rows[path])
 def download(self,artifact,path,size):
  self.downloaded+=1;self.assertEqual(artifact,999);self.assertEqual(size,len(self.zip));path.write_bytes(self.zip)
 def acquire(self,**changes):
  values=dict(controller=self.controller,manifest_bytes=self.manifest,work=self.work,read=self.read,download=self.download);values.update(changes)
  return candidate.acquire_migration(**values)
 def refused(self,**changes):
  with self.assertRaises(candidate.CandidateError) as caught:self.acquire(**changes)
  self.assertEqual(str(caught.exception),candidate.ERROR)
 def test_native_zip_produces_exact_40_source_bound_units(self):
  verified=self.acquire();units,payloads=self.fixture.module.application_inputs(verified)
  self.assertEqual(len(units),40);self.assertEqual(len(payloads),40)
  self.assertEqual(verified.controller,self.controller);self.assertFalse(verified.deployable)
  self.assertEqual((verified.directory/'source-manifest.json').read_bytes(),self.manifest)
 def test_native_run_branch_and_source_drift_refuse_before_download(self):
  path=admission.PREFIX+f'/actions/runs/{self.controller["run_id"]}'
  self.rows[path]['conclusion']='failure';self.refused();self.assertEqual(self.downloaded,0)
  self.rows[path]['conclusion']='success';self.refused(manifest_bytes=self.manifest+b' ');self.assertEqual(self.downloaded,0)
 def test_missing_duplicate_or_foreign_migration_artifact_refuses(self):
  path=admission.PREFIX+f'/actions/runs/{self.controller["run_id"]}/artifacts?per_page=100&page=1'
  self.rows[path]={'total_count':0,'artifacts':[]};self.refused()
  other=copy.deepcopy(self.metadata);other['id']=1000
  self.rows[path]={'total_count':2,'artifacts':[self.metadata,other]};self.refused()
  self.rows[path]={'total_count':1,'artifacts':[self.metadata]};self.metadata['workflow_run']['head_repository_id']=1;self.refused()
 def test_complete_source_plan_is_revalidated_and_no_archive_source_executes(self):
  self.zip=self.zip[:-1]+b'x';self.refused()
  self.assertFalse((self.work/'migration').exists())
 def test_late_main_drift_never_returns_applicable_plan(self):
  count=0
  def read(path):
   nonlocal count
   result=self.read(path)
   if path.endswith('/branches/main'):
    count+=1
    if count>1:result['commit']['sha']='b'*40
   return result
  self.refused(read=read)

if __name__=='__main__':unittest.main()
