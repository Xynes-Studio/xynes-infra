#!/usr/bin/env python3
"""Coordinator-only owned host boundary planning/install; never an SSH API.

Readiness capabilities and their verifiers come from the trusted coordinator.
This module neither creates readiness evidence nor implements root execution.
The optional root namespace is exclusively for offline fixtures; live operations
require root. Unknown/mutable/link-substituted resources always refuse.
"""
import base64
from dataclasses import dataclass, asdict
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

ERROR='Host deployment boundary refused'
STAGES=('qa','staging','prod')
LIBRARY_FILES=tuple('scripts/release/'+name+'.py' for name in (
    'admission','artifact_download','artifact_handoff','attestation_binding','deploy_admission',
    'deploy_request','deployment_candidate','github_read','host_compose','host_release_state',
    'host_migrate','host_seed','host_assets','host_backup','environment_inputs','provider_inputs',
    'host_runtime','host_supervisor','host_controls','host_smoke','host_smoke_setup','host_boundary','host_retained_dev',
    'host_storage_control','host_quiescence','host_idle',
    'image_build','migration_inventory','migration_plan','migration_sql','migration_transport',
    'pg_container_bridge','native_attestation','publication_record','publish_batch','publish_images',
    'registry_https','registry_io','registry_namespace','registry_policy','release_manifest',
    'source_context','ssh_entry','root_executor'))+(
    'scripts/release/validate-hosted.py','scripts/lib/backup_recovery.py',
    'infra/release/registry-policy.json','infra/release/source-check-catalog.json','infra/release/verifier-toolchain.json',
    'infra/release/backup-toolchain.json','infra/release/source-manifest.json',
    'infra/release/auth/compose.fragment.yml')
LIBRARY='/usr/local/lib/xynes-deploy/releases/'
ENTRY='/usr/local/libexec/xynes-deploy-ssh'
EXECUTOR='/usr/local/libexec/xynes-deploy-root'
SHELL='/usr/local/libexec/xynes-deploy-shell'
BACKUP='/usr/local/libexec/xynes-backup'
MAIN='/etc/ssh/sshd_config'
SNIPPET='/etc/ssh/xynes-deploy-boundary.conf'
LEDGER='/etc/xynes/deploy-boundary.json'
BEGIN=b'# BEGIN XYNES APEX DEPLOY BOUNDARY\n'
SUFFIX=BEGIN+b'Match all\nInclude /etc/ssh/xynes-deploy-boundary.conf\n# END XYNES APEX DEPLOY BOUNDARY\n'
SEAL=object()
MAX_FILE=1024*1024
REPEATED_PROFILE_KEYS=frozenset(('listenaddress','hostkey','hostcertificate','acceptenv',
                               'allowusers','denyusers','allowgroups','denygroups'))


class BoundaryError(ValueError):
    """Fixed diagnostics contain no account/config/key/process/source payload."""


def require(value):
    if not value:raise BoundaryError(ERROR)


def digest(data):return hashlib.sha256(data).hexdigest()


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


@dataclass(frozen=True,repr=False)
class Asset:
    path:str
    data:bytes
    mode:int


@dataclass(frozen=True,repr=False)
class Account:
    stage:str
    name:str
    uid:int
    gid:int
    home:str
    shell:str
    groups:tuple


@dataclass(frozen=True,repr=False)
class Plan:
    files:tuple
    accounts:tuple
    sources:tuple
    original:bytes
    bundle_sha256:str
    plan_sha256:str
    seal:object
    @property
    def source_hashes(self):return dict(self.sources)


def plan_digest(files,accounts,sources,original,bundle):
    return digest(canonical({'files':[{'path':a.path,'sha256':digest(a.data),'mode':a.mode} for a in files],
        'accounts':[asdict(a) for a in accounts],'sources':dict(sources),'original_sha256':digest(original),'bundle_sha256':bundle}))


def check_plan(plan):
    require(type(plan) is Plan and plan.seal is SEAL and tuple(name for name,_ in plan.sources)==LIBRARY_FILES
            and plan.plan_sha256==plan_digest(plan.files,plan.accounts,plan.sources,plan.original,plan.bundle_sha256))


