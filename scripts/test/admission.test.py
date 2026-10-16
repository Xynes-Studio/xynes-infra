#!/usr/bin/env python3
"""Official REST-shaped fixtures; no network or authorization side effects."""
import base64
import copy
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'scripts/release'))
TOOL = ROOT/'scripts/release/admission.py'
REPO = 'Xynes-Studio/xynes-infra'; P = '/repos/'+REPO
SHA = 'a'*40; NAMES = ['lint','test','coverage','build','typecheck','security']

def file_record(path, value):
 data=json.dumps(value).encode()
 return dict(type='file',path=path,encoding='base64',content=base64.b64encode(data).decode(),size=len(data),sha=hashlib.sha1(f'blob {len(data)}\0'.encode()+data).hexdigest())

def fixtures(manifest=None):
 repository=dict(id=1415400917,full_name=REPO,default_branch='main',private=False,fork=False)
 actor=dict(id=7,login='archan96',type='User'); reviewer=dict(id=8,login='Aman9804',type='User')
 run=dict(id=100,run_attempt=1,workflow_id=10,path='.github/workflows/apex-build.yml',head_sha=SHA,head_branch='main',event='workflow_dispatch',status='in_progress',conclusion=None,repository=repository,head_repository=repository,actor=actor,triggering_actor=actor)
 d={P:repository,'/users/Aman9804':reviewer,P+'/branches/main':dict(name='main',protected=True,commit=dict(sha=SHA)),P+'/actions/runs/100':run,P+'/actions/workflows/10':dict(id=10,path=run['path'],state='active'),P+'/actions/jobs/200':dict(id=200,name='publish-images',head_sha=SHA,head_branch='main',run_id=100,run_attempt=1,status='in_progress',conclusion=None,run_url='https://api.github.com'+P+'/actions/runs/100')}
 d[P+'/contents/.github/workflows/apex-build.yml?ref='+SHA]=file_record(run['path'],{'on':{'workflow_dispatch':{}},'jobs':{'admission':{'permissions':{'contents':'read'}},'build-images':{'permissions':{'contents':'read'}},'publish-images':{'name':'publish-images','environment':'release','needs':['admission','build-images'],'permissions':{'contents':'read','actions':'read','packages':'write'}}}})
 d[P+'/branches/main/protection']=dict(required_status_checks=dict(strict=True,checks=[dict(context=n,app_id=15368) for n in NAMES]),enforce_admins=dict(enabled=True),required_pull_request_reviews=dict(dismiss_stale_reviews=True,require_code_owner_reviews=True,require_last_push_approval=True,required_approving_review_count=1,bypass_pull_request_allowances=dict(users=[],teams=[],apps=[])),required_conversation_resolution=dict(enabled=True),allow_force_pushes=dict(enabled=False),allow_deletions=dict(enabled=False))
 env=dict(id=300,name='release',url='https://api.github.com'+P+'/environments/release',can_admins_bypass=False,deployment_branch_policy=dict(protected_branches=False,custom_branch_policies=True),protection_rules=[dict(id=301,type='required_reviewers',prevent_self_review=True,reviewers=[dict(type='User',reviewer=reviewer)])])
 d[P+'/environments/release']=env
 d[P+'/environments/release/deployment-branch-policies?per_page=100&page=1']=dict(total_count=1,branch_policies=[dict(id=302,name='main',type='branch')])
 d[P+'/actions/runs/100/approvals']=[dict(state='approved',comment='Reviewed candidate.',environments=[dict(id=300,name='release',url=env['url'])],user=reviewer)]
 checks=[]
 for i,n in enumerate(NAMES):
  rid=400+i;jid=500+i;cid=600+i;wid=700+i;path='.github/workflows/'+('security.yml' if n=='security' else 'quality-gates.yml')
  checks.append(dict(id=cid,name=n,head_sha=SHA,app=dict(id=15368),status='completed',conclusion='success',details_url=f'https://github.com/{REPO}/actions/runs/{rid}/job/{jid}',check_suite=dict(id=800+i)))
  d[f'{P}/actions/jobs/{jid}']=dict(id=jid,name=n,head_sha=SHA,run_id=rid,run_attempt=1,status='completed',conclusion='success',check_run_url=f'https://api.github.com{P}/check-runs/{cid}')
  d[f'{P}/actions/runs/{rid}']=dict(id=rid,run_attempt=1,head_sha=SHA,path=path,workflow_id=wid,check_suite_id=800+i,repository=repository,head_repository=repository,status='completed',conclusion='success')
  d[f'{P}/actions/workflows/{wid}']=dict(id=wid,path=path,state='active')
 d[P+'/commits/'+SHA+'/check-runs?filter=all&per_page=100&page=1']=dict(total_count=6,check_runs=checks)
 if manifest is None:
  manifest=json.loads((ROOT/'infra/release/source-manifest.json').read_text());manifest.update(status='approved',approval_reference='https://github.com/'+REPO+'/pull/1')
 head='b'*40;tree='c'*40;path='infra/release/source-manifest.json'
 d[P+'/contents/'+path+'?ref='+SHA]=file_record(path,manifest)
 d[P+'/contents/'+path+'?ref='+head]=file_record(path,manifest)
 d[P+'/pulls/1']=dict(number=1,state='closed',draft=False,merged=True,merged_at='2026-10-15T02:00:00Z',merge_commit_sha=SHA,html_url='https://github.com/'+REPO+'/pull/1',user=actor,base=dict(ref='main',repo=repository),head=dict(sha=head,repo=repository))
 d[P+'/pulls/1/reviews?per_page=100&page=1']=[dict(id=901,state='APPROVED',commit_id=head,user=reviewer,submitted_at='2026-10-15T01:00:00Z')]
 for commit in (SHA,head):d[P+'/git/commits/'+commit]=dict(sha=commit,tree=dict(sha=tree))
 return d

