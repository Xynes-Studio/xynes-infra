#!/usr/bin/env python3
"""Root-only, data-reading PostgreSQL wrapper over fixed Docker exec.

Install root-owned wrapper files named psql/pg_dump/pg_restore in a private PATH.
Each wrapper imports this absolute root-owned module using Python -I and calls
main(stage='qa', program='psql') with literal values; no stage/program/path comes
from SSH input or environment. Its work root is /srv/xynes/<stage>/backups.
Importing this module performs no commands. The coordinator holds the host lock
and backup lease. This module does not restore a hosted DB, apply migrations,
reset data, open ports, or grant admission. inspected_container can also identify
an owned fixed DB for a separately qualified coordinator migration operation.

Trusted fixture dependencies/owner override exist for local subprocess tests;
production main uses only the fixed defaults and requires euid 0.
"""
import grp
import importlib.util
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import time

ERROR = 'PostgreSQL container bridge refused'
STAGES = ('qa', 'staging', 'prod')
DB_IMAGE = 'postgres:17-alpine@sha256:5a6fcbc5d93831991d2386fa634509b3c49a1ac5ffb70c13c2322840f821d7e7'
BASE = ['/usr/bin/docker', '--host', 'unix:///var/run/docker.sock', '--config', '/etc/xynes/deploy/docker']
CLEAN_ENV = {'PATH':'/usr/bin:/bin','HOME':'/nonexistent','LANG':'C','LC_ALL':'C'}
PSQL_FLAGS = ['-X','-qAt','--no-password','--set=ON_ERROR_STOP=1']
SETTINGS = "SET search_path=pg_catalog; SET TIME ZONE 'UTC'; SET DateStyle='ISO'; SET extra_float_digits=3; SET statement_timeout=1800000; "
SNAPSHOT_SQL = "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SET LOCAL search_path=pg_catalog; SET LOCAL TIME ZONE 'UTC'; SET LOCAL DateStyle='ISO'; SET LOCAL extra_float_digits=3; SET LOCAL statement_timeout=1800000; SELECT pg_export_snapshot();"
INSPECT = ('{"id":{{json .Id}},"image":{{json .Config.Image}},"labels":{{json .Config.Labels}},'
           '"state":{{json .State.Status}},"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}}}')
MAX_DUMP = 10*1024**3
MAX_QUERY = 8192
MAX_META = 65536
TIMEOUT = 3600


class BridgeError(ValueError):
    """Fixed redacted diagnostic."""


def require(value):
    if not value:
        raise BridgeError(ERROR)


def identity(info):
    return (info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_gid,info.st_nlink,
            info.st_size,info.st_mtime_ns,info.st_ctime_ns)


def root_path(path, *, directory=False):
    for parent in reversed((path, *path.parents[:-1])):
        info=parent.lstat()
        require(info.st_uid==0 and not info.st_mode & 0o022 and not stat.S_ISLNK(info.st_mode)
                and (stat.S_ISDIR(info.st_mode) if parent!=path or directory else stat.S_ISREG(info.st_mode)))


def stage_environment(stage):
    require(type(stage) is str and stage in STAGES and os.geteuid()==0)
    path=Path('/srv/xynes')/stage/'env'/('.env.'+stage)
    root_path(path);root_path(Path(BASE[0]));root_path(Path(BASE[4]),directory=True)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK)
    try:
        before=os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_uid==0 and before.st_gid==grp.getgrnam('xynes').gr_gid
                and stat.S_IMODE(before.st_mode)==0o640 and before.st_nlink==1 and 0<before.st_size<=65536)
        raw=os.read(fd,65537)
        require(len(raw)==before.st_size and identity(os.fstat(fd))==identity(before)
                and identity(path.lstat())==identity(before) and b'\x00' not in raw)
        selected={};seen=set()
        for line in raw.decode('utf-8').splitlines():
            if not line or line.startswith('#'):continue
            key, separator, value=line.partition('=')
            require(separator and re.fullmatch(r'[A-Z][A-Z0-9_]*',key) and key not in seen)
            seen.add(key)
            if key in ('XYNES_ENV','POSTGRES_PASSWORD'):selected[key]=value
        require(selected.get('XYNES_ENV')==stage and type(selected.get('POSTGRES_PASSWORD')) is str
                and re.fullmatch(r'[a-f0-9]{64}',selected['POSTGRES_PASSWORD']))
        return selected
    finally:
        os.close(fd)


