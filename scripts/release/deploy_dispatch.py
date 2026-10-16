#!/usr/bin/env python3
"""Readonly Actions dispatch into the fixed SSH boundary, never host acceptance.

Native artifacts/signatures and fresh approvals are verified again by the host.
This entry creates only private ephemeral SSH files and sends bounded data to
the literal host/account/command. Acknowledgment is not deployment evidence.
"""
import base64
import copy
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys
import tempfile

try:
    from . import admission, artifact_handoff, deploy_admission, deploy_request, github_read, registry_io, ssh_entry
    from . import release_manifest as records
except ImportError:
    import admission, artifact_handoff, deploy_admission, deploy_request, github_read, registry_io, ssh_entry
    import release_manifest as records

ERROR = 'Deployment dispatch refused'
HOST = '84.247.176.134'
WORKFLOW = '.github/workflows/apex-deploy.yml'
SSH_TIMEOUT = 3000


class DispatchError(ValueError):
    """Fixed diagnostic excludes credentials, API/process output and local paths."""


def require(value):
    if not value:
        raise DispatchError(ERROR)


def number(value):
    require(type(value) is str and re.fullmatch(r'[1-9][0-9]{0,15}',value)
            and admission.positive(int(value)))
    return int(value)


def context(env):
    stage = env.get('APEX_STAGE')
    require(type(stage) is str and stage in deploy_request.STAGES)
    expected = {'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':admission.REPOSITORY,
        'GITHUB_REPOSITORY_ID':str(admission.REPOSITORY_ID),'GITHUB_REF':'refs/heads/main',
        'GITHUB_REF_PROTECTED':'true','GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_RUN_ATTEMPT':'1',
        'GITHUB_JOB':'deploy-'+stage,'GITHUB_API_URL':'https://api.github.com','GITHUB_SERVER_URL':'https://github.com',
        'GITHUB_WORKFLOW_REF':admission.REPOSITORY+'/'+WORKFLOW+'@refs/heads/main'}
    require(all(env.get(key) == value for key,value in expected.items())
            and type(env.get('GH_TOKEN')) is str and env['GH_TOKEN'].startswith('ghs_')
            and registry_io.TOKEN_PATTERN.fullmatch(env['GH_TOKEN']))
    for key in ('GITHUB_ACTOR','GITHUB_TRIGGERING_ACTOR'):
        require(type(env.get(key)) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,38}',env[key]))
    controller = {'repository':admission.REPOSITORY,'commit':env.get('GITHUB_SHA'),
                  'workflow':'apex-build.yml','run_id':number(env.get('APEX_BUILD_RUN_ID')),'run_attempt':1}
    records.validate_controller(controller)
    require(env.get('GITHUB_WORKFLOW_SHA') == controller['commit'])
    deployment = {'run_id':number(env.get('GITHUB_RUN_ID')),'run_attempt':1,'job_id':1}
    require(deployment['run_id'] != controller['run_id'])
    number(env.get('GITHUB_ACTOR_ID'))
    return stage,controller,deployment


def build_run(value, controller):
    require(type(value) is dict)
    deploy_admission.repository(value.get('repository'))
    deploy_admission.repository(value.get('head_repository'))
    expected = {'id':controller['run_id'],'run_attempt':1,'head_sha':controller['commit'],'head_branch':'main',
                'path':admission.WORKFLOW,'event':'workflow_dispatch','status':'completed','conclusion':'success'}
    require(all(type(value.get(key)) is type(item) and value[key] == item for key,item in expected.items())
            and admission.positive(value.get('workflow_id')))
    return {**expected,'workflow_id':value['workflow_id']}


def private_key(value):
    require(type(value) is str and 0 < len(value) <= 16384 and value.isascii())
    match = re.fullmatch(r'-----BEGIN OPENSSH PRIVATE KEY-----\n([A-Za-z0-9+/=\n]+)\n-----END OPENSSH PRIVATE KEY-----\n?',value)
    require(match is not None)
    encoded = match[1].replace('\n','')
    raw = base64.b64decode(encoded,validate=True)
    require(base64.b64encode(raw).decode() == encoded and raw.startswith(b'openssh-key-v1\0'))
    class Reader:
        def __init__(self,data): self.data,self.at = data,0
        def take(self,count):
            require(0 <= count <= len(self.data)-self.at)
            result = self.data[self.at:self.at+count]; self.at += count
            return result
        def integer(self): return struct.unpack('>I',self.take(4))[0]
        def string(self): return self.take(self.integer())
        def done(self): require(self.at == len(self.data))
    reader = Reader(raw[len(b'openssh-key-v1\0'):])
    require(reader.string() == b'none' and reader.string() == b'none' and reader.string() == b''
            and reader.integer() == 1)
    public_blob,private_blob = reader.string(),reader.string(); reader.done()
    public = Reader(public_blob)
    require(public.string() == b'ssh-ed25519')
    key = public.string(); require(len(key) == 32); public.done()
    private = Reader(private_blob)
    require(private.integer() == private.integer() and private.string() == b'ssh-ed25519'
            and private.string() == key)
    secret = private.string()
    require(len(secret) == 64 and secret[32:] == key and len(private.string()) <= 256)
    padding = private.take(len(private_blob)-private.at)
    require(len(private_blob)%8 == 0 and 1 <= len(padding) <= 8 and padding == bytes(range(1,len(padding)+1)))
    return value.encode('ascii')


