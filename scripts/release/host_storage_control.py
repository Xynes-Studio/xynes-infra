#!/usr/bin/env python3
"""Protected Storage assets. No deploy/stop permission or arbitrary cleanup API.

Generation identity is64hex; its unique disk key is the first32hex to fit native
Unix addresses even in private fixtures. Root tombstones reserve that disk key
forever. Only an inspected exact container can bind it; retirement requires that
container gone and the original socket/directory unchanged. Unknown files stay.
"""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys

try:
    from . import host_compose,release_manifest as records
except ImportError:
    import host_compose,release_manifest as records

ERROR='Storage control provisioning refused'
AUTO=object()
INSPECT=(' {"id":{{json .Id}},"image":{{json .Config.Image}},"user":{{json .Config.User}},'
    '"state":{{json .State.Status}},"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}},'
    '"started_at":{{json .State.StartedAt}},"pid":{{json .State.Pid}},'
    '"labels":{{json .Config.Labels}},"mounts":{{json .Mounts}}}')

class StorageControlError(ValueError):pass

def require(value):
    if not value:raise StorageControlError(ERROR)

def identity(info):
    return [info.st_dev,info.st_ino,info.st_uid,info.st_gid,stat.S_IMODE(info.st_mode)]

@dataclass(frozen=True)
class Generation:
    stage:str
    name:str
    token_sha256:str
    directory_identity:tuple