def backup_module(owner_uid):
    path=Path(__file__).resolve().parents[1]/'lib'/'backup_recovery.py'
    if owner_uid==0:root_path(path)
    spec=importlib.util.spec_from_file_location('apex_bridge_backup_contract',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def write_all(fd, raw, deadline):
    remaining=memoryview(raw)
    while remaining:
        require(time.monotonic()<deadline)
        try:
            count=os.write(fd,remaining)
            require(count>0);remaining=remaining[count:]
        except BlockingIOError:
            with selectors.DefaultSelector() as waiter:
                waiter.register(fd,selectors.EVENT_WRITE)
                require(waiter.select(max(0,deadline-time.monotonic())))


class Bridge:
    def __init__(self,stage,*,work_root,owner_uid=0,environment_loader=stage_environment):
        require(type(stage) is str and stage in STAGES and type(owner_uid) is int
                and os.geteuid()==owner_uid and callable(environment_loader))
        self.stage,self.work_root,self.owner_uid,self.environment_loader=stage,work_root,owner_uid,environment_loader
        self.contract=backup_module(owner_uid)

    def query(self,query,*,snapshot=False):
        require(type(query) is str and 0<len(query.encode())<=MAX_QUERY and '\n' not in query and '\r' not in query)
        if snapshot and query==SNAPSHOT_SQL:return
        if query.startswith(SETTINGS):query=query[len(SETTINGS):]
        allowed=(self.contract.TABLES_SQL,self.contract.SCHEMAS_SQL,self.contract.COLUMNS_SQL)
        if query in allowed:return
        match=re.search(r'FROM "([A-Za-z_][A-Za-z0-9_]*)"\."([A-Za-z_][A-Za-z0-9_]*)" t;$',query)
        require(match is not None and query==self.contract.table_sql(match.group(1)+'.'+match.group(2)))

    def metadata(self,args):
        process=None
        try:
            process=subprocess.Popen(BASE+args,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                     env=CLEAN_ENV,cwd='/',start_new_session=True)
            selector=selectors.DefaultSelector();selector.register(process.stdout,selectors.EVENT_READ,'out')
            selector.register(process.stderr,selectors.EVENT_READ,'err')
            deadline=time.monotonic()+30;output=bytearray();errors=0
            with selector:
                while selector.get_map():
                    require(time.monotonic()<deadline)
                    for key,_ in selector.select(min(1,max(0,deadline-time.monotonic()))):
                        chunk=os.read(key.fileobj.fileno(),65536)
                        if not chunk:selector.unregister(key.fileobj);continue
                        if key.data=='out':output.extend(chunk);require(len(output)<=MAX_META)
                        else:errors+=len(chunk);require(errors<=MAX_META)
            require(process.wait(timeout=max(.001,deadline-time.monotonic()))==0)
            return bytes(output)
        finally:
            if process:
                if process.poll() is None:os.killpg(process.pid,signal.SIGKILL);process.wait()
                process.stdout.close();process.stderr.close()

    def inspected_container(self):
        raw=self.metadata(['ps','--all','--filter','label=com.docker.compose.project=xynes-'+self.stage,
            '--filter','label=com.docker.compose.service=supabase-db','--no-trunc','--format','{{.ID}}'])
        ids=raw.decode('ascii').splitlines()
        require(len(ids)==1 and re.fullmatch(r'[a-f0-9]{64}',ids[0]))
        value=json.loads(self.metadata(['inspect','--format',INSPECT,ids[0]]))
        require(type(value) is dict and set(value)=={'id','image','labels','state','health'}
                and value['id']==ids[0] and value['image']==DB_IMAGE and value['state']=='running'
                and value['health']=='healthy' and type(value['labels']) is dict)
        labels=value['labels']
        expected={'com.docker.compose.project':'xynes-'+self.stage,'com.docker.compose.service':'supabase-db',
                  'com.docker.compose.oneoff':'False','com.xynes.managed':'true','com.xynes.environment':self.stage,
                  'com.xynes.role':'infrastructure'}
        require(all(labels.get(key)==item for key,item in expected.items()))
        return ids[0]

    def dump_file(self,path):
        require(type(path) is str and isinstance(self.work_root,Path) and self.work_root.is_absolute()
                and self.work_root.resolve(strict=True)==self.work_root)
        value=Path(path)
        require(value.is_absolute() and value.parent.parent==self.work_root and value.name=='database.dump'
                and re.fullmatch(r'\.backup-[a-z0-9_]{1,64}',value.parent.name)
                and value.resolve(strict=True)==value)
        for parent in (self.work_root,value.parent):
            info=parent.lstat()
            require(stat.S_ISDIR(info.st_mode) and info.st_uid==self.owner_uid and stat.S_IMODE(info.st_mode)==0o700)
        if self.owner_uid==0:root_path(self.work_root,directory=True)
        fd=os.open(value,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
        try:
            info=os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_uid==self.owner_uid and info.st_nlink==1
                    and stat.S_IMODE(info.st_mode)==0o600 and 0<info.st_size<=MAX_DUMP
                    and identity(value.lstat())==identity(info))
            return fd,value,identity(info)
        except BaseException:
            os.close(fd);raise

    def arguments(self,program,args):
        require(type(program) is str and type(args) is list and all(type(item) is str for item in args))
        if program=='psql':
            require(args==PSQL_FLAGS or (len(args)==len(PSQL_FLAGS)+2 and args[:4]==PSQL_FLAGS and args[4]=='--command'))
            if len(args)>4:self.query(args[5])
            return args,None,args==PSQL_FLAGS
        if program=='pg_dump':
            require(args==['--version'] or (len(args)==6 and args[:5]==[
                '--no-password','--format=custom','--no-owner','--no-acl','--snapshot']
                and re.fullmatch(r'[0-9A-Fa-f-]{1,80}',args[5])))
            return args,None,False
        if program=='pg_restore':
            require(len(args)==5 and args[:4]==['--schema-only','--no-owner','--no-acl','--file=-'])
            return args[:4],self.dump_file(args[4]),False
        require(False)

    def transfer(self,process,*,input_fd,output_fd,validate_input,limit):
        deadline=time.monotonic()+TIMEOUT;total=0;errors=0;pending=bytearray();queries=0
        selector=selectors.DefaultSelector()
        selector.register(process.stdout,selectors.EVENT_READ,'out');selector.register(process.stderr,selectors.EVENT_READ,'err')
        if validate_input:selector.register(input_fd,selectors.EVENT_READ,'input')
        output_blocking=os.get_blocking(output_fd)
        os.set_blocking(output_fd,False)
        try:
            with selector:
                while selector.get_map():
                    require(time.monotonic()<deadline)
                    # A dead child cannot consume further caller input. Drain
                    # its remaining output, then enforce its exit status below.
                    if validate_input and process.poll() is not None and input_fd in selector.get_map():
                        selector.unregister(input_fd)
                        process.stdin.close()
                    for key,_ in selector.select(min(1,max(0,deadline-time.monotonic()))):
                        fd=key.fileobj if type(key.fileobj) is int else key.fileobj.fileno()
                        chunk=os.read(fd,65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            if key.data=='input':
                                require(not pending);process.stdin.close()
                            continue
                        if key.data=='out':
                            total+=len(chunk);require(total<=limit);write_all(output_fd,chunk,deadline)
                        elif key.data=='err':errors+=len(chunk);require(errors<=MAX_META)
                        else:
                            pending.extend(chunk)
                            while b'\n' in pending:
                                line,_,rest=pending.partition(b'\n');pending=bytearray(rest)
                                self.query(line.decode('utf-8'),snapshot=True);queries+=1;require(queries<=512)
                                write_all(process.stdin.fileno(),line+b'\n',deadline)
                            require(len(pending)<=MAX_QUERY)
                require(process.wait(timeout=max(.001,deadline-time.monotonic()))==0)
        finally:
            os.set_blocking(output_fd,output_blocking)

    def run(self,program,args,*,input_fd=0,output_fd=1):
        process=None;dump=None
        try:
            require(type(input_fd) is int and type(output_fd) is int)
            flags,dump,persistent=self.arguments(program,args)
            values=self.environment_loader(self.stage)
            require(type(values) is dict and values.get('XYNES_ENV')==self.stage
                    and type(values.get('POSTGRES_PASSWORD')) is str and 0<len(values['POSTGRES_PASSWORD'])<=256
                    and not any(char in values['POSTGRES_PASSWORD'] for char in '\x00\r\n'))
            container=self.inspected_container()
            argv=BASE+['exec','-i','--env','PGPASSWORD']
            for value in ('PGHOST=127.0.0.1','PGPORT=5432','PGDATABASE=postgres','PGUSER=postgres',
                          'PGSERVICE','PGSERVICEFILE','PGHOSTADDR','PGOPTIONS',
                          'PGPASSFILE=/nonexistent','PGSSLMODE=disable','PGCONNECT_TIMEOUT=15'):
                argv += ['--env',value]
            argv += [container,program,*flags]
            process=subprocess.Popen(argv,stdin=subprocess.PIPE if persistent else dump[0] if dump else subprocess.DEVNULL,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,env={**CLEAN_ENV,'PGPASSWORD':values['POSTGRES_PASSWORD']},
                cwd='/',start_new_session=True)
            if persistent:os.set_blocking(process.stdin.fileno(),False)
            self.transfer(process,input_fd=input_fd,output_fd=output_fd,validate_input=persistent,
                          limit=16*self.contract.MAX_METADATA if program=='psql' else MAX_META if args==['--version'] else MAX_DUMP)
            if dump:
                require(identity(os.fstat(dump[0]))==dump[2] and identity(dump[1].lstat())==dump[2])
            return 0
        except Exception:
            sys.stderr.write(ERROR+'\n');return 1
        finally:
            if process:
                if process.poll() is None:os.killpg(process.pid,signal.SIGKILL);process.wait()
                for file in (process.stdin,process.stdout,process.stderr):
                    if file and not file.closed:file.close()
            if dump:os.close(dump[0])


def inspected_container(stage):
    """Root coordinator helper: ownership/pin/readiness only, no mutation authority."""
    try:
        require(os.geteuid()==0);stage_environment(stage)
        return Bridge(stage,work_root=Path('/srv/xynes')/stage/'backups').inspected_container()
    except Exception:
        raise BridgeError(ERROR) from None


def main(*,stage,program):
    """Called by a root-owned wrapper with literal stage and program, never argparse."""
    try:
        require(os.geteuid()==0);root_path(Path(__file__).resolve())
        bridge=Bridge(stage,work_root=Path('/srv/xynes')/stage/'backups')
        return bridge.run(program,sys.argv[1:])
    except Exception:
        sys.stderr.write(ERROR+'\n');return 1