def identity(value):
    return value.st_dev,value.st_ino,value.st_uid,value.st_mode,value.st_nlink,value.st_size,value.st_mtime_ns,value.st_ctime_ns


def read_file(path,uid):
    before=path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_uid==uid and before.st_nlink==1
            and not stat.S_IMODE(before.st_mode)&0o022 and 0<=before.st_size<=MAX_FILE)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
    with os.fdopen(fd,'rb') as file:
        require(identity(os.fstat(file.fileno()))==identity(before))
        data=file.read(MAX_FILE+1)
        require(len(data)==before.st_size and identity(os.fstat(file.fileno()))==identity(before)
                and identity(path.lstat())==identity(before))
    return data,before


def public_key(value):
    require(type(value) is str and len(value)<=1024)
    match=re.fullmatch(r'ssh-ed25519 ([A-Za-z0-9+/=]+)(?: [ -~]{1,128})?\n?',value)
    require(match is not None)
    raw=base64.b64decode(match[1],validate=True)
    require(len(raw)==51 and raw[:19]==b'\0\0\0\x0bssh-ed25519\0\0\0\x20'
            and base64.b64encode(raw).decode()==match[1])
    return 'ssh-ed25519 '+match[1]


def bootstrap(bundle,hashes,role):
    # No environment/cwd search path is used. All imports beyond stdlib occur
    # only after root ownership, full tree inventory and exact byte hashes pass.
    prefix=f'''#!/usr/bin/python3 -ISB
import hashlib,os,stat,sys
ROOT={LIBRARY+bundle!r}
FILES={dict(hashes)!r}
def fail():
    print('Host deployment boundary refused',file=sys.stderr)
    raise SystemExit(1)
def directory(path):
    info=os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or stat.S_IMODE(info.st_mode)&0o022:fail()
def validate():
    here='/'
    for part in ROOT.strip('/').split('/'):
        here=os.path.join(here,part);directory(here)
    found=set()
    for current,dirs,names in os.walk(ROOT,followlinks=False):
        directory(current)
        for name in dirs:directory(os.path.join(current,name))
        for name in names:
            path=os.path.join(current,name);relative=os.path.relpath(path,ROOT)
            if relative not in FILES:fail()
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
            with os.fdopen(fd,'rb') as file:
                before=os.fstat(file.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_uid!=0 or before.st_nlink!=1 or stat.S_IMODE(before.st_mode)!=0o444 or before.st_size>1048576:fail()
                data=file.read(1048577)
                stamp=lambda s:(s.st_dev,s.st_ino,s.st_uid,s.st_mode,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
                if len(data)!=before.st_size or hashlib.sha256(data).hexdigest()!=FILES[relative] or stamp(os.fstat(file.fileno()))!=stamp(before) or stamp(os.lstat(path))!=stamp(before):fail()
            found.add(relative)
    if found!=set(FILES):fail()
try:
    validate()
    sys.path.insert(0,ROOT)
'''
    require(role in ('shell','root','ssh','backup'))
    if role=='backup':
        body="    if os.geteuid()!=0:fail()\n    from scripts.release import host_backup as entry\n    raise SystemExit(entry.main())\n"
    elif role=='shell':
        body=f'''    if len(sys.argv)!=3 or sys.argv[1:]!=['-c',{ENTRY!r}] or os.environ.get('SSH_ORIGINAL_COMMAND')!='apex-deploy':fail()
    from scripts.release import ssh_entry
    sys.argv=[{ENTRY!r}]
    raise SystemExit(ssh_entry.main())
'''
    else:
        body=f'    from scripts.release import {"root_executor" if role=="root" else "ssh_entry"} as entry\n    raise SystemExit(entry.main())\n'
    return (prefix+body+"except Exception:\n    fail()\n").encode()


