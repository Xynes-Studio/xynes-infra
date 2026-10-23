#!/usr/bin/env python3
"""Real Git transport and inert Docker processes; no hosted SQL acceptance."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/release'))
MODULE=ROOT/'scripts/release/host_migrate.py'
CID='a'*64


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module;spec.loader.exec_module(module);return module


class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not MODULE.is_file():return
        test=load('host_transport_fixture',ROOT/'scripts/test/migration-transport.test.py').Tests()
        test.setUp();cls.addClassCleanup(test.doCleanups)
        cls.fixture=test
        cls.receipt=test.create();test.receipt=cls.receipt
        cls.prior=test.extract()
        cls.current=cls.prior
        cls.module=load('host_migrate',MODULE)
        cls.units,_=test.module.application_inputs(cls.current)
        cls.baseline=hashlib.sha256(json.dumps([{k:unit[k] for k in ('owner','path','sha256')} for unit in cls.units],
                                             sort_keys=True,separators=(',',':')).encode()).hexdigest()
        cls.old_controller=copy.deepcopy(test.controller)
        repo=test.fixture.roots['xynes-infra']
        subprocess.run(['git','-C',str(repo),'-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false',
                        'commit','--allow-empty','-qm','inert later application release'],check=True)
        test.controller={**test.controller,'commit':subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD']).decode().strip()}
        new_bundle=test.work/'new-migration-bundle.tar'
        new_receipt=test.create(output=new_bundle)
        receiver=test.work/'receiver-next';receiver.mkdir(mode=0o700)
        cls.new_verified=test.extract(new_receipt,bundle=new_bundle,work=receiver)

    def setUp(self):
        self.assertTrue(MODULE.is_file(),'host SQL application module is missing')
        self.m=self.module
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.root.chmod(0o700)
        self.state_path=self.root/'state';self.state_path.mkdir(mode=0o700)
        self.meta=self.root/'container.json';self.ledger=self.root/'ledger.json'
        self.control=self.root/'control.json';self.log=self.root/'calls.jsonl'
        self.row={'id':CID,'image':self.m.pg_container_bridge.DB_IMAGE,'labels':{
            'com.docker.compose.project':'xynes-qa','com.docker.compose.service':'supabase-db',
            'com.docker.compose.oneoff':'False','com.xynes.managed':'true',
            'com.xynes.environment':'qa','com.xynes.role':'infrastructure'},'state':'running','health':'healthy'}
        self.meta.write_text(json.dumps(self.row));self.control.write_text('{}')
        self.evidence={'database':'postgres','user':'postgres','in_recovery':False,'migration_count':40,
            'bootstrap':[{'environment':'qa','baseline_sha256':self.baseline,'completed':True}],
            'migrations':copy.deepcopy(self.units)}
        self.ledger.write_text(json.dumps(self.evidence))
        self.docker=self.root/'docker-fixture'
        self.docker.write_text('''#!/usr/bin/env python3
import hashlib,json,os,pathlib,sys,time
args=sys.argv[1:]
assert args[:4]==['--host','unix:///var/run/docker.sock','--config','/etc/xynes/deploy/docker']
args=args[4:]
control=json.loads(pathlib.Path(%r).read_text())
if args[0]=='ps':print(json.loads(pathlib.Path(%r).read_text())['id'])
elif args[0]=='inspect':print(pathlib.Path(%r).read_text())
elif args[0]=='exec':
    assert os.environ.get('PGPASSWORD')=='inert-migration-db-password'
    assert 'UNRELATED_SECRET' not in os.environ and 'DOCKER_HOST' not in os.environ
    assert '-t' not in args and '--env' in args and 'PGPASSWORD' in args
    assert args[args.index('psql')+1:args.index('psql')+5]==['-X','-qAt','--no-password','--set=ON_ERROR_STOP=1']
    query='--command' in args
    raw=b'' if query else sys.stdin.buffer.read()
    with open(%r,'a') as log:
        log.write(json.dumps({'argv':args,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'query':query})+'\\n')
    if control.get('sleep'):time.sleep(30)
    if control.get('fail'):
        print('inert-migration-db-password INERT_SENTINEL',file=sys.stderr);sys.exit(1)
    if control.get('large_error'):
        sys.stderr.write('E'*65537);sys.exit(1)
    if query:
        if control.get('malformed'):print('{malformed')
        elif control.get('large_output'):sys.stdout.write('x'*65537)
        else:print(pathlib.Path(%r).read_text())
    else:
        assert b'pg_advisory_lock' in raw and b'CREATE SCHEMA xynes_release' in raw
        if control.get('replace'):
            path=pathlib.Path(%r);value=json.loads(path.read_text());value['id']='b'*64;path.write_text(json.dumps(value))
        print('t')
else:sys.exit(1)
''' % (str(self.control),str(self.meta),str(self.meta),str(self.log),str(self.ledger),str(self.meta)))
        self.docker.chmod(0o700)
        self.base=[str(self.docker),*self.m.pg_container_bridge.BASE[1:]]
        self.environment={'XYNES_ENV':'qa','POSTGRES_PASSWORD':'inert-migration-db-password'}
        for patcher in (
            patch.object(self.m,'ROOT_UID',os.getuid()),
            patch.object(self.m,'BASE',self.base),
            patch.object(self.m.pg_container_bridge,'BASE',self.base),
            patch.object(self.m.pg_container_bridge,'stage_environment',lambda stage:self.environment),
            patch.object(self.m.pg_container_bridge,'inspected_container',self.inspect),
            patch.dict(os.environ,{'UNRELATED_SECRET':'INERT_SENTINEL','DOCKER_HOST':'tcp://hostile.invalid'})):
            patcher.start();self.addCleanup(patcher.stop)
        self.request={'schema_version':1,'artifact_kind':'apex-deployment-request',
            'controller':copy.deepcopy(self.current.controller),'publication':{'id':456,'digest':'sha256:'+'b'*64},
            'deployment':{'run_id':789,'run_attempt':1,'job_id':901}}
        self.source='sha256:'+self.current.source_manifest_sha256

    def inspect(self,stage):
        return self.m.pg_container_bridge.Bridge(stage,work_root=self.root,owner_uid=os.getuid(),
               environment_loader=lambda stage:self.environment).inspected_container()

    def state(self):
        return self.m.host_release_state.ReleaseState(self.state_path,stage='qa',owner_uid=os.getuid())

    def pending(self,state,verified=None,number=0):
        verified=verified or self.current
        request={**self.request,'controller':copy.deepcopy(verified.controller),
                 'publication':{'id':456+number,'digest':'sha256:'+('%064x' % (number+1))},
                 'deployment':{'run_id':789+number,'run_attempt':1,'job_id':901+number}}
        return state.begin(request,source_sha256='sha256:'+verified.source_manifest_sha256,started_at=100)

    def accepted(self,state,token,verified):
        pending=state.read_pending()
        state.accept(token,smoke={'stage':'qa','publication':pending['request']['publication'],
            'source_sha256':'sha256:'+verified.source_manifest_sha256,'result':'passed','observed_at':101},accepted_at=102)

    def refuse(self,callback):
        with self.assertRaises(self.m.MigrationError) as caught:callback()
        self.assertEqual(str(caught.exception),'Hosted migration refused')

    def test_actual_native_40_row_baseline_keeps_original_commits_on_successor(self):
        fixture=json.loads((ROOT/'scripts/test/fixtures/qa-completed-initial-baseline.json').read_bytes())
        original=fixture['units'];units=copy.deepcopy(original)
        for row in units:
            if row['owner']=='xynes-infra':row['source_commit']='f'*40 # Synthetic, never a claimed release.
        raw=json.dumps(fixture['ledger']).encode()
        self.assertEqual(self.m.baseline(original),'e4c769a64a873a8cc3bcd63d1f2eee7545f3bc8ca3ac1466c9c89a2ca1594ed5')
        self.refuse(lambda:self.m.ledger(raw,stage='qa',units=units,prior={}))
        prior={(u['owner'],u['path']):u for u in original}
        self.assertEqual(len(self.m.ledger(raw,stage='qa',units=units,prior=prior)),40)
        hostile=copy.deepcopy(fixture['ledger']);hostile['migrations'][0]['source_commit']='e'*40
        self.refuse(lambda:self.m.ledger(json.dumps(hostile).encode(),stage='qa',units=units,prior=prior))

    def test_real_transport_binds_exact_sql_stdin_and_readback_without_accepting_release(self):
        with self.state() as state:
            token=self.pending(state)
            result=self.m.apply(self.current,state)
            self.assertTrue(result['migration_ledger_verified']);self.assertFalse(result['deployable'])
            self.assertEqual(result['migration_count'],40)
            self.assertEqual(result['source_origins'],sorted(self.units,key=lambda unit:(unit['owner'],unit['path'])))
            self.assertEqual(state.read_pending()['token'],token)
            self.assertIsNone(state.read_accepted()['current'])
        calls=[json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(len(calls),2)
        units,payloads=self.fixture.module.application_inputs(self.current)
        expected=self.m.migration_sql.render('qa','postgres',units,payloads).encode()
        self.assertEqual(calls[0]['sha256'],hashlib.sha256(expected).hexdigest())
        self.assertEqual(calls[0]['bytes'],len(expected))
        self.assertTrue(calls[1]['query'])
        self.assertNotIn('inert-migration-db-password',self.log.read_text())
        self.assertNotIn('INERT_SENTINEL',json.dumps(result))

    def test_migration_unsets_inherited_libpq_service_and_option_variables(self):
        with self.state() as state:
            self.pending(state)
            self.m.apply(self.current,state)
        calls=[json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(len(calls),2)
        for call in calls:
            for key in ('PGSERVICE','PGSERVICEFILE','PGHOSTADDR','PGOPTIONS'):
                self.assertIn(key,call['argv'])
                self.assertNotIn(key+'=',call['argv'])

    def test_closed_or_missing_pending_stage_controller_and_source_mismatches_refuse_before_exec(self):
        state=self.state();self.refuse(lambda:self.m.apply(self.current,state))
        with state:
            self.refuse(lambda:self.m.apply(self.current,state))
            self.request['controller']['run_id']+=1
            state.begin(self.request,source_sha256=self.source,started_at=100)
            self.refuse(lambda:self.m.apply(self.current,state))
        self.assertFalse(self.log.exists())
        with self.state() as state:
            pending=state.read_pending();state.fail(pending['token'],reason='interrupted',failed_at=101)
            request={**self.request,'controller':copy.deepcopy(self.current.controller),
                'deployment':{'run_id':800,'run_attempt':1,'job_id':950},'publication':{'id':500,'digest':'sha256:'+'f'*64}}
            state.begin(request,source_sha256='sha256:'+'f'*64,started_at=102)
            self.refuse(lambda:self.m.apply(self.current,state))
        self.assertFalse(self.log.exists())

    def test_native_failure_malformed_bounds_and_timeout_preserve_pending(self):
        for control in ({'fail':True},{'malformed':True},{'large_error':True},{'large_output':True},{'sleep':True}):
            self.control.write_text(json.dumps(control))
            if self.log.exists():self.log.unlink()
            with self.subTest(control=control),self.state() as state:
                if state.read_pending() is None:self.pending(state)
                with patch.object(self.m,'TIMEOUT',0.1 if control.get('sleep') else 900):
                    self.refuse(lambda:self.m.apply(self.current,state))
                self.assertIsNotNone(state.read_pending())
                self.assertIsNone(state.read_accepted()['current'])

    def test_replaced_container_refuses_before_ledger_query(self):
        self.control.write_text(json.dumps({'replace':True}))
        with self.state() as state:
            self.pending(state);self.refuse(lambda:self.m.apply(self.current,state))
        calls=[json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(len(calls),1)

    def test_exact_ledger_binding_and_no_foreign_rows(self):
        changes=[{'migration_count':41},{'database':'other'},{'user':'other'},{'in_recovery':True},
            {'bootstrap':[{**self.evidence['bootstrap'][0],'completed':False}]},
            {'bootstrap':[{**self.evidence['bootstrap'][0],'environment':'prod'}]},
            {'bootstrap':[{**self.evidence['bootstrap'][0],'baseline_sha256':'f'*64}]},
            {'bootstrap':self.evidence['bootstrap']*2}, {'migrations':self.units[:-1]}]
        for field,value in (('owner','foreign'),('path','../private.sql'),('sha256','f'*64),('source_commit','f'*40)):
            rows=copy.deepcopy(self.units);rows[0][field]=value;changes.append({'migrations':rows})
        for change in changes:
            self.ledger.write_text(json.dumps({**self.evidence,**change}))
            with self.subTest(change=change),self.state() as state:
                if state.read_pending() is None:self.pending(state)
                self.refuse(lambda:self.m.apply(self.current,state))
        self.ledger.write_text(json.dumps(self.evidence))

    def test_unmodified_sql_advancing_commits_needs_bound_accepted_origin_bundle(self):
        with self.state() as state:
            token=self.pending(state);self.accepted(state,token,self.current)
            self.pending(state,self.new_verified,1)
            self.refuse(lambda:self.m.apply(self.new_verified,state))
            result=self.m.apply(self.new_verified,state,prior_verified=self.prior)
            self.assertEqual(result['source_origins'],sorted(self.units,key=lambda unit:(unit['owner'],unit['path'])))
            token=state.read_pending()['token'];self.accepted(state,token,self.new_verified)
            self.pending(state,self.new_verified,2)
            self.refuse(lambda:self.m.apply(self.new_verified,state,prior_verified=self.new_verified))
            result=self.m.apply(self.new_verified,state,prior_verified=self.prior)
            self.assertTrue(result['migration_ledger_verified'])

    def test_accepted_unchanged_catalog_is_verified_without_executing_sql(self):
        with self.state() as state:
            token=self.pending(state);self.accepted(state,token,self.current)
            self.pending(state,self.new_verified,1)
            result=self.m.apply(self.new_verified,state,prior_verified=self.prior)
            self.assertTrue(result['migration_ledger_verified'])
            calls=[json.loads(line) for line in self.log.read_text().splitlines()]
            self.assertTrue(calls)
            self.assertTrue(all(call['query'] and call['bytes']==0 for call in calls),
                            'accepted unchanged migrations must be read-only')

    def test_corrupt_accepted_catalog_never_falls_back_to_migration_sql(self):
        evidence=copy.deepcopy(self.evidence)
        evidence['migrations'][0]['sha256']='f'*64
        self.ledger.write_text(json.dumps(evidence))
        with self.state() as state:
            token=self.pending(state);self.accepted(state,token,self.current)
            self.pending(state,self.new_verified,1)
            self.refuse(lambda:self.m.apply(self.new_verified,state,prior_verified=self.prior))
            self.assertIsNotNone(state.read_pending())
        calls=[json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertTrue(all(call['query'] and call['bytes']==0 for call in calls),
                        'unknown ledger must refuse without schema-write fallback')

    def test_readonly_existing_proof_binds_exact_container_and_pending_release(self):
        with self.state() as state:
            token=self.pending(state);self.accepted(state,token,self.current)
            pending=self.pending(state,self.new_verified,1)
            result=self.m.verify_existing(self.new_verified,state,prior_verified=self.prior)
            self.assertFalse(result['schema_sql_applied'])
            self.assertEqual(result['database_container_id'],CID)
            self.assertEqual(result['source_manifest_sha256'],self.new_verified.source_manifest_sha256)
            self.assertEqual(state.read_pending()['token'],pending)

    def test_readonly_existing_api_cannot_bootstrap_without_accepted_origin(self):
        with self.state() as state:
            self.pending(state)
            self.refuse(lambda:self.m.verify_existing(self.current,state,prior_verified=None))
            self.refuse(lambda:self.m.verify_existing(self.current,state,prior_verified=self.prior))
        self.assertFalse(self.log.exists())

    def test_unaccepted_or_tampered_prior_bundle_cannot_grant_ledger_adoption(self):
        with self.state() as state:
            self.pending(state,self.new_verified)
            self.refuse(lambda:self.m.apply(self.new_verified,state,prior_verified=self.prior))
        first=self.current.plan['units'][0]
        path=self.current.roots[first['owner']]/first['path'];raw=path.read_bytes()
        try:
            path.write_bytes(raw+b'-- modified\n')
            with self.state() as state:
                self.refuse(lambda:self.m.apply(self.current,state))
        finally:path.write_bytes(raw)

    def test_privileged_owner_and_bundle_types_are_not_caller_booleans(self):
        with self.state() as state:
            self.pending(state)
            for value in ({'verified':True},True,None):self.refuse(lambda:self.m.apply(value,state))
            with patch.object(self.m,'ROOT_UID',os.getuid()+1):self.refuse(lambda:self.m.apply(self.current,state))
        self.assertFalse(self.log.exists())

    def test_rendered_sql_bounds_and_corrupt_accepted_origin_fail_before_exec(self):
        with self.state() as state:
            token=self.pending(state)
            with patch.object(self.m,'MAX_SQL',1):self.refuse(lambda:self.m.apply(self.current,state))
            self.assertFalse(self.log.exists())
            self.accepted(state,token,self.current)
            self.pending(state,self.new_verified,1)
            bad=copy.deepcopy(self.prior)
            bad.plan['units'][0]['sha256']='f'*64
            self.refuse(lambda:self.m.apply(self.new_verified,state,prior_verified=bad))
            self.assertFalse(self.log.exists())
            bad=copy.deepcopy(self.prior)
            bad.controller['run_id']+=1
            self.refuse(lambda:self.m.apply(self.new_verified,state,prior_verified=bad))
            self.assertFalse(self.log.exists())

    def test_stage_and_root_state_memory_tampering_cannot_supply_authority(self):
        with self.state() as state:
            self.pending(state)
            self.environment['XYNES_ENV']='prod'
            self.refuse(lambda:self.m.apply(self.current,state))
            self.assertFalse(self.log.exists())
            self.environment['XYNES_ENV']='qa'
            state.data['attempts'][0]['source_sha256']='sha256:'+'f'*64
            self.refuse(lambda:self.m.apply(self.current,state))
            self.assertFalse(self.log.exists())


if __name__=='__main__':unittest.main()
