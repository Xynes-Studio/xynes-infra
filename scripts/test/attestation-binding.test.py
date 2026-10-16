#!/usr/bin/env python3
"""Native verifier result shape fixtures; these tests do not supply signatures."""
import copy,importlib.util,json
from pathlib import Path
import sys,unittest
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/release'))
import admission

class Tests(unittest.TestCase):
 def setUp(self):
  path=ROOT/'scripts/release/attestation_binding.py';self.assertTrue(path.is_file(),'native attestation binding is missing')
  spec=importlib.util.spec_from_file_location('attestation_binding',path);self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
  self.controller={'repository':admission.REPOSITORY,'commit':'a'*40,'workflow':'apex-build.yml','run_id':100,'run_attempt':1}
  self.name='ghcr.io/xynes-studio/apex/xynes-gateway';self.digest='b'*64;self.image=self.name+'@sha256:'+self.digest
  repo='https://github.com/'+admission.REPOSITORY;workflow=repo+'/'+admission.WORKFLOW+'@refs/heads/main';invocation=repo+'/actions/runs/100/attempts/1'
  cert={'issuer':'https://token.actions.githubusercontent.com','subjectAlternativeName':workflow,'buildSignerURI':workflow,'buildSignerDigest':'a'*40,'sourceRepositoryURI':repo,'sourceRepositoryDigest':'a'*40,'sourceRepositoryRef':'refs/heads/main','sourceRepositoryIdentifier':str(admission.REPOSITORY_ID),'sourceRepositoryOwnerURI':'https://github.com/Xynes-Studio','runnerEnvironment':'github-hosted','buildConfigURI':workflow,'buildConfigDigest':'a'*40,'buildTrigger':'workflow_dispatch','runInvocationURI':invocation,'sourceRepositoryVisibilityAtSigning':'public'}
  statement={'_type':'https://in-toto.io/Statement/v1','subject':[{'name':self.name,'digest':{'sha256':self.digest}}],'predicateType':'https://slsa.dev/provenance/v1','predicate':{'buildDefinition':{'buildType':'https://actions.github.io/buildtypes/workflow/v1','externalParameters':{'workflow':{'path':admission.WORKFLOW,'ref':'refs/heads/main','repository':repo}},'internalParameters':{'github':{'event_name':'workflow_dispatch','repository_id':str(admission.REPOSITORY_ID),'runner_environment':'github-hosted'}},'resolvedDependencies':[{'digest':{'gitCommit':'a'*40},'uri':'git+'+repo+'@refs/heads/main'}]},'runDetails':{'builder':{'id':workflow},'metadata':{'invocationId':invocation}}}}
  self.result={'verificationResult':{'mediaType':'application/vnd.dev.sigstore.verificationresult+json;version=0.1','signature':{'certificate':cert},'statement':statement,'verifiedTimestamps':[{'type':'Tlog','uri':'https://rekor.sigstore.dev','timestamp':'2026-10-13T00:00:00Z'}]}}
 def qualify(self,result=None):return self.module.qualify_native_result(json.dumps([self.result if result is None else result]).encode(),self.image,self.controller)
 def refused(self,result):
  with self.assertRaises(self.module.AttestationError) as c:self.qualify(result)
  self.assertEqual(str(c.exception),'Attestation verification refused')
 def test_exact_flags_bind_the_immutable_subject_and_protected_workflow(self):
  args=self.module.verification_args(self.image,self.controller)
  self.assertEqual(args[:3],['attestation','verify','oci://'+self.image])
  for flag,value in {'--repo':admission.REPOSITORY,'--hostname':'github.com','--source-ref':'refs/heads/main','--source-digest':'a'*40,'--signer-digest':'a'*40,'--cert-identity':'https://github.com/'+admission.REPOSITORY+'/'+admission.WORKFLOW+'@refs/heads/main','--cert-oidc-issuer':'https://token.actions.githubusercontent.com','--predicate-type':'https://slsa.dev/provenance/v1','--digest-alg':'sha256','--format':'json'}.items():self.assertEqual(args[args.index(flag)+1],value)
  self.assertIn('--deny-self-hosted-runners',args);self.assertNotIn('--custom-trusted-root',args)
 def test_actual_native_shape_is_bound_without_granting_deployment_authority(self):
  proof=self.qualify();self.assertTrue(proof['bindings_verified']);self.assertFalse(proof['deployable'])
  self.assertEqual(proof['image'],self.image);self.assertEqual(proof['controller'],self.controller)
 def test_certificate_cannot_change_repository_workflow_commit_runner_or_run_attempt(self):
  for key,value in [('issuer','https://foreign.invalid'),('subjectAlternativeName','https://github.com/other/repo/workflow'),('buildSignerDigest','c'*40),('sourceRepositoryDigest','c'*40),('sourceRepositoryRef','refs/heads/develop'),('sourceRepositoryIdentifier','1113600873'),('runnerEnvironment','self-hosted'),('buildTrigger','pull_request'),('runInvocationURI','https://github.com/'+admission.REPOSITORY+'/actions/runs/100/attempts/2'),('sourceRepositoryVisibilityAtSigning','private')]:
   result=copy.deepcopy(self.result);result['verificationResult']['signature']['certificate'][key]=value;self.refused(result)
 def test_statement_subject_workflow_dependencies_and_invocation_are_bound(self):
  for mode in ('subject','digest','extra_subject','workflow','event','repository','commit','builder','invocation'):
   result=copy.deepcopy(self.result);s=result['verificationResult']['statement'];d=s['predicate']['buildDefinition']
   if mode=='subject':s['subject'][0]['name']='ghcr.io/other/app'
   elif mode=='digest':s['subject'][0]['digest']['sha256']='c'*64
   elif mode=='extra_subject':s['subject'].append(copy.deepcopy(s['subject'][0]))
   elif mode=='workflow':d['externalParameters']['workflow']['path']='.github/workflows/other.yml'
   elif mode=='event':d['internalParameters']['github']['event_name']='pull_request'
   elif mode=='repository':d['internalParameters']['github']['repository_id']='1113600873'
   elif mode=='commit':d['resolvedDependencies'][0]['digest']['gitCommit']='c'*40
   elif mode=='builder':s['predicate']['runDetails']['builder']['id']='foreign'
   else:s['predicate']['runDetails']['metadata']['invocationId']='foreign'
   self.refused(result)
 def test_absent_or_fabricated_result_and_timestamp_fields_are_refused(self):
  for mode in ('signature','certificate','statement','timestamps'):
   result=copy.deepcopy(self.result);v=result['verificationResult']
   if mode=='signature':v.pop('signature')
   elif mode=='certificate':v['signature'].pop('certificate')
   elif mode=='statement':v.pop('statement')
   else:v['verifiedTimestamps']=[]
   self.refused(result)
  for raw in (b'[]',b'{"verified":true}',b'not-json',b'X'*262145):
   with self.assertRaises(self.module.AttestationError):self.module.qualify_native_result(raw,self.image,self.controller)
 def test_foreign_tags_shell_arguments_and_retries_are_rejected_before_verification(self):
  for image in ('ghcr.io/other/app@sha256:'+self.digest,self.name+':latest',self.image+';echo-leak','file:///tmp/artifact'):
   with self.assertRaises(self.module.AttestationError):self.module.verification_args(image,self.controller)
  controller=copy.deepcopy(self.controller);controller['run_attempt']=2
  with self.assertRaises(self.module.AttestationError):self.module.verification_args(self.image,controller)

if __name__=='__main__':unittest.main()