def known_hosts(value):
    require(type(value) is str and len(value) <= 1024 and value.isascii())
    match = re.fullmatch(r'(?:84\.247\.176\.134|\[84\.247\.176\.134\]:22) ssh-ed25519 ([A-Za-z0-9+/=]+)\n?',value)
    require(match is not None)
    raw = base64.b64decode(match[1],validate=True)
    require(base64.b64encode(raw).decode() == match[1] and len(raw) == 51
            and raw[:19] == struct.pack('>I',11)+b'ssh-ed25519'+struct.pack('>I',32))
    return (HOST+' ssh-ed25519 '+match[1]+'\n').encode('ascii')


def write(path, raw):
    descriptor = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    with os.fdopen(descriptor,'wb') as file:
        file.write(raw); file.flush(); os.fsync(file.fileno())


def argv(stage, key, binding):
    account = 'xynes_'+stage+'_deploy'
    options = ['BatchMode=yes','StrictHostKeyChecking=yes','IdentitiesOnly=yes','IdentityAgent=none',
        'UserKnownHostsFile='+str(binding),'GlobalKnownHostsFile=/dev/null','KnownHostsCommand=none',
        'HostKeyAlgorithms=ssh-ed25519','PubkeyAcceptedAlgorithms=ssh-ed25519','UpdateHostKeys=no',
        'VerifyHostKeyDNS=no','CanonicalizeHostname=no','ProxyCommand=none','ProxyJump=none',
        'ForwardAgent=no','ForwardX11=no','ClearAllForwardings=yes','PermitLocalCommand=no',
        'ControlMaster=no','ControlPath=none','ControlPersist=no','EscapeChar=none',
        'PasswordAuthentication=no','KbdInteractiveAuthentication=no','PreferredAuthentications=publickey',
        'NumberOfPasswordPrompts=0','ConnectTimeout=10','ConnectionAttempts=1',
        'ServerAliveInterval=30','ServerAliveCountMax=3']
    return ['/usr/bin/ssh','-F','/dev/null','-T','-p','22','-i',str(key),'-l',account,
            *[part for option in options for part in ('-o',option)],HOST,'apex-deploy']


