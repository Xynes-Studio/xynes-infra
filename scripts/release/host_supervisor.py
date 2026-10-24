#!/usr/bin/env python3
"""Root systemd service supervision; no deployment/preflight success substitute.

Outer sudo entry accepts only the account's literal stage and bounded deployment
request. It invokes only the installed immutable root wrapper in a transient
service with a 45-minute cgroup limit. Internal worker arguments additionally
carry root-derived outer PID/start ticks, never request data. The worker verifies
actual systemd properties, kernel cgroup membership and InvocationID, then opens
an actual Linux pidfd watchdog: outer death kills the verified owned service.

Install this module in the immutable boundary bundle, wire root_executor.main to
main only after concrete host_runtime.Runtime is complete, and retain the boundary
ledger wrapper hash. Fixed private directories must be root-owned. No shell,
request-selected command/env/path, Docker cleanup or app/data removal is exposed.
Cancelled work does not rewrite release state; pending or uncertain acceptance
must be reconciled from the root deployment ledger, never inferred from an ACK.
"""
from contextlib import contextmanager
from decimal import Decimal
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import select
import selectors
import signal
import stat
import subprocess
import sys
import threading
import time

try:
    from . import deploy_request, host_release_state
except ImportError:
    import deploy_request, host_release_state

ERROR='Root deployment supervision refused'
ROOT_UID=0
WORKER='/usr/local/libexec/xynes-deploy-root'
SYSTEMD_RUN='/usr/bin/systemd-run'
SYSTEMCTL='/usr/bin/systemctl'
BOUNDARY=Path('/etc/xynes/deploy-boundary.json')
STATE_DIRECTORY=Path('/var/lib/xynes/deploy/state')
LOCK_DIRECTORY=Path('/var/lib/xynes/deploy/work/supervisor-locks')
CLEAN_ENV={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','HOME':'/nonexistent','LANG':'C','LC_ALL':'C'}
OUTER_TIMEOUT=2775
STARTUP_TIMEOUT=30
MAX_OUTPUT=65536
PROPERTIES=('Id','LoadState','ActiveState','SubState','MainPID','ExecMainPID','InvocationID','ControlGroup',
            'User','Group','Type','KillMode','RuntimeMaxUSec','TimeoutStopUSec','Transient','CollectMode','ExecStart')


class SupervisorError(ValueError):pass


def require(value):
    if not value:raise SupervisorError(ERROR)


def integer(value):
    require(type(value) is str and re.fullmatch(r'[1-9][0-9]{0,15}',value) and int(value)<=9007199254740991)
    return int(value)


def unit_name(stage,job):
    require(type(stage) is str and stage in deploy_request.STAGES
            and type(job) is int and 0<job<=deploy_request.MAX_ID)
    return 'xynes-apex-deploy-'+stage+'-'+str(job)+'.service'


def canonical(request):
    return (json.dumps(request,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode('ascii')


def identity(info):
    return (info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_nlink,
            info.st_size,info.st_mtime_ns,info.st_ctime_ns)


def root_path(path,*,directory=False):
    for parent in reversed((path,*path.parents[:-1])):
        info=parent.lstat()
        require(info.st_uid==ROOT_UID and not info.st_mode & 0o022 and not stat.S_ISLNK(info.st_mode)
                and (stat.S_ISDIR(info.st_mode) if parent!=path or directory else stat.S_ISREG(info.st_mode)))


def root_bytes(path):
    root_path(path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
    try:
        before=os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_uid==ROOT_UID and before.st_nlink==1
                and 0<before.st_size<=1024*1024)
        data=bytearray()
        while len(data)<=1024*1024:
            chunk=os.read(fd,65536)
            if not chunk:break
            data.extend(chunk)
        require(len(data)==before.st_size and identity(os.fstat(fd))==identity(before)
                and identity(path.lstat())==identity(before))
        return bytes(data)
    finally:os.close(fd)


def fixed_wrapper():
    ledger=json.loads(root_bytes(BOUNDARY))
    require(type(ledger) is dict and set(ledger)=={'schema_version','plan_sha256','bundle_sha256','files'}
            and type(ledger['schema_version']) is int and ledger['schema_version']==1
            and type(ledger['files']) is dict)
    raw=root_bytes(Path(WORKER))
    require(stat.S_IMODE(Path(WORKER).stat().st_mode)==0o555
            and ledger['files'].get(WORKER)==hashlib.sha256(raw).hexdigest())
    root_path(Path(SYSTEMD_RUN));root_path(Path(SYSTEMCTL))


def sudo_identity(stage):
    require(os.geteuid()==ROOT_UID and type(stage) is str and stage in deploy_request.STAGES)
    name=os.environ.get('SUDO_USER');number=integer(os.environ.get('SUDO_UID'))
    require(name=='xynes_'+stage+'_deploy' and 0<number<=2147483647)
    named=pwd.getpwnam(name);numbered=pwd.getpwuid(number)
    require(named.pw_name==numbered.pw_name==name and named.pw_uid==numbered.pw_uid==number)


@contextmanager
def job_lock(stage,job):
    fd=None;directory=None
    try:
        root_path(LOCK_DIRECTORY,directory=True)
        directory=os.open(LOCK_DIRECTORY,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
        info=os.fstat(directory)
        require(info.st_uid==ROOT_UID and stat.S_IMODE(info.st_mode)==0o700)
        name=unit_name(stage,job)+'.lock'
        fd=os.open(name,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,0o600,dir_fd=directory)
        info=os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid==ROOT_UID and info.st_nlink==1
                and info.st_size==0 and stat.S_IMODE(info.st_mode)==0o600
                and identity(os.stat(name,dir_fd=directory,follow_symlinks=False))==identity(info))
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    except Exception:raise SupervisorError(ERROR) from None
    finally:
        if fd is not None:os.close(fd)
        if directory is not None:os.close(directory)


def duration(value):
    try:
        require(type(value) is str and 0<len(value)<=100)
        if re.fullmatch(r'[0-9]+',value):return Decimal(value)/Decimal(1000000)
        units={'h':3600,'hr':3600,'min':60,'m':60,'s':1,'sec':1,'ms':Decimal('.001'),
               'us':Decimal('.000001'),'µs':Decimal('.000001')}
        found=list(re.finditer(r'([0-9]+(?:\.[0-9]+)?)\s*(h|hr|min|m|sec|s|ms|us|µs)(?:\s+|$)',value))
        require(found and ''.join(match.group(0) for match in found)==value)
        result=sum((Decimal(match.group(1))*Decimal(units[match.group(2)]) for match in found),Decimal(0))
        require(result.is_finite());return result
    except Exception:raise SupervisorError(ERROR) from None


def bounded(argv,*,timeout=10):
    process=None
    try:
        require(type(argv) is list and argv[0]==SYSTEMCTL)
        process=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                 env=CLEAN_ENV,cwd='/',start_new_session=True)
        deadline=time.monotonic()+timeout;out=bytearray();errors=0
        selector=selectors.DefaultSelector();selector.register(process.stdout,selectors.EVENT_READ,'out')
        selector.register(process.stderr,selectors.EVENT_READ,'err')
        with selector:
            while selector.get_map():
                require(time.monotonic()<deadline)
                for key,_ in selector.select(min(.1,max(0,deadline-time.monotonic()))):
                    chunk=os.read(key.fileobj.fileno(),65536)
                    if not chunk:selector.unregister(key.fileobj);continue
                    if key.data=='out':out.extend(chunk);require(len(out)<=MAX_OUTPUT)
                    else:errors+=len(chunk);require(errors<=MAX_OUTPUT)
            code=process.wait(timeout=max(.001,deadline-time.monotonic()))
        return code,bytes(out)
    except Exception:raise SupervisorError(ERROR) from None
    finally:
        if process:
            if process.poll() is None:os.killpg(process.pid,signal.SIGKILL);process.wait()
            process.stdout.close();process.stderr.close()


def show(unit):
    code,raw=bounded([SYSTEMCTL,'show','--no-pager','--property='+','.join(PROPERTIES),unit])
    value={}
    for line in raw.decode('utf-8').splitlines():
        key,separator,item=line.partition('=');require(separator and key not in value and key in PROPERTIES)
        value[key]=item
    if value.get('LoadState')=='not-found':return None
    require(code==0 and set(value)==set(PROPERTIES));return value


def scope_binding(value,*,stage,job,parent,ticks,live=True):
    unit=unit_name(stage,job)
    require(type(value) is dict and set(value)==set(PROPERTIES) and value['Id']==unit and value['LoadState']=='loaded'
            and value['User']=='root' and value['Group']=='root' and value['Type']=='exec'
            and value['KillMode']=='control-group' and value['Transient']=='yes'
            and value['CollectMode']=='inactive-or-failed' and duration(value['RuntimeMaxUSec'])==2700
            and duration(value['TimeoutStopUSec'])==15 and value['ControlGroup']=='/system.slice/'+unit
            and re.fullmatch(r'[a-f0-9]{32}',value['InvocationID']))
    prefix='{ path='+WORKER+' ; argv[]='+WORKER+' --systemd-worker '+stage+' '+str(job)+' '+str(parent)+' '+str(ticks)+' ; ignore_errors=no ;'
    require(value['ExecStart'].startswith(prefix) and len(value['ExecStart'])<=8192)
    require(re.fullmatch(r'[0-9]+',value['MainPID']) and re.fullmatch(r'[0-9]+',value['ExecMainPID']))
    if live:require(value['ActiveState']=='active' and value['SubState']=='running'
                    and int(value['MainPID'])>0 and value['MainPID']==value['ExecMainPID'])
    else:require(value['ActiveState'] in ('active','activating','deactivating','failed','inactive'))
    return (value['InvocationID'],value['MainPID'],value['ControlGroup'])


def capture_scope(value,*,stage,job,parent,ticks,owned=None):
    """Preserve one invocation while its initial zero MainPID is assigned.

    Once a nonzero PID has been observed it may only stay the same or disappear
    on exit; a replacement invocation, cgroup or process is never adopted.
    Every observation revalidates the fixed ExecStart and service properties.
    """
    found=scope_binding(value,stage=stage,job=job,parent=parent,ticks=ticks,live=False)
    if owned is None:return found
    require(found[0]==owned[0] and found[2]==owned[2]
            and (owned[1]=='0' or found[1] in (owned[1],'0')))
    return (found[0],found[1] if found[1]!='0' else owned[1],found[2])


def process_identity(pid=None):
    pid=os.getpid() if pid is None else pid
    require(type(pid) is int and 0<pid<=2147483647)
    path=Path('/proc')/str(pid)
    require(path.stat().st_uid==ROOT_UID)
    raw=(path/'stat').read_text();end=raw.rfind(')')
    require(end>0 and raw[:raw.find(' ')]==str(pid))
    fields=raw[end+2:].split();require(len(fields)>=20)
    return pid,integer(fields[19])


def parent_identity(parent,ticks,stage):
    require(process_identity(parent)==(parent,ticks))
    raw=(Path('/proc')/str(parent)/'cmdline').read_bytes()
    require(len(raw)<=8192 and raw.endswith(b'\0'))
    values=raw[:-1].split(b'\0')
    require(values==[b'/usr/bin/python3',b'-ISB',WORKER.encode(),stage.encode()]
            or values==[WORKER.encode(),stage.encode()])


def self_cgroup():
    raw=Path('/proc/self/cgroup').read_text();require(len(raw)<=8192)
    values=[]
    for line in raw.splitlines():
        fields=line.split(':',2);require(len(fields)==3)
        if fields[0]=='0' and fields[1]=='' or 'name=systemd' in fields[1].split(','):values.append(fields[2])
    require(len(values)==1);return values[0]


def worker_binding(stage,job,parent,ticks):
    require(os.geteuid()==ROOT_UID);fixed_wrapper();parent_identity(parent,ticks,stage)
    value=show(unit_name(stage,job));owned=scope_binding(value,stage=stage,job=job,parent=parent,ticks=ticks)
    require(value['MainPID']==str(os.getpid()) and value['ExecMainPID']==str(os.getpid())
            and self_cgroup()==value['ControlGroup'] and os.environ.get('INVOCATION_ID')==value['InvocationID'])
    return owned


def kill_owned(stage,job,parent,ticks,owned):
    value=show(unit_name(stage,job))
    if value is None:return
    capture_scope(value,stage=stage,job=job,parent=parent,ticks=ticks,owned=owned)
    code,_=bounded([SYSTEMCTL,'kill','--kill-whom=all','--signal=SIGKILL',unit_name(stage,job)])
    require(code==0)
    code,_=bounded([SYSTEMCTL,'stop',unit_name(stage,job)],timeout=20);require(code==0)
    remaining=show(unit_name(stage,job))
    require(remaining is None or (remaining.get('InvocationID')==owned[0]
            and remaining.get('ActiveState') in ('inactive','failed') and remaining.get('MainPID')=='0'))


def accepted_result(raw,request,stage):
    try:
        value=json.loads(raw)
        keys={'artifact_kind','stage','token','controller','publication','source_manifest_sha256','deployment_accepted'}
        require(type(value) is dict and set(value)==keys and value['artifact_kind']=='root-deployment-result'
                and value['stage']==stage and value['controller']==request['controller']
                and value['publication']==request['publication'] and value['deployment_accepted'] is True
                and type(value['token']) is str and re.fullmatch(r'[a-f0-9]{64}',value['token'])
                and type(value['source_manifest_sha256']) is str and re.fullmatch(r'[a-f0-9]{64}',value['source_manifest_sha256']))
        require(raw==canonical(value));return value
    except Exception:raise SupervisorError(ERROR) from None


def observe_accepted(result,request,stage):
    with host_release_state.ReleaseState(STATE_DIRECTORY,stage=stage,owner_uid=ROOT_UID) as state:
        state.read_accepted();require(state._read()==state.data)
        matching=[record for record in state.data['attempts'] if record['token']==result['token']]
        require(len(matching)==1 and matching[0]['status']=='accepted' and matching[0]['request']==request
                and matching[0]['source_sha256']=='sha256:'+result['source_manifest_sha256'])


def service_stream(argv,data,*,stage,job,parent,ticks):
    process=None;owned=None;complete=False;running=False
    try:
        process=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                 env=CLEAN_ENV,cwd='/',start_new_session=True)
        now=time.monotonic();deadline=now+OUTER_TIMEOUT;startup_deadline=min(deadline,now+STARTUP_TIMEOUT)
        out=bytearray();errors=0;offset=0;next_inspect=0
        selector=selectors.DefaultSelector()
        for stream in (process.stdin,process.stdout,process.stderr):os.set_blocking(stream.fileno(),False)
        selector.register(process.stdin,selectors.EVENT_WRITE,'in');selector.register(process.stdout,selectors.EVENT_READ,'out')
        selector.register(process.stderr,selectors.EVENT_READ,'err')
        with selector:
            while selector.get_map() or not running:
                require(time.monotonic()<deadline)
                if not running:
                    require(time.monotonic()<startup_deadline)
                if not running and time.monotonic()>=next_inspect:
                    value=show(unit_name(stage,job))
                    if value is not None:
                        owned=capture_scope(value,stage=stage,job=job,parent=parent,ticks=ticks,owned=owned)
                        if value['ActiveState']=='active' and value['SubState']=='running':
                            scope_binding(value,stage=stage,job=job,parent=parent,ticks=ticks);running=True
                        else:
                            require(value['ActiveState']=='activating'
                                    and value['SubState'] in ('start','start-pre','start-post','condition'))
                    else:require(owned is None)
                    next_inspect=time.monotonic()+.05
                for key,_ in selector.select(min(.05,max(0,deadline-time.monotonic()))):
                    if key.data=='in':
                        count=os.write(key.fileobj.fileno(),data[offset:]);require(count>0);offset+=count
                        if offset==len(data):selector.unregister(key.fileobj);process.stdin.close()
                    else:
                        chunk=os.read(key.fileobj.fileno(),65536)
                        if not chunk:selector.unregister(key.fileobj);continue
                        if key.data=='out':out.extend(chunk);require(len(out)<=MAX_OUTPUT)
                        else:errors+=len(chunk);require(errors<=MAX_OUTPUT)
            require(running and process.wait(timeout=max(.001,deadline-time.monotonic()))==0 and offset==len(data))
        complete=True;return bytes(out)
    except Exception:raise SupervisorError(ERROR) from None
    finally:
        if process:
            try:
                if not complete:
                    try:
                        if owned is None:
                            value=show(unit_name(stage,job))
                            if value is not None:owned=capture_scope(value,stage=stage,job=job,parent=parent,ticks=ticks)
                        if owned is not None:kill_owned(stage,job,parent,ticks,owned)
                    finally:
                        # An unowned/replaced unit refusal must still stop our
                        # own systemd-run client and close its private pipes.
                        if process.poll() is None:os.killpg(process.pid,signal.SIGKILL);process.wait()
            finally:
                for stream in (process.stdin,process.stdout,process.stderr):
                    if stream and not stream.closed:stream.close()


def outer(stage,*,fd=0):
    try:
        require(os.geteuid()==ROOT_UID and not any(os.isatty(item) for item in (fd,1,2)))
        sudo_identity(stage);fixed_wrapper()
        parsed=deploy_request.receive(fd,stage=stage,timeout_seconds=5)
        request={key:value for key,value in parsed.items() if key!='stage'};data=canonical(request)
        job=request['deployment']['job_id'];parent,ticks=process_identity()
        argv=[SYSTEMD_RUN,'--quiet','--wait','--pipe','--collect','--unit='+unit_name(stage,job),
            '--property=Type=exec','--property=User=root','--property=Group=root','--property=RuntimeMaxSec=2700',
            '--property=KillMode=control-group','--property=TimeoutStopSec=15','--property=PassEnvironment=',
            '--property=Environment=PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C LC_ALL=C',
            WORKER,'--systemd-worker',stage,str(job),str(parent),str(ticks)]
        with job_lock(stage,job):
            require(show(unit_name(stage,job)) is None)
            raw=service_stream(argv,data,stage=stage,job=job,parent=parent,ticks=ticks)
            result=accepted_result(raw,request,stage);observe_accepted(result,request,stage);return result
    except Exception:raise SupervisorError(ERROR) from None


@contextmanager
def parent_watchdog(stage,job,parent,ticks,owned):
    fd=None;stop=threading.Event();thread=None
    try:
        require(hasattr(os,'pidfd_open'));parent_identity(parent,ticks,stage)
        fd=os.pidfd_open(parent,0);parent_identity(parent,ticks,stage)
        def watch():
            while not stop.is_set():
                ready,_,_=select.select([fd],[],[],.1)
                if ready:
                    try:kill_owned(stage,job,parent,ticks,owned)
                    except Exception:pass
                    finally:
                        # Main death also triggers the verified control-group stop
                        # when a concurrent unit collection races explicit kill.
                        os.kill(os.getpid(),signal.SIGKILL)
                    return
        thread=threading.Thread(target=watch,name='apex-parent-watchdog',daemon=True);thread.start()
        yield
    except Exception:raise SupervisorError(ERROR) from None
    finally:
        stop.set()
        if thread:thread.join(timeout=1)
        if fd is not None:os.close(fd)


def diagnose_worker(error,stage,job,request):
    """Private root record for worker failures the executor did not already record."""
    try:
        try:from . import root_executor
        except ImportError:import root_executor
        if type(job) is int and not any(type(item).__name__=='ExecutorError' and getattr(item,'diagnosed',None) is True
                                        for item,_ in root_executor.chain(error)):
            root_executor.diagnose(error,stage=stage,scope='worker',name='worker-'+str(job)+'.json',request=request)
    except Exception:pass


def worker(stage,job,parent,ticks,*,fd=0):
    request=None
    try:
        require(os.geteuid()==ROOT_UID and not any(os.isatty(item) for item in (fd,1,2)))
        owned=worker_binding(stage,job,parent,ticks)
        parsed=deploy_request.receive(fd,stage=stage,timeout_seconds=5)
        request={key:value for key,value in parsed.items() if key!='stage'}
        require(request['deployment']['job_id']==job)
        try:from .host_runtime import Runtime
        except ImportError:from host_runtime import Runtime
        with parent_watchdog(stage,job,parent,ticks,owned):
            result=Runtime(stage).execute(canonical(request))
            result=accepted_result(canonical(result),request,stage);observe_accepted(result,request,stage)
            return result
    except Exception as error:
        diagnose_worker(error,stage,job,request)
        raise SupervisorError(ERROR) from None


@contextmanager
def cancellation_handlers():
    old={}
    def cancelled(number,frame):raise SupervisorError(ERROR)
    try:
        for number in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP):
            old[number]=signal.getsignal(number);signal.signal(number,cancelled)
        yield
    finally:
        for number,handler in old.items():signal.signal(number,handler)


def main():
    try:
        if len(sys.argv)==2:
            with cancellation_handlers():result=outer(sys.argv[1])
        else:
            require(len(sys.argv)==6 and sys.argv[1]=='--systemd-worker')
            result=worker(sys.argv[2],integer(sys.argv[3]),integer(sys.argv[4]),integer(sys.argv[5]))
        sys.stdout.buffer.write(canonical(result));sys.stdout.buffer.flush();return 0
    except Exception:
        print(ERROR,file=sys.stderr);return 1


if __name__=='__main__':sys.exit(main())
