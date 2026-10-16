#!/usr/bin/env python3
"""Encrypted offsite backup phase; no restore or deployment authority.

The installed coordinator supplies literal configuration and holds the host lock
and warm leases. Credentials stay inside their respective native child processes.
Only the public age recipient belongs on the VPS. Independent offsite retrieval,
restore and recovered product acceptance remain separate operations.
"""
from dataclasses import dataclass, field
import hashlib
import copy
import fcntl
import http.client
from urllib.parse import urlsplit
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
from types import SimpleNamespace

try:
    from . import github_read, host_release_state, pg_container_bridge
except ImportError:
    import github_read, host_release_state, pg_container_bridge

ERROR='Hosted encrypted backup refused'
# Fixed reviewed policy for local ciphertext copies; B2 retention is independent.
LOCAL_RETENTION_DAYS=7
ROOT=Path(__file__).resolve().parents[2]
ACCEPTED_TABLES=('auth.users','platform.workspaces','platform.routes','authz.roles',
    'authz.permissions','authz.role_permissions','authz.user_roles','cms.content_entries',
    'cms.content_directories','cms.content_types','docs.documents','platform.storage_objects',
    'platform.storage_object_variants','platform.storage_processing_jobs','platform.storage_upload_sessions')

class BackupError(ValueError):pass

def require(value):
    if not value:raise BackupError(ERROR)

@dataclass(frozen=True,repr=False)
class Configuration:
    root:Path=field(default=Path('/'))
    platform:str=field(default='linux-amd64')
    toolchain:Path=field(default=ROOT/'infra/release/backup-toolchain.json')

    @property
    def uid(self):return 0 if self.root==Path('/') else os.geteuid()

    def path(self,value):
        require(type(value) is str and value.startswith('/'))
        return self.root/value.lstrip('/')

def protected(path,config,*,directory=False):
    require(isinstance(path,Path) and path.is_absolute())
    # Alternate roots are explicit inert test integration settings, never stdin.
    limit=config.root if path==config.root or config.root in path.parents else Path('/')
    require(path.resolve(strict=True)==path)
    selected=[]
    current=path
    while True:
        selected.append(current)
        if current==limit:break
        current=current.parent
    for current in reversed(selected):
        value=current.lstat()
        require(value.st_uid==config.uid and not value.st_mode&0o022 and not stat.S_ISLNK(value.st_mode)
                and (stat.S_ISDIR(value.st_mode) if current!=path or directory else stat.S_ISREG(value.st_mode)))

def stamp(info):
    return (info.st_dev,info.st_ino,info.st_uid,info.st_mode,info.st_nlink,
            info.st_size,info.st_mtime_ns,info.st_ctime_ns)

def read(path,config,*,limit=65536,mode=None):
    protected(path,config)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
    with os.fdopen(fd,'rb') as file:
        before=os.fstat(file.fileno())
        require(before.st_nlink==1 and 0<before.st_size<=limit
                and (mode is None or stat.S_IMODE(before.st_mode)==mode))
        raw=file.read(limit+1)
        require(len(raw)==before.st_size and stamp(before)==stamp(os.fstat(file.fileno()))
                and stamp(before)==stamp(path.lstat()))
        return raw

def file_digest(path,config):
    protected(path,config)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
    with os.fdopen(fd,'rb') as file:
        before=os.fstat(file.fileno());require(before.st_nlink==1 and 0<before.st_size<=10*1024**3)
        digest=hashlib.sha1();size=0
        for chunk in iter(lambda:file.read(65536),b''):
            size+=len(chunk);require(size<=before.st_size);digest.update(chunk)
        require(size==before.st_size and stamp(before)==stamp(os.fstat(file.fileno()))
                and stamp(before)==stamp(path.lstat()))
        return digest.hexdigest()

def credentials(config):
    raw=read(config.path('/etc/xynes/deploy/backup.env'),config,mode=0o600).decode()
    result={}
    for line in raw.splitlines():
        key,sep,value=line.partition('=')
        require(sep and key not in result and value and len(value)<=256
                and not any(c.isspace() for c in value))
        result[key]=value
    require(set(result)=={'B2_BACKUP_BUCKET','B2_KEY_ID','B2_APPLICATION_KEY'}
            and re.fullmatch(r'[A-Za-z0-9-]{6,63}',result['B2_BACKUP_BUCKET']))
    return result

