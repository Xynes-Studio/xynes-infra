#!/usr/bin/env python3
"""Concrete readonly DB audit/query and private deployment warm controls.

No approvals, source trust, backup, deployment or product acceptance are granted.
The coordinator supplies fixed stage/configuration and holds its host lock. Test
adapters are trusted code dependencies; stdin cannot select paths or commands.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import time
import math

try:
    from . import host_compose, host_migrate, host_seed, pg_container_bridge, release_manifest as records
except ImportError:
    import host_compose, host_migrate, host_seed, pg_container_bridge
    import release_manifest as records

ERROR='Host runtime control refused'
PEER_UID={'qa':12002,'staging':12003,'prod':12004}
COUNTS={'authz_permissions','authz_roles','authz_mappings','cms_templates','workspaces','cms_defaults_satisfied'}

class ControlError(ValueError):pass

def require(value):
    if not value:raise ControlError(ERROR)

def transaction():
    return ("BEGIN READ ONLY; SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='120s';\n"
            +host_seed.POSTCONDITIONS_SQL+'\nCOMMIT;\n').encode()

def stamp(value):
    return (value.st_dev,value.st_ino,value.st_uid,value.st_gid,value.st_mode,value.st_nlink,
            value.st_size,value.st_mtime_ns,value.st_ctime_ns)

def kernel_peer(sock):
    require(hasattr(socket,'SO_PEERCRED'))
    return struct.unpack('3i',sock.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))

def command(args,*,env,data=None):
    if args[:5]==host_compose.BASE and 'exec' in args:
        # Reuse the same bounded private stdin/pipes implementation. The only
        # caller here supplies the fixed readonly seed-condition transaction.
        require(type(data) is bytes and data==transaction() and len(args)==11
                and args[5:9]==['exec','-i','--env','PGPASSWORD']
                and re.fullmatch(r'[a-f0-9]{64}',args[-2]) and args[-1]=='psql')
        return host_migrate._execute(args[-2],env['PGPASSWORD'],sql=data)
    if args[:5]==host_compose.BASE:
        expected=[]
        for stage in PEER_UID:
            expected.append(host_compose.BASE+['ps','--all','--filter','label=com.docker.compose.project=xynes-'+stage,
                '--filter','label=com.docker.compose.service=supabase-db','--no-trunc','--format','{{.ID}}'])
            for role in ('idle','idle-proxy','idle-broker'):
                expected.append(host_compose.BASE+['ps','--all','--filter','label=com.xynes.environment='+stage,
                    '--filter','label=com.xynes.role='+role,'--no-trunc','--format','{{.ID}}'])
        require(args in expected and data is None)
        return host_compose.run_command(args,env=env,timeout=30,limit=65536)
    require(args[0]=='/usr/bin/systemctl' and len(args)==5
            and args[1:4]==['show','--property=ActiveState','--value']
            and re.fullmatch(r'xynes-idle-(qa|staging|prod)-(auth|cms|broker)\.service',args[4]))
    return host_compose.run_command(args,env=env,timeout=15,limit=65536)

class Controls:
    def __init__(self,stage,*,root=Path('/'),owner_uid=0,data_uid=70,run=command,
                 environment=pg_container_bridge.stage_environment,container=None,
                 peer_reader=None,proxy_identity=None,lease_timeout=125,idle_control=None):
        require(type(stage) is str and stage in PEER_UID and isinstance(root,Path) and root.is_absolute()
                and root.resolve(strict=True)==root and type(owner_uid) is int and owner_uid>=0
                and type(data_uid) is int and data_uid>=0 and callable(run) and callable(environment)
                and (container is None or callable(container)) and (peer_reader is None or callable(peer_reader))
                and type(lease_timeout) in (int,float) and math.isfinite(lease_timeout) and 0<lease_timeout<=125)
        require(proxy_identity is None or (root!=Path('/') and type(proxy_identity) is tuple and len(proxy_identity)==2
                and all(type(value) is int and value>=0 for value in proxy_identity)))
        self.stage,self.root,self.owner_uid,self.data_uid=stage,root,owner_uid,data_uid
        self.run,self.environment=run,environment
        self.container=self.db_container if container is None else container
        self.peer_reader=kernel_peer if peer_reader is None else peer_reader
        self.proxy_uid,self.proxy_gid=proxy_identity or (PEER_UID[stage],PEER_UID[stage])
        self.lease_timeout=lease_timeout
        require(idle_control is None or (getattr(idle_control,'stage',None)==stage and all(callable(getattr(idle_control,n,None)) for n in ('begin','finish'))))
        if idle_control is None and root==Path('/'):
            try:from .host_idle import Idle
            except ImportError:from host_idle import Idle
            idle_control=Idle(stage)
        self.idle_control=idle_control

    def path(self,name):return self.root/name.lstrip('/')

    def stage_check(self,stage):
        require(stage==self.stage and os.geteuid()==self.owner_uid)

    def protected(self,path,*,leaf_uid=None,mode=None,directory=False):
        require(isinstance(path,Path) and path.is_absolute() and self.root in (path,*path.parents))
        require(path.resolve(strict=True)==path)
        limit=self.root
        current=path
        while True:
            value=current.lstat()
            expected=leaf_uid if current==path and leaf_uid is not None else self.owner_uid
            require(value.st_uid==expected and not stat.S_ISLNK(value.st_mode)
                    and not value.st_mode&0o022
                    and (stat.S_ISDIR(value.st_mode) if current!=path or directory else stat.S_ISREG(value.st_mode) or stat.S_ISSOCK(value.st_mode)))
            if current==path and mode is not None:require(stat.S_IMODE(value.st_mode)==mode)
            if current==limit:break
            current=current.parent

    def sealed(self,path,limit,*,uid=None,mode=None,gid=None):
        self.protected(path,leaf_uid=uid,mode=mode)
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
        with os.fdopen(fd,'rb') as file:
            before=os.fstat(file.fileno())
            require(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and 0<before.st_size<=limit
                    and (gid is None or before.st_gid==gid))
            raw=file.read(limit+1)
            require(len(raw)==before.st_size and stamp(os.fstat(file.fileno()))==stamp(before)
                    and stamp(path.lstat())==stamp(before))
        return raw,stamp(before),hashlib.sha256(raw).hexdigest()

    def db_container(self,stage):
        self.stage_check(stage)
        raw=self.run(host_compose.BASE+['ps','--all','--filter','label=com.docker.compose.project=xynes-'+stage,
            '--filter','label=com.docker.compose.service=supabase-db','--no-trunc','--format','{{.ID}}'],env=host_compose.CLEAN_ENV)
        require(type(raw) is bytes and len(raw)<=65536)
        rows=raw.decode('ascii').splitlines()
        if not rows:return None
        require(len(rows)==1 and re.fullmatch(r'[a-f0-9]{64}',rows[0]))
        # Independently inspect exact current pin, readiness and stage ownership.
        require(pg_container_bridge.inspected_container(stage)==rows[0])
        return rows[0]

    def _pg_version(self,directory):
        # PGDATA is owned by PostgreSQL, unlike our root-owned configuration
        # ancestors. Read only this fixed marker through the validated PGDATA fd.
        before=os.stat('PG_VERSION',dir_fd=directory,follow_symlinks=False)
        require(stat.S_ISREG(before.st_mode) and before.st_uid==self.data_uid
                and stat.S_IMODE(before.st_mode)==0o600 and before.st_nlink==1 and before.st_size==3)
        fd=os.open('PG_VERSION',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,dir_fd=directory)
        with os.fdopen(fd,'rb') as file:
            require(stamp(os.fstat(file.fileno()))==stamp(before))
            raw=file.read(4)
            require(len(raw)==3 and stamp(os.fstat(file.fileno()))==stamp(before)
                    and stamp(os.stat('PG_VERSION',dir_fd=directory,follow_symlinks=False))==stamp(before))
        return raw,stamp(before),hashlib.sha256(raw).hexdigest()

    def seed_counts(self,stage,query):
        try:
            self.stage_check(stage);require(query==host_seed.POSTCONDITIONS_SQL)
            before=self.container(stage);require(type(before) is str and re.fullmatch(r'[a-f0-9]{64}',before))
            values=self.environment(stage);require(values['XYNES_ENV']==stage)
            args=host_compose.BASE+['exec','-i','--env','PGPASSWORD',before,'psql']
            raw=self.run(args,env={**pg_container_bridge.CLEAN_ENV,'PGPASSWORD':values['POSTGRES_PASSWORD']},data=transaction())
            require(type(raw) is bytes and 0<len(raw)<=65536)
            value=records.strict_json(raw.decode())
            require(type(value) is dict and set(value)==COUNTS
                    and all(type(v) is int and 0<=v<=1000000 for v in value.values())
                    and self.container(stage)==before)
            return value
        except Exception:raise ControlError(ERROR) from None

    def target_audit(self,stage,current,previous):
        try:
            self.stage_check(stage)
            require(current.prepared.images['IMAGE_SUPABASE_DB']==pg_container_bridge.DB_IMAGE)
            container=self.container(stage);data=self.path('/srv/xynes/'+stage+'/supabase/db')
            self.protected(data,leaf_uid=self.data_uid,mode=0o700,directory=True)
            fd=os.open(data,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
            with _descriptor(fd):
                before=os.fstat(fd);require(stamp(before)==stamp(data.lstat()))
                if container is None:
                    with os.scandir(fd) as entries:require(previous is None and next(entries,None) is None)
                    state='fresh'
                else:
                    require(type(container) is str and re.fullmatch(r'[a-f0-9]{64}',container))
                    marker=self._pg_version(fd)
                    require(marker[0]==b'17\n');state='existing'
                require(self.container(stage)==container and stamp(os.fstat(fd))==stamp(before)
                        and stamp(data.lstat())==stamp(before))
                if container is not None:require(self._pg_version(fd)==marker)
                require(stamp(os.fstat(fd))==stamp(before) and stamp(data.lstat())==stamp(before))
            evidence={'stage':stage,'database_state':state,'database_image':pg_container_bridge.DB_IMAGE,
                'database_container_id':container,'data_inode':[before.st_dev,before.st_ino],
                'data_uid':before.st_uid,'data_mode':stat.S_IMODE(before.st_mode)}
            return {key:evidence[key] for key in ('stage','database_state','database_image','database_container_id')}|{
                'proof_sha256':hashlib.sha256(json.dumps(evidence,sort_keys=True).encode()).hexdigest(),
                'observed_at':int(time.time())}
        except Exception:raise ControlError(ERROR) from None

    def policy(self):
        return self.policy_snapshot()[0]

    def policy_snapshot(self):
        path=self.path('/etc/xynes/idle/'+self.stage+'/deployment-mode.json')
        raw,identity,digest=self.sealed(path,4096,mode=0o600)
        value=records.strict_json(raw.decode())
        require(type(value) is dict and set(value)=={'schema_version','stage','mode','groups'}
                and type(value['schema_version']) is int and value['schema_version']==1 and value['stage']==self.stage
                and ((value['mode']=='disabled' and value['groups']==[])
                     or (value['mode']=='enabled' and value['groups']==['auth','cms'])))
        return value,identity,digest

    def disabled(self):
        for role in ('idle','idle-proxy','idle-broker'):
            raw=self.run(host_compose.BASE+['ps','--all','--filter','label=com.xynes.environment='+self.stage,
                '--filter','label=com.xynes.role='+role,'--no-trunc','--format','{{.ID}}'],env=host_compose.CLEAN_ENV)
            require(type(raw) is bytes and len(raw)<=65536 and not raw.strip())
        for group in ('auth','cms','broker'):
            raw=self.run(['/usr/bin/systemctl','show','--property=ActiveState','--value',
                'xynes-idle-'+self.stage+'-'+group+'.service'],env=host_compose.CLEAN_ENV)
            require(raw.strip() in (b'inactive',b'failed'))
        for name in ('control.sock','auth/leases.sock','cms/leases.sock'):
            target=self.path('/run/xynes/idle/'+self.stage+'/'+name)
            try:target.lstat()
            except FileNotFoundError:continue
            require(False)

    def lease(self,group,path,owner):
        sock=None
        try:
            self.stage_check(self.stage)
            require(group in ('auth','cms') and path in ('/warm','/release','/drain','/resume') and type(owner) is str and re.fullmatch(r'deploy-[1-9][0-9]{0,15}',owner))
            token_file=self.path('/etc/xynes/idle/'+self.stage+'/control.token')
            token_snapshot=self.sealed(token_file,65,mode=0o440,gid=self.proxy_gid)
            require(re.fullmatch(rb'[a-f0-9]{64}\n?',token_snapshot[0]));token=token_snapshot[0].removesuffix(b'\n')
            target=self.path('/run/xynes/idle/'+self.stage+'/'+group+'/leases.sock')
            # Only the dedicated leaf directory/socket can belong to the proxy UID.
            self.protected(target.parent.parent,directory=True)
            for p,kind,mode in ((target.parent,stat.S_ISDIR,0o700),(target,stat.S_ISSOCK,0o600)):
                info=p.lstat();require(kind(info.st_mode) and info.st_uid==self.proxy_uid and info.st_gid==self.proxy_gid
                                     and stat.S_IMODE(info.st_mode)==mode and not p.is_symlink())
                if p==target:require(info.st_nlink==1)
            socket_stamp=stamp(target.lstat());parent_stamp=stamp(target.parent.lstat())
            deadline=time.monotonic()+self.lease_timeout
            def remaining():
                value=deadline-time.monotonic();require(value>0);sock.settimeout(value)
            sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            remaining();sock.connect(str(target))
            credentials=self.peer_reader(sock)
            require(type(credentials) is tuple and len(credentials)==3 and all(type(item) is int for item in credentials))
            pid,uid,gid=credentials
            require(pid>0 and uid==self.proxy_uid and gid==self.proxy_gid)
            require(stamp(target.lstat())==socket_stamp and stamp(target.parent.lstat())==parent_stamp)
            body={'group':self.stage+'-'+group,'owner':owner}
            if path=='/warm':body['ttl_seconds']=3600
            body=json.dumps(body,separators=(',',':')).encode()
            request=(b'POST '+path.encode()+b' HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer '+token+
                b'\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
            remaining();sock.sendall(request)
            raw=bytearray()
            while b'\r\n\r\n' not in raw:
                remaining();chunk=sock.recv(min(4096,8193-len(raw)));require(chunk)
                raw.extend(chunk);require(len(raw)<=8192)
            header,extra=bytes(raw).split(b'\r\n\r\n',1);require(not extra)
            lines=header.split(b'\r\n');require(len(lines)<=51 and re.fullmatch(rb'HTTP/1\.[01] 204(?: [ -~]{0,256})?',lines[0]))
            headers={}
            for line in lines[1:]:
                name,separator,value=line.partition(b':')
                require(separator and re.fullmatch(rb'[A-Za-z0-9-]{1,128}',name) and all(32<=byte<=126 for byte in value))
                name=name.lower();require(name not in headers);headers[name]=value.strip(b' ')
            require(b'transfer-encoding' not in headers and headers.get(b'content-length',b'0')==b'0')
            remaining();require(sock.recv(1)==b'')
            require(self.sealed(token_file,65,mode=0o440,gid=self.proxy_gid)==token_snapshot
                    and stamp(target.lstat())==socket_stamp and stamp(target.parent.lstat())==parent_stamp)
        except Exception:raise ControlError(ERROR) from None
        finally:
            if sock is not None:sock.close()


    @contextmanager
    def warm(self,stage,current,previous):
        acquired=[]
        try:
            self.stage_check(stage);snapshot=self.policy_snapshot();policy=snapshot[0]
            owner='deploy-'+str(current.controller['run_id'])
            if policy['mode']=='disabled':
                self.disabled()
                try:yield
                finally:require(self.policy_snapshot()==snapshot);self.disabled()
                return
            if self.idle_control is not None:
                self.idle_control.begin(owner,first=previous is None,lease=self.lease)
                require(self.policy_snapshot()==snapshot)
                try:yield
                finally:
                    require(self.policy_snapshot()==snapshot)
                    self.idle_control.finish(owner)
                return
            try:
                for group in ('auth','cms'):
                    acquired.append(group);self.lease(group,'/warm',owner)
                require(self.policy_snapshot()==snapshot)
                yield
                require(self.policy_snapshot()==snapshot)
            finally:
                failed=False
                for group in reversed(acquired):
                    try:self.lease(group,'/release',owner)
                    except Exception:failed=True
                require(not failed)
        except Exception:raise ControlError(ERROR) from None

@contextmanager
def _descriptor(fd):
    try:yield fd
    finally:os.close(fd)