class StorageControl:
    def __init__(self,stage,*,root=Path('/'),runner=host_compose.run_command):
        require(stage in ('qa','staging','prod') and isinstance(root,Path) and root.is_absolute()
                and root.resolve(strict=True)==root and callable(runner))
        if root==Path('/'):
            require(os.geteuid()==0 and sys.platform.startswith('linux'))
        self.stage,self.root,self.runner=stage,root,runner
        self.owner_uid=os.geteuid()
        self.service_uid,self.service_gid=(1001,1001) if root==Path('/') else (os.geteuid(),os.getegid())
        self.token_path=root/('etc/xynes/storage-control/'+stage+'/control.token')
        self.base=root/('run/xynes/storage/'+stage)
        self.registry=root/('var/lib/xynes/storage-control/'+stage)

    def protected(self,path,*,directory=False,uid=None,gid=None,mode=0o700):
        require(path.resolve(strict=True)==path and self.root in (path,*path.parents))
        info=path.lstat()
        require((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
                and info.st_uid==(self.owner_uid if uid is None else uid)
                and (gid is None or info.st_gid==gid) and stat.S_IMODE(info.st_mode)==mode)
        if not directory:require(info.st_nlink==1)
        cursor=path.parent
        while cursor!=self.root.parent:
            parent=cursor.lstat()
            require(stat.S_ISDIR(parent.st_mode) and parent.st_uid==self.owner_uid
                    and not parent.st_mode&0o022)
            if cursor==self.root:break
            cursor=cursor.parent
        return identity(info)

    def mkdirs(self,path):
        require(self.root in path.parents)
        current=self.root
        for part in path.relative_to(self.root).parts:
            current=current/part
            if not current.exists() and not current.is_symlink():current.mkdir(mode=0o700)
            info=current.lstat()
            system=('/'+current.relative_to(self.root).as_posix()) in (
                '/etc','/etc/xynes','/run','/run/xynes','/var','/var/lib','/var/lib/xynes')
            if system:
                require(current.resolve(strict=True)==current and stat.S_ISDIR(info.st_mode)
                        and info.st_uid==self.owner_uid and not info.st_mode&0o022)
            else:self.protected(current,directory=True)

    def read_token(self):
        try:
            for parent in (self.token_path.parent,self.token_path.parent.parent):
                self.protected(parent,directory=True)
            before=self.protected(self.token_path,uid=self.service_uid,gid=self.service_gid,mode=0o400)
            fd=os.open(self.token_path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK)
            try:
                raw=os.read(fd,65)
                require(len(raw)==64 and re.fullmatch(rb'[a-f0-9]{64}',raw)
                        and identity(os.fstat(fd))==before
                        and self.protected(self.token_path,uid=self.service_uid,gid=self.service_gid,mode=0o400)==before)
            finally:os.close(fd)
            return raw.decode('ascii')
        except Exception:raise StorageControlError(ERROR) from None

    def provision_token(self):
        try:
            self.mkdirs(self.token_path.parent)
            if self.token_path.exists() or self.token_path.is_symlink():self.read_token();return
            fd=os.open(self.token_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o400)
            with os.fdopen(fd,'wb') as stream:
                os.fchmod(stream.fileno(),0o400);os.fchown(stream.fileno(),self.service_uid,self.service_gid)
                stream.write(secrets.token_hex(32).encode());stream.flush();os.fsync(stream.fileno())
            self.sync(self.token_path.parent);self.read_token()
        except Exception:raise StorageControlError(ERROR) from None

    def valid(self,generation):
        require(type(generation) is Generation and generation.stage==self.stage
                and type(generation.name) is str and re.fullmatch(r'[a-f0-9]{64}',generation.name))

    def directory(self,generation):
        self.valid(generation);return self.base/generation.name[:32]

    def record(self,generation):
        self.valid(generation);return self.registry/(generation.name[:32]+'.json')

    def sync(self,path):
        fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
        try:os.fsync(fd)
        finally:os.close(fd)

    def write(self,path,value,*,new=False):
        raw=(json.dumps(value,sort_keys=True,separators=(',',':'))+'\n').encode()
        temp=path if new else path.with_name('.'+secrets.token_hex(16))
        fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
        with os.fdopen(fd,'wb') as stream:
            os.fchmod(stream.fileno(),0o600);stream.write(raw);stream.flush();os.fsync(stream.fileno())
        if not new:os.replace(temp,path)
        self.sync(path.parent)

    def load(self,generation):
        path=self.record(generation);self.protected(self.registry,directory=True)
        before=self.protected(path,mode=0o600)
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK)
        try:
            raw=os.read(fd,8193);require(0<len(raw)<=8192 and identity(os.fstat(fd))==before
                    and self.protected(path,mode=0o600)==before)
        finally:os.close(fd)
        value=records.strict_json(raw.decode())
        require(type(value) is dict and set(value)=={'stage','generation','token_sha256','directory','state','container','socket'}
                and value['stage']==self.stage and value['generation']==generation.name
                and value['token_sha256']==generation.token_sha256
                and value['directory']==list(generation.directory_identity))
        return value

    def provision_generation(self,name=AUTO):
        try:
            name=secrets.token_hex(32) if name is AUTO else name
            require(type(name) is str and re.fullmatch(r'[a-f0-9]{64}',name))
            token=self.read_token();self.mkdirs(self.base);self.mkdirs(self.registry)
            generation=Generation(self.stage,name,hashlib.sha256(token.encode()).hexdigest(),())
            path=self.directory(generation);record=self.record(generation)
            require(not path.exists() and not path.is_symlink() and not record.exists() and not record.is_symlink())
            path.mkdir(mode=0o700);os.chown(path,self.service_uid,self.service_gid)
            stamp=self.protected(path,directory=True,uid=self.service_uid,gid=self.service_gid)
            generation=Generation(self.stage,name,generation.token_sha256,tuple(stamp))
            self.write(record,{'stage':self.stage,'generation':name,'token_sha256':generation.token_sha256,
                'directory':stamp,'state':'prepared','container':None,'socket':None},new=True)
            self.sync(self.base);return generation
        except Exception:raise StorageControlError(ERROR) from None

    def verify_generation(self,generation):
        try:
            record=self.load(generation);require(record['state'] in ('prepared','bound'))
            require(hashlib.sha256(self.read_token().encode()).hexdigest()==generation.token_sha256
                    and self.protected(self.directory(generation),directory=True,uid=self.service_uid,gid=self.service_gid)
                    ==list(generation.directory_identity))
            return record
        except Exception:raise StorageControlError(ERROR) from None

    def socket_identity(self,generation):
        path=self.directory(generation)/'control.sock';info=path.lstat()
        require(path.resolve(strict=True)==path and stat.S_ISSOCK(info.st_mode) and info.st_nlink==1
                and info.st_uid==self.service_uid and info.st_gid==self.service_gid and stat.S_IMODE(info.st_mode)==0o600)
        return identity(info)

    def native(self,args):
        raw=self.runner(host_compose.BASE+args,env=host_compose.CLEAN_ENV,timeout=15,limit=65536)
        require(type(raw) is bytes and len(raw)<=65536);return raw

    def bind_generation(self,generation,container_id,*,image):
        try:
            record=self.verify_generation(generation)
            require(type(container_id) is str and re.fullmatch(r'[a-f0-9]{64}',container_id)
                    and type(image) is str and re.fullmatch(r'ghcr\.io/xynes-studio/[a-z0-9/_.-]+@sha256:[a-f0-9]{64}',image))
            row=records.strict_json(self.native(['inspect','--format',INSPECT,container_id]).decode())
            require(type(row) is dict and set(row)=={'id','image','user','state','health','started_at','pid','labels','mounts'}
                    and row['id']==container_id and row['image']==image and row['user']=='1001:1001'
                    and row['state']=='running' and row['health']=='healthy' and type(row['pid']) is int and row['pid']>0
                    and type(row['started_at']) is str and re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{9}Z',row['started_at'])
                    and type(row['labels']) is dict and type(row['mounts']) is list)
            expected={'com.xynes.managed':'true','com.xynes.environment':self.stage,
                'com.docker.compose.project':'xynes-'+self.stage,'com.docker.compose.service':'storage-service',
                'com.docker.compose.oneoff':'False','com.xynes.storage-generation':generation.name}
            require(all(row['labels'].get(k)==v for k,v in expected.items()))
            mounts={'/run/xynes/storage-control':('/run/xynes/storage/'+self.stage+'/'+generation.name[:32],True),
                    '/run/secrets/storage-quiesce-token':('/etc/xynes/storage-control/'+self.stage+'/control.token',False)}
            for destination,(source,writable) in mounts.items():
                matches=[m for m in row['mounts'] if m.get('Destination')==destination]
                require(len(matches)==1 and matches[0].get('Type')=='bind' and matches[0].get('Source')==source
                        and matches[0].get('RW') is writable)
            bound={k:row[k] for k in ('id','image','pid','started_at')};sock=self.socket_identity(generation)
            if record['state']=='bound':require(record['container']==bound and record['socket']==sock);return
            require(record['state']=='prepared' and self.verify_generation(generation)==record)
            self.write(self.record(generation),{**record,'state':'bound','container':bound,'socket':sock})
        except Exception:raise StorageControlError(ERROR) from None

    def retire(self,generation):
        try:
            record=self.verify_generation(generation);require(record['state']=='bound')
            cid=record['container']['id']
            require(type(cid) is str and re.fullmatch(r'[a-f0-9]{64}',cid)
                    and self.native(['ps','--all','--no-trunc','--filter','id='+cid,'--format','{{.ID}}'])==b'')
            directory=self.directory(generation)
            require([p.name for p in directory.iterdir()]==['control.sock']
                    and self.socket_identity(generation)==record['socket'] and self.verify_generation(generation)==record)
            # Root private ancestors and the exact gone container prevent another
            # process mounting/writing this generation under the held host lock.
            (directory/'control.sock').unlink();directory.rmdir();self.sync(self.base)
            self.write(self.record(generation),{**record,'state':'retired'})
            return {'stage':self.stage,'generation':generation.name,'retired':True,'deployment_accepted':False}
        except Exception:raise StorageControlError(ERROR) from None
