#!/usr/bin/env python3
"""Owned pinned GitHub CLI signature verification, with private ephemeral auth."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import stat
import subprocess
import sys
import tempfile

try:
    from . import attestation_binding, registry_io, release_manifest as records
except ImportError:
    import attestation_binding
    import registry_io
    import release_manifest as records

VERSION='2.102.0'
ROOT=Path(__file__).resolve().parents[2]


class NativeError(ValueError):
    """Fixed diagnostics contain no process output, credential or rejected input."""


def require(value):
    if not value:raise NativeError('Native attestation verification refused')


def validate_binary(path):
    path=Path(path)
    require(path.is_absolute() and path.resolve(strict=True)==path)
    key=('darwin-arm64' if sys.platform=='darwin' and platform.machine()=='arm64' else
         'linux-amd64' if sys.platform.startswith('linux') and platform.machine() in ('x86_64','amd64') else None)
    require(key is not None)
    tool=records.read_json(ROOT/'infra/release/verifier-toolchain.json')
    require(tool.get('schema_version')==1 and tool.get('name')=='github-cli' and tool.get('version')==VERSION)
    expected=tool['platforms'][key]['binary_sha256'];records.hash_value(expected)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
    with os.fdopen(fd,'rb') as binary:
        before=os.fstat(binary.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_uid in (0,os.getuid())
                and before.st_nlink==1 and not stat.S_IMODE(before.st_mode)&0o022
                and stat.S_IMODE(before.st_mode)&0o111 and 0<before.st_size<=128*1024**2)
        digest=hashlib.sha256();size=0
        while True:
            data=binary.read(1024*1024)
            if not data:break
            size+=len(data);require(size<=before.st_size);digest.update(data)
        signature=lambda s:(s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
        require(size==before.st_size and digest.hexdigest()==expected
                and signature(os.fstat(binary.fileno()))==signature(before)
                and signature(path.lstat())==signature(before))
    # Deployment installs this executable beneath a root-owned immutable tool
    # directory. The admin installation/ownership check is a separate host gate.
    return path


def output_limit():
    resource.setrlimit(resource.RLIMIT_FSIZE,(attestation_binding.MAX_OUTPUT,attestation_binding.MAX_OUTPUT))


def verify(*,image,controller,binary,api_token,registry_token):
    """Invoke only the pinned native verifier before interpreting its result.

    No source execution, shell, saved auth helpers, global login, update, Docker
    daemon operation or deployment mutation occurs. Fresh stage approval is
    deliberately separate from a verified artifact signature.
    """
    try:
        controller=copy.deepcopy(controller)
        args=attestation_binding.verification_args(image,controller)
        for token in (api_token,registry_token):
            require(isinstance(token,str) and registry_io.TOKEN_PATTERN.fullmatch(token))
        binary=validate_binary(binary)
        with tempfile.TemporaryDirectory(prefix='apex-native-attestation-') as temporary:
            home=Path(temporary);home.chmod(0o700)
            docker=home/'docker';config=home/'github';docker.mkdir(mode=0o700);config.mkdir(mode=0o700)
            path=docker/'config.json'
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
            auth=base64.b64encode(('archan96:'+registry_token).encode()).decode()
            with os.fdopen(fd,'w') as file:
                json.dump({'auths':{'ghcr.io':{'auth':auth}}},file);file.flush();os.fsync(file.fileno())
            environment={'PATH':os.defpath,'HOME':str(home),'GH_CONFIG_DIR':str(config),
                         'DOCKER_CONFIG':str(docker),'GH_TOKEN':api_token,'GH_HOST':'github.com',
                         'GH_PROMPT_DISABLED':'1','GH_NO_UPDATE_NOTIFIER':'1'}
            with tempfile.TemporaryFile() as output,tempfile.TemporaryFile() as errors:
                process=subprocess.Popen([str(binary),*args],stdin=subprocess.DEVNULL,
                    stdout=output,stderr=errors,env=environment,cwd=home,
                    close_fds=True,preexec_fn=output_limit)
                try:
                    code=process.wait(timeout=90)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:pass
                    raise NativeError('Native attestation verification refused') from None
                require(code==0 and process.returncode==0 and output.tell()<=attestation_binding.MAX_OUTPUT
                        and errors.tell()<=attestation_binding.MAX_OUTPUT)
                output.seek(0);raw=output.read(attestation_binding.MAX_OUTPUT+1)
                proof=attestation_binding.qualify_native_result(raw,image,controller)
            return {**proof,'native_signature_verified':True,'verifier_version':VERSION}
    except Exception:
        raise NativeError('Native attestation verification refused') from None
