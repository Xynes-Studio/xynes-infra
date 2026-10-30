#!/usr/bin/env python3
"""Inert subprocess/systemd fixtures; never a host service or deployment claim."""
import copy
import hashlib
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/release'))
MODULE=ROOT/'scripts/release/host_supervisor.py'


def load():
    spec=importlib.util.spec_from_file_location('host_supervisor',MODULE)
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module);return module


class Tests(unittest.TestCase):
    def test_boundary_generated_sealed_wrapper_matches_supervisor_mode(self):
        import host_boundary
        worker=self.root/'sealed-worker'
        worker.write_bytes(host_boundary.bootstrap('a'*64,{'scripts/release/root_executor.py':'b'*64},'root'))
        worker.chmod(0o555)
        ledger=self.root/'boundary.json'
        ledger.write_text(json.dumps({'schema_version':1,'plan_sha256':'c'*64,'bundle_sha256':'a'*64,
            'files':{str(worker):hashlib.sha256(worker.read_bytes()).hexdigest()}}));ledger.chmod(0o600)
        self.patches[4].stop()
        with patch.object(self.m,'WORKER',str(worker)),patch.object(self.m,'BOUNDARY',ledger):
            try:self.m.fixed_wrapper();accepted=True
            except self.m.SupervisorError:accepted=False
            self.assertTrue(accepted,'boundary-produced immutable wrapper must be accepted')
            worker.chmod(0o755)
            self.refuse(self.m.fixed_wrapper)

    def setUp(self):
        self.assertTrue(MODULE.is_file(),'root process-tree supervisor is missing')
        self.m=load()
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.root.chmod(0o700)
        self.logs=self.root/'argv.jsonl';self.unit_file=self.root/'unit.json';self.control=self.root/'control.json'
        self.control.write_text('{}')
        self.request={'schema_version':1,'artifact_kind':'apex-deployment-request',
            'controller':{'repository':'Xynes-Studio/xynes-infra','commit':'a'*40,'workflow':'apex-build.yml','run_id':123,'run_attempt':1},
            'publication':{'id':456,'digest':'sha256:'+'b'*64},
            'deployment':{'run_id':789,'run_attempt':1,'job_id':901}}
        self.result={'artifact_kind':'root-deployment-result','stage':'qa','token':'c'*64,
            'controller':self.request['controller'],'publication':self.request['publication'],
            'source_manifest_sha256':'d'*64,'deployment_accepted':True}
        self.unit='xynes-apex-deploy-qa-901.service'
        self.expected_scope={'Id':self.unit,'LoadState':'loaded','ActiveState':'active','SubState':'running',
            'MainPID':str(os.getpid()),'ExecMainPID':str(os.getpid()),'InvocationID':'a'*32,
            'ControlGroup':'/system.slice/'+self.unit,'User':'root','Group':'root','Type':'exec',
            'KillMode':'control-group','RuntimeMaxUSec':'45min','TimeoutStopUSec':'15s',
            'Transient':'yes','CollectMode':'inactive-or-failed','ExecStart':''}
        self.systemctl=self.root/'systemctl'
        self.systemctl.write_text('''#!/usr/bin/env python3
import json,pathlib,sys
args=sys.argv[1:]
with open(%r,'a') as log:log.write(json.dumps(['systemctl',*args])+'\\n')
path=pathlib.Path(%r)
if 'show' in args:
    if not path.exists():print('LoadState=not-found')
    else:
        values=json.loads(path.read_text())
        if values.get('MainPID')=='0':path.with_suffix('.observed').touch()
        for key,value in values.items():print(key+'='+value)
elif 'kill' in args or 'stop' in args:
    if path.exists():path.unlink()
else:sys.exit(1)
''' % (str(self.logs),str(self.unit_file)));self.systemctl.chmod(0o700)
        self.systemd=self.root/'systemd-run'
        self.systemd.write_text('''#!/usr/bin/env python3
import json,os,pathlib,sys,time
args=sys.argv[1:]
with open(%r,'a') as log:log.write(json.dumps(['systemd-run',*args])+'\\n')
assert 'UNRELATED_SECRET' not in os.environ and 'SUDO_USER' not in os.environ
control=json.loads(pathlib.Path(%r).read_text())
expected_request=%r
def receive_request():
    raw=sys.stdin.buffer.read();assert raw==expected_request
    if control.get('request_processing_delay'):time.sleep(control['request_processing_delay'])
worker=args.index(%r)
stage,job,parent,ticks=args[worker+2:worker+6]
assert args[worker+1]=='--systemd-worker'
scope=%r
scope['ExecStart']='{ path='+args[worker]+' ; argv[]='+' '.join(args[worker:])+' ; ignore_errors=no ; }'
unit_path=pathlib.Path(%r)
def publish(value):
    temporary=unit_path.with_suffix('.tmp');temporary.write_text(json.dumps(value));temporary.replace(unit_path)
activating=control.get('transition') or control.get('never_running') or control.get('activation_ack')
# systemd creates the service independently of worker stdin consumption. Publish
# ownership before request processing so slow fixture scheduling cannot invent
# a no-service timeout and then assert that a service kill should have occurred.
if activating:
    publish({**scope,'ActiveState':'activating','SubState':'start','MainPID':'0','ExecMainPID':'0'})
else:publish(scope)
receive_request()
if activating:
    deadline=time.monotonic()+5
    while not unit_path.with_suffix('.observed').exists():
        assert time.monotonic()<deadline;time.sleep(.01)
    time.sleep(.25)
    publish({**scope,'ActiveState':'activating','SubState':'start'})
    time.sleep(.25)
    if control.get('never_running'):time.sleep(30)
if not control.get('activation_ack'):publish(scope)
if control.get('sleep'):time.sleep(30)
if control.get('stderr'):
    print('INERT_SECRET_SENTINEL',file=sys.stderr);sys.exit(1)
if control.get('large'):sys.stdout.write('x'*65537)
elif control.get('preflight'):print(json.dumps({'preflight_ack':True}))
else:print(%r)
sys.stdout.flush()
if control.get('activation_ack'):unit_path.with_suffix('.output').touch()
''' % (str(self.logs),str(self.control),self.canonical(),self.m.WORKER,self.expected_scope,str(self.unit_file),json.dumps(self.result,sort_keys=True,separators=(',',':'))))
        self.systemd.chmod(0o700)
        self.locks=self.root/'locks';self.locks.mkdir(mode=0o700)
        self.patches=[patch.object(self.m,'ROOT_UID',os.getuid()),patch.object(self.m,'SYSTEMD_RUN',str(self.systemd)),
            patch.object(self.m,'SYSTEMCTL',str(self.systemctl)),patch.object(self.m,'LOCK_DIRECTORY',self.locks),
            patch.object(self.m,'fixed_wrapper',lambda:None),patch.object(self.m,'root_path',lambda *args,**kwargs:None),
            patch.object(self.m,'sudo_identity',lambda stage:None),
            patch.object(self.m,'process_identity',lambda pid=None:(os.getpid(),12345)),
            patch.object(self.m,'observe_accepted',lambda result,request,stage:None)]
        for item in self.patches:item.start();self.addCleanup(item.stop)

    def canonical(self):
        return (json.dumps(self.request,sort_keys=True,separators=(',',':'))+'\n').encode()

    def outer(self):
        read,write=os.pipe();os.write(write,self.canonical());os.close(write)
        try:return self.m.outer('qa',fd=read)
        finally:os.close(read)

    def refuse(self,callback):
        with self.assertRaises(self.m.SupervisorError) as caught:callback()
        self.assertEqual(str(caught.exception),'Root deployment supervision refused')

    @contextlib.contextmanager
    def expire_after_owned_observation(self,*,assigned=True,output=False,whole=False):
        # Advance only this module's injected clock after a real inert native
        # ownership observation. CPU scheduling and Python interpreter startup
        # are not evidence that a service has reached the tested lifecycle state.
        # The production thirty-second deadline and ownership validation remain
        # unchanged, and cleanup continues through real subprocess streams.
        original=self.m.capture_scope;advance=[0];seen=[]
        def capture(value,**kwargs):
            binding=original(value,**kwargs)
            if (value['ActiveState']==('active' if whole else 'activating') and (not assigned or binding[1]!='0')
                    and (not output or self.unit_file.with_suffix('.output').exists())):
                seen.append(binding);advance[0]=(self.m.OUTER_TIMEOUT if whole else self.m.STARTUP_TIMEOUT)+1
            return binding
        clock=SimpleNamespace(monotonic=lambda:time.monotonic()+advance[0])
        with patch.object(self.m,'time',clock),patch.object(self.m,'capture_scope',capture):yield seen

    def test_fixed_systemd_service_and_real_streamed_request(self):
        with patch.dict(os.environ,{'UNRELATED_SECRET':'INERT_SECRET_SENTINEL','SUDO_USER':'hostile'}):
            result=self.outer()
        self.assertEqual(result,self.result)
        calls=[json.loads(line) for line in self.logs.read_text().splitlines()]
        command=next(call for call in calls if call[0]=='systemd-run')
        for arg in ('--wait','--pipe','--collect','--quiet','--property=RuntimeMaxSec=2700',
                    '--property=KillMode=control-group','--property=TimeoutStopSec=15',
                    '--property=User=root','--property=Group=root','--property=Type=exec'):
            self.assertIn(arg,command)
        self.assertIn('--unit='+self.unit,command)
        self.assertEqual(command[-6:],[self.m.WORKER,'--systemd-worker','qa','901',str(os.getpid()),'12345'])
        self.assertNotIn('INERT_SECRET_SENTINEL',self.logs.read_text())
        self.assertFalse(any(call[1]=='kill' for call in calls if call[0]=='systemctl'))

    def test_errors_preflight_and_output_bounds_never_acknowledge(self):
        for value in ({'stderr':True},{'preflight':True},{'large':True}):
            self.control.write_text(json.dumps(value))
            if self.unit_file.exists():self.unit_file.unlink()
            with self.subTest(value=value):self.refuse(self.outer)

    def test_timeout_kills_only_owned_service_control_group(self):
        self.control.write_text(json.dumps({'sleep':True}))
        with self.expire_after_owned_observation(whole=True) as seen:self.refuse(self.outer)
        self.assertTrue(seen)
        calls=[json.loads(line) for line in self.logs.read_text().splitlines()]
        kills=[call for call in calls if call[:2]==['systemctl','kill']]
        self.assertEqual(len(kills),1)
        self.assertIn('--kill-whom=all',kills[0]);self.assertIn('--signal=SIGKILL',kills[0])
        self.assertEqual(kills[0][-1],self.unit)
        self.assertFalse(self.unit_file.exists())

    def test_startup_zero_pid_to_assigned_pid_waits_for_native_running(self):
        self.control.write_text(json.dumps({'transition':True}))
        self.assertEqual(self.outer(),self.result)
        calls=[json.loads(line) for line in self.logs.read_text().splitlines()]
        self.assertFalse(any(call[1]=='kill' for call in calls if call[0]=='systemctl'))

    def test_never_running_times_out_and_kills_owned_pid_assignment(self):
        self.control.write_text(json.dumps({'never_running':True}))
        with self.expire_after_owned_observation() as seen:self.refuse(self.outer)
        self.assertTrue(seen);self.assertEqual(seen[0][1],self.expected_scope['MainPID'])
        calls=[json.loads(line) for line in self.logs.read_text().splitlines()]
        kills=[call for call in calls if call[:2]==['systemctl','kill']]
        self.assertEqual(len(kills),1);self.assertEqual(kills[0][-1],self.unit)
        self.assertFalse(self.unit_file.exists())

    def test_slow_first_inspection_still_cleans_created_fixture_service(self):
        self.control.write_text(json.dumps({'never_running':True,'request_processing_delay':2}))
        original=self.m.show;calls=[0]
        def slow_inspection(unit):
            calls[0]+=1
            if calls[0]==2:
                deadline=time.monotonic()+10
                while not self.unit_file.exists():
                    self.assertLess(time.monotonic(),deadline,'service must exist independently of worker stdin')
                    time.sleep(.01)
            value=original(unit)
            if calls[0]==2:self.assertIsNotNone(value,'service must exist before worker request processing')
            return value
        with patch.object(self.m,'show',slow_inspection),self.expire_after_owned_observation(assigned=False) as seen:
            self.refuse(self.outer)
        self.assertTrue(seen);self.assertEqual(seen[0][1],'0')
        logged=[json.loads(line) for line in self.logs.read_text().splitlines()]
        self.assertEqual(len([call for call in logged if call[:2]==['systemctl','kill']]),1)
        self.assertFalse(self.unit_file.exists())

    def test_accepted_shaped_output_without_running_scope_is_refused(self):
        self.control.write_text(json.dumps({'activation_ack':True}))
        with self.expire_after_owned_observation(output=True) as seen:self.refuse(self.outer)
        self.assertTrue(seen)
        calls=[json.loads(line) for line in self.logs.read_text().splitlines()]
        self.assertEqual(len([call for call in calls if call[:2]==['systemctl','kill']]),1)

    def test_scope_capture_preserves_invocation_command_cgroup_and_assigned_pid(self):
        value=copy.deepcopy(self.expected_scope)
        value['ExecStart']='{ path='+self.m.WORKER+' ; argv[]='+self.m.WORKER+' --systemd-worker qa 901 123 456 ; ignore_errors=no ; }'
        initial={**value,'ActiveState':'activating','SubState':'start','MainPID':'0','ExecMainPID':'0'}
        capture=lambda item,owned=None:self.m.capture_scope(item,stage='qa',job=901,parent=123,ticks=456,owned=owned)
        zero=capture(initial);assigned=capture(value,zero)
        self.assertEqual(assigned[1],str(os.getpid()))
        self.assertEqual(capture({**initial,'ActiveState':'inactive'},assigned),assigned)
        for change in ({'InvocationID':'b'*32},{'ControlGroup':'/system.slice/other.service'},
                       {'ExecStart':'/bin/sh'},{'MainPID':str(os.getpid()+1),'ExecMainPID':str(os.getpid()+1)}):
            self.refuse(lambda:capture({**value,**change},assigned))

    def test_cleanup_can_bind_assigned_pid_after_zero_pid_capture(self):
        scope=copy.deepcopy(self.expected_scope)
        scope['ExecStart']='{ path='+self.m.WORKER+' ; argv[]='+self.m.WORKER+' --systemd-worker qa 901 123 456 ; ignore_errors=no ; }'
        self.unit_file.write_text(json.dumps({**scope,'ActiveState':'activating','SubState':'start'}))
        try:self.m.kill_owned('qa',901,123,456,('a'*32,'0','/system.slice/'+self.unit));cleaned=True
        except self.m.SupervisorError:cleaned=False
        self.assertTrue(cleaned,'cleanup must retain invocation binding across PID assignment')

    def test_startup_replacement_invocation_is_not_adopted_or_killed(self):
        original=self.m.show;seen=[False]
        def replaced(unit):
            value=original(unit)
            if value is not None and value.get('MainPID')=='0':seen[0]=True
            elif value is not None and seen[0]:value={**value,'InvocationID':'b'*32}
            return value
        self.control.write_text(json.dumps({'transition':True}))
        with patch.object(self.m,'show',replaced):self.refuse(self.outer)
        self.assertTrue(seen[0]);self.assertNotIn('"kill"',self.logs.read_text())

    def test_existing_unit_and_job_lock_contention_never_kill_other_work(self):
        self.unit_file.write_text(json.dumps(self.expected_scope))
        self.refuse(self.outer)
        self.assertNotIn('systemd-run',self.logs.read_text())
        self.assertNotIn('"kill"',self.logs.read_text())
        self.unit_file.unlink()
        with self.m.job_lock('qa',901):self.refuse(self.outer)
        self.assertNotIn('"kill"',self.logs.read_text())

    def test_sudo_os_identity_binding_is_checked_without_environment_boolean(self):
        self.patches[6].stop()
        user=type('User',(),{'pw_name':'xynes_qa_deploy','pw_uid':1005})()
        with patch.dict(os.environ,{'SUDO_USER':'xynes_qa_deploy','SUDO_UID':'1005'},clear=True),\
             patch.object(self.m.pwd,'getpwnam',return_value=user),patch.object(self.m.pwd,'getpwuid',return_value=user):
            self.m.sudo_identity('qa')
            self.refuse(lambda:self.m.sudo_identity('prod'))
            for value in ('0','1006','True','01005','-1','1005;id'):
                with patch.dict(os.environ,{'SUDO_UID':value}):self.refuse(lambda:self.m.sudo_identity('qa'))

    def test_worker_requires_real_cgroup_invocation_and_main_pid(self):
        args=['qa','901',str(os.getpid()),'12345']
        scope=copy.deepcopy(self.expected_scope)
        scope['ExecStart']='{ path='+self.m.WORKER+' ; argv[]='+self.m.WORKER+' --systemd-worker '+' '.join(args)+' ; ignore_errors=no ; }'
        self.unit_file.write_text(json.dumps(scope))
        with patch.dict(os.environ,{'INVOCATION_ID':'a'*32}),patch.object(self.m,'self_cgroup',return_value='/system.slice/'+self.unit),\
             patch.object(self.m,'parent_identity',lambda pid,ticks,stage:None):
            self.m.worker_binding('qa',901,os.getpid(),12345)
            for key,value in (('MainPID','1'),('InvocationID','b'*32),('KillMode','process'),
                              ('RuntimeMaxUSec','infinity'),('RuntimeMaxUSec','46min'),('ExecStart','/bin/sh'),
                              ('ControlGroup','/system.slice/other.service'),('User','nobody')):
                self.unit_file.write_text(json.dumps({**scope,key:value}))
                self.refuse(lambda:self.m.worker_binding('qa',901,os.getpid(),12345))
            self.unit_file.write_text(json.dumps(scope))
            with patch.object(self.m,'self_cgroup',return_value='/user.slice/arbitrary'):
                self.refuse(lambda:self.m.worker_binding('qa',901,os.getpid(),12345))

    def test_equivalent_finite_duration_formats_and_result_identity(self):
        for value in ('45min','2700s','45min 0s','2700000000us'):
            self.assertEqual(self.m.duration(value),2700)
        for value in ('infinity','NaN','-1s','2700;id',''):
            self.refuse(lambda:self.m.duration(value))
        self.m.accepted_result(self.m.canonical(self.result),self.request,'qa')
        for change in ({'deployment_accepted':False},{'stage':'prod'},{'token':'bad'},
                       {'controller':{**self.request['controller'],'commit':'f'*40}},{'preflight_ack':True}):
            self.refuse(lambda:self.m.accepted_result(self.m.canonical({**self.result,**change}),self.request,'qa'))

    def test_sealed_isolated_wrapper_cmdline_binds_root_parent_start_ticks(self):
        self.patches[7].stop()
        process_path=self.root/'proc-parent';process_path.mkdir()
        cmdline=process_path/'cmdline'
        expected=('/usr/bin/python3\0-ISB\0'+self.m.WORKER+'\0qa\0').encode()
        cmdline.write_bytes(expected)
        with patch.object(self.m,'process_identity',return_value=(1234,5678)),             patch.object(self.m.Path,'read_bytes',return_value=expected):
            self.m.parent_identity(1234,5678,'qa')
            self.refuse(lambda:self.m.parent_identity(1234,5679,'qa'))
            self.refuse(lambda:self.m.parent_identity(1234,5678,'prod'))
        for value in (b'/bin/sh\0-c\0id\0',('/usr/bin/python3\0-I\0'+self.m.WORKER+'\0qa\0extra\0').encode()):
            with patch.object(self.m,'process_identity',return_value=(1234,5678)),                 patch.object(self.m.Path,'read_bytes',return_value=value):
                self.refuse(lambda:self.m.parent_identity(1234,5678,'qa'))

    def test_parent_death_watchdog_always_stops_worker_even_when_kill_races(self):
        read,write=os.pipe();stopped=threading.Event();errors=[]
        def failed(*args):raise self.m.SupervisorError(self.m.ERROR)
        def own_kill(pid,number):
            self.assertEqual(pid,os.getpid());self.assertEqual(number,self.m.signal.SIGKILL);stopped.set()
        try:
            with patch.object(self.m.os,'pidfd_open',create=True,side_effect=lambda pid,flags:os.dup(read)),                 patch.object(self.m,'parent_identity',lambda *args:None),                 patch.object(self.m,'kill_owned',failed),patch.object(self.m.os,'kill',own_kill),                 patch.object(threading,'excepthook',side_effect=lambda args:errors.append(args)):
                with self.m.parent_watchdog('qa',901,os.getpid(),12345,('a'*32,str(os.getpid()),'/system.slice/'+self.unit)):
                    os.write(write,b'x');self.assertTrue(stopped.wait(2))
                self.assertEqual(errors,[])
        finally:os.close(read);os.close(write)

    def test_ack_requires_actual_accepted_private_ledger_observation(self):
        self.patches[8].stop()
        directory=self.root/'state';directory.mkdir(mode=0o700)
        result=copy.deepcopy(self.result)
        with patch.object(self.m,'STATE_DIRECTORY',directory):
            self.refuse(lambda:self.m.observe_accepted(result,self.request,'qa'))
            with self.m.host_release_state.ReleaseState(directory,stage='qa',owner_uid=os.getuid()) as state:
                token=state.begin(self.request,source_sha256='sha256:'+result['source_manifest_sha256'],started_at=100)
                result['token']=token
            self.refuse(lambda:self.m.observe_accepted(result,self.request,'qa'))
            with self.m.host_release_state.ReleaseState(directory,stage='qa',owner_uid=os.getuid()) as state:
                state.accept(token,smoke={'storage_verified':True,'stage':'qa','publication':self.request['publication'],
                    'source_sha256':'sha256:'+result['source_manifest_sha256'],'result':'passed','observed_at':101},accepted_at=102)
            self.m.observe_accepted(result,self.request,'qa')
            changed={**self.request,'deployment':{**self.request['deployment'],'job_id':902}}
            self.refuse(lambda:self.m.observe_accepted(result,changed,'qa'))

    def test_worker_failure_writes_private_record_once_without_secrets(self):
        import root_executor,host_runtime
        secret='ghp_'+'Y'*36;deploy=self.root/'deploy';deploy.mkdir(mode=0o700);diagnostics=deploy/'diagnostics'
        read,write=os.pipe();self.addCleanup(os.close,read);self.addCleanup(os.close,write)
        parsed={**self.request,'stage':'qa'}
        def runtime(diagnosed):
            class Runtime:
                def __init__(self,stage):pass
                def execute(self,raw):
                    try:raise root_executor.ExecutorError(diagnosed=diagnosed)
                    except Exception:raise ValueError(secret) from None
            return Runtime
        common=(patch.object(self.m,'ROOT_UID',os.getuid()),patch.object(root_executor,'ROOT_UID',os.getuid()),
                patch.object(root_executor,'DIAGNOSTICS',diagnostics),patch.object(self.m.os,'isatty',return_value=False))
        with contextlib.ExitStack() as stack:
            for item in common:stack.enter_context(item)
            with patch.object(self.m,'worker_binding',side_effect=ValueError(secret)):
                self.refuse(lambda:self.m.worker('qa',901,os.getpid(),12345,fd=read))
            path=diagnostics/'qa'/'worker-901.json';raw=path.read_bytes();record=json.loads(raw)
            self.assertNotIn(secret.encode(),raw);self.assertEqual(path.stat().st_mode&0o777,0o600)
            self.assertEqual((record['scope'],record['controller'],record['exceptions'][0]['class']),('worker',None,'ValueError'))
            path.unlink()
            stack.enter_context(patch.object(self.m,'worker_binding',return_value=('a'*32,'1','/system.slice/x')))
            stack.enter_context(patch.object(self.m.deploy_request,'receive',return_value=parsed))
            stack.enter_context(patch.object(self.m,'parent_watchdog',lambda *args:contextlib.nullcontext()))
            # The executor already recorded this failure: the worker adds nothing.
            with patch.object(host_runtime,'Runtime',runtime(True)):
                self.refuse(lambda:self.m.worker('qa',901,os.getpid(),12345,fd=read))
            self.assertEqual(list((diagnostics/'qa').iterdir()),[])
            with patch.object(host_runtime,'Runtime',runtime(False)):
                self.refuse(lambda:self.m.worker('qa',901,os.getpid(),12345,fd=read))
            record=json.loads(path.read_bytes())
            self.assertEqual(record['deployment'],self.request['deployment'])
            self.assertEqual([row['class'] for row in record['exceptions']],['ValueError','ExecutorError'])
            # A failing record write never changes the supervisor refusal.
            path.unlink();(diagnostics/'qa').chmod(0o755)
            with patch.object(host_runtime,'Runtime',runtime(False)):
                self.refuse(lambda:self.m.worker('qa',901,os.getpid(),12345,fd=read))
            self.assertFalse(path.exists())


if __name__=='__main__':unittest.main()
