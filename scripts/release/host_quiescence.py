#!/usr/bin/env python3
"""Root Storage maintenance guard; never idle sleep, stop, reclaim or admission.

The installed coordinator supplies an actual held pending ReleaseState and
qualified Prepared objects. Native Docker inspection is independent of supplied
inventory. Each process's private Unix mount, peer ancestry/cgroup, token and
unexpired authenticated proof are bound to its exact CID/image. No CLI/request
selects assets, SQL, programs or adapters. Changed remote processors refuse until
an independent native drain contract exists; unchanged processors remain awake.
"""
import copy
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import struct
import sys
import time

try:
    from . import host_compose,host_migrate,host_release_state,pg_container_bridge,release_manifest as records
except ImportError:
    import host_compose,host_migrate,host_release_state,pg_container_bridge,release_manifest as records

ERROR='Host Storage quiescence refused'
SERVICE_KEYS={'storage-service':'IMAGE_STORAGE','clamav-clamd':'IMAGE_ARCHIVE_SCANNER',
    'clamav-freshclam':'IMAGE_FRESHCLAM','libreoffice-sidecar':'IMAGE_LIBREOFFICE'}
REMOTE=set(SERVICE_KEYS)-{'storage-service'}
CORE=('id','image','labels','state','health')
INSPECT=(' {"id":{{json .Id}},"image":{{json .Config.Image}},"labels":{{json .Config.Labels}},'
    '"state":{{json .State.Status}},"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}},'
    '"pid":{{json .State.Pid}},"user":{{json .Config.User}},"mounts":{{json .Mounts}},'
    '"enabled":{{ $enabled := false }}{{range .Config.Env}}{{if eq . "STORAGE_QUIESCENCE_ENABLED=1"}}'
    '{{$enabled = true}}{{end}}{{end}}{{json $enabled}}}')
CLAIMS_SQL=("BEGIN READ ONLY; SET LOCAL search_path=pg_catalog; SET LOCAL lock_timeout='5s'; "
    "SET LOCAL statement_timeout='120s'; SELECT json_build_object('database',current_database(),"
    "'user',current_user,'in_recovery',pg_is_in_recovery(),'running',"
    "(SELECT count(*) FROM platform.storage_processing_jobs WHERE status='running')); COMMIT;")

class QuiescenceError(ValueError):pass

def require(value):
    if not value:raise QuiescenceError(ERROR)

def stamp(info):
    return (info.st_dev,info.st_ino,info.st_uid,info.st_gid,info.st_mode,info.st_nlink,
        info.st_size,info.st_mtime_ns,info.st_ctime_ns)

def kernel_peer(sock):
    require(hasattr(socket,'SO_PEERCRED'))
    return struct.unpack('3i',sock.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))

def proc(pid,name):
    require(type(pid) is int and 0<pid<=2147483647 and name in ('stat','cgroup'))
    with (Path('/proc')/str(pid)/name).open('rb') as stream:raw=stream.read(8193)
    require(0<len(raw)<=8192);return raw

def process_identity(raw):
    end=raw.rfind(b')');require(end>0)
    fields=raw[end+2:].split();require(len(fields)>=20)
    parent,start=int(fields[1]),int(fields[19]);require(parent>0 and start>0)
    return parent,start

def process_member(pid,init,cid):
    """Bun may be a child of Compose init; bind kernel cgroup AND ancestry."""
    require(sys.platform.startswith('linux') and type(init) is int and init>0)
    cgroup=proc(init,'cgroup');require(proc(pid,'cgroup')==cgroup)
    paths=[line.split(b':',2)[2] for line in cgroup.splitlines() if line.count(b':')==2]
    require(any(cid.encode() in path.split(b'/') or ('docker-'+cid+'.scope').encode() in path.split(b'/') for path in paths))
    original=process_identity(proc(pid,'stat'));initial=process_identity(proc(init,'stat'))
    cursor=pid;seen=set()
    for _ in range(32):
        if cursor==init:break
        require(cursor not in seen);seen.add(cursor)
        cursor=process_identity(proc(cursor,'stat'))[0]
    else:raise QuiescenceError(ERROR)
    require(cursor==init and process_identity(proc(pid,'stat'))==original
            and process_identity(proc(init,'stat'))==initial and proc(init,'cgroup')==cgroup)