class Tests(unittest.TestCase):
 def setUp(self):
  self.assertTrue(TOOL.is_file(),'remote admission validator is missing')
  spec=importlib.util.spec_from_file_location('admission',TOOL);self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
  self.data=fixtures();self.controller=dict(repository=REPO,commit=SHA,workflow='apex-build.yml',run_id=100,run_attempt=1)
 def verify(self):
  return self.module.validate_admission(self.controller,200,lambda path:copy.deepcopy(self.data[path]),json.loads)
 def refused(self):
  with self.assertRaises(self.module.AdmissionError):self.verify()
 def test_source_approval_binds_final_reviewed_head_and_same_tree_merge(self):
  proof=self.module.verify_source_approval(lambda path:copy.deepcopy(self.data[path]),SHA,{7})
  self.assertEqual(proof['head_commit'],'b'*40);self.assertEqual(proof['merge_commit'],SHA)
  self.assertEqual(proof['reviewer_id'],8);self.assertEqual(proof['review_id'],901)
 def test_native_publication_refuses_unmerged_dismissed_wrong_head_and_issue_reference(self):
  for mode in ('unmerged','wrong-base','wrong-repo','wrong-merge','new-head','different-tree','dismissed','wrong-review-head','self','late-review','issue-ref','changed-manifest'):
   self.data=fixtures();pr=self.data[P+'/pulls/1'];review=self.data[P+'/pulls/1/reviews?per_page=100&page=1'][0]
   if mode=='unmerged':pr['merged']=False
   elif mode=='wrong-base':pr['base']['ref']='develop'
   elif mode=='wrong-repo':pr['head']['repo']={**pr['head']['repo'],'id':1}
   elif mode=='wrong-merge':pr['merge_commit_sha']='d'*40
   elif mode=='new-head':pr['head']['sha']='d'*40
   elif mode=='different-tree':self.data[P+'/git/commits/'+SHA]['tree']['sha']='d'*40
   elif mode=='dismissed':review['state']='DISMISSED'
   elif mode=='wrong-review-head':review['commit_id']='d'*40
   elif mode=='self':pr['user']=review['user']
   elif mode=='late-review':review['submitted_at']='2026-10-15T03:00:00Z'
   elif mode=='issue-ref':
    manifest=json.loads(base64.b64decode(self.data[P+'/contents/infra/release/source-manifest.json?ref='+SHA]['content']));manifest['approval_reference']='https://github.com/'+REPO+'/issues/1';self.data[P+'/contents/infra/release/source-manifest.json?ref='+SHA]=file_record('infra/release/source-manifest.json',manifest)
   elif mode=='changed-manifest':self.data[P+'/contents/infra/release/source-manifest.json?ref='+('b'*40)]=file_record('infra/release/source-manifest.json',{'status':'approved'})
   with self.subTest(mode=mode):self.refused()
 def test_paginated_source_reviews_cannot_hide_later_revocation_or_duplicates(self):
  first=[dict(id=1000+i,state='COMMENTED',user=dict(id=20,login='Other',type='User')) for i in range(100)]
  self.data[P+'/pulls/1/reviews?per_page=100&page=1']=first
  decisive=dict(id=2000,state='APPROVED',commit_id='b'*40,user=self.data['/users/Aman9804'],submitted_at='2026-10-15T01:00:00Z')
  self.data[P+'/pulls/1/reviews?per_page=100&page=2']=[decisive]
  self.assertTrue(self.verify()['admitted'])
  self.data[P+'/pulls/1/reviews?per_page=100&page=2'].append({**decisive,'id':2001,'state':'DISMISSED'});self.refused()
  self.data[P+'/pulls/1/reviews?per_page=100&page=2']=[{**decisive,'id':first[0]['id']}];self.refused()
 def test_source_approval_final_readback_refuses_head_or_review_drift(self):
  for mode in ('head','merged','review'):
   self.data=fixtures();counts={}
   def read(path):
    counts[path]=counts.get(path,0)+1;value=copy.deepcopy(self.data[path])
    if counts[path]>1 and path==P+'/pulls/1':
     if mode=='head':value['head']['sha']='d'*40
     elif mode=='merged':value['merged']=False
    if counts[path]>1 and path==P+'/pulls/1/reviews?per_page=100&page=1' and mode=='review':value[0]['state']='DISMISSED'
    return value
   with self.subTest(mode=mode):
    with self.assertRaises(self.module.AdmissionError):self.module.verify_source_approval(read,SHA,{7})
 def test_source_approval_supports_same_tree_merge_squash_and_identical_head(self):
  self.assertTrue(self.verify()['admitted'])
  self.data[P+'/pulls/1']['head']['sha']=SHA;self.data[P+'/pulls/1/reviews?per_page=100&page=1'][0]['commit_id']=SHA
  self.assertTrue(self.verify()['admitted'])
 def test_exact_native_metadata_admits_first_attempt_only(self):
  result=self.verify();self.assertTrue(result['admitted']);self.assertEqual(result['reviewer_id'],8);self.assertEqual(result['run_attempt'],1)
 def test_wrong_repository_commit_run_attempt_and_job_refuse(self):
  for target,key,value in [('controller','repository','Other/repo'),('controller','commit','b'*40),('controller','run_id',101),('controller','run_attempt',2),('job','run_id',101),('job','run_attempt',2),('job','name','build-images'),('job','head_sha','b'*40),('run','repository',dict(id=1,full_name=REPO)),('run','event','pull_request'),('run','head_branch','develop')]:
   with self.subTest(target=target,key=key):
    self.data=fixtures();self.controller=dict(repository=REPO,commit=SHA,workflow='apex-build.yml',run_id=100,run_attempt=1)
    obj=self.controller if target=='controller' else self.data[P+'/actions/jobs/200' if target=='job' else P+'/actions/runs/100'];obj[key]=value;self.refused()
 def test_missing_or_self_approval_and_environment_drift_refuse(self):
  for mode in ['missing','self','other','rejected','bypass','self_allowed','no_reviewer','wide_branch','missing_bypass','source_env']:
   with self.subTest(mode=mode):
    self.data=fixtures();env=self.data[P+'/environments/release'];approvals=self.data[P+'/actions/runs/100/approvals']
    if mode=='missing':approvals.clear()
    elif mode=='self':self.data[P+'/actions/runs/100']['actor']=approvals[0]['user']
    elif mode=='other':approvals[0]['user']=dict(id=9,login='Other',type='User')
    elif mode=='rejected':approvals[0]['state']='rejected'
    elif mode=='bypass':env['can_admins_bypass']=True
    elif mode=='self_allowed':env['protection_rules'][0]['prevent_self_review']=False
    elif mode=='no_reviewer':env['protection_rules'][0]['reviewers']=[]
    elif mode=='wide_branch':self.data[P+'/environments/release/deployment-branch-policies?per_page=100&page=1']['branch_policies'][0]['name']='*'
    elif mode=='missing_bypass':del env['can_admins_bypass']
    elif mode=='source_env':self.data[P+'/contents/.github/workflows/apex-build.yml?ref='+SHA]=file_record('.github/workflows/apex-build.yml',{'on':{'workflow_dispatch':{}},'jobs':{'publish-images':{'environment':'other','name':'publish-images'}}})
    self.refused()
 def test_branch_review_policy_and_spoofed_producer_refuse(self):
  for mode in ['codeowner','lastpush','stale','admin','bypass','app','failure','workflow','attempt','job']:
   with self.subTest(mode=mode):
    self.data=fixtures();policy=self.data[P+'/branches/main/protection'];checks=self.data[P+'/commits/'+SHA+'/check-runs?filter=all&per_page=100&page=1']['check_runs']
    if mode in ['codeowner','lastpush','stale']:policy['required_pull_request_reviews'][{'codeowner':'require_code_owner_reviews','lastpush':'require_last_push_approval','stale':'dismiss_stale_reviews'}[mode]]=False
    elif mode=='admin':policy['enforce_admins']['enabled']=False
    elif mode=='bypass':policy['required_pull_request_reviews']['bypass_pull_request_allowances']['users']=[dict(id=7)]
    elif mode=='app':checks[0]['app']['id']=1
    elif mode=='failure':checks[0]['conclusion']='failure'
    elif mode=='workflow':self.data[P+'/actions/runs/400']['path']='.github/workflows/other.yml'
    elif mode=='attempt':self.data[P+'/actions/jobs/500']['run_attempt']=2
    elif mode=='job':self.data[P+'/actions/jobs/500']['check_run_url']='https://api.github.com'+P+'/check-runs/999'
    self.refused()
 def test_missing_remote_metadata_or_supplied_approval_boolean_refuse(self):
  del self.data[P+'/actions/workflows/10'];self.refused()
  self.data=fixtures();self.controller['approved']=True;self.refused()
 def test_unsupported_attempt_type_and_incomplete_api_pages_refuse(self):
  for mode in ['boolean_run_attempt','boolean_job_attempt','wrong_env_name','truncated_checks']:
   with self.subTest(mode=mode):
    self.data=fixtures()
    if mode=='boolean_run_attempt':self.data[P+'/actions/runs/100']['run_attempt']=True
    elif mode=='boolean_job_attempt':self.data[P+'/actions/jobs/200']['run_attempt']=True
    elif mode=='wrong_env_name':self.data[P+'/actions/runs/100/approvals'][0]['environments'][0]['name']='other'
    else:self.data[P+'/commits/'+SHA+'/check-runs?filter=all&per_page=100&page=1']['total_count']=7
    self.refused()
 def test_actual_rest_omission_needs_affirmative_graphql_no_bypass(self):
  del self.data[P+'/branches/main/protection']['required_pull_request_reviews']['bypass_pull_request_allowances']
  self.refused()
  self.assertIn('read_graphql',inspect.signature(self.module.validate_admission).parameters,'missing native no-bypass evidence reader')
  proof={'data':{'repository':{'databaseId':1415400917,'nameWithOwner':REPO,'ref':{'name':'main','prefix':'refs/heads/','target':{'oid':SHA},'branchProtectionRule':{'pattern':'main','bypassPullRequestAllowances':{'totalCount':0,'nodes':[],'pageInfo':{'hasNextPage':False}}}}}}}
  def verify(value):
   def query(text):
    self.assertTrue(text.startswith('query ApexMainNoBypass'))
    return copy.deepcopy(value)
   return self.module.validate_admission(self.controller,200,lambda path:copy.deepcopy(self.data[path]),json.loads,read_graphql=query)
  self.assertTrue(verify(proof)['admitted'])
  for mode in ['missing','count','errors','wrong_sha','wrong_repo','pagination','null_rule','null_data','null_proof']:
   with self.subTest(mode=mode):
    bad=copy.deepcopy(proof);repo=bad['data']['repository'];ref=repo['ref']
    if mode=='missing':del ref['branchProtectionRule']['bypassPullRequestAllowances']['totalCount']
    elif mode=='count':ref['branchProtectionRule']['bypassPullRequestAllowances']['totalCount']=1
    elif mode=='errors':bad['errors']=[{'message':'Access denied'}]
    elif mode=='wrong_sha':ref['target']['oid']='b'*40
    elif mode=='wrong_repo':repo['databaseId']=1
    elif mode=='pagination':ref['branchProtectionRule']['bypassPullRequestAllowances']['pageInfo']['hasNextPage']=True
    elif mode=='null_rule':ref['branchProtectionRule']=None
    elif mode=='null_data':bad['data']=None
    elif mode=='null_proof':bad=None
    with self.assertRaises(self.module.AdmissionError):verify(bad)
 def test_incidental_run_progress_is_accepted_but_protection_drift_refuses(self):
  for policy_drift in [False,True]:
   with self.subTest(policy_drift=policy_drift):
    reads={};self.data=fixtures()
    def read(path):
     reads[path]=reads.get(path,0)+1;result=copy.deepcopy(self.data[path])
     if path==P+'/actions/runs/100':result['updated_at']=f'2026-10-12T00:00:0{reads[path]}Z'
     if policy_drift and path==P+'/branches/main/protection' and reads[path]>1:result['enforce_admins']['enabled']=False
     return result
    if policy_drift:
     with self.assertRaises(self.module.AdmissionError):self.module.validate_admission(self.controller,200,read,json.loads)
    else:
     try:result=self.module.validate_admission(self.controller,200,read,json.loads)['admitted']
     except self.module.AdmissionError:result=False
     self.assertTrue(result,'incidental updated_at drift falsely refused a stable authorized run')
 def test_authorization_changes_during_final_readback_refuse(self):
  for mode in ['attempt','commit','actor','triggering_actor','status','main']:
   with self.subTest(mode=mode):
    self.data=fixtures();reads={}
    def read(path):
     reads[path]=reads.get(path,0)+1;value=copy.deepcopy(self.data[path])
     if reads[path]>1 and path==P+'/actions/runs/100':
      if mode=='attempt':value['run_attempt']=2
      elif mode=='commit':value['head_sha']='b'*40
      elif mode in ['actor','triggering_actor']:value[mode]=dict(id=9,login='Other',type='User')
      elif mode=='status':value['status']='completed'
     if reads[path]>1 and path==P+'/branches/main' and mode=='main':value['commit']['sha']='b'*40
     return value
    with self.assertRaises(self.module.AdmissionError):self.module.validate_admission(self.controller,200,read,json.loads)
 def test_job_environment_and_approval_changes_after_initial_evidence_refuse(self):
  for mode in ['job_cancelled','job_attempt','job_run','job_sha','job_name','environment_bypass','environment_reviewer','environment_branch','approval_removed','approval_rejected','approval_reviewer']:
   with self.subTest(mode=mode):
    self.data=fixtures();changed=False
    def read(path):
     nonlocal changed
     if '/check-runs?' in path and not changed:
      changed=True;job=self.data[P+'/actions/jobs/200'];env=self.data[P+'/environments/release'];approvals=self.data[P+'/actions/runs/100/approvals']
      if mode=='job_cancelled':job.update(status='completed',conclusion='cancelled')
      elif mode=='job_attempt':job['run_attempt']=2
      elif mode=='job_run':job['run_id']=999
      elif mode=='job_sha':job['head_sha']='b'*40
      elif mode=='job_name':job['name']='other-publisher'
      elif mode=='environment_bypass':env['can_admins_bypass']=True
      elif mode=='environment_reviewer':env['protection_rules'][0]['reviewers']=[]
      elif mode=='environment_branch':self.data[P+'/environments/release/deployment-branch-policies?per_page=100&page=1']['branch_policies'][0]['name']='*'
      elif mode=='approval_removed':approvals.clear()
      elif mode=='approval_rejected':approvals[0]['state']='rejected'
      elif mode=='approval_reviewer':approvals[0]['user']=dict(id=9,login='Other',type='User')
     return copy.deepcopy(self.data[path])
    with self.assertRaises(self.module.AdmissionError):self.module.validate_admission(self.controller,200,read,json.loads)
    self.assertTrue(changed,'regression must change native evidence after its initial read')


 def test_cancellation_and_withdrawal_during_source_review_reads_refuse(self):
  for mode in ('job','approval'):
   with self.subTest(mode=mode):
    self.data=fixtures();changed=False
    def read(path):
     nonlocal changed
     if '/pulls/1/reviews?' in path and not changed:
      changed=True
      if mode=='job':self.data[P+'/actions/jobs/200'].update(status='completed',conclusion='cancelled')
      else:self.data[P+'/actions/runs/100/approvals'].clear()
     return copy.deepcopy(self.data[path])
    with self.assertRaises(self.module.AdmissionError):self.module.validate_admission(self.controller,200,read,json.loads)
    self.assertTrue(changed)

if __name__=='__main__':unittest.main()