def binaries(config):
    chain=json.loads(read(config.toolchain,config))
    require(chain['schema_version']==1 and chain['age']['version']=='1.3.2'
            and chain['b2']['version']=='5.0.0' and config.platform in ('linux-amd64','darwin-arm64'))
    if config.root==Path('/'):
        require(os.geteuid()==0 and sys.platform.startswith('linux')
                and platform.machine() in ('x86_64','amd64') and config.platform=='linux-amd64')
    result={}
    for name,relative,expected in (
        ('age','/usr/local/lib/xynes/tools/age/1.3.2/age',chain['age'][config.platform]['binaries']['age']),
        ('b2','/usr/local/lib/xynes/tools/b2/5.0.0/b2',chain['b2']['linux-amd64']['binary_sha256'])):
        path=config.path(relative);raw=read(path,config,limit=64*1024**2)
        require(path.stat().st_mode&0o111 and hashlib.sha256(raw).hexdigest()==expected)
        result[name]=path
    return result

def bridge(stage,config):
    base=pg_container_bridge.BASE.copy();base[0]=str(config.path('/usr/bin/docker'))
    def environment(_):
        raw=read(config.path('/srv/xynes/'+stage+'/env/.env.'+stage),config,mode=0o640).decode()
        values={}
        for line in raw.splitlines():
            if not line or line.startswith('#'):continue
            key,sep,value=line.partition('=');require(sep and key not in values);values[key]=value
        require(values.get('XYNES_ENV')==stage and re.fullmatch(r'[a-f0-9]{64}',values.get('POSTGRES_PASSWORD','')))
        return {key:values[key] for key in ('XYNES_ENV','POSTGRES_PASSWORD')}
    previous=pg_container_bridge.BASE
    pg_container_bridge.BASE=base
    instance=pg_container_bridge.Bridge(stage,work_root=config.path('/srv/xynes/'+stage+'/backups'),
        owner_uid=config.uid,environment_loader=environment)
    return instance,previous

def bounded(argv,env,*,limit=1024*1024,output_fd=None):
    child=None
    try:
        child=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,env=env,cwd='/',start_new_session=True)
        selector=selectors.DefaultSelector();out=bytearray();errors=0;size=0;deadline=time.monotonic()+600
        with selector:
            selector.register(child.stdout,selectors.EVENT_READ,'out');selector.register(child.stderr,selectors.EVENT_READ,'err')
            while selector.get_map():
                require(time.monotonic()<deadline)
                for key,_ in selector.select(min(1,deadline-time.monotonic())):
                    raw=os.read(key.fd,65536)
                    if not raw:selector.unregister(key.fileobj);continue
                    if key.data=='out':
                        size+=len(raw);require(size<=limit)
                        if output_fd is None:out.extend(raw)
                        else:
                            os.set_blocking(output_fd,False)
                            pg_container_bridge.write_all(output_fd,raw,deadline)
                    else:errors+=len(raw);require(errors<=65536)
            require(child.wait(timeout=max(.01,deadline-time.monotonic()))==0)
        return bytes(out)
    finally:
        if child is not None:
            # wait() has already reaped a successful child; its PID may be reused.
            if child.returncode is None:
                try:os.killpg(child.pid,signal.SIGKILL)
                except ProcessLookupError:pass
            child.wait()
            child.stdout.close();child.stderr.close()