def run(env, *, read, process=ssh_entry.bounded_process):
    try:
        require(type(env) is dict and callable(read) and callable(process))
        stage,controller,deployment = context(env)
        key_data,binding_data = private_key(env.get('APEX_SSH_PRIVATE_KEY')),known_hosts(env.get('APEX_SSH_KNOWN_HOSTS'))
        base = Path(env['RUNNER_TEMP'])
        require(base.is_absolute() and base.resolve(strict=True) == base)
        info = base.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not stat.S_IMODE(info.st_mode)&0o022)
        def get(path):
            value = copy.deepcopy(read(path)); require(type(value) in (dict,list)); return value
        repo = get(admission.PREFIX); deploy_admission.repository(repo)
        require(repo.get('private') is False and repo.get('fork') is False and repo.get('default_branch') == 'main')
        def main():
            value = get(admission.PREFIX+'/branches/main')
            require(value.get('name') == 'main' and value.get('protected') is True
                    and type(value.get('commit')) is dict and value['commit'].get('sha') == controller['commit'])
        main()
        build_path = admission.PREFIX+f'/actions/runs/{controller["run_id"]}'
        build = build_run(get(build_path),controller)
        def active(identity,path):
            workflow = get(admission.PREFIX+f'/actions/workflows/{identity["workflow_id"]}')
            require(type(workflow.get('id')) is int and workflow['id'] == identity['workflow_id']
                    and workflow.get('path') == path and workflow.get('state') == 'active')
        active(build,admission.WORKFLOW)
        deploy_path = admission.PREFIX+f'/actions/runs/{deployment["run_id"]}'
        initial = get(deploy_path)
        deployed = deploy_admission.run(initial,controller,deployment)
        require(initial['actor']['login'] == env['GITHUB_ACTOR'] and initial['actor']['id'] == number(env['GITHUB_ACTOR_ID'])
                and initial['triggering_actor']['login'] == env['GITHUB_TRIGGERING_ACTOR'])
        active(deployed,WORKFLOW)
        jobs = get(deploy_path+'/attempts/1/jobs?per_page=100&page=1')
        require(type(jobs) is dict and type(jobs.get('total_count')) is int and 0 < jobs['total_count'] <= 100
                and type(jobs.get('jobs')) is list and len(jobs['jobs']) == jobs['total_count']
                and all(type(job) is dict and admission.positive(job.get('id')) for job in jobs['jobs'])
                and len({job['id'] for job in jobs['jobs']}) == len(jobs['jobs']))
        selected = [job for job in jobs['jobs'] if job.get('name') == 'deploy-'+stage]
        require(len(selected) == 1); deployment['job_id'] = selected[0]['id']
        job_path = admission.PREFIX+f'/actions/jobs/{deployment["job_id"]}'
        deploy_admission.job(get(job_path),stage,controller,deployment)
        artifacts = get(build_path+'/artifacts?per_page=100&page=1')
        require(type(artifacts) is dict and type(artifacts.get('total_count')) is int and 0 < artifacts['total_count'] <= 100
                and type(artifacts.get('artifacts')) is list and len(artifacts['artifacts']) == artifacts['total_count']
                and all(type(item) is dict and admission.positive(item.get('id')) for item in artifacts['artifacts'])
                and len({item['id'] for item in artifacts['artifacts']}) == len(artifacts['artifacts']))
        selected = [item for item in artifacts['artifacts'] if item.get('name') == f'apex-publication-{controller["run_id"]}-1']
        require(len(selected) == 1)
        publication = {'id':selected[0]['id'],'digest':selected[0].get('digest')}
        meta = artifact_handoff.metadata(selected[0],'publication',publication,controller)
        artifact_path = artifact_handoff.PREFIX+str(publication['id'])
        require(artifact_handoff.metadata(get(artifact_path),'publication',publication,controller) == meta)
        request = {'schema_version':1,'artifact_kind':'apex-deployment-request',
                   'controller':controller,'publication':publication,'deployment':deployment}
        data = (json.dumps(request,sort_keys=True,separators=(',',':'))+'\n').encode('ascii')
        deploy_request.parse(data,stage=stage)
        with tempfile.TemporaryDirectory(prefix='apex-deploy-ssh-',dir=base) as directory:
            home = Path(directory); home.chmod(0o700)
            key,binding = home/'identity',home/'known_hosts'; write(key,key_data); write(binding,binding_data)
            require(build_run(get(build_path),controller) == build
                    and deploy_admission.run(get(deploy_path),controller,deployment) == deployed)
            main(); deploy_admission.job(get(job_path),stage,controller,deployment)
            require(artifact_handoff.metadata(get(artifact_path),'publication',publication,controller) == meta)
            response = process(argv(stage,key,binding),data=data,env=dict(ssh_entry.ENVIRONMENT),cwd='/',
                               timeout_seconds=SSH_TIMEOUT,max_output=ssh_entry.MAX_OUTPUT)
            require(type(response) is tuple and len(response) == 3)
            code,output,errors = response
            require(type(code) is int and code == 0 and type(output) is bytes and type(errors) is bytes
                    and 0 < len(output) and len(output)+len(errors) <= ssh_entry.MAX_OUTPUT)
            ack = records.strict_json(output)
            require(type(ack) is dict and set(ack) == {'artifact_kind','stage','root_command_completed','deployable'}
                    and ack['artifact_kind'] == 'apex-ssh-entry-result' and ack['stage'] == stage
                    and ack['root_command_completed'] is True and ack['deployable'] is False)
        return {'artifact_kind':'apex-deployment-dispatch-result','stage':stage,'controller':controller,
                'deployment':deployment,'publication':publication,'ssh_entry_acknowledged':True,'deployable':False}
    except Exception:
        raise DispatchError(ERROR) from None


def main():
    try:
        require(len(sys.argv) == 1)
        env = dict(os.environ)
        context(env)
        result = run(env,read=github_read.GitHubReadAdapter(env['GH_TOKEN']).read)
        print(json.dumps(result,sort_keys=True,separators=(',',':')))
        return 0
    except Exception:
        print(ERROR,file=sys.stderr)
        return 1


if __name__=='__main__': sys.exit(main())
