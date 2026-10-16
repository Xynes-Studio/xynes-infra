#!/usr/bin/env python3
"""Native metadata and inert key fixtures; SSH/process spies never connect."""
import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/release'))
P = '/repos/Xynes-Studio/xynes-infra'
SHA = 'a'*40
ERROR = 'Deployment dispatch refused'


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def string(value):
    return struct.pack('>I',len(value))+value


def inert_key():
    # Deliberately non-signing seed/public bytes, constructed at runtime only.
    public=b'p'*32
    blob=string(b'ssh-ed25519')+string(public)
    secret=struct.pack('>II',123,123)+string(b'ssh-ed25519')+string(public)+string(b's'*32+public)+string(b'inert-fixture')
    padding=8-len(secret)%8
    secret+=bytes(range(1,padding+1))
    raw=b'openssh-key-v1\0'+string(b'none')+string(b'none')+string(b'')+struct.pack('>I',1)+string(blob)+string(secret)
    return '-----BEGIN OPENSSH PRIVATE KEY-----\n'+base64.b64encode(raw).decode()+'\n-----END OPENSSH PRIVATE KEY-----\n', blob


class Tests(unittest.TestCase):
    def setUp(self):
        path=ROOT/'scripts/release/deploy_dispatch.py'
        self.assertTrue(path.is_file(),'protected deployment dispatch is missing')
        self.module=load('deploy_dispatch',path)
        fixture=load('native_fixture',ROOT/'scripts/test/admission.test.py')
        self.data=fixture.fixtures()
        self.tmp=tempfile.TemporaryDirectory(prefix='apex-dispatch-test-')
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve()
        self.key,self.public=inert_key()
        self.env={'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':'Xynes-Studio/xynes-infra',
            'GITHUB_REPOSITORY_ID':'1415400917','GITHUB_REF':'refs/heads/main','GITHUB_REF_PROTECTED':'true',
            'GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_RUN_ATTEMPT':'1','GITHUB_RUN_ID':'101',
            'GITHUB_SHA':SHA,'GITHUB_WORKFLOW_SHA':SHA,'GITHUB_JOB':'deploy-qa',
            'GITHUB_WORKFLOW_REF':'Xynes-Studio/xynes-infra/.github/workflows/apex-deploy.yml@refs/heads/main',
            'GITHUB_API_URL':'https://api.github.com','GITHUB_SERVER_URL':'https://github.com',
            'GITHUB_ACTOR':'archan96','GITHUB_ACTOR_ID':'7','GITHUB_TRIGGERING_ACTOR':'archan96',
            'GH_TOKEN':'ghs_INERT_FIXTURE_READ_ONLY_TOKEN_123456',
            'APEX_STAGE':'qa','APEX_BUILD_RUN_ID':'100','APEX_SSH_PRIVATE_KEY':self.key,
            'APEX_SSH_KNOWN_HOSTS':'84.247.176.134 ssh-ed25519 '+base64.b64encode(self.public).decode(),
            'RUNNER_TEMP':str(self.base)}
        self.data[P+'/actions/runs/100'].update(status='completed',conclusion='success')
        run=copy.deepcopy(self.data[P+'/actions/runs/100'])
        run.update(id=101,workflow_id=11,path='.github/workflows/apex-deploy.yml',status='in_progress',conclusion=None)
        self.data[P+'/actions/runs/101']=run
        self.data[P+'/actions/workflows/11']=dict(id=11,path=run['path'],state='active')
        job=copy.deepcopy(self.data[P+'/actions/jobs/200'])
        job.update(id=201,run_id=101,name='deploy-qa',run_url='https://api.github.com'+P+'/actions/runs/101')
        self.data[P+'/actions/jobs/201']=job
        self.data[P+'/actions/runs/101/attempts/1/jobs?per_page=100&page=1']=dict(total_count=1,jobs=[job])
        artifact=dict(id=99,name='apex-publication-100-1',digest='sha256:'+'b'*64,expired=False,
            size_in_bytes=123,url='https://api.github.com'+P+'/actions/artifacts/99',
            archive_download_url='https://api.github.com'+P+'/actions/artifacts/99/zip',
            workflow_run=dict(id=100,repository_id=1415400917,head_repository_id=1415400917,head_branch='main',head_sha=SHA))
        self.data[P+'/actions/artifacts/99']=artifact
        self.data[P+'/actions/runs/100/artifacts?per_page=100&page=1']=dict(total_count=1,artifacts=[artifact])
        self.calls=[]
        self.reads=[]
        self.paths=[]

    def read(self,path):
        self.reads.append(path)
        return copy.deepcopy(self.data[path])

    def process(self,argv,*,data,env,cwd,timeout_seconds,max_output):
        self.calls.append((argv,data,env,cwd,timeout_seconds,max_output))
        key=Path(argv[argv.index('-i')+1])
        binding=next(arg.split('=',1)[1] for arg in argv if arg.startswith('UserKnownHostsFile='))
        known=Path(binding)
        self.paths.extend((key,known,key.parent))
        self.assertEqual(key.stat().st_mode&0o777,0o600)
        self.assertEqual(known.stat().st_mode&0o777,0o600)
        self.assertEqual(key.parent.stat().st_mode&0o777,0o700)
        self.assertEqual(key.read_text(),self.key)
        self.assertEqual(known.read_text(),self.env['APEX_SSH_KNOWN_HOSTS']+'\n')
        account=argv[argv.index('-l')+1]
        stage=account.removeprefix('xynes_').removesuffix('_deploy')
        return (0,json.dumps(dict(artifact_kind='apex-ssh-entry-result',stage=stage,root_command_completed=True,deployable=False)).encode(),b'private diagnostic discarded')

    def invoke(self,**overrides):
        return self.module.run(self.env,read=self.read,process=overrides.get('process',self.process))

    def refused(self,**overrides):
        with self.assertRaises(self.module.DispatchError) as caught:self.invoke(**overrides)
        self.assertEqual(str(caught.exception),ERROR)

    def test_native_ids_bound_request_and_private_ssh_files_are_owned_and_removed(self):
        result=self.invoke()
        argv,data,env,cwd,timeout,limit=self.calls[0]
        self.assertEqual(argv[0],'/usr/bin/ssh')
        self.assertEqual(argv[-2:],['84.247.176.134','apex-deploy'])
        self.assertIn('-T',argv)
        self.assertIn('BatchMode=yes',argv)
        self.assertIn('StrictHostKeyChecking=yes',argv)
        self.assertIn('IdentitiesOnly=yes',argv)
        self.assertIn('IdentityAgent=none',argv)
        self.assertIn('ForwardAgent=no',argv)
        self.assertEqual(argv[argv.index('-F')+1],'/dev/null')
        self.assertEqual(argv[argv.index('-l')+1],'xynes_qa_deploy')
        self.assertNotIn(self.key,str(argv))
        self.assertNotIn('GH_TOKEN',env)
        self.assertNotIn('SSH_AUTH_SOCK',env)
        request=json.loads(data)
        self.assertEqual(request['controller']['run_id'],100)
        self.assertEqual(request['deployment'],dict(run_id=101,run_attempt=1,job_id=201))
        self.assertEqual(request['publication'],dict(id=99,digest='sha256:'+'b'*64))
        self.assertNotIn('stage',request)
        self.assertEqual(data,(json.dumps(request,sort_keys=True,separators=(',',':'))+'\n').encode())
        self.assertTrue(result['ssh_entry_acknowledged'])
        self.assertFalse(result['deployable'])
        self.assertTrue(all(not path.exists() for path in self.paths))

    def test_all_stages_select_only_their_literal_accounts(self):
        for stage in ('staging','prod'):
            self.env.update(APEX_STAGE=stage,GITHUB_JOB='deploy-'+stage)
            self.data[P+'/actions/jobs/201']['name']='deploy-'+stage
            self.assertEqual(self.invoke()['stage'],stage)
            self.assertEqual(self.calls[-1][0][self.calls[-1][0].index('-l')+1],'xynes_'+stage+'_deploy')

    def test_forged_github_context_retry_input_actor_and_source_sha_are_refused(self):
        for key,value in [('GITHUB_ACTIONS','false'),('GITHUB_REPOSITORY','other/repo'),
            ('GITHUB_REF','refs/heads/develop'),('GITHUB_REF_PROTECTED','false'),('GITHUB_EVENT_NAME','push'),
            ('GITHUB_RUN_ATTEMPT','2'),('GITHUB_JOB','deploy-prod'),('APEX_STAGE','qa;id'),
            ('APEX_BUILD_RUN_ID','100;id'),('APEX_BUILD_RUN_ID','101'),('GITHUB_SHA','bad'),
            ('GITHUB_WORKFLOW_SHA','b'*40),('GITHUB_ACTOR_ID','8'),('GITHUB_ACTOR','Other'),
            ('GITHUB_TRIGGERING_ACTOR','Other'),('GH_TOKEN','ghp_INERT_WRONG_TOKEN_TYPE_123456')]:
            saved=self.env[key];self.env[key]=value;self.refused();self.env[key]=saved
        self.assertEqual(self.calls,[])

    def test_native_failed_foreign_wrong_commit_or_rerun_build_cannot_dispatch(self):
        target=self.data[P+'/actions/runs/100']
        for key,value in [('status','in_progress'),('conclusion','failure'),('run_attempt',2),
                          ('head_sha','b'*40),('head_branch','develop'),('path','.github/workflows/other.yml')]:
            saved=target[key];target[key]=value;self.refused();target[key]=saved
        target['head_repository']['id']=1113600873
        self.refused();self.assertEqual(self.calls,[])

    def test_ambiguous_expired_wrong_run_artifacts_and_job_ids_are_refused(self):
        listing=self.data[P+'/actions/runs/100/artifacts?per_page=100&page=1']
        original=copy.deepcopy(listing)
        for mode in ('missing','duplicate','expired','count','digest','run'):
            listing.clear();listing.update(copy.deepcopy(original))
            if mode=='missing':listing.update(total_count=0,artifacts=[])
            elif mode=='duplicate':listing['artifacts'].append(copy.deepcopy(listing['artifacts'][0]));listing['total_count']=2
            elif mode=='expired':listing['artifacts'][0]['expired']=True
            elif mode=='count':listing['total_count']=101
            elif mode=='digest':listing['artifacts'][0]['digest']='invalid'
            else:self.data[P+'/actions/artifacts/99']['workflow_run']['id']=999
            self.refused()
            self.data[P+'/actions/artifacts/99']['workflow_run']['id']=100
        listing.clear();listing.update(original)
        self.data[P+'/actions/jobs/201']['run_id']=100
        self.refused();self.assertEqual(self.calls,[])

    def test_malformed_encrypted_keys_and_foreign_host_bindings_are_refused_without_ssh(self):
        raw=base64.b64decode(self.key.splitlines()[1])
        encrypted=raw.replace(string(b'none'),string(b'aes256-ctr'),1)
        encrypted='-----BEGIN OPENSSH PRIVATE KEY-----\n'+base64.b64encode(encrypted).decode()+'\n-----END OPENSSH PRIVATE KEY-----\n'
        for key,value in [('APEX_SSH_PRIVATE_KEY','private-value'),('APEX_SSH_PRIVATE_KEY',self.key+'extra'),
            ('APEX_SSH_PRIVATE_KEY',encrypted),
            ('APEX_SSH_PRIVATE_KEY',self.key.replace('b3BlbnNzaC1rZXktdjE','wrong')),
            ('APEX_SSH_KNOWN_HOSTS',self.env['APEX_SSH_KNOWN_HOSTS'].replace('84.247.176.134','other.invalid')),
            ('APEX_SSH_KNOWN_HOSTS',self.env['APEX_SSH_KNOWN_HOSTS']+'\nother.invalid ssh-ed25519 AAAA'),
            ('APEX_SSH_KNOWN_HOSTS','84.247.176.134 ssh-rsa AAAA')]:
            saved=self.env[key];self.env[key]=value;self.refused();self.env[key]=saved
        self.assertEqual(self.calls,[])

    def test_default_bounded_process_accepts_full_fixed_ssh_argv_without_network(self):
        actual=self.module.ssh_entry.subprocess.Popen
        def popen(argv,**options):
            self.assertEqual(argv[0],'/usr/bin/ssh')
            self.assertGreater(len(argv),8)
            self.assertLessEqual(len(argv),128)
            self.assertEqual(options['env'],self.module.ssh_entry.ENVIRONMENT)
            self.assertIs(options['close_fds'],True)
            self.assertIs(options['start_new_session'],True)
            code='import json,sys; json.loads(sys.stdin.buffer.read()); print(json.dumps({"artifact_kind":"apex-ssh-entry-result","stage":"qa","root_command_completed":True,"deployable":False}))'
            return actual([sys.executable,'-I','-c',code],**options)
        with patch.object(self.module.ssh_entry.subprocess,'Popen',side_effect=popen):
            result=self.module.run(self.env,read=self.read)
        self.assertTrue(result['ssh_entry_acknowledged'])
        self.assertEqual(list(self.base.iterdir()),[])

    def test_explicit_default_port_host_binding_normalizes_only_the_literal_host(self):
        binding=self.env['APEX_SSH_KNOWN_HOSTS']
        self.assertEqual(self.module.known_hosts(binding.replace('84.247.176.134','[84.247.176.134]:22')),
                         (binding+'\n').encode())

    def test_failed_or_unbounded_or_fabricated_ssh_response_is_redacted_and_keys_cleaned(self):
        for response in ((1,b'private',b'private'),(0,b'X'*65537,b''),
                         (0,b'{"deployed":true}',b''),(0,b'not-json',b''),
                         (True,b'',b''),(0,b'',b'X'*65537)):
            self.refused(process=lambda *args,**kwargs:response)
            self.assertEqual(list(self.base.iterdir()),[])
        def failed(*args,**kwargs):raise RuntimeError(self.key)
        self.refused(process=failed)
        self.assertEqual(list(self.base.iterdir()),[])

    def test_cancelled_deployment_or_main_drift_is_rechecked_before_ssh(self):
        original=self.read
        for mode in ('run','main'):
            counts={}
            def read(path):
                counts[path]=counts.get(path,0)+1
                value=original(path)
                if counts[path]>1:
                    if mode=='run' and path==P+'/actions/runs/101':value.update(status='completed',conclusion='cancelled')
                    if mode=='main' and path==P+'/branches/main':value['commit']['sha']='b'*40
                return value
            self.read=read
            self.refused()
        self.assertEqual(self.calls,[])

    def test_workflow_has_only_three_literal_readonly_stage_roles_and_fixed_dispatch(self):
        path=ROOT/'.github/workflows/apex-deploy.yml'
        self.assertTrue(path.is_file(),'deployment workflow is missing')
        workflow=json.loads(path.read_text())
        self.assertEqual(set(workflow['on']),{'workflow_dispatch'})
        inputs=workflow['on']['workflow_dispatch']['inputs']
        self.assertEqual(set(inputs),{'stage','build_run_id'})
        self.assertEqual(inputs['stage']['options'],['qa','staging','prod'])
        self.assertIs(workflow['concurrency']['cancel-in-progress'],False)
        self.assertEqual(set(workflow['jobs']),{'deploy-qa','deploy-staging','deploy-prod'})
        for stage in ('qa','staging','prod'):
            job=workflow['jobs']['deploy-'+stage]
            self.assertEqual(job['if'],self.module.deploy_admission.guard(stage))
            self.assertEqual(job['environment'],stage)
            self.assertEqual(job['permissions'],{'contents':'read','actions':'read'})
            checkout,dispatch=job['steps']
            self.assertEqual(checkout['uses'],'actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683')
            self.assertIs(checkout['with']['persist-credentials'],False)
            self.assertEqual(checkout['with']['ref'],'${{ github.sha }}')
            self.assertEqual(checkout['with']['sparse-checkout'],'/scripts/release/\n/scripts/lib/\n/infra/release/')
            self.assertEqual(dispatch['run'],'python3 scripts/release/deploy_dispatch.py')
            self.assertEqual(dispatch['env']['APEX_STAGE'],stage)
            self.assertEqual(dispatch['env']['APEX_BUILD_RUN_ID'],'${{ inputs.build_run_id }}')
            self.assertEqual(dispatch['env']['APEX_SSH_PRIVATE_KEY'],'${{ secrets.APEX_SSH_PRIVATE_KEY }}')


if __name__=='__main__':unittest.main()