def wrapper(*,stage,program,configuration,args):
    previous=None
    try:
        require(stage in ('qa','staging','prod') and type(args) is list)
        config=configuration;tools=binaries(config)
        output=config.path('/srv/xynes/'+stage+'/backups');protected(output,config,directory=True)
        if program in ('psql','pg_dump','pg_restore'):
            instance,previous=bridge(stage,config)
            return instance.run(program,args)
        clean={'PATH':'/usr/bin:/bin','HOME':str(output),'LANG':'C','LC_ALL':'C'}
        if program=='age':
            require(len(args)==3 and args[0]=='--recipients-file')
            recipient,archive=map(Path,args[1:])
            require(recipient.parent==archive.parent and recipient.name=='recipients.txt' and archive.name=='bundle.tar'
                    and recipient.parent.parent==output and recipient.parent.name.startswith('.backup-'))
            protected(recipient,config);protected(archive,config)
            raw=bounded([str(tools['age']),*args],clean,limit=10*1024**3,output_fd=1)
        else:
            require(program=='b2' and len(args)==8 and args[:4]==['file','upload','--no-progress','--sha1']
                    and re.fullmatch(r'[a-f0-9]{40}',args[4]))
            values=credentials(config);path=Path(args[6])
            require(args[5]==values['B2_BACKUP_BUCKET'] and path.name in ('backup.tar.age','record.json')
                    and path.parent.parent==output and args[7].startswith(stage+'/')
                    and '..' not in args[7].split('/'))
            protected(path,config)
            require(file_digest(path,config)==args[4])
            cache=output/'b2-account-info';clean.update(B2_APPLICATION_KEY_ID=values['B2_KEY_ID'],
                B2_APPLICATION_KEY=values['B2_APPLICATION_KEY'],B2_ACCOUNT_INFO=str(cache))
            raw=bounded([str(tools['b2']),*args],clean)
            if cache.exists():read(cache,config,limit=4*1024**2,mode=0o600)
        sys.stdout.buffer.write(raw);sys.stdout.buffer.flush();return 0
    except Exception:
        print(ERROR,file=sys.stderr);return 1
    finally:
        if previous is not None:pg_container_bridge.BASE=previous

def _create(state,token,*,configuration=Configuration(),phase=None,scheduled=False):
    old_env=None;old_umask=None;previous=None
    try:
        config=configuration
        require(type(config) is Configuration and type(state) is host_release_state.ReleaseState
                and state.owner_uid==config.uid and os.geteuid()==config.uid)
        pending=state.read_pending();ledger=copy.deepcopy(state.data)
        accepted_record=state.read_accepted()['current']
        require(state._read()==state.data and type(scheduled) is bool)
        if scheduled:require(token is None and pending is None and accepted_record is not None)
        else:require(pending is not None and pending['token']==token)
        stage=state.stage;accepted=accepted_record is not None
        require(phase in (None,'bootstrap','accepted') and not (accepted and phase=='bootstrap'))
        mode='accepted' if accepted or phase=='accepted' else 'bootstrap'
        tools=binaries(config);values=credentials(config)
        recipient=config.path('/etc/xynes/backup-recipient.txt');raw=read(recipient,config,limit=4096)
        require(re.fullmatch(rb'age1[a-z0-9]{58}\n?',raw) is not None)
        output=config.path('/srv/xynes/'+stage+'/backups')
        protected(output.parent,config,directory=True)
        if not output.exists():output.mkdir(mode=0o700)
        protected(output,config,directory=True);require(stat.S_IMODE(output.stat().st_mode)==0o700)
        instance,previous=bridge(stage,config)
        if scheduled:
            rows=instance.metadata(['ps','--all','--filter','label=com.docker.compose.project=xynes-'+stage,
                '--filter','label=com.docker.compose.service=supabase-db','--no-trunc','--format','{{.ID}}'])
            if rows==b'':return {'status':'backup-skipped','stage':stage,'reason':'no-database'}
        before=instance.inspected_container()
        pg_container_bridge.BASE=previous;previous=None
        wrappers=prepare_wrappers(stage,config,output)
        module=pg_container_bridge.backup_module(config.uid);module.TIMEOUT=600
        tables=list(ACCEPTED_TABLES if mode=='accepted' else ('auth.users',))
        old_env=dict(os.environ);old_umask=os.umask(0o077)
        os.environ.clear();os.environ.update(PATH=str(wrappers)+':/usr/bin:/bin',HOME=str(output),LANG='C',LC_ALL='C',
            PGHOST='127.0.0.1',PGPORT='5432',PGDATABASE='postgres',PGUSER='postgres')
        args=SimpleNamespace(environment=stage,output_dir=output,recipient_file=recipient,
            verify_table=tables,max_bytes=10*1024**3,upload=True,bucket=values['B2_BACKUP_BUCKET'])
        release_binding={'accepted_token':accepted_record['token'],
            'accepted_source_sha256':accepted_record['source_sha256'],'database_container_id':before} if scheduled else None
        result=module.backup(args,release_binding=release_binding);require(result['uploaded'] is True)
        monitored=module.monitor(SimpleNamespace(environment=stage,output_dir=output))
        require(monitored['sha256']==result['sha256'])
        instance,previous=bridge(stage,config);require(instance.inspected_container()==before)
        pg_container_bridge.BASE=previous;previous=None
        module.write_json(output/'monitor.json',monitored)
        require(state.read_pending()==pending and state._read()==ledger and state.data==ledger
                and state.read_accepted()['current']==accepted_record)
        binding={'accepted_token':accepted_record['token'],'accepted_source_sha256':accepted_record['source_sha256']} if scheduled else {'pending_token':token}
        retention={'local_retention':prune_local(module,stage,output)} if scheduled else {}
        return {**result,**binding,**retention,'database_container_id':before,'verification_mode':mode,'verification_tables':tables,
            'execution_scope':'hosted' if config.root==Path('/') else 'offline-fixture',
            'ciphertext_sha256':result['sha256'],'deployable':False}
    except Exception:
        raise BackupError(ERROR) from None
    finally:
        if old_env is not None:os.environ.clear();os.environ.update(old_env)
        if old_umask is not None:os.umask(old_umask)
        if previous is not None:pg_container_bridge.BASE=previous