def running_claims(stage):
    environment=pg_container_bridge.stage_environment(stage)
    cid=pg_container_bridge.inspected_container(stage)
    raw=host_migrate._execute(cid,environment['POSTGRES_PASSWORD'],sql=CLAIMS_SQL.encode())
    value=records.strict_json(raw.decode())
    require(type(value) is dict and set(value)=={'database','user','in_recovery','running'}
        and value['database']=='postgres' and value['user']=='postgres' and value['in_recovery'] is False
        and type(value['running']) is int and value['running']>=0
        and pg_container_bridge.inspected_container(stage)==cid)
    return value['running']

class Guard:
    def __init__(self,stage,state,*,root=Path('/'),owner_uid=0,service_uid=1001,service_gid=1001,
                 runner=host_compose.run_command,peer_reader=kernel_peer,process_verifier=process_member,
                 claims_reader=running_claims):
        require(stage in host_compose.STAGES and type(state) is host_release_state.ReleaseState
            and isinstance(root,Path) and root.is_absolute() and root.resolve(strict=True)==root
            and all(type(value) is int and value>=0 for value in (owner_uid,service_uid,service_gid))
            and all(callable(value) for value in (runner,peer_reader,process_verifier,claims_reader)))
        if root==Path('/'):
            require(owner_uid==0 and service_uid==service_gid==1001 and sys.platform.startswith('linux'))
        self.stage,self.state,self.root=stage,state,root
        self.owner_uid,self.service_uid,self.service_gid=owner_uid,service_uid,service_gid
        self.runner,self.peer_reader,self.process_verifier,self.claims_reader=runner,peer_reader,process_verifier,claims_reader
        self.lease=None;self.allowed=None;self.owner=None

    def bound(self):
        require(os.geteuid()==self.owner_uid and self.state.owner_uid==self.owner_uid and self.state.stage==self.stage)
        pending=self.state.read_pending();require(pending is not None and self.state._read()==self.state.data)
        owner=pending['token'][:32]
        require(self.owner is None or self.owner==owner);self.owner=owner
        return pending

    def protected(self,path,*,uid=None,gid=None,mode=None,directory=False,sock=False):
        require(path.is_absolute() and path.resolve(strict=True)==path and self.root in (path,*path.parents))
        cursor=path
        while True:
            info=cursor.lstat();leaf=cursor==path
            require(info.st_uid==(uid if leaf and uid is not None else self.owner_uid)
                and not stat.S_ISLNK(info.st_mode) and not info.st_mode&0o022)
            if leaf:
                require((stat.S_ISSOCK(info.st_mode) if sock else stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
                    and (mode is None or stat.S_IMODE(info.st_mode)==mode)
                    and (gid is None or info.st_gid==gid))
                if not directory:require(info.st_nlink==1)
            else:require(stat.S_ISDIR(info.st_mode))
            if cursor==self.root:break
            cursor=cursor.parent
        return stamp(path.lstat())

    def token(self):
        path=self.root/('etc/xynes/storage-control/'+self.stage+'/control.token')
        before=self.protected(path,uid=self.service_uid,gid=self.service_gid,mode=0o400)
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
        try:
            raw=os.read(fd,66);require(64<=len(raw)<=65 and re.fullmatch(rb'[a-f0-9]{64}\n?',raw)
                and stamp(os.fstat(fd))==before and stamp(path.lstat())==before)
            return raw.rstrip(b'\n'),before
        finally:os.close(fd)

    def call(self,argv):
        raw=self.runner(host_compose.BASE+argv,env=host_compose.CLEAN_ENV,timeout=30,limit=65536)
        require(type(raw) is bytes and len(raw)<=65536);return raw

    def inventory(self):
        require(self.allowed is not None);rows={}
        for service,key in SERVICE_KEYS.items():
            raw=self.call(['ps','--all','--filter','label=com.docker.compose.project=xynes-'+self.stage,
                '--filter','label=com.docker.compose.service='+service,'--no-trunc','--format','{{.ID}}'])
            ids=raw.decode('ascii').splitlines();require(len(ids)<=1 and all(host_compose.CID.fullmatch(cid) for cid in ids))
            if not ids:continue
            row=records.strict_json(self.call(['inspect','--format',INSPECT,ids[0]]).decode())
            require(type(row) is dict and set(row)==set(CORE)|{'pid','user','enabled','mounts'}
                and row['id']==ids[0] and row['image'] in self.allowed[service]
                and type(row['pid']) is int and row['pid']>0 and row['state']=='running' and row['health']=='healthy'
                and type(row['labels']) is dict and type(row['mounts']) is list)
            labels=row['labels']
            expected={'com.docker.compose.project':'xynes-'+self.stage,'com.docker.compose.service':service,
                'com.docker.compose.oneoff':'False','com.xynes.managed':'true','com.xynes.environment':self.stage}
            require(all(labels.get(key)==value for key,value in expected.items()))
            if service=='storage-service':
                require(row['enabled'] is True and row['user']=='1001:1001')
                generation=labels.get('com.xynes.storage-generation');require(type(generation) is str and re.fullmatch(r'[a-f0-9]{64}',generation))
                expected_mounts={'/run/secrets/storage-quiesce-token':('/etc/xynes/storage-control/'+self.stage+'/control.token',False),
                    '/run/xynes/storage-control':('/run/xynes/storage/'+self.stage+'/'+generation[:32],True)}
                for dest,(source,rw) in expected_mounts.items():
                    matches=[mount for mount in row['mounts'] if mount.get('Destination')==dest]
                    require(len(matches)==1 and matches[0].get('Type')=='bind' and matches[0].get('Source')==source and matches[0].get('RW') is rw)
                row['generation']=generation
            rows[service]=row
        require(len({row['id'] for row in rows.values()})==len(rows));return rows

    def compare(self,inventory,native):
        require(type(inventory) is dict)
        supplied={service:{key:row[key] for key in CORE} for service,row in inventory.items() if service in SERVICE_KEYS}
        require(supplied=={service:{key:row[key] for key in CORE} for service,row in native.items()})

    def assets(self,row):
        directory=self.root/('run/xynes/storage/'+self.stage+'/'+row['generation'][:32])
        parent_stamp=self.protected(directory,uid=self.service_uid,gid=self.service_gid,mode=0o700,directory=True)
        path=directory/'control.sock'
        # Generation directory belongs to the service; all ancestors stay root.
        info=path.lstat();require(stat.S_ISSOCK(info.st_mode) and info.st_uid==self.service_uid
            and info.st_gid==self.service_gid and stat.S_IMODE(info.st_mode)==0o600 and info.st_nlink==1 and path.resolve(strict=True)==path)
        return path,stamp(info),parent_stamp

    def control(self,row,path,body):
        sock=None
        try:
            self.bound();require(path in ('/quiesce','/resume','/status'))
            target,socket_stamp,parent_stamp=self.assets(row);token,token_stamp=self.token()
            deadline=time.monotonic()+(125 if path=='/quiesce' else 10)
            sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            def remaining():
                seconds=deadline-time.monotonic();require(seconds>0);sock.settimeout(seconds)
            remaining();sock.connect(str(target));peer=self.peer_reader(sock)
            require(type(peer) is tuple and len(peer)==3 and all(type(value) is int for value in peer)
                and peer[0]>0 and peer[1:]==(self.service_uid,self.service_gid))
            self.process_verifier(peer[0],row['pid'],row['id'])
            require(self.assets(row)==(target,socket_stamp,parent_stamp))
            encoded=json.dumps(body,separators=(',',':'),allow_nan=False).encode('ascii')
            request=(b'POST '+path.encode()+b' HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer '+token+
                b'\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: '+str(len(encoded)).encode()+b'\r\n\r\n'+encoded)
            remaining();sock.sendall(request);buffer=bytearray();received=0
            def take(count):
                nonlocal received
                while len(buffer)<count:
                    remaining();raw=sock.recv(min(4096,8193-received));require(raw)
                    received+=len(raw);require(received<=8192);buffer.extend(raw)
                result=bytes(buffer[:count]);del buffer[:count];return result
            # Keep framed reads in one bounded buffer, including coalesced bytes.
            def read_line(limit):
                nonlocal received
                while b'\r\n' not in buffer:
                    require(len(buffer)<=limit);remaining();raw=sock.recv(4096);require(raw)
                    received+=len(raw);require(received<=8192);buffer.extend(raw)
                index=buffer.index(b'\r\n');require(index<=limit);return take(index+2)[:-2]
            require(re.fullmatch(rb'HTTP/1\.[01] 200(?: [ -~]{0,128})?',read_line(256)))
            headers={};header_size=0
            for _ in range(40):
                entry=read_line(1024);header_size+=len(entry)+2;require(header_size<=4096)
                if not entry:break
                name,sep,value=entry.partition(b':');require(sep and re.fullmatch(rb'[A-Za-z0-9-]{1,64}',name))
                name=name.lower();require(name not in headers);headers[name]=value.strip(b' ')
            else:raise QuiescenceError(ERROR)
            require(headers.get(b'content-type')==b'application/json' and headers.get(b'connection',b'').lower()==b'close')
            transfer=headers.get(b'transfer-encoding');length=headers.get(b'content-length');require(not (transfer and length))
            if transfer:
                require(transfer==b'chunked');output=bytearray()
                while True:
                    size=read_line(8);require(re.fullmatch(rb'[a-fA-F0-9]{1,8}',size));size=int(size,16)
                    require(len(output)+size<=4096)
                    if size==0:require(read_line(0)==b'');break
                    output.extend(take(size));require(take(2)==b'\r\n')
                raw=bytes(output)
            else:
                require(length is not None and re.fullmatch(rb'[0-9]{1,4}',length) and 0<int(length)<=4096)
                raw=take(int(length))
            require(not buffer);remaining();require(sock.recv(1)==b'')
            require(self.token()==(token,token_stamp) and self.assets(row)==(target,socket_stamp,parent_stamp))
            self.process_verifier(peer[0],row['pid'],row['id']);self.bound()
            return records.strict_json(raw.decode('utf-8'))
        except Exception:raise QuiescenceError(ERROR) from None
        finally:
            if sock:sock.close()

    def proof(self,value,budget):
        require(type(budget) in (int,float) and math.isfinite(budget) and 0<budget<=2700)
        require(type(value) is dict and set(value)=={'state','owner','expires_at','admission_open','active_http','active_background'}
            and value['state']=='quiesced' and value['owner']==self.owner and value['admission_open'] is False
            and type(value['active_http']) is int and value['active_http']==0
            and type(value['active_background']) is int and value['active_background']==0
            and type(value['expires_at']) is int and value['expires_at']>int(time.time()*1000)+(budget+5)*1000)

    def zero_claims(self):
        count=self.claims_reader(self.stage);require(type(count) is int and count==0)

    def before_replacement(self,prepared,previous,inventory,replace_services,*,stop_budget_seconds=900):
        touched=False;row=None
        try:
            self.bound();require(prepared.stage==self.stage and type(replace_services) is set and replace_services<=host_compose.SERVICES)
            self.allowed={service:{prepared.images[key]} for service,key in SERVICE_KEYS.items()}
            if previous is not None:
                require(previous.stage==self.stage)
                for service,key in SERVICE_KEYS.items():self.allowed[service].add(previous.images[key])
            native=self.inventory();self.compare(inventory,native)
            require(not (REMOTE & set(native) & replace_services))
            row=native.get('storage-service')
            if row is None:require(not native and previous is None);self.lease=None;return
            if self.lease is not None:
                old=self.lease['row']
                if row['id']==old['id']:
                    self.verify_before_stop(stop_budget_seconds=stop_budget_seconds);return
                # Partial recreation may have succeeded before another readiness
                # check failed. Its new process needs its own proof for rollback.
                require(row['generation'][:32]!=old['generation'][:32]
                        and self.call(['ps','--all','--filter','id='+old['id'],'--no-trunc','--format','{{.ID}}'])==b'')
                self.lease=None
            touched=True
            value=self.control(row,'/quiesce',{'owner':self.owner,'timeout_ms':120000,'lease_ms':3600000})
            self.proof(value,stop_budget_seconds);require(self.inventory()==native);self.zero_claims()
            self.lease={'row':copy.deepcopy(row),'proof':copy.deepcopy(value)}
        except Exception:
            if touched and row is not None:
                try:
                    require(self.inventory().get('storage-service')==row)
                    self.control(row,'/resume',{'owner':self.owner})
                except Exception:pass
            if touched:self.lease=None
            raise QuiescenceError(ERROR) from None

    def verify_before_stop(self,*,stop_budget_seconds=900):
        try:
            self.bound();require(self.lease is not None)
            row=self.lease['row'];require(self.inventory().get('storage-service')==row)
            value=self.control(row,'/status',{'owner':self.owner});self.proof(value,stop_budget_seconds)
            self.zero_claims();require(self.inventory().get('storage-service')==row)
        except Exception:raise QuiescenceError(ERROR) from None

    def resume(self):
        try:
            self.bound()
            if self.lease is None:return
            row=self.lease['row'];require(self.inventory().get('storage-service')==row)
            require(self.control(row,'/resume',{'owner':self.owner})=={'resumed':True})
            status=self.control(row,'/status',{'owner':self.owner})
            require(type(status) is dict and status.get('state')=='open' and status.get('owner') is None and status.get('admission_open') is True)
            require(self.inventory().get('storage-service')==row);self.lease=None
        except Exception:raise QuiescenceError(ERROR) from None

    def after_replacement(self,prepared,previous,inventory):
        try:
            self.bound();native=self.inventory();self.compare(inventory,native)
            row=native.get('storage-service');require(row is not None and row['image']==prepared.images['IMAGE_STORAGE'])
            if self.lease is not None and row['id']==self.lease['row']['id']:self.resume();return
            if self.lease is not None:
                # Never resume an old proof against a replacement process.
                old=self.lease['row']['id']
                require(not self.call(['ps','--all','--filter','id='+old,'--no-trunc','--format','{{.ID}}']).strip())
            status=self.control(row,'/status',{'owner':self.owner})
            require(status.get('state')=='open' and status.get('owner') is None and status.get('admission_open') is True)
            self.lease=None
        except Exception:raise QuiescenceError(ERROR) from None

    def assert_sql_safe(self,verified,*,prior_verified=None):
        try:
            self.bound();native=self.inventory()
            if not native:return {'stage':self.stage,'storage_present':False}
            self.verify_before_stop()
            require(prior_verified is not None)
            result=host_migrate.verify_existing(verified,self.state,prior_verified=prior_verified)
            require(result['schema_sql_applied'] is False and result['migration_ledger_verified'] is True
                and result['stage']==self.stage and result['controller']==self.bound()['request']['controller'])
            self.verify_before_stop();return result
        except Exception:raise QuiescenceError(ERROR) from None
