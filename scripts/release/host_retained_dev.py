#!/usr/bin/env python3
"""Coordinator-only parking of the retained legacy Dev deployment account.

No SSH API, new deployment stage, deletion or preservation-proof generation.
The trusted coordinator independently audits Dev data, processes, sessions,
sudo authority and protected stage paths. Partial privilege removal never
automatically regrants authority; recovery remains the xynes administrator.
"""
from dataclasses import asdict, dataclass
from pathlib import Path
import re
import stat

try:
    from . import host_boundary as boundary
except ImportError:
    import host_boundary as boundary

ERROR='Retained Dev parking refused'
NAME='xynes_dev_deploy'
UID=GID=1004
HOME='/home/'+NAME
NOLOGIN='/usr/sbin/nologin'
BEGIN=b'# BEGIN XYNES RETAINED DEV PARKING\n'
END=b'# END XYNES RETAINED DEV PARKING\n'
BLOCK=BEGIN+b'DenyUsers xynes_dev_deploy\n'+END
EVIDENCE='/etc/xynes/retained-dev/'
SEAL=object()
FLAGS=('dev_files_preserved','no_active_uid_processes','no_active_uid_sessions',
       'no_other_sudo_authority','protected_stage_paths','recovery_ssh_smoke')


class ParkingError(ValueError):pass


def require(value):
    if not value:raise ParkingError(ERROR)


@dataclass(frozen=True,repr=False)
class Plan:
    original:bytes
    updated:bytes
    account:boundary.Account
    preservation_sha256:str
    plan_sha256:str
    seal:object


def plan_digest(original,updated,account,preservation):
    return boundary.digest(boundary.canonical(dict(original_sha256=boundary.digest(original),
        updated_sha256=boundary.digest(updated),account=asdict(account),dev_preservation_sha256=preservation)))


def check(plan):
    require(type(plan) is Plan and plan.seal is SEAL and
            plan.plan_sha256==plan_digest(plan.original,plan.updated,plan.account,plan.preservation_sha256))


def prepare(*,sshd_config,account,preservation_sha256):
    try:
        require(type(sshd_config) is bytes and 0<len(sshd_config)<=boundary.MAX_FILE
                and b'\0' not in sshd_config and b'\r' not in sshd_config
                and type(preservation_sha256) is str and re.fullmatch(r'[a-f0-9]{64}',preservation_sha256)
                and type(account) is dict and set(account)=={'name','uid','gid','home','shell','groups'}
                and account['name']==NAME and type(account['uid']) is int and account['uid']==UID
                and type(account['gid']) is int and account['gid']==GID and account['home']==HOME
                and account['shell'] in ('/bin/bash','/bin/sh',NOLOGIN)
                and type(account['groups']) is list and len(set(account['groups']))==len(account['groups'])
                and NAME in account['groups'] and set(account['groups'])<={NAME,'docker','xynes'})
        if BEGIN in sshd_config or END in sshd_config:
            require(sshd_config.count(BEGIN)==1 and sshd_config.count(END)==1 and sshd_config.count(BLOCK)==1)
            updated=sshd_config
        else:
            lines=sshd_config.splitlines(keepends=True)
            index=next((i for i,line in enumerate(lines) if re.match(br'(?i)^[ \t]*Match(?:[ \t]|\n|$)',line)),len(lines))
            prefix=b''.join(lines[:index]);suffix=b''.join(lines[index:])
            updated=prefix+(b'' if prefix.endswith(b'\n') or not prefix else b'\n')+BLOCK+suffix
        # A managed block inside a Match scope is not an acceptable substitute.
        first_match=re.search(br'(?im)^[ \t]*Match(?:[ \t]|$)',updated)
        require(first_match is None or updated.index(BEGIN)<first_match.start())
        native=boundary.Account('dev',NAME,UID,GID,HOME,account['shell'],tuple(account['groups']))
        return Plan(sshd_config,updated,native,preservation_sha256,
                    plan_digest(sshd_config,updated,native,preservation_sha256),SEAL)
    except Exception:raise ParkingError(ERROR) from None


def native_account(run,plan):
    values=boundary.invoke(run,['/usr/bin/getent','passwd',NAME]).decode('ascii').rstrip('\n').split(':')
    require(len(values)==7 and values[0]==NAME and values[2:4]==[str(UID),str(GID)]
            and values[5]==HOME and values[6] in (plan.account.shell,NOLOGIN))
    require(boundary.invoke(run,['/usr/bin/id','-gn',NAME]).decode('ascii').strip()==NAME)
    groups=boundary.invoke(run,['/usr/bin/id','-nG',NAME]).decode('ascii').split()
    require(len(groups)==len(set(groups)) and NAME in groups and set(groups)<=set(plan.account.groups))
    return values[6],groups