def prepare(*,source,reviewed_hashes,public_keys,accounts,sshd_config):
    try:
        require(isinstance(source,Path) and source.is_absolute() and source.resolve(strict=True)==source
                and type(reviewed_hashes) is dict and set(reviewed_hashes)==set(LIBRARY_FILES)
                and type(public_keys) is dict and set(public_keys)==set(STAGES)
                and type(accounts) is dict and set(accounts)==set(STAGES)
                and type(sshd_config) is bytes and 0<len(sshd_config)<=MAX_FILE and b'\0' not in sshd_config)
        source_uid=source.lstat().st_uid
        require(source_uid in (0,os.getuid()) and not stat.S_IMODE(source.lstat().st_mode)&0o022)
        source_data=[]
        for name in LIBRARY_FILES:
            expected=reviewed_hashes[name]
            require(type(expected) is str and re.fullmatch(r'[a-f0-9]{64}',expected))
            current=source
            for part in Path(name).parts[:-1]:
                current=current/part;info=current.lstat()
                require(stat.S_ISDIR(info.st_mode) and info.st_uid==source_uid and not stat.S_IMODE(info.st_mode)&0o022)
            data,_=read_file(source/name,source_uid)
            require(digest(data)==expected)
            compile(data,name,'exec') if name.endswith('.py') else json.loads(data)
            source_data.append((name,data))
        users=[];uids=set();keys=[]
        for stage in STAGES:
            value=accounts[stage];name='xynes_'+stage+'_deploy'
            require(type(value) is dict and set(value)=={'name','uid','gid','home','shell','groups'}
                    and value['name']==name and type(value['uid']) is int and 0<value['uid']<=2147483647
                    and value['uid'] not in uids and type(value['gid']) is int and 0<value['gid']<=2147483647
                    and value['home']=='/home/'+name and value['shell'] in ('/bin/bash','/bin/sh','/usr/sbin/nologin',SHELL)
                    and type(value['groups']) is list and len(set(value['groups']))==len(value['groups'])
                    and name in value['groups'] and set(value['groups'])<={name,'docker','xynes'})
            uids.add(value['uid']);users.append(Account(stage,name,value['uid'],value['gid'],value['home'],value['shell'],tuple(value['groups'])))
            keys.append(public_key(public_keys[stage]))
        require(len(set(keys))==len(STAGES))
        if BEGIN in sshd_config:
            require(sshd_config.count(BEGIN)==1 and sshd_config.endswith(SUFFIX))
            new_main=sshd_config
        else:new_main=sshd_config+(b'' if sshd_config.endswith(b'\n') else b'\n')+SUFFIX
        sources=tuple((name,reviewed_hashes[name]) for name in LIBRARY_FILES)
        bundle=digest(canonical(dict(sources)))
        files=[Asset(LIBRARY+bundle+'/'+name,data,0o444) for name,data in source_data]
        for path,role in ((ENTRY,'ssh'),(EXECUTOR,'root'),(SHELL,'shell'),(BACKUP,'backup')):
            files.append(Asset(path,bootstrap(bundle,sources,role),0o555))
        snippet=('Match User '+','.join(user.name for user in users)+'\n'
            '    ForceCommand '+ENTRY+'\n    AuthorizedKeysFile /etc/ssh/xynes-deploy-keys/%u\n'
            '    AuthorizedKeysCommand none\n    AuthenticationMethods publickey\n    PubkeyAuthentication yes\n'
            '    PasswordAuthentication no\n    KbdInteractiveAuthentication no\n    PermitTTY no\n'
            '    DisableForwarding yes\n    PermitUserRC no\n').encode()
        files.append(Asset(SNIPPET,snippet,0o644))
        for user,key in zip(users,keys):
            files.append(Asset('/etc/ssh/xynes-deploy-keys/'+user.name,
                ('restrict,command="'+ENTRY+'" '+key+'\n').encode(),0o644))
            files.append(Asset('/etc/sudoers.d/xynes-'+user.stage+'-deploy',
                (user.name+' ALL=(root) NOPASSWD: '+EXECUTOR+' '+user.stage+'\n').encode(),0o440))
        files.append(Asset(MAIN,new_main,0o644))
        files=tuple(files);users=tuple(users)
        return Plan(files,users,sources,sshd_config,bundle,plan_digest(files,users,sources,sshd_config,bundle),SEAL)
    except Exception:raise BoundaryError(ERROR) from None


