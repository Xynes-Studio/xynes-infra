#!/usr/bin/env python3
"""Allowlisted exact-Git operational source only; never archive the full tree."""
import argparse
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import tarfile
try:
    from . import source_context
except ImportError:
    import source_context
SOURCE_ROOTS=('scripts/release/','scripts/lib/','infra/idle/adapter/')
PRIVATE_DIRS={'reports','generated','logs','artifacts','private','node_modules','__pycache__'}
FILES={'scripts/backup-pg.sh','scripts/restore-pg.sh','scripts/smoke-health.sh',
       'infra/compose/hosted.yml','infra/idle/adapter/go.mod','infra/idle/adapter/go.sum',
       'infra/release/source-manifest.json','infra/release/source-manifest.schema.json',
       'infra/release/release-manifest.schema.json','infra/release/registry-policy.json',
       'infra/release/source-check-catalog.json','infra/release/verifier-toolchain.json',
       'infra/release/backup-toolchain.json'}
FILES |= {'infra/release/auth/'+path for path in
          ('compose.fragment.yml','bootstrap.sh','bootstrap.sql','templates.Caddyfile')}
FILES |= {'infra/release/auth/templates/'+kind+'.html' for kind in
          ('confirmation','recovery','magic_link','invite','email_change','reauthentication')}
FILES |= {'infra/compose/'+name for name in
          ('qa.yml','staging.yml','prod.yml','internal-request-identities.yml')}
FILES |= {'infra/processors/'+name+'.sh' for name in
          ('definitions-ready','freshclam-updater','freshclam-health','scanner-start','scanner-health')}
FILES |= {'scripts/setup/vps-reconcile.sh','scripts/test/cms-content-delivery-routes.test.sh',
          'scripts/test/cms-delivery-contract-parity.test.sh'}
FILES |= {'scripts/test/fixtures/cms-delivery/'+name for name in
          ('routes-prerequisites.test.py','routes-contract.py','access-smoke.py')}
FILES |= {'scripts/test/fixtures/cms-content-integrations/'+name for name in
          ('pinned-inputs.test.py','pinned_inputs.py','smoke-safety.test.py','smoke.py','parity.py',
           'schema-probe.ts','copied-request.ts','copied-request-hooks.ts','copied-request-unit.test.ts')}
BACKUP_UNITS={'infra/release/systemd/'+name for name in
              ('xynes-backup@.service','xynes-backup@.timer','xynes-backup-alert@.service')}
FILES |= BACKUP_UNITS
IDLE_UNITS={'infra/release/systemd/xynes-idle-'+stage+'-'+role+'.service'
            for stage in ('qa','staging','prod') for role in ('auth','cms','broker')}
FILES |= IDLE_UNITS | {'infra/release/systemd/xynes-idle-tmpfiles.conf',
                       'scripts/setup/configure-apex-ingress.py'}
SOURCE_EXTENSIONS={'.py','.sh','.ts','.go'}
MAX_FILE=2*1024*1024
MAX_TOTAL=32*1024*1024

def require(value):
    if not value:raise ValueError('Public operational bundle refused')

def allowed(path):
    require(isinstance(path,str) and (path in BACKUP_UNITS or re.fullmatch(r'[A-Za-z0-9._/-]{1,255}',path)))
    require(all(part not in ('.','..') for part in path.split('/')))
    if any(part.startswith('.env') or part in PRIVATE_DIRS for part in path.split('/')):
        return False
    if path in FILES:
        return True
    suffix=Path(path).suffix
    return (path.startswith(SOURCE_ROOTS) and suffix in SOURCE_EXTENSIONS
            or path.startswith('infra/images/') and suffix in ('.Dockerfile','.dockerignore')
            or path.startswith('supabase/migrations/') and suffix=='.sql')

def bundle(repo,commit,output):
    require(isinstance(commit,str) and re.fullmatch(r'[a-f0-9]{40}',commit))
    require(source_context.git(repo,'cat-file','-t',commit).strip()==b'commit')
    output=Path(output);require(output.is_absolute() and output.parent.is_dir() and not output.parent.is_symlink())
    rows=source_context.git(repo,'ls-tree','-rz',commit).split(b'\0');selected=[];total=0
    for row in rows:
        if not row:continue
        header,path=row.split(b'\t',1);mode,kind,oid=header.decode().split();path=path.decode('utf8')
        if not allowed(path):continue
        require(mode in ('100644','100755') and kind=='blob')
        size=int(source_context.git(repo,'cat-file','-s',oid));total+=size
        require(size<=MAX_FILE and total<=MAX_TOTAL and len(selected)<1000)
        data=source_context.git(repo,'cat-file','blob',oid);require(len(data)==size)
        selected.append((path,mode,data))
    require(selected)
    descriptor=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
    created=os.fstat(descriptor)
    try:
        with os.fdopen(descriptor,'wb') as file:
            with tarfile.open(fileobj=file,mode='w') as archive:
                for path,mode,data in sorted(selected):
                    member=tarfile.TarInfo(path);member.size=len(data);member.mode=0o755 if mode=='100755' else 0o644
                    member.uid=member.gid=member.mtime=0;archive.addfile(member,io.BytesIO(data))
            file.flush();os.fsync(file.fileno())
    except Exception:
        # Only remove this newly created output; no input or old output touched.
        try:
            current=output.lstat()
            if stat.S_ISREG(current.st_mode) and os.path.samestat(created,current):output.unlink()
        except FileNotFoundError:pass
        raise
    return {'artifact_kind':'allowlisted-operational-source','source_commit':commit,'files':len(selected),'source_bytes':total,'approval_verified':False}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--repo',type=Path,default=Path('.'));p.add_argument('--commit',required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    try:record=bundle(a.repo,a.commit,a.output)
    except (OSError,ValueError,UnicodeError,KeyError):
        print('Public operational bundle refused: unsafe or unavailable source/output',file=sys.stderr);return 1
    print(json.dumps(record,sort_keys=True));return 0
if __name__=='__main__':sys.exit(main())