def prune_local(module,stage,output):
    """Guarded seven-day local copies only; no offsite delete authority."""
    require(stage in ('qa','staging','prod') and isinstance(output,Path))
    result=module.prune(SimpleNamespace(environment=stage,output_dir=output,
        confirm_prune=True,days=LOCAL_RETENTION_DAYS))
    require(type(result) is dict and result.get('status')=='retention-complete'
            and type(result.get('removed')) is int and result['removed']>=0)
    return {'days':LOCAL_RETENTION_DAYS,'removed':result['removed']}


def prepare_wrappers(stage,config,output):
    """Versioned native wrappers retain old immutable release imports on upgrade."""
    require(stage in ('qa','staging','prod'))
    bodies={}
    for program in ('psql','pg_dump','pg_restore','age','b2'):
        bodies[program]=('#!/usr/bin/python3 -ISB\nimport sys\n'
            +'sys.path.insert(0,'+repr(str(ROOT))+')\nfrom scripts.release import host_backup as m\n'
            +'c=m.Configuration(root=m.Path('+repr(str(config.root))+'),platform='+repr(config.platform)
            +',toolchain=m.Path('+repr(str(config.toolchain))+'))\n'
            +'raise SystemExit(m.wrapper(stage='+repr(stage)+',program='+repr(program)+',configuration=c,args=sys.argv[1:]))\n').encode()
    identity=hashlib.sha256(b''.join(name.encode()+b'\0'+body for name,body in sorted(bodies.items()))).hexdigest()
    parent=output/'wrappers'
    if not parent.exists():parent.mkdir(mode=0o700)
    protected(parent,config,directory=True);require(stat.S_IMODE(parent.stat().st_mode)==0o700)
    wrappers=parent/identity
    if not wrappers.exists():wrappers.mkdir(mode=0o700)
    protected(wrappers,config,directory=True);require(stat.S_IMODE(wrappers.stat().st_mode)==0o700)
    for program,body in bodies.items():
        path=wrappers/program
        if path.exists() or path.is_symlink():require(read(path,config,mode=0o500)==body)
        else:
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o500)
            with os.fdopen(fd,'wb') as file:file.write(body);file.flush();os.fsync(file.fileno())
    require(set(p.name for p in wrappers.iterdir())==set(bodies))
    return wrappers


def create(state,token,*,configuration=Configuration(),phase=None):
    # Deployment failure remains the original redacted backup error even if the
    # independently configured notification channel is unavailable.
    try:return _create(state,token,configuration=configuration,phase=phase)
    except Exception:
        if configuration.root==Path('/'):
            try:alert(state.stage,configuration=configuration)
            except Exception:pass
        raise BackupError(ERROR) from None


def locks_busy(directory,stage,config):
    # Read-only lock identity checks are repeated after a raced ReleaseState
    # acquisition; busy stages defer without mistaking contention for failure.
    for name in ('host.lock',stage+'.lock'):
        path=directory/name
        if not path.exists():continue
        descriptor=os.open(path,os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK)
        try:
            info=os.fstat(descriptor)
            require(stat.S_ISREG(info.st_mode) and info.st_uid==config.uid and info.st_nlink==1
                    and info.st_size==0 and stat.S_IMODE(info.st_mode)==0o600 and stamp(info)==stamp(path.lstat()))
            try:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return True
        finally:os.close(descriptor)
    return False