def namespace(root):
    require(isinstance(root,Path) and root.is_absolute() and root.resolve(strict=True)==root)
    info=root.lstat();uid=0 if root==Path('/') else os.getuid()
    require((root!=Path('/') or os.geteuid()==0) and stat.S_ISDIR(info.st_mode)
            and info.st_uid==uid and not stat.S_IMODE(info.st_mode)&0o022)
    return uid


def scope(root):return 'live-host' if root==Path('/') else 'offline-fixture'


def location(root,path,uid,create=False):
    require(path.startswith('/') and str(Path(path))==path and all(part not in ('.','..') for part in Path(path).parts))
    current=root
    for part in Path(path).parts[1:-1]:
        current=current/part
        try:info=current.lstat()
        except FileNotFoundError:
            if not create:raise
            current.mkdir(mode=0o755);current.chmod(0o755);info=current.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid==uid and not stat.S_IMODE(info.st_mode)&0o022)
    return current/Path(path).name


def command(argv,*,timeout_seconds,max_output):
    # Only fixed control-tool argv assembled below reaches this coordinator API.
    try:
        try:from . import ssh_entry
        except ImportError:import ssh_entry
        return ssh_entry.bounded_process(argv,data=b'\n',env=dict(ssh_entry.ENVIRONMENT),cwd='/',
            timeout_seconds=timeout_seconds,max_output=min(max_output,ssh_entry.MAX_OUTPUT))
    except Exception:raise BoundaryError(ERROR) from None


def invoke(run,argv):
    value=run(argv,timeout_seconds=15,max_output=1024*1024)
    require(type(value) is tuple and len(value)==3 and type(value[0]) is int and value[0]==0
            and type(value[1]) is bytes and type(value[2]) is bytes and len(value[1])+len(value[2])<=1024*1024)
    return value[1]


def profile(run,root,name):
    raw=invoke(run,['/usr/sbin/sshd','-T','-f',str(root/MAIN.lstrip('/')),'-C','user='+name+',host=localhost,addr=127.0.0.1'])
    result={}
    for line in raw.decode('ascii').splitlines():
        key,space,value=line.partition(' ')
        require(space and re.fullmatch(r'[a-z][a-z0-9]*',key)
                and '\0' not in value and (key not in result or key in REPEATED_PROFILE_KEYS))
        # Native sshd -T emits one line per element for these list options.
        # Keep their order and every element for recovery-profile comparison.
        result[key]=result[key]+' '+value if key in result else value
    return result


def allowed(profile,name):
    # This preflight has no remote-host or group-membership context. Refuse
    # rules needing those facts instead of treating an incomplete match as safe.
    if 'allowgroups' in profile or 'denygroups' in profile:return False
    patterns={key:profile.get(key,'').split() for key in ('allowusers','denyusers')}
    if any(any(char in pattern for char in '@![]\\') for values in patterns.values() for pattern in values):return False
    if any(fnmatch.fnmatchcase(name,pattern) for pattern in patterns['denyusers']):return False
    return 'allowusers' not in profile or any(fnmatch.fnmatchcase(name,pattern) for pattern in patterns['allowusers'])


def account(run,user):
    value=invoke(run,['/usr/bin/getent','passwd',user.name]).decode('ascii').rstrip('\n').split(':')
    require(len(value)==7 and value[0]==user.name and value[2]==str(user.uid) and value[3]==str(user.gid)
            and value[5]==user.home and value[6] in ('/bin/bash','/bin/sh','/usr/sbin/nologin',SHELL))
    require(invoke(run,['/usr/bin/id','-gn',user.name]).decode('ascii').strip()==user.name)
    groups=invoke(run,['/usr/bin/id','-nG',user.name]).decode('ascii').split()
    require(len(set(groups))==len(groups) and user.name in groups and set(groups)<={user.name,'docker','xynes'})
    return value[6],groups


def opaque(token):
    require(token is not None and type(token) not in (bool,str,bytes,int,float,dict,list,tuple))


