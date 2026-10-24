#!/usr/bin/env python3
"""Private filesystem orchestration fixtures, never hosted/native acceptance."""
import copy
import contextlib
import io
import dataclasses
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/release'))
MODULE=ROOT/'scripts/release/root_executor.py'


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module;spec.loader.exec_module(module);return module


class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not MODULE.is_file():return
        cls.transport=load('executor_transport_fixture',ROOT/'scripts/test/migration-transport.test.py').Tests()
        cls.transport.setUp();cls.addClassCleanup(cls.transport.doCleanups)
        receipt=cls.transport.create();cls.transport.receipt=receipt
        cls.verified=cls.transport.extract()
        cls.manifest=json.loads(cls.transport.manifest_bytes)
        cls.m=load('root_executor',MODULE)

    def setUp(self):
        self.assertTrue(MODULE.is_file(),'root deployment coordinator is missing')
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.root.chmod(0o700)
        self.work=self.root/'work';self.work.mkdir(mode=0o700)
        self.directory=self.root/'state';self.directory.mkdir(mode=0o700)
        self.stage='qa';self.events=[];self.failure=None;self.admit_count=0
        self.root_uid=patch.object(self.m,'ROOT_UID',os.getuid());self.root_uid.start();self.addCleanup(self.root_uid.stop)
        self.request={'schema_version':1,'artifact_kind':'apex-deployment-request',
            'controller':copy.deepcopy(self.verified.controller),'publication':{'id':456,'digest':'sha256:'+'b'*64},
            'deployment':{'run_id':789,'run_attempt':1,'job_id':901}}
        self.clock=lambda:int(time.time())
        self.public={};self.receipt={'fixture':'data-only'}
        self.assets={};self.driver=self.Driver(self)
        self.native=self.m.NativeAdapters(read=lambda path:{},read_graphql=lambda query:{},
            download=lambda *args,**kwargs:None,verify=lambda **kwargs:None,parse_workflow=json.loads)
        extra={}
        if 'guard_factory' in self.m.Operations.__dataclass_fields__:
            extra['guard_factory']=lambda stage,state:self.Guard(self,stage,state)
        if 'persist_rollback' in self.m.Operations.__dataclass_fields__:
            extra['persist_rollback']=self.persist_rollback
        self.operations=self.m.Operations(target_audit=self.audit,warm=self.warm,backup=self.backup,
            product_smoke=self.smoke,load_release=self.load_release,persist=self.persist,
            seed_counts=lambda *args:{},compose_factory=lambda stage:self.driver,seed_factory=self.seed_factory,
            clock=self.clock,**extra)
        patches=(patch.object(self.m.deployment_candidate,'acquire',self.candidate),
            patch.object(self.m.deployment_candidate,'acquire_migration',lambda **kwargs:self.verified),
            patch.object(self.m.deploy_admission,'validate',self.admit),
            patch.object(self.m.host_compose,'image_pointers',lambda **kwargs:self.pointers()),
            patch.object(self.m.publication_record,'qualify',self.qualify),
            patch.object(self.m.host_migrate,'apply',self.migrate))
        for patcher in patches:patcher.start();self.addCleanup(patcher.stop)
        self.deploy=self.root/'deploy';self.deploy.mkdir(mode=0o700)
        self.diagnostics=self.deploy/'diagnostics'
        patcher=patch.object(self.m,'DIAGNOSTICS',self.diagnostics);patcher.start();self.addCleanup(patcher.stop)

    def records(self):
        directory=self.diagnostics/self.stage
        if not directory.exists():return {}
        return {path.name:path for path in directory.iterdir()}

    def event(self,name):
        self.events.append(name)
        if self.failure==name:raise RuntimeError('INERT_SECRET_SENTINEL')

    def pointers(self):
        return {key:'ghcr.io/xynes-studio/apex/inert@sha256:'+'a'*64
                for key in self.m.host_compose.SERVICE_IMAGES.values()}

    def qualify(self,**kwargs):
        return {'source_manifest_sha256':self.verified.source_manifest_sha256,'receipt_sha256':'c'*64}

    def candidate(self,raw,*,stage,work,**kwargs):
        self.event('candidate')
        for name in ('admission','publication'):(work/name).mkdir(mode=0o700)
        for path,value in ((work/'admission/source-manifest.json',self.manifest),
                           (work/'admission/public-inputs.json',self.public),
                           (work/'publication/published-receipt.json',self.receipt)):
            path.write_text(json.dumps(value));path.chmod(0o600)
        return {'schema_version':1,'artifact_kind':'verified-deployment-candidate','stage':stage,
            'controller':copy.deepcopy(self.request['controller']),'publication':copy.deepcopy(self.request['publication']),
            'source_manifest_sha256':self.verified.source_manifest_sha256,'receipt_sha256':'c'*64,
            'stage_approved':False,'deployable':False}

    def admit(self,*args,**kwargs):
        self.admit_count+=1;self.event('admit')
        if self.failure=='admit-final' and 'smoke-current' in self.events:raise RuntimeError('revoked')
        if self.failure=='admit-after-release' and 'warm-exit' in self.events:raise RuntimeError('revoked')
        return {'artifact_kind':'native-deployment-admission','stage':self.stage,
                'controller':copy.deepcopy(self.request['controller']),
                'deployment':copy.deepcopy(self.request['deployment']),'admitted':True,'deployable':False,
                'source_approval':{'source_manifest_sha256':'0'*64 if self.failure=='source-approval-hash' else self.verified.source_manifest_sha256}}

    def test_source_approval_hash_mismatch_refuses_root_write_boundary(self):
        self.failure='source-approval-hash'
        with self.assertRaises(self.m.ExecutorError):
            self.m.admission(self.stage,self.request,self.native,source_sha256=self.verified.source_manifest_sha256)
        self.assertNotIn('bootstrap',self.events)

    def audit(self,stage,current,previous):
        self.event('audit')
        return {'stage':stage,'database_state':'existing' if previous else 'fresh',
            'database_image':current.prepared.images['IMAGE_SUPABASE_DB'],
            'database_container_id':'a'*64 if previous else None,'proof_sha256':'a'*64,
            'observed_at':self.clock()}

    class Warm:
        def __init__(self,parent):self.parent=parent
        def __enter__(self):self.parent.event('warm-enter');return self
        def __exit__(self,*args):self.parent.event('warm-exit')

    def warm(self,*args):return self.Warm(self)

    class Guard:
        def __init__(self,parent,stage,state):
            self.parent,self.stage,self.state=parent,stage,state
            parent.event('guard-create')
        def assert_sql_safe(self,*args,**kwargs):
            self.parent.assertIsNotNone(self.state.read_pending())
            self.parent.event('guard-sql');return {'stage':self.stage,'storage_present':False}
        def resume(self):self.parent.event('guard-release')

    def backup(self,stage,*,phase,current,previous,state,token):
        self.assertEqual(state.stage,stage);self.assertEqual(state.read_pending()['token'],token)
        self.assertEqual(state.read_pending()['request']['controller'],current.controller)
        self.event('backup-'+phase)
        now=time.strftime('%Y-%m-%dT%H:%M:%S+00:00',time.gmtime())
        return {'status':'backup-created','version':1,'environment':stage,'snapshot_at':now,'completed_at':now,
                'sha256':'d'*64,'bytes':123,'uploaded':True,
                'upload':{'file_id':'inert-id','name':stage+'/inert/backup.tar.age','sha1':'a'*40}}

    def smoke(self,assets,*,phase):
        self.event('smoke-'+phase)
        if phase=='rollback' and getattr(self,'expect_rebound',False):
            self.assertEqual(assets.prepared.storage_generation,'4'*64)
        return {'stage':self.stage,'publication':assets.publication,
                'source_sha256':'sha256:'+self.verified.source_manifest_sha256,
                'result':'passed','observed_at':self.clock()}

    def load_release(self,stage,record):
        self.event('load')
        return self.assets[record['token']]

    def persist(self,assets,*,token,proofs,state):
        self.event('persist')
        destination=self.root/'durable'/token;destination.mkdir(parents=True,mode=0o700)
        destination.parent.chmod(0o700)
        file=destination/'compose.json';file.write_bytes(assets.prepared.file.read_bytes());file.chmod(0o600)
        prepared=dataclasses.replace(assets.prepared,file=file)
        result=dataclasses.replace(assets,prepared=prepared)
        self.assets[token]=result
        return result

    def persist_rollback(self,previous,rebound,*,state,token):
        self.assertEqual(state.read_pending()['token'],token)
        self.event('rollback-persist')
        return dataclasses.replace(previous,prepared=rebound)

    def migrate(self,verified,state,**kwargs):
        self.event('migrate')
        return {'stage':self.stage,'controller':verified.controller,
                'source_manifest_sha256':verified.source_manifest_sha256,'migration_count':40,
                'migration_ledger_verified':True,'deployable':False}

    class Driver:
        def __init__(self,parent):self.parent=parent;self.stage=parent.stage
        def bind_guard(self,guard):
            self.parent.assertEqual(guard.stage,self.stage);self.parent.event('guard-bind')
        def prepare(self,*,work,**kwargs):
            self.parent.event('prepare')
            file=work/'compose-current.json';file.write_text('{}');file.chmod(0o600)
            return self.parent.m.host_compose.Prepared(self.parent.stage,file,hashlib.sha256(b'{}').hexdigest(),self.parent.pointers())
        def config(self,prepared):
            self.parent.event('config');return {}
        def bootstrap(self,current,*,previous=None):
            self.parent.event('bootstrap');return {'stage':self.parent.stage,'services_ready':2}
        def start(self,current,*,previous=None):
            self.parent.event('start');return {'stage':self.parent.stage,'services_ready':14}
        def rollback(self,previous,*,current):
            self.parent.event('rollback');return {'stage':self.parent.stage,'services_ready':14,'database_rolled_back':False,
                'prepared':dataclasses.replace(previous,storage_generation='4'*64)}

    def seed_factory(self,driver):
        parent=self
        class Seeder:
            def run(self,*args,**kwargs):
                parent.event('seed');return {'stage':parent.stage,'seeds_applied':True,'product_accepted':False}
        return Seeder()

    def execute(self):
        return self.m.run(json.dumps(self.request).encode(),stage=self.stage,work=self.work,
            state_directory=self.directory,native=self.native,operations=self.operations)

    def read_state(self,stage=None):
        with self.m.host_release_state.ReleaseState(self.directory,stage=stage or self.stage,owner_uid=os.getuid()) as state:
            return state.read_pending(),state.read_accepted()

    def prior(self):
        file=self.root/'prior.json';file.write_text('{}');file.chmod(0o600)
        prepared=self.m.host_compose.Prepared(self.stage,file,hashlib.sha256(b'{}').hexdigest(),self.pointers())
        request=copy.deepcopy(self.request);request['publication']['id']=450
        request['deployment'].update(run_id=780,job_id=890)
        with self.m.host_release_state.ReleaseState(self.directory,stage=self.stage,owner_uid=os.getuid()) as state:
            token=state.begin(request,source_sha256='sha256:'+self.verified.source_manifest_sha256,started_at=self.clock()-10)
            state.accept(token,smoke={'stage':self.stage,'publication':request['publication'],
                'source_sha256':'sha256:'+self.verified.source_manifest_sha256,'result':'passed','observed_at':self.clock()-9},
                accepted_at=self.clock()-8)
        self.assets[token]=self.m.ReleaseAssets(prepared=prepared,migration=self.verified,origin=self.verified,
            controller=request['controller'],publication=request['publication'],manifest=self.manifest,
            public=self.public,receipt=self.receipt)
        return token

    def test_sequence_initial_release_accepted_only_after_complete_operational_success(self):
        result=self.execute()
        self.assertTrue(result['deployment_accepted']);self.assertEqual(result['stage'],'qa')
        pending,accepted=self.read_state();self.assertIsNone(pending);self.assertIsNotNone(accepted['current'])
        expected=['candidate','prepare','audit','warm-enter','bootstrap','backup-before-sql','migrate','seed',
                  'start','smoke-current','persist','warm-exit']
        self.assertEqual([event for event in self.events if event in expected],expected)
        self.assertGreaterEqual(self.admit_count,7)
        self.assertNotIn('backup-before-bootstrap',self.events)
        self.assertNotIn('rollback',self.events)
        self.assertNotIn('INERT_SECRET_SENTINEL',json.dumps(result))

    def test_storage_guard_is_bound_before_bootstrap_and_qualifies_sql_before_migration(self):
        self.execute()
        self.assertIn('guard-bind',self.events)
        self.assertIn('guard-sql',self.events)
        self.assertLess(self.events.index('guard-bind'),self.events.index('bootstrap'))
        self.assertLess(self.events.index('guard-sql'),self.events.index('migrate'))
        self.assertLess(self.events.index('guard-release'),self.events.index('warm-exit'))

    def test_rollback_persists_new_control_generation_before_product_smoke(self):
        prior=self.prior();self.failure='smoke-current';self.expect_rebound=True
        with self.assertRaises(self.m.ExecutorError):self.execute()
        self.assertIn('rollback-persist',self.events)
        self.assertLess(self.events.index('rollback-persist'),self.events.index('smoke-rollback'))
        pending,accepted=self.read_state();self.assertIsNone(pending)
        self.assertEqual(accepted['current']['token'],prior)

    def test_admission_denial_never_mutates_or_creates_pending(self):
        self.failure='admit'
        with self.assertRaises(self.m.ExecutorError):self.execute()
        pending,accepted=self.read_state();self.assertIsNone(pending)
        self.assertNotIn('warm-enter',self.events);self.assertNotIn('bootstrap',self.events)

    def test_existing_database_backup_failure_happens_before_bootstrap(self):
        token=self.prior();self.failure='backup-before-bootstrap'
        with self.assertRaises(self.m.ExecutorError):self.execute()
        pending,accepted=self.read_state();self.assertIsNone(pending)
        self.assertEqual(accepted['current']['token'],token)
        self.assertNotIn('bootstrap',self.events);self.assertNotIn('migrate',self.events)

    def test_initial_migration_failure_stays_pending_without_destructive_cleanup(self):
        self.failure='migrate'
        with self.assertRaises(self.m.ExecutorError) as caught:self.execute()
        self.assertTrue(caught.exception.reconciliation_required)
        pending,accepted=self.read_state();self.assertIsNotNone(pending);self.assertIsNone(accepted['current'])
        self.assertNotIn('start',self.events);self.assertNotIn('rollback',self.events)

    def test_smoke_failure_rolls_back_previous_apps_and_preserves_accepted_pointer(self):
        token=self.prior();self.failure='smoke-current'
        with self.assertRaises(self.m.ExecutorError):self.execute()
        pending,accepted=self.read_state();self.assertIsNone(pending)
        self.assertEqual(accepted['current']['token'],token)
        self.assertLess(self.events.index('backup-before-bootstrap'),self.events.index('bootstrap'))
        self.assertLess(self.events.index('rollback'),self.events.index('smoke-rollback'))
        self.assertLess(self.events.index('smoke-rollback'),self.events.index('warm-exit'))

    def test_rollback_failure_keeps_pending_for_reconciliation(self):
        token=self.prior()
        original=self.driver.rollback
        def rollback(*args,**kwargs):self.events.append('rollback');raise RuntimeError('unknown')
        self.driver.rollback=rollback;self.failure='smoke-current'
        with self.assertRaises(self.m.ExecutorError) as caught:self.execute()
        self.assertTrue(caught.exception.reconciliation_required)
        pending,accepted=self.read_state();self.assertIsNotNone(pending);self.assertEqual(accepted['current']['token'],token)

    def test_warm_release_or_post_smoke_revocation_cannot_accept(self):
        token=self.prior();self.failure='warm-exit'
        with self.assertRaises(self.m.ExecutorError):self.execute()
        pending,accepted=self.read_state();self.assertIsNotNone(pending)
        self.assertEqual(accepted['current']['token'],token);self.assertIn('rollback',self.events)

    def test_final_revocation_restores_previous_release(self):
        token=self.prior();self.failure='admit-final'
        with self.assertRaises(self.m.ExecutorError):self.execute()
        pending,accepted=self.read_state();self.assertIsNone(pending)
        self.assertEqual(accepted['current']['token'],token);self.assertIn('rollback',self.events)

    def test_replay_and_global_lock_contention_cannot_run_mutation(self):
        self.execute();self.events=[]
        another=self.root/'replay';another.mkdir(mode=0o700);self.work=another
        with self.assertRaises(self.m.ExecutorError):self.execute()
        self.assertNotIn('warm-enter',self.events)
        self.events=[]
        another=self.root/'contention';another.mkdir(mode=0o700);self.work=another
        with self.m.host_release_state.ReleaseState(self.directory,stage='staging',owner_uid=os.getuid()):
            with self.assertRaises(self.m.ExecutorError):self.execute()
        self.assertNotIn('warm-enter',self.events)

    def test_production_requires_exact_completed_staging_soak(self):
        self.stage='prod';self.driver.stage='prod'
        with self.assertRaises(self.m.ExecutorError):self.execute()
        self.assertNotIn('bootstrap',self.events)
        another=self.root/'prod-retry';another.mkdir(mode=0o700);self.work=another;self.events=[]
        with self.m.host_release_state.ReleaseState(self.directory,stage='staging',owner_uid=os.getuid()) as state:
            request=copy.deepcopy(self.request);request['deployment'].update(run_id=700,job_id=800)
            started=self.clock()-90000
            token=state.begin(request,source_sha256='sha256:'+self.verified.source_manifest_sha256,started_at=started)
            state.accept(token,smoke={'stage':'staging','publication':request['publication'],
                'source_sha256':'sha256:'+self.verified.source_manifest_sha256,'result':'passed','observed_at':started+1},
                accepted_at=started+2)
            state.record_soak(token,soak={'stage':'staging','publication':request['publication'],
                'source_sha256':'sha256:'+self.verified.source_manifest_sha256,'result':'passed',
                'started_at':started+2,'completed_at':started+86402},recorded_at=self.clock())
        self.assertTrue(self.execute()['deployment_accepted'])

    def test_cli_delegates_to_the_process_tree_supervisor(self):
        with patch('host_supervisor.main',return_value=0) as supervisor:
            self.assertEqual(self.m.main(),0)
            supervisor.assert_called_once_with()

    def test_cli_refuses_an_absent_literal_stage_without_success_ack(self):
        output,errors=io.StringIO(),io.StringIO()
        with patch.object(sys,'argv',['xynes-deploy-root']),contextlib.redirect_stdout(output),contextlib.redirect_stderr(errors):
            self.assertEqual(self.m.main(),1)
        self.assertEqual(output.getvalue(),'')
        self.assertEqual(errors.getvalue(),'Root deployment supervision refused\n')

    def test_revocation_after_lease_release_reacquires_warmth_before_rollback(self):
        token=self.prior();self.failure='admit-after-release'
        with self.assertRaises(self.m.ExecutorError):self.execute()
        pending,accepted=self.read_state();self.assertIsNone(pending)
        self.assertEqual(accepted['current']['token'],token)
        self.assertEqual(self.events.count('warm-enter'),2)
        self.assertEqual(self.events.count('warm-exit'),2)
        second_enter=self.events.index('warm-enter',self.events.index('warm-enter')+1)
        self.assertLess(second_enter,self.events.index('rollback'))

    def test_persistence_cannot_drop_the_retained_origin_bundle(self):
        original=self.persist
        def persist(*args,**kwargs):
            return dataclasses.replace(original(*args,**kwargs),origin=None)
        self.operations=dataclasses.replace(self.operations,persist=persist)
        with self.assertRaises(self.m.ExecutorError):self.execute()
        pending,accepted=self.read_state();self.assertIsNotNone(pending)
        self.assertIsNone(accepted['current'])

    def test_persistence_failure_restores_previous_release_before_releasing_warmth(self):
        token=self.prior();self.failure='persist'
        with self.assertRaises(self.m.ExecutorError):self.execute()
        pending,accepted=self.read_state();self.assertIsNone(pending)
        self.assertEqual(accepted['current']['token'],token)
        self.assertLess(self.events.index('rollback'),self.events.index('warm-exit'))

    def test_unuploaded_or_stale_backup_cannot_reach_sql(self):
        original=self.backup
        for number,change in enumerate(({'uploaded':False},{'snapshot_at':'2000-01-01T00:00:00+00:00'})):
            if number:
                another=self.root/('backup-'+str(number));another.mkdir(mode=0o700);self.work=another
            def backup(stage,*,phase,current,previous):
                return {**original(stage,phase=phase,current=current,previous=previous),**change}
            self.operations=dataclasses.replace(self.operations,backup=backup)
            with self.assertRaises(self.m.ExecutorError):self.execute()
            pending,accepted=self.read_state();self.assertIsNotNone(pending)
            self.assertIsNone(accepted['current']);self.assertNotIn('migrate',self.events)
            # This test's next case receives a distinct private ledger fixture;
            # production never discards a pending record to retry.
            another_state=self.root/('state-'+str(number));another_state.mkdir(mode=0o700);self.directory=another_state

    def test_native_revocation_after_long_backup_refuses_before_sql(self):
        original=self.admit
        def admit(*args,**kwargs):
            if 'backup-before-sql' in self.events:raise RuntimeError('approval revoked')
            return original(*args,**kwargs)
        with patch.object(self.m.deploy_admission,'validate',admit):
            with self.assertRaises(self.m.ExecutorError):self.execute()
        self.assertNotIn('migrate',self.events)
        self.assertIsNotNone(self.read_state()[0])

    def test_atomic_accept_durability_failure_is_reconciled_by_reading_disk(self):
        original=self.m.host_release_state.ReleaseState.accept
        def accept(state,*args,**kwargs):
            real_sync=os.fsync
            def sync(fd):
                if fd==state.fd:raise OSError('private durability error')
                return real_sync(fd)
            with patch.object(self.m.host_release_state.os,'fsync',side_effect=sync):
                return original(state,*args,**kwargs)
        with patch.object(self.m.host_release_state.ReleaseState,'accept',accept):
            with self.assertRaises(self.m.ExecutorError) as caught:self.execute()
        self.assertTrue(caught.exception.reconciliation_required)
        pending,accepted=self.read_state()
        self.assertIsNone(pending)
        self.assertIsNotNone(accepted['current'])
        self.assertNotIn('rollback',self.events,'uncertain atomic acceptance must not trigger a blind rollback')

    SECRET='ghp_'+'Z'*36

    def secret_failure(self,name):
        # Secret-shaped values in the exception message/args, environment and argv.
        original=self.event
        def event(value):
            original(value)
            if value==name:
                raise KeyError('INERT_SECRET_SENTINEL '+self.SECRET,{'password':self.SECRET})
        self.event=event

    def assert_private_record(self,path,*,scope):
        raw=path.read_bytes();record=json.loads(raw)
        self.assertEqual(path.stat().st_mode&0o777,0o600);self.assertEqual(path.parent.stat().st_mode&0o777,0o700)
        self.assertEqual(path.parent.parent.stat().st_mode&0o777,0o700)
        for forbidden in (self.SECRET,'INERT_SECRET_SENTINEL','password',str(self.root),'unknown'):
            self.assertNotIn(forbidden.encode(),raw)
        self.assertLessEqual(len(raw),self.m.MAX_DIAGNOSTIC)
        self.assertEqual(set(record),{'schema_version','artifact_kind','stage','scope','token','controller','deployment',
                                      'phase','last_step','recorded_at','flags','exceptions'})
        self.assertEqual((record['schema_version'],record['artifact_kind'],record['stage'],record['scope']),
                         (1,'root-deployment-diagnostic',self.stage,scope))
        self.assertEqual(record['controller'],{key:self.request['controller'][key] for key in ('commit','run_id','run_attempt')})
        self.assertEqual(record['deployment'],self.request['deployment'])
        for row in record['exceptions']:
            self.assertEqual(set(row)-{'native'},{'class','relation','frames'})
            for name,line,function in row['frames']:
                self.assertTrue((ROOT/'scripts/release'/name).is_file());self.assertIsInstance(line,int)
        return record

    def test_attempt_failure_records_phase_step_classes_and_frames_without_secrets(self):
        self.secret_failure('migrate')
        with patch.dict(os.environ,{'POSTGRES_PASSWORD':self.SECRET}):
            with self.assertRaises(self.m.ExecutorError) as caught:self.execute()
        self.assertTrue(caught.exception.reconciliation_required);self.assertEqual(str(caught.exception),self.m.ERROR)
        pending,_=self.read_state();files=self.records()
        self.assertEqual(set(files),{pending['token']+'.json'})
        record=self.assert_private_record(files[pending['token']+'.json'],scope='execute')
        self.assertEqual((record['token'],record['phase'],record['last_step']),(pending['token'],'migration','storage-sql'))
        self.assertEqual(record['flags'],{'mutated':True,'lease-entered':True,'lease-released':False,'previous-accepted':False})
        self.assertEqual(record['exceptions'][0]['class'],'KeyError')
        self.assertIn(['root_executor.py',record['exceptions'][0]['frames'][0][1],'execute_locked'],record['exceptions'][0]['frames'])

    def test_each_representative_phase_records_last_completed_step(self):
        cases=[('bootstrap','runtime','warm'),('seed','migration','migration'),('start','runtime','seeds'),
               ('smoke-current','smoke','start'),('persist','runtime','smoke')]
        for failure,phase,step in cases:
            with self.subTest(failure=failure):
                self.setUp();self.failure=failure
                with self.assertRaises(self.m.ExecutorError):self.execute()
                (path,)=self.records().values()
                record=self.assert_private_record(path,scope='execute')
                self.assertEqual((record['phase'],record['last_step']),(phase,step))
                self.assertEqual(record['exceptions'][0]['class'],'RuntimeError')

    def test_native_compose_classification_reaches_the_record_through_suppressed_context(self):
        import subprocess
        real_popen=subprocess.Popen;compose=self.m.host_compose
        def flood(argv,**kwargs):
            return real_popen([sys.executable,'-c',"import sys;sys.stderr.write('"+self.SECRET+"\\n'*3000)"],**kwargs)
        def start(current,*,previous=None):
            self.event('start')
            try:
                with patch.object(compose.subprocess,'Popen',flood):
                    compose.run_command(compose.BASE+['compose','--env-file','/srv/'+self.SECRET,'up','-d','gateway'],
                                        env={**compose.CLEAN_ENV,'TOKEN':self.SECRET},timeout=30,limit=65536)
            except Exception:raise compose.ComposeError(compose.ERROR) from None
        self.driver.start=start
        with self.assertRaises(self.m.ExecutorError):self.execute()
        (path,)=self.records().values()
        record=self.assert_private_record(path,scope='execute')
        self.assertEqual(record['last_step'],'seeds')
        natives=[row['native'] for row in record['exceptions'] if 'native' in row]
        self.assertEqual(len(natives),1)
        self.assertEqual({key:natives[0][key] for key in ('command','outcome','exit_code','stdout_bytes')},
                         {'command':'compose up','outcome':'stderr-limit','exit_code':None,'stdout_bytes':0})
        self.assertEqual([row['relation'] for row in record['exceptions']][:2],['raised','context'])

    def test_pre_attempt_failure_is_recorded_under_run_identity_once(self):
        self.failure='admit'
        with self.assertRaises(self.m.ExecutorError) as caught:self.execute()
        self.assertFalse(caught.exception.reconciliation_required)
        files=self.records();self.assertEqual(set(files),{'run-789-1-901.json'})
        record=self.assert_private_record(files['run-789-1-901.json'],scope='run')
        self.assertEqual((record['token'],record['phase'],record['last_step']),(None,None,'qualified'))
        self.assertIn('admission',[function for _,_,function in record['exceptions'][0]['frames']])
        # Exclusive create: an existing record is never replaced and never changes the refusal.
        files['run-789-1-901.json'].write_bytes(b'retained\n');self.events=[]
        another=self.root/'again';another.mkdir(mode=0o700);self.work=another
        with self.assertRaises(self.m.ExecutorError) as again:self.execute()
        self.assertFalse(again.exception.reconciliation_required)
        self.assertEqual(files['run-789-1-901.json'].read_bytes(),b'retained\n')

    def test_diagnostic_write_failures_never_change_refusal_or_ledger(self):
        def outcome():
            self.setUp();self.failure='start'
            with self.assertRaises(self.m.ExecutorError) as caught:self.execute()
            pending,accepted=self.read_state()
            return caught.exception.reconciliation_required,pending is not None,accepted['current'] is None,str(caught.exception)
        expected=outcome();self.assertEqual(len(self.records()),1)
        breakers=[lambda:patch.object(self.m,'write_diagnostic',side_effect=OSError('INERT_SECRET_SENTINEL')),
                  lambda:patch.object(self.m,'diagnostic',side_effect=MemoryError()),
                  lambda:patch.object(self.m,'DIAGNOSTICS',self.root/'absent'/'diagnostics'),
                  lambda:patch.object(self.m,'MAX_DIAGNOSTIC',8)]
        for breaker in breakers:
            with self.subTest(breaker=breaker):
                self.setUp()
                with breaker():
                    self.failure='start'
                    with self.assertRaises(self.m.ExecutorError) as caught:self.execute()
                pending,accepted=self.read_state()
                self.assertEqual((caught.exception.reconciliation_required,pending is not None,accepted['current'] is None,str(caught.exception)),expected)
        # Unsafe directories are refused (nothing written) without changing the refusal.
        for unsafe in ('parent-mode','stage-mode','stage-symlink','diagnostics-symlink','file-symlink'):
            with self.subTest(unsafe=unsafe):
                self.setUp();outside=self.root/'outside';outside.mkdir(mode=0o700)
                if unsafe=='parent-mode':self.deploy.chmod(0o777)
                elif unsafe=='stage-mode':(self.diagnostics/self.stage).mkdir(parents=True,mode=0o700);(self.diagnostics/self.stage).chmod(0o755)
                elif unsafe=='stage-symlink':self.diagnostics.mkdir(mode=0o700);(self.diagnostics/self.stage).symlink_to(outside)
                elif unsafe=='diagnostics-symlink':self.diagnostics.symlink_to(outside)
                self.failure='start'
                if unsafe=='file-symlink':
                    original=self.m.diagnose
                    def plant(error,**kwargs):
                        (self.diagnostics/self.stage).mkdir(parents=True,mode=0o700)
                        (self.diagnostics/self.stage/(kwargs['token']+'.json')).symlink_to(outside/'target')
                        return original(error,**kwargs)
                    with patch.object(self.m,'diagnose',plant):
                        with self.assertRaises(self.m.ExecutorError) as caught:self.execute()
                else:
                    with self.assertRaises(self.m.ExecutorError) as caught:self.execute()
                pending,accepted=self.read_state()
                self.assertEqual((caught.exception.reconciliation_required,pending is not None,accepted['current'] is None,str(caught.exception)),expected)
                self.assertEqual(list(outside.iterdir()),[])
                stage=self.diagnostics/self.stage
                if unsafe in ('parent-mode','stage-mode'):
                    self.assertFalse(stage.exists() and any(stage.iterdir()))
        self.deploy.chmod(0o700)

    def test_record_is_bounded_and_drops_frames_before_exceeding_limit(self):
        def deep(depth):
            if depth:return deep(depth-1)
            raise ValueError('INERT_SECRET_SENTINEL')
        errors=[]
        for index in range(20):
            try:
                try:deep(200)
                except ValueError as inner:
                    raise RuntimeError('INERT_SECRET_SENTINEL') from (errors[-1] if errors else inner)
            except RuntimeError as outer:errors.append(outer)
        with patch.object(self.m,'MAX_DIAGNOSTIC',4096),patch.object(self.m,'RELEASE_DIRECTORY',str(Path(__file__).resolve().parent)):
            raw=self.m.diagnostic(errors[-1],stage='qa',scope='execute',phase='runtime',step='begin',token='a'*64,request=self.request)
            self.assertLessEqual(len(raw),4096)
        record=json.loads(raw);self.assertLessEqual(len(record['exceptions']),16)
        self.assertNotIn(b'INERT_SECRET_SENTINEL',raw)
        for bad in (dict(scope='Execute'),dict(phase='x'*41),dict(token='g'*64),dict(flags={'mutated':'yes'}),dict(stage='dev')):
            arguments=dict(stage='qa',scope='execute',phase=None,step=None,token=None,request=None);arguments.update(bad)
            with self.assertRaises(self.m.ExecutorError):self.m.diagnostic(errors[-1],**arguments)
        identity=self.m.identity_fields({'controller':{'commit':self.SECRET,'run_id':1,'run_attempt':1},
                                         'deployment':{'run_id':True,'run_attempt':1,'job_id':2}})
        self.assertEqual(identity,(None,None))
        with self.assertRaises(self.m.ExecutorError):self.m.write_diagnostic('qa','../escape.json',b'{}\n')


if __name__=='__main__':unittest.main()
