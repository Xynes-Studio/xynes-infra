#!/usr/bin/env python3
"""Activate Docker's OCI-capable image store only on an empty GitHub CI runner.

Never installs Docker, prunes objects, migrates data, removes conflicting daemon
flags, or touches hosted deployment files. The caller owns the private backup
lifetime. A failed restart leaves that backup intact and cannot produce success.
"""
from collections.abc import Mapping
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import signal
import stat
import subprocess
import time

ROOT_UID=0
DAEMON_CONFIG=Path('/etc/docker/daemon.json')
ERROR='CI Docker image store preparation refused'
DRIVER_STATUS=[['driver-type','io.containerd.snapshotter.v1']]
MAX_CONFIG=65536

class EngineError(ValueError):pass

def require(value):
    if not value:raise EngineError(ERROR)

def guard(env):
    require(isinstance(env,Mapping) and os.geteuid()==0 and env.get('GITHUB_ACTIONS')=='true'
            and env.get('GITHUB_REPOSITORY')=='Xynes-Studio/xynes-infra'
            and env.get('GITHUB_EVENT_NAME')=='workflow_dispatch'
            and env.get('APEX_CI_RUNNER_ENVIRONMENT')=='github-hosted'
            and env.get('APEX_CI_RUNNER_OS')=='Linux')
    job=env.get('GITHUB_JOB');ref=env.get('GITHUB_REF')
    main=job=='product-smoke' and ref=='refs/heads/main' and env.get('GITHUB_REF_PROTECTED')=='true'
    historical=(job=='historical-integration' and type(ref) is str
                and re.fullmatch(r'refs/heads/test/apex-ci-smoke-[a-z0-9-]{1,64}',ref))
    require(main or historical)

def clean_environment():return {'PATH':'/usr/local/bin:/usr/bin:/bin','LANG':'C.UTF-8','LC_ALL':'C.UTF-8'}

def command(argv,*,timeout=30):
    """Fixed native operations, bounded streams, no ambient auth or daemon env."""
    process=None
    try:
        require(type(argv) is list and argv and all(type(value) is str and '\x00' not in value for value in argv)
                and (argv[0]=='/usr/bin/docker' or argv==['/usr/bin/systemctl','restart','docker'])
                and type(timeout) is int and 0<timeout<=60)
        process=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            env=clean_environment(),cwd='/',start_new_session=True)
        output=bytearray();errors=0;deadline=time.monotonic()+timeout
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout,selectors.EVENT_READ,'out');selector.register(process.stderr,selectors.EVENT_READ,'err')
            while selector.get_map():
                require(time.monotonic()<deadline)
                for key,_ in selector.select(min(1,max(0,deadline-time.monotonic()))):
                    data=os.read(key.fileobj.fileno(),65536)
                    if not data:selector.unregister(key.fileobj);continue
                    if key.data=='out':output.extend(data);require(len(output)<=65536)
                    else:errors+=len(data);require(errors<=65536)
            require(process.wait(timeout=max(.001,deadline-time.monotonic()))==0)
        return bytes(output)
    except Exception:raise EngineError(ERROR) from None
    finally:
        if process:
            if process.poll() is None:os.killpg(process.pid,signal.SIGKILL);process.wait()
            for stream in (process.stdout,process.stderr):
                if stream:stream.close()

def private_directory(path):
    require(isinstance(path,Path) and path.is_absolute() and path.resolve(strict=True)==path)
    info=path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid==ROOT_UID and stat.S_IMODE(info.st_mode)==0o700)

def write_new(path,raw):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    with os.fdopen(fd,'wb') as file:file.write(raw);file.flush();os.fsync(file.fileno())

def parent_directory():
    parent=DAEMON_CONFIG.parent
    require(parent.is_absolute() and parent.resolve(strict=True)==parent)
    info=parent.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid==ROOT_UID and not stat.S_IMODE(info.st_mode)&0o022)

def identity(info):
    return (info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_gid,info.st_nlink,
            info.st_size,info.st_mtime_ns,info.st_ctime_ns)

def original_config():
    parent_directory()
    try:fd=os.open(DAEMON_CONFIG,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
    except FileNotFoundError:
        require(not DAEMON_CONFIG.is_symlink());return b'',None,{}
    with os.fdopen(fd,'rb') as file:
        before=os.fstat(file.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_uid==ROOT_UID and before.st_nlink==1
                and not stat.S_IMODE(before.st_mode)&0o022 and 0<before.st_size<=MAX_CONFIG)
        raw=file.read(MAX_CONFIG+1)
        require(len(raw)==before.st_size and identity(os.fstat(file.fileno()))==identity(before)
                and identity(DAEMON_CONFIG.lstat())==identity(before))
    def pairs(values):
        result={}
        for key,value in values:require(key not in result);result[key]=value
        return result
    value=json.loads(raw.decode(),object_pairs_hook=pairs,parse_constant=lambda _:require(False))
    require(type(value) is dict)
    return raw,before,value

def atomic_config(raw,before):
    require(type(raw) is bytes and 0<len(raw)<=MAX_CONFIG);parent_directory()
    temporary=DAEMON_CONFIG.parent/('.apex-ci-daemon-'+secrets.token_hex(16))
    mode=stat.S_IMODE(before.st_mode) if before else 0o644
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    try:
        with os.fdopen(fd,'wb') as file:
            os.fchown(file.fileno(),ROOT_UID,before.st_gid if before else ROOT_UID)
            os.fchmod(file.fileno(),mode);file.write(raw);file.flush();os.fsync(file.fileno())
        if before is None:require(not os.path.lexists(DAEMON_CONFIG))
        else:require(identity(DAEMON_CONFIG.lstat())==identity(before))
        parent_directory();os.replace(temporary,DAEMON_CONFIG)
        directory=os.open(DAEMON_CONFIG.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if temporary.exists():temporary.unlink()

def prepare(env,work):
    """Return {containerd_store: true, changed: bool} after native readback only."""
    try:
        guard(env);private_directory(work)
        client=work/'engine-client';client.mkdir(mode=0o700);write_new(client/'config.json',b'{}')
        docker=['/usr/bin/docker','--config',str(client)]
        def enabled():
            raw=command([*docker,'info','--format','{{json .DriverStatus}}'],timeout=15)
            status=json.loads(raw.decode())
            require(type(status) is list and len(status)<=32 and all(type(row) is list and len(row)==2
                and all(type(value) is str and len(value)<=256 for value in row) for row in status))
            return status==DRIVER_STATUS
        if enabled():return {'containerd_store':True,'changed':False}
        # Stopped containers count too; no existing container may be hidden by a
        # store switch. Images/volumes are never removed or migrated by this code.
        require(command([*docker,'ps','--all','--quiet','--no-trunc'],timeout=15).strip()==b'')
        original,before,config=original_config()
        require('userns-remap' not in config and 'storage-driver' not in config)
        features=config.get('features',{})
        require(type(features) is dict and ('containerd-snapshotter' not in features
                or type(features['containerd-snapshotter']) is bool))
        write_new(work/'daemon.json.before',original)
        write_new(work/'daemon-config-backup.json',json.dumps({'config_existed':before is not None}).encode())
        config['features']={**features,'containerd-snapshotter':True}
        encoded=(json.dumps(config,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode()
        atomic_config(encoded,before)
        command(['/usr/bin/systemctl','restart','docker'],timeout=60)
        for attempt in range(30):
            try:
                if enabled():return {'containerd_store':True,'changed':True}
            except EngineError:pass
            if attempt<29:time.sleep(2)
        raise EngineError(ERROR)
    except Exception:raise EngineError(ERROR) from None