def write(root,asset,uid,allowed_hash=None):
    target=location(root,asset.path,uid,create=True)
    old=None;mode=None
    try:
        old,info=read_file(target,uid);mode=stat.S_IMODE(info.st_mode)
        if old==asset.data:
            require(mode==asset.mode);return None
        require(allowed_hash is not None and digest(old)==allowed_hash)
    except FileNotFoundError:info=None
    fd,temporary=tempfile.mkstemp(prefix='.apex-boundary-',dir=target.parent);temp=Path(temporary)
    try:
        with os.fdopen(fd,'wb') as file:
            file.write(asset.data);file.flush();os.fchmod(file.fileno(),asset.mode)
            if root==Path('/'):os.fchown(file.fileno(),0,0)
            os.fsync(file.fileno())
        if info is not None:require(identity(target.lstat())==identity(info))
        else:
            try:target.lstat()
            except FileNotFoundError:pass
            else:require(False)
        os.replace(temp,target)
        return asset,old,mode,target.lstat()
    finally:
        if temp.exists():temp.unlink()


def rollback(root,changes,uid):
    for asset,old,mode,installed in reversed(changes):
        try:
            target=location(root,asset.path,uid)
            require(identity(target.lstat())==identity(installed))
            if old is None:target.unlink()
            else:write(root,Asset(asset.path,old,mode),uid,digest(asset.data))
        except Exception:pass


def audit(plan,*,root=Path('/')):
    try:
        check_plan(plan);uid=namespace(root)
        for asset in plan.files:
            data,info=read_file(location(root,asset.path,uid),uid)
            require(data==asset.data and stat.S_IMODE(info.st_mode)==asset.mode)
        library=root/(LIBRARY+plan.bundle_sha256).lstrip('/')
        found=set()
        for path in library.rglob('*'):
            info=path.lstat()
            require(info.st_uid==uid and not stat.S_IMODE(info.st_mode)&0o022)
            if stat.S_ISDIR(info.st_mode):continue
            require(stat.S_ISREG(info.st_mode))
            found.add(str(path.relative_to(library)))
        require(found==set(LIBRARY_FILES))
        return {'plan_sha256':plan.plan_sha256,'bundle_sha256':plan.bundle_sha256,
                'execution_scope':scope(root),'owned_assets_verified':True,'boundary_ready':False}
    except Exception:raise BoundaryError(ERROR) from None