def periodic(stage,*,configuration=Configuration()):
    """Actual accepted release only; never manufacture a deployment attempt."""
    config=configuration
    try:
        require(stage in ('qa','staging','prod') and type(config) is Configuration and os.geteuid()==config.uid)
        directory=config.path('/var/lib/xynes/deploy/state');protected(directory,config,directory=True)
        busy={'status':'backup-skipped','stage':stage,'reason':'busy'}
        if locks_busy(directory,stage,config):return busy
        state=host_release_state.ReleaseState(directory,stage=stage,owner_uid=config.uid)
        try:state.__enter__()
        except host_release_state.StateError:
            if locks_busy(directory,stage,config):return busy
            raise
        try:
            require(state.read_pending() is None)
            if state.read_accepted()['current'] is None:
                return {'status':'backup-skipped','stage':stage,'reason':'no-accepted-release'}
            return _create(state,None,configuration=config,phase='accepted',scheduled=True)
        finally:state.__exit__(None,None,None)
    except Exception:raise BackupError(ERROR) from None


def discord_send(path,body):
    deadline=time.monotonic()+15
    connection=http.client.HTTPSConnection('discord.com',443,timeout=15)
    try:
        def response_class(sock,*args,**kwargs):
            response=http.client.HTTPResponse(sock,*args,**kwargs)
            response.fp=github_read.DeadlineReader(response.fp,sock,deadline)
            return response
        connection.response_class=response_class
        def remaining():
            timeout=deadline-time.monotonic();require(timeout>0)
            if connection.sock is not None:connection.sock.settimeout(timeout)
        connection.connect();remaining()
        connection.request('POST',path,body=body,headers={'Content-Type':'application/json','User-Agent':'Xynes-Backup/1'})
        remaining();response=connection.getresponse()
        # Header lines, chunk framing and body share one absolute read deadline.
        # Never follow redirects or expose a response that may contain secrets.
        require(response.status in (200,204) and len(response.read(4097))<=4096)
        remaining()
    finally:connection.close()


def alert(stage,*,configuration=Configuration(),test=False,transport=discord_send):
    try:
        config=configuration
        require(type(config) is Configuration and os.geteuid()==config.uid and stage in ('qa','staging','prod')
                and type(test) is bool and callable(transport))
        value=json.loads(read(config.path('/etc/xynes/deploy/alerts.json'),config,limit=4096,mode=0o600))
        require(type(value) is dict and set(value)=={'schema_version','provider','webhook_url'}
                and type(value['schema_version']) is int and value['schema_version']==1 and value['provider']=='discord'
                and type(value['webhook_url']) is str and len(value['webhook_url'])<=512)
        url=urlsplit(value['webhook_url'])
        require(url.scheme=='https' and url.netloc=='discord.com' and not url.query and not url.fragment
                and re.fullmatch(r'/api/webhooks/[0-9]{17,20}/[A-Za-z0-9_-]{60,200}',url.path))
        message=('Xynes '+stage+': TEST of the configured encrypted backup alert channel. No backup failure is being reported.'
                 if test else 'Xynes '+stage+': encrypted offsite backup failed. Check the protected backup service journal.')
        body=json.dumps({'content':message,'allowed_mentions':{'parse':[]}},separators=(',',':')).encode()
        require(len(body)<=512);transport(url.path,body)
        return {'status':'backup-alert-test-delivered' if test else 'backup-alert-delivered','stage':stage}
    except Exception:raise BackupError(ERROR) from None


def main(argv=None):
    try:
        argv=sys.argv[1:] if argv is None else argv
        require(os.geteuid()==0 and type(argv) is list and len(argv)==2
                and argv[0] in ('run','alert','alert-test') and argv[1] in ('qa','staging','prod'))
        os.umask(0o077)
        result=periodic(argv[1]) if argv[0]=='run' else alert(argv[1],test=argv[0]=='alert-test')
        # Raw receipts/CIDs remain in protected backup records, not the journal.
        print(json.dumps({'status':result['status'],'stage':argv[1]}));return 0
    except Exception:
        print(ERROR,file=sys.stderr);return 1


if __name__=='__main__':sys.exit(main())
