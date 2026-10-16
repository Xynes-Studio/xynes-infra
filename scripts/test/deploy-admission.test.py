#!/usr/bin/env python3
"""Native REST-shaped stage admission fixtures, without host or network effects."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'scripts/release'))
P = '/repos/Xynes-Studio/xynes-infra'
SHA = 'a'*40
ERROR = 'Native deployment admission refused'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Tests(unittest.TestCase):
    def setUp(self):
        path = ROOT/'scripts/release/deploy_admission.py'
        self.assertTrue(path.is_file(), 'fresh deployment stage admission is missing')
        self.module = load('deploy_admission', path)
        self.fixture = load('admission_fixture', ROOT/'scripts/test/admission.test.py')
        self.controller = dict(repository='Xynes-Studio/xynes-infra', commit=SHA,
                               workflow='apex-build.yml', run_id=100, run_attempt=1)
        self.deployment = dict(run_id=101, run_attempt=1, job_id=201)
        self.reset('prod')

    def reset(self, stage):
        self.stage = stage
        self.data = self.fixture.fixtures()
        run = copy.deepcopy(self.data[P+'/actions/runs/100'])
        run.update(id=101, workflow_id=11, path='.github/workflows/apex-deploy.yml')
        self.data[P+'/actions/runs/101'] = run
        self.data[P+'/actions/workflows/11'] = dict(id=11, path=run['path'], state='active')
        job = copy.deepcopy(self.data[P+'/actions/jobs/200'])
        job.update(id=201, name='deploy-'+stage, run_id=101, run_url='https://api.github.com'+P+'/actions/runs/101')
        self.data[P+'/actions/jobs/201'] = job
        self.workflow = {'on':{'workflow_dispatch':{'inputs':{'stage':{'type':'choice','options':['qa','staging','prod']}}}},
            'jobs': {'deploy-'+stage: {'name':'deploy-'+stage, 'environment':stage,
                 'if':self.module.guard(stage), 'permissions':{'contents':'read','actions':'read'}}}}
        self.refresh_workflow()
        environment = copy.deepcopy(self.data[P+'/environments/release'])
        environment.update(name=stage, url='https://api.github.com'+P+'/environments/'+stage)
        if stage != 'prod':
            environment['protection_rules'] = [{'id':303,'type':'branch_policy'}]
        self.data[P+'/environments/'+stage] = environment
        self.data[P+'/environments/'+stage+'/deployment-branch-policies?per_page=100&page=1'] = copy.deepcopy(
            self.data[P+'/environments/release/deployment-branch-policies?per_page=100&page=1'])
        approval = copy.deepcopy(self.data[P+'/actions/runs/100/approvals'][0])
        approval['environments'][0].update(name=stage, url=environment['url'])
        self.data[P+'/actions/runs/101/approvals'] = [approval] if stage == 'prod' else []

    def refresh_workflow(self):
        path = '.github/workflows/apex-deploy.yml'
        self.data[P+'/contents/'+path+'?ref='+SHA] = self.fixture.file_record(path, self.workflow)

    def verify(self, read=None, read_graphql=None):
        return self.module.validate(self.stage, self.controller, self.deployment,
            read=read or (lambda path:copy.deepcopy(self.data[path])), read_graphql=read_graphql, parse_workflow=json.loads)

    def refused(self, **arguments):
        with self.assertRaises(self.module.DeployAdmissionError) as caught:
            self.verify(**arguments)
        self.assertEqual(str(caught.exception), ERROR)

    def test_host_stage_admission_refuses_unreviewed_or_changed_source_pr(self):
        for mode in ('unmerged','dismissed','changed-tree'):
            self.reset('qa')
            if mode=='unmerged':self.data[P+'/pulls/1']['merged']=False
            elif mode=='dismissed':self.data[P+'/pulls/1/reviews?per_page=100&page=1'][0]['state']='DISMISSED'
            else:self.data[P+'/git/commits/'+SHA]['tree']['sha']='d'*40
            with self.subTest(mode=mode):self.refused()

    def test_native_stage_job_and_fresh_policy_admit_without_host_authority(self):
        for stage in ('qa','staging','prod'):
            self.reset(stage)
            proof = self.verify()
            self.assertEqual(proof['stage'], stage)
            self.assertEqual(proof['controller'], self.controller)
            self.assertEqual(proof['deployment'], self.deployment)
            self.assertTrue(proof['admitted'])
            self.assertFalse(proof['deployable'])
            self.assertEqual(len(proof['checks']), 6)
            self.assertEqual(proof['reviewer_id'], 8 if stage == 'prod' else None)

    def test_caller_refs_are_exact_first_attempt_and_distinct_from_build(self):
        for target, key, value in (('deployment','run_id',100),('deployment','run_attempt',2),
                ('deployment','run_attempt',True),('deployment','job_id',True),('deployment','approved',True),
                ('controller','run_attempt',2),('controller','commit','bad'),('controller','approved',True)):
            original = copy.deepcopy(getattr(self,target))
            getattr(self,target)[key] = value
            self.refused()
            setattr(self,target,original)
        self.stage = 'dev'
        self.refused()

    def test_wrong_native_run_job_repo_sha_attempt_and_state_refuse(self):
        for target,key,value in [('run','id',999),('run','run_attempt',True),('run','event','push'),
                ('run','head_sha','b'*40),('run','head_branch','develop'),('run','status','completed'),
                ('run','path','.github/workflows/apex-build.yml'),('job','name','deploy-qa'),
                ('job','run_attempt',2),('job','run_id',100),('job','head_sha','b'*40),
                ('job','conclusion','cancelled'),('job','head_branch','develop')]:
            self.reset('prod')
            self.data[P+('/actions/runs/101' if target=='run' else '/actions/jobs/201')][key] = value
            self.refused()
        self.reset('prod')
        self.data[P+'/actions/runs/101']['head_repository']['id'] = 1113600873
        self.refused()

    def test_hash_verified_workflow_requires_literal_role_environment_permissions_and_guard(self):
        for mode in ('hash','environment','reusable','permissions','guard','name','trigger'):
            self.reset('prod')
            job = self.workflow['jobs']['deploy-prod']
            if mode=='environment': job['environment'] = '${{ inputs.stage }}'
            elif mode=='reusable': job['uses'] = './other.yml'
            elif mode=='permissions': job['permissions']['packages'] = 'write'
            elif mode=='guard': job['if'] = '${{ always() }}'
            elif mode=='name': job['name'] = 'other'
            elif mode=='trigger': self.workflow['on']['push'] = {}
            self.refresh_workflow()
            if mode=='hash': self.data[P+'/contents/.github/workflows/apex-deploy.yml?ref='+SHA]['sha'] = 'b'*40
            self.refused()

    def test_prod_requires_unique_independent_aman_approval_and_no_bypass(self):
        for mode in ('missing','rejected','duplicate','self','trigger_self','other','reviewer','bypass','self_allowed','branches'):
            self.reset('prod')
            env = self.data[P+'/environments/prod']
            approvals = self.data[P+'/actions/runs/101/approvals']
            if mode=='missing': approvals.clear()
            elif mode=='rejected': approvals[0]['state'] = 'rejected'
            elif mode=='duplicate': approvals.append(copy.deepcopy(approvals[0]))
            elif mode in ('self','trigger_self'): self.data[P+'/actions/runs/101']['actor' if mode=='self' else 'triggering_actor'] = approvals[0]['user']
            elif mode=='other': approvals[0]['user'] = dict(id=9,login='Other',type='User')
            elif mode=='reviewer': env['protection_rules'][0]['reviewers'].append(dict(type='User',reviewer=dict(id=9,login='Other',type='User')))
            elif mode=='bypass': env['can_admins_bypass'] = True
            elif mode=='self_allowed': env['protection_rules'][0]['prevent_self_review'] = False
            else: self.data[P+'/environments/prod/deployment-branch-policies?per_page=100&page=1']['branch_policies'][0]['name'] = '*'
            self.refused()

    def test_qa_and_staging_preserve_required_reviewers_and_refuse_unsupported_rules(self):
        for stage in ('qa','staging'):
            self.reset(stage)
            env = self.data[P+'/environments/'+stage]
            env['protection_rules'] = copy.deepcopy(self.data[P+'/environments/release']['protection_rules'])
            self.refused()
            approval = copy.deepcopy(self.data[P+'/actions/runs/100/approvals'][0])
            approval['environments'][0].update(name=stage,url=env['url'])
            self.data[P+'/actions/runs/101/approvals'] = [approval]
            self.assertEqual(self.verify()['reviewer_id'], 8)
            env['protection_rules'][0]['reviewers'][0]['type'] = 'Team'
            self.refused()
            for rule in ('wait_timer','custom'):
                env['protection_rules'] = [dict(id=999,type=rule)]
                self.refused()

    def test_main_checks_and_protection_require_native_producers(self):
        for mode in ('main','policy','checks','producer'):
            self.reset('prod')
            if mode=='main': self.data[P+'/branches/main']['commit']['sha'] = 'b'*40
            elif mode=='policy': self.data[P+'/branches/main/protection']['enforce_admins']['enabled'] = False
            elif mode=='checks': self.data[P+'/commits/'+SHA+'/check-runs?filter=all&per_page=100&page=1']['check_runs'][0]['conclusion'] = 'failure'
            else: self.data[P+'/actions/jobs/500']['check_run_url'] = 'https://api.github.com'+P+'/check-runs/999'
            self.refused()

    def test_late_cancellation_withdrawal_and_setting_drift_are_refused_after_checks(self):
        for mode in ('job','run','approval','env','reviewers_removed','policy','main','workflow'):
            self.reset('prod')
            changed = False
            def read(path):
                nonlocal changed
                if '/check-runs?' in path and not changed:
                    changed = True
                    if mode=='job': self.data[P+'/actions/jobs/201'].update(status='completed',conclusion='cancelled')
                    elif mode=='run': self.data[P+'/actions/runs/101']['triggering_actor']['id'] = 9
                    elif mode=='approval': self.data[P+'/actions/runs/101/approvals'].clear()
                    elif mode=='env': self.data[P+'/environments/prod']['id'] = 999
                    elif mode=='reviewers_removed': self.data[P+'/environments/prod']['protection_rules'].clear()
                    elif mode=='policy': self.data[P+'/branches/main/protection']['required_pull_request_reviews']['required_approving_review_count'] = 2
                    elif mode=='main': self.data[P+'/branches/main']['commit']['sha'] = 'b'*40
                    else: self.data[P+'/actions/workflows/11']['state'] = 'disabled_manually'
                return copy.deepcopy(self.data[path])
            self.refused(read=read)
            self.assertTrue(changed)

    def test_incidental_run_updates_do_not_change_authorization_identity(self):
        count = 0
        def read(path):
            nonlocal count
            value = copy.deepcopy(self.data[path])
            if path==P+'/actions/runs/101':
                count += 1
                value['updated_at'] = str(count)
            return value
        self.assertTrue(self.verify(read=read)['admitted'])
        self.assertEqual(count, 2)

    def test_omitted_rest_bypass_field_requires_affirmative_native_graphql(self):
        del self.data[P+'/branches/main/protection']['required_pull_request_reviews']['bypass_pull_request_allowances']
        self.refused()
        proof = {'data':{'repository':{'databaseId':1415400917,'nameWithOwner':'Xynes-Studio/xynes-infra',
            'ref':{'name':'main','prefix':'refs/heads/','target':{'oid':SHA},'branchProtectionRule':{
            'pattern':'main','bypassPullRequestAllowances':{'totalCount':0,'nodes':[],'pageInfo':{'hasNextPage':False}}}}}}}
        queries = []
        def read_graphql(query):
            queries.append(query)
            return copy.deepcopy(proof)
        self.assertTrue(self.verify(read_graphql=read_graphql)['admitted'])
        self.assertEqual(len(queries), 2)
        proof['data']['repository']['ref']['branchProtectionRule']['bypassPullRequestAllowances']['totalCount'] = 1
        self.refused(read_graphql=read_graphql)

    def test_reader_parser_exceptions_and_bad_policy_shapes_have_fixed_diagnostics(self):
        def failed(path):
            raise RuntimeError('private native response')
        self.refused(read=failed)
        for field,value in [('can_admins_bypass',None),('protection_rules',{}),
                            ('deployment_branch_policy',{'protected_branches':True,'custom_branch_policies':False})]:
            self.reset('prod')
            self.data[P+'/environments/prod'][field] = value
            self.refused()
        with self.assertRaises(self.module.DeployAdmissionError) as caught:
            self.module.validate(self.stage,self.controller,self.deployment,
                read=lambda path:copy.deepcopy(self.data[path]),read_graphql=None,
                parse_workflow=lambda raw:(_ for _ in ()).throw(ValueError('private workflow bytes')))
        self.assertEqual(str(caught.exception), ERROR)


    def test_cancellation_and_withdrawal_during_source_review_reads_refuse(self):
        for mode in ('job','approval'):
            self.reset('prod')
            changed = False
            def read(path):
                nonlocal changed
                if '/pulls/1/reviews?' in path and not changed:
                    changed = True
                    if mode=='job': self.data[P+'/actions/jobs/201'].update(status='completed',conclusion='cancelled')
                    else: self.data[P+'/actions/runs/101/approvals'].clear()
                return copy.deepcopy(self.data[path])
            self.refused(read=read)
            self.assertTrue(changed)


if __name__=='__main__':
    unittest.main()