def install(plan,*,root=Path('/'),run=command,executor_token=None,verify_executor=None):
    changes=[];shell_changes=[];reload_started=False
    try:
        check_plan(plan);uid=namespace(root);opaque(executor_token);require(callable(run) and callable(verify_executor))
        proof=verify_executor(executor_token,plan)
        expected={'plan_sha256':plan.plan_sha256,'bundle_sha256':plan.bundle_sha256,
            'executor_sha256':plan.source_hashes['scripts/release/root_executor.py'],'executor_complete':True}
        require(type(proof) is dict and proof==expected and proof['executor_complete'] is True)
        main_path=location(root,MAIN,uid);current,_=read_file(main_path,uid)
        main_asset=next(asset for asset in plan.files if asset.path==MAIN)
        require(current in (plan.original,main_asset.data))
        previous={}
        try:
            raw,_=read_file(location(root,LEDGER,uid),uid);ledger=json.loads(raw)
            require(type(ledger) is dict and set(ledger)=={'schema_version','plan_sha256','bundle_sha256','files'}
                    and type(ledger['schema_version']) is int and ledger['schema_version']==1
                    and all(type(ledger[key]) is str and re.fullmatch(r'[a-f0-9]{64}',ledger[key]) for key in ('plan_sha256','bundle_sha256'))
                    and type(ledger['files']) is dict and 0<len(ledger['files'])<=256
                    and all(type(path) is str and type(value) is str and re.fullmatch(r'[a-f0-9]{64}',value)
                            for path,value in ledger['files'].items()));previous=ledger['files']
        except FileNotFoundError:pass
        recovery=profile(run,root,'xynes')
        facts={}
        for user in plan.accounts:
            facts[user.name]=account(run,user)
            require(allowed(profile(run,root,user.name),user.name))
        for asset in plan.files:
            allowed_hash=digest(plan.original) if asset.path==MAIN else previous.get(asset.path)
            change=write(root,asset,uid,allowed_hash)
            if change:changes.append(change)
        for user in plan.accounts:
            invoke(run,['/usr/sbin/visudo','-c','-f',str(root/('etc/sudoers.d/xynes-'+user.stage+'-deploy'))])
        invoke(run,['/usr/sbin/sshd','-t','-f',str(main_path)])
        require(profile(run,root,'xynes')==recovery)
        required={'forcecommand':ENTRY,'authorizedkeysfile':'/etc/ssh/xynes-deploy-keys/%u',
            'authenticationmethods':'publickey','passwordauthentication':'no','kbdinteractiveauthentication':'no',
            'permittty':'no','disableforwarding':'yes','permituserrc':'no','authorizedkeyscommand':'none'}
        for user in plan.accounts:
            effective=profile(run,root,user.name)
            require(allowed(effective,user.name) and all(effective.get(key)==value for key,value in required.items()))
            if facts[user.name][0]!=SHELL:
                invoke(run,['/usr/sbin/usermod','--shell',SHELL,user.name]);shell_changes.append((user,facts[user.name][0]))
        library=root/(LIBRARY+plan.bundle_sha256).lstrip('/')
        for directory in sorted((p for p in library.rglob('*') if p.is_dir()),key=lambda p:len(p.parts),reverse=True):directory.chmod(0o555)
        library.chmod(0o555)
        audit(plan,root=root)
        reload_started=True;invoke(run,['/usr/bin/systemctl','reload','ssh'])
        ledger={'schema_version':1,'plan_sha256':plan.plan_sha256,'bundle_sha256':plan.bundle_sha256,
                'files':{asset.path:digest(asset.data) for asset in plan.files}}
        prior_hash=None
        try:prior_hash=digest(read_file(location(root,LEDGER,uid),uid)[0])
        except FileNotFoundError:pass
        change=write(root,Asset(LEDGER,canonical(ledger)+b'\n',0o444),uid,prior_hash)
        if change:changes.append(change)
        return {'installed':True,'plan_sha256':plan.plan_sha256,'bundle_sha256':plan.bundle_sha256,
                'execution_scope':scope(root),'privileges_revoked':False,'boundary_ready':False}
    except Exception:
        if 'uid' in locals():rollback(root,changes,uid)
        for user,shell in reversed(shell_changes):
            try:invoke(run,['/usr/sbin/usermod','--shell',shell,user.name])
            except Exception:pass
        if reload_started:
            try:invoke(run,['/usr/bin/systemctl','reload','ssh'])
            except Exception:pass
        raise BoundaryError(ERROR) from None


def revoke(plan,*,root=Path('/'),run=command,token=None,verify=None):
    try:
        check_plan(plan);namespace(root);opaque(token);require(callable(verify) and callable(run));audit(plan,root=root)
        expected_keys={'stage','plan_sha256','executor_sha256','entry_smoke','native_admission_smoke','recovery_ssh_smoke','managed_paths_sealed','no_active_stage_sessions'}
        # Every stage must qualify before the first irreversible membership change.
        for user in plan.accounts:
            proof=verify(token,user.stage,plan)
            require(type(proof) is dict and set(proof)==expected_keys and proof['stage']==user.stage
                    and proof['plan_sha256']==plan.plan_sha256
                    and proof['executor_sha256']==plan.source_hashes['scripts/release/root_executor.py']
                    and all(proof[key] is True for key in ('entry_smoke','native_admission_smoke','recovery_ssh_smoke','managed_paths_sealed','no_active_stage_sessions')))
        facts={user.name:account(run,user) for user in plan.accounts}
        require(all(shell==SHELL for shell,groups in facts.values()))
        for user in plan.accounts:
            shell,groups=facts[user.name]
            for group in ('docker','xynes'):
                if group in groups:invoke(run,['/usr/bin/gpasswd','--delete',user.name,group])
            require(not set(account(run,user)[1])&{'docker','xynes'})
        return {'plan_sha256':plan.plan_sha256,'execution_scope':scope(root),'privileges_revoked':True,'boundary_ready':False}
    except Exception:raise BoundaryError(ERROR) from None