def qualify(token,verify,plan):
    boundary.opaque(token);require(callable(verify))
    proof=verify(token,plan)
    require(type(proof) is dict and set(proof)=={'plan_sha256','dev_preservation_sha256',*FLAGS}
            and proof['plan_sha256']==plan.plan_sha256
            and proof['dev_preservation_sha256']==plan.preservation_sha256
            and all(proof[key] is True for key in FLAGS))


def private_evidence(root,uid,plan):
    current=root
    for part in ('etc','xynes','retained-dev',plan.plan_sha256):
        current=current/part
        private=part in ('retained-dev',plan.plan_sha256)
        try:info=current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700 if private else 0o755);info=current.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid==uid and not stat.S_IMODE(info.st_mode)&0o022)
        if private:require(stat.S_IMODE(info.st_mode)==0o700)
    boundary.write(root,boundary.Asset(EVIDENCE+plan.plan_sha256+'/sshd_config.before',plan.original,0o600),uid)


def compare_profile(before,after,*,added):
    expected=dict(before);actual=dict(after)
    if added:
        deny=actual.get('denyusers','').split()
        require(NAME in deny)
        deny.remove(NAME)
        if deny:actual['denyusers']=' '.join(deny)
        else:actual.pop('denyusers',None)
    require(actual==expected)


def park(plan,*,root=Path('/'),run=boundary.command,token=None,verify=None):
    """Install Dev denial then revoke legacy groups using fresh trusted proofs.

    root/run are trusted fixture boundaries, never request data. Every command
    has fixed operation/account and 15-second/64KiB bounds. Failed SSH changes
    roll back before revocation; once revocation starts, denial stays installed.
    No isolation acceptance follows without independent key/access checks.
    """
    change=None;reload_started=False;revocation_started=False
    try:
        check(plan);uid=boundary.namespace(root);require(callable(run))
        def bounded(argv,*,timeout_seconds,max_output):
            return run(argv,timeout_seconds=15,max_output=65536)
        qualify(token,verify,plan)
        main=boundary.location(root,boundary.MAIN,uid)
        current,info=boundary.read_file(main,uid)
        require(current in (plan.original,plan.updated) and stat.S_IMODE(info.st_mode)==0o644)
        native_account(bounded,plan)
        profiles={name:boundary.profile(bounded,root,name) for name in ('xynes','xynes_qa_deploy','xynes_staging_deploy','xynes_prod_deploy',NAME)}
        require(boundary.allowed(profiles['xynes'],'xynes'))
        private_evidence(root,uid,plan)
        change=boundary.write(root,boundary.Asset(boundary.MAIN,plan.updated,0o644),uid,boundary.digest(plan.original))
        boundary.invoke(bounded,['/usr/sbin/sshd','-t','-f',str(main)])
        added=current!=plan.updated and plan.original!=plan.updated
        for name,before in profiles.items():
            after=boundary.profile(bounded,root,name)
            compare_profile(before,after,added=added)
            if name=='xynes':require(boundary.allowed(after,name))
            if name==NAME:require(NAME in after.get('denyusers','').split() and not boundary.allowed(after,name))
        reload_started=True;boundary.invoke(bounded,['/usr/bin/systemctl','reload','ssh'])
        qualify(token,verify,plan)
        shell,groups=native_account(bounded,plan)
        revocation_started=True
        if shell!=NOLOGIN:boundary.invoke(bounded,['/usr/sbin/usermod','--shell',NOLOGIN,NAME])
        for group in ('docker','xynes'):
            shell,groups=native_account(bounded,plan)
            require(shell==NOLOGIN)
            if group in groups:boundary.invoke(bounded,['/usr/bin/gpasswd','--delete',NAME,group])
        require(native_account(bounded,plan)==(NOLOGIN,[NAME]))
        qualify(token,verify,plan)
        final,info=boundary.read_file(main,uid)
        require(final==plan.updated and stat.S_IMODE(info.st_mode)==0o644)
        return dict(plan_sha256=plan.plan_sha256,dev_preservation_sha256=plan.preservation_sha256,
                    parked=True,isolation_ready=False,execution_scope=boundary.scope(root))
    except Exception:
        if not revocation_started and change is not None:
            boundary.rollback(root,[change],uid)
            if reload_started:
                try:boundary.invoke(bounded,['/usr/bin/systemctl','reload','ssh'])
                except Exception:pass
        raise ParkingError(ERROR) from None
