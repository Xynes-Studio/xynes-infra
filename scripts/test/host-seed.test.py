#!/usr/bin/env python3
"""Fixed seed execution boundaries: inert subprocesses; no Docker daemon or DB."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/release'))
import host_compose
import migration_transport
import release_manifest as records

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module;spec.loader.exec_module(module);return module

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path=ROOT/'scripts/release/host_seed.py'
        if cls.path.exists():
            cls.fixture_module=load('host_seed_compose_fixture',ROOT/'scripts/test/host-compose.test.py')
            cls.fixture_module.Tests.setUpClass()
            cls.module=load('host_seed',cls.path)

    def setUp(self):
        self.assertTrue(self.path.is_file(),'original source image seed runner is missing')
        self.fixture=self.fixture_module.Tests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.compose=self.fixture.driver;self.prepared=self.fixture.prepare();self.work=self.fixture.work
        mf=load('host_seed_plan_fixture',ROOT/'scripts/test/migration-plan.test.py').MigrationPlanTests()
        mf.setUp();self.addCleanup(mf.doCleanups)
        manifest=self.fixture.inputs['manifest'];inventory=copy.deepcopy(mf.inventory)
        inventory['source_manifest_sha256']=records.canonical_sha256(manifest);inventory['infra_commit']=self.fixture.inputs['controller']['commit']
        plan=mf.call(manifest=manifest,inventory=inventory)
        self.verified=migration_transport.Verified(mf.base,mf.roots,plan,inventory,{},self.fixture.inputs['controller'],records.canonical_sha256(manifest))
        self.database=self.fixture.config['services']['authz-service']['environment']['DATABASE_URL']
        self.owner=patch.object(self.module,'SEED_UID',os.getuid());self.owner.start();self.addCleanup(self.owner.stop)
        self.network_id='a'*64;self.database_id='b'*64
        self.commands=[];self.created={};self.env_contents=[];self.fail_start=False;self.foreign_seed=False
        self.network={'Id':self.network_id,'Name':'xynes_qa_net','Driver':'bridge','Scope':'local','Internal':False,'Ingress':False,
                      'Labels':{'com.xynes.managed':'true','com.xynes.environment':'qa'},
                      'Containers':{self.database_id:{'Name':'owned-db'}}}
        self.db={'id':self.database_id,'image':self.prepared.images['IMAGE_SUPABASE_DB'],
                 'labels':{'com.xynes.managed':'true','com.xynes.environment':'qa','com.docker.compose.project':'xynes-qa','com.docker.compose.service':'supabase-db','com.docker.compose.oneoff':'False'},
                 'state':'running','health':'healthy','networks':{'xynes_qa_net':{'NetworkID':self.network_id,'Aliases':['supabase-db']}}}
        self.counts={'authz_permissions':200,'authz_roles':5,'authz_mappings':700,'cms_templates':3,'workspaces':2,'cms_defaults_satisfied':6}
        self.compose.runner=self.native
        self.driver=self.module.Driver(self.compose,database_loader=lambda stage:self.database,
                migration_validator=lambda verified:(verified.plan['units'],{}),verify_counts=self.query)

    def query(self,stage,sql):
        self.assertEqual(stage,'qa');self.assertEqual(sql,self.module.POSTCONDITIONS_SQL)
        return copy.deepcopy(self.counts)

    def native(self,argv,*,env,timeout,limit):
        self.commands.append(argv)
        self.assertEqual(argv[:5],host_compose.BASE)
        self.assertNotIn('DATABASE_URL',env);self.assertNotIn(self.database,' '.join(argv))
        if argv[5:7]==['network','inspect']:return json.dumps(self.network).encode()
        if argv[5:7]==['image','inspect']:
            image=argv[-1];owner='xynes-authz-service' if image==self.prepared.images['IMAGE_AUTHZ'] else 'xynes-cms-core'
            return json.dumps({'os':'linux','architecture':'amd64','labels':{'org.opencontainers.image.revision':self.verified.plan['source_commits'][owner],
                'com.xynes.apex.controller-commit':self.verified.controller['commit'],'com.xynes.apex.run-id':'123','com.xynes.apex.run-attempt':'1'}}).encode()
        if argv[5]=='pull':return b''
        if argv[5]=='create':
            cid=f'{len(self.created)+1:064x}';labels={}
            for index,token in enumerate(argv):
                if token=='--label':
                    key,value=argv[index+1].split('=',1);labels[key]=value
            file=Path(argv[argv.index('--env-file')+1]);self.assertEqual(file.stat().st_mode&0o777,0o600)
            self.env_contents.append(file.read_text());self.assertEqual(file.read_text(),'DATABASE_URL='+self.database+'\n')
            image=argv[argv.index('--entrypoint')+2]
            self.created[cid]={'id':cid,'image':image,'labels':labels,'user':'1001:1001','readonly':True,'cap_drop':['ALL'],
                'security_opt':['no-new-privileges:true'],'networks':{'xynes_qa_net':{'NetworkID':self.network_id}},'state':'created','exit_code':0}
            return (cid+'\n').encode()
        if argv[5]=='inspect':
            row=copy.deepcopy(self.db if argv[-1]==self.database_id else self.created[argv[-1]])
            if self.foreign_seed and argv[-1] in self.created:row['labels']['com.xynes.apex.seed-nonce']='foreign'
            return json.dumps(row).encode()
        if argv[5]=='start':
            if self.fail_start:raise RuntimeError('inert-private-child-error')
            cid=argv[-1];row=self.created[cid];row['state']='exited'
            kind=row['labels']['com.xynes.apex.seed']
            return json.dumps({'kind':'authz','permissions':200,'roles':5,'mappings':650} if kind=='authz'
                              else {'kind':'cms','templates':3,'workspaces':2,'defaults':6}).encode()
        if argv[5]=='rm':return b''
        self.fail('unexpected seed command')

    def execute(self):return self.driver.run(self.prepared,self.verified,work=self.work)

    def refused(self,callback):
        with self.assertRaises(self.module.SeedError) as caught:callback()
        self.assertEqual(str(caught.exception),'Hosted seed execution refused')

    def test_original_image_apis_fixed_hardening_and_db_only_credential(self):
        result=self.execute();self.assertTrue(result['seeds_applied']);self.assertFalse(result['product_accepted'])
        creates=[argv for argv in self.commands if argv[5]=='create'];self.assertEqual(len(creates),2)
        for argv,key in zip(creates,('IMAGE_AUTHZ','IMAGE_CMS')):
            self.assertIn(self.prepared.images[key],argv)
            for token in ('--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true','--user','1001:1001','--memory','256m','--pids-limit','128','--no-healthcheck'):
                self.assertIn(token,argv)
            self.assertIn('--log-driver',argv);self.assertIn('none',argv)
            self.assertNotIn('--publish',argv);self.assertNotIn('--privileged',argv);self.assertNotIn('--volume',argv)
        self.assertIn('seedAuthz({db})',creates[0][-1])
        self.assertIn('seedAllDefaultTemplates(db)',creates[1][-1]);self.assertIn('seedAllDefaultContentTypes(db,row.id)',creates[1][-1])
        self.assertEqual(result['seed_requirements'],self.verified.plan['seed_requirements'])
        self.assertFalse(list(self.work.glob('seed-db-*.env')))
        self.assertEqual(len([a for a in self.commands if a[5]=='rm']),2)

    def test_foreign_network_db_image_and_alias_refused_before_create(self):
        original_network=copy.deepcopy(self.network);original_db=copy.deepcopy(self.db)
        for mode in ('network-name','network-label','db-image','db-stage','db-health','db-alias'):
            self.network=copy.deepcopy(original_network);self.db=copy.deepcopy(original_db)
            if mode=='network-name':self.network['Name']='xynes_prod_net'
            elif mode=='network-label':self.network['Labels']['com.xynes.environment']='prod'
            elif mode=='db-image':self.db['image']='postgres:unknown'
            elif mode=='db-stage':self.db['labels']['com.xynes.environment']='prod'
            elif mode=='db-health':self.db['health']='unhealthy'
            else:self.db['networks']['xynes_qa_net']['Aliases']=[]
            before=len(self.commands);self.refused(self.execute)
            self.assertFalse(any(a[5]=='create' for a in self.commands[before:]))

    def test_shared_ingress_permitted_but_competing_db_alias_refused(self):
        peer='c'*64;self.network['Containers'][peer]={'Name':'shared-caddy'};native=self.native
        def with_ingress(argv,**kwargs):
            if argv[5]=='inspect' and argv[-1]==peer:
                return json.dumps({'id':peer,'image':'caddy:fixture','labels':{},'state':'running','health':None,
                    'networks':{'xynes_qa_net':{'NetworkID':self.network_id,'Aliases':['caddy']}}}).encode()
            return native(argv,**kwargs)
        self.compose.runner=with_ingress;self.assertTrue(self.execute()['seeds_applied'])
        def hostile(argv,**kwargs):
            value=with_ingress(argv,**kwargs)
            if argv[5]=='inspect' and argv[-1]==peer:
                row=json.loads(value);row['networks']['xynes_qa_net']['Aliases']=['supabase-db'];return json.dumps(row).encode()
            return value
        self.compose.runner=hostile;before=len(self.commands);self.refused(self.execute)
        self.assertFalse(any(a[5]=='create' for a in self.commands[before:]))

    def test_child_failure_cleans_only_exact_owned_container_and_redacts(self):
        self.fail_start=True;self.refused(self.execute)
        removals=[a for a in self.commands if a[5]=='rm'];self.assertEqual(len(removals),1)
        self.assertEqual(removals[0][-1],next(iter(self.created)))
        self.assertFalse(list(self.work.glob('seed-db-*.env')))

    def test_foreign_seed_nonce_never_removed(self):
        self.foreign_seed=True;self.refused(self.execute)
        self.assertFalse(any(a[5]=='rm' for a in self.commands))
        self.assertFalse(list(self.work.glob('seed-db-*.env')))

    def test_invalid_postcondition_counts_and_db_credentials_never_claim_success(self):
        self.counts['cms_defaults_satisfied']=5;self.refused(self.execute)
        self.assertFalse(list(self.work.glob('seed-db-*.env')))
        self.database='postgres://postgres:private@foreign-db:5432/postgres'
        before=len(self.commands);self.refused(self.execute)
        self.assertEqual(len(self.commands),before)

    def test_source_bindings_and_seed_requirements_refused(self):
        original=copy.deepcopy(self.verified.plan)
        for mutation in (lambda p:p['seed_requirements'][0].update(function='unownedSeed'),
                         lambda p:p['seed_requirements'].pop(),
                         lambda p:p['source_commits'].update({'xynes-authz-service':'0'*40})):
            self.verified.plan.clear();self.verified.plan.update(copy.deepcopy(original));mutation(self.verified.plan)
            self.refused(self.execute)
        self.verified.plan.clear();self.verified.plan.update(original)

    def test_default_dns_name_conflict_refused_without_explicit_alias(self):
        peer='c'*64;self.network['Containers'][peer]={'Name':'supabase-db'};native=self.native
        def hostile(argv,**kwargs):
            if argv[5]=='inspect' and argv[-1]==peer:
                return json.dumps({'id':peer,'image':'foreign:fixture','labels':{},'state':'running','health':None,
                    'networks':{'xynes_qa_net':{'NetworkID':self.network_id,'Aliases':[],'DNSNames':['supabase-db']}}}).encode()
            return native(argv,**kwargs)
        self.compose.runner=hostile;self.refused(self.execute)
        self.assertFalse(any(a[5]=='create' for a in self.commands))

    def test_lost_create_reply_recovers_and_cleans_only_nonce_owned_cid(self):
        native=self.native
        def lost_reply(argv,**kwargs):
            if argv[5]=='ps':return ('\n'.join(self.created)+'\n').encode()
            value=native(argv,**kwargs)
            if argv[5]=='create':raise RuntimeError('inert private create timeout')
            return value
        self.compose.runner=lost_reply;self.refused(self.execute)
        removals=[a for a in self.commands if a[5]=='rm'];self.assertEqual(len(removals),1)
        self.assertEqual(removals[0][-1],next(iter(self.created)))
        self.assertFalse(list(self.work.glob('seed-db-*.env')))

    def test_fixed_bun_entrypoints_parse_without_executing_app_or_sql(self):
        binary=shutil.which('bun');self.assertIsNotNone(binary,'Bun syntax verifier unavailable')
        for kind,code in (('authz',self.module.AUTHZ_CODE),('cms',self.module.CMS_CODE)):
            file=self.work/(kind+'-seed-fixture.ts');file.write_text(code)
            output=self.work/(kind+'-syntax');output.mkdir(mode=0o700)
            result=subprocess.run([binary,'build','--target=bun','--external=*','--outdir',str(output),str(file)],
                                  env=host_compose.CLEAN_ENV,capture_output=True,timeout=15)
            self.assertEqual(result.returncode,0,'fixed Bun seed entrypoint syntax refused')
            self.assertTrue(list(output.iterdir()))

    def test_seed_queries_match_actual_workspace_and_pinned_cms_schema(self):
        infra=(ROOT/'supabase/migrations/20251219133000_accounts_schema_1_identity_workspace_core.sql').read_text()
        self.assertIn('CREATE TABLE IF NOT EXISTS platform.workspaces',infra)
        self.assertNotIn('CREATE TABLE IF NOT EXISTS identity.workspaces',infra)
        self.assertIn('FROM platform.workspaces',self.module.CMS_CODE)
        self.assertIn('FROM platform.workspaces',self.module.POSTCONDITIONS_SQL)
        self.assertNotIn('identity.workspaces',self.module.CMS_CODE+self.module.POSTCONDITIONS_SQL)
        # Pinned peer schema was audited and the real candidate bootstrap
        # verifies these tables. The infra-only unit gate must need no peer clone.
        for identifier in ('cms.global_content_templates','cms.content_types','workspace_id','route_segment'):
            self.assertIn(identifier,self.module.POSTCONDITIONS_SQL)

    def test_concrete_runner_with_real_inert_subprocesses(self):
        real_popen=subprocess.Popen
        def spawn(argv,**kwargs):
            raw=self.native(argv,env=kwargs['env'],timeout=30,limit=65536)
            return real_popen([sys.executable,'-c','import sys;sys.stdout.buffer.write('+repr(raw)+')'],**kwargs)
        self.compose.runner=host_compose.run_command
        with patch.object(host_compose.subprocess,'Popen',spawn):self.assertTrue(self.execute()['seeds_applied'])

if __name__=='__main__':unittest.main()
