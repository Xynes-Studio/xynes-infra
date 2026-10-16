#!/usr/bin/env python3
"""Transport exact Git migration/prerequisite blobs as data, never execute them.

create() runs in the credential-free exact-source build. extract() runs as root
inside private caller-owned work AFTER native artifact/signature verification.
The expected archive hash/controller/manifest must come from that trusted intake,
not request fields. application_inputs() returns the existing reviewed SQL plan
and bytes for migration_sql.render; no admission, freshness, seeds or SQL execution
is conferred by any function here.
"""
import copy
from dataclasses import dataclass, field
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tarfile
import tempfile
try:
    from . import migration_inventory, migration_plan, release_manifest as records, source_context
except ImportError:
    import migration_inventory, migration_plan, release_manifest as records, source_context

ERROR='Migration transport refused'
MAX_BUNDLE=32*1024*1024
MAX_FILE=migration_inventory.MAX_BLOB
EXTRACT_UID=0
METADATA=('controller.json','migration-inventory.json','migration-plan.json','source-manifest.json')
RECORD='transport-record.json'

class TransportError(ValueError):pass

def require(value):
    if not value:raise TransportError(ERROR)

def sha(raw):return hashlib.sha256(raw).hexdigest()

def encode(value):
    records.bounded_tree(value)
    return (json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)+'\n').encode()

def inputs(manifest_bytes,controller):
    require(type(manifest_bytes) is bytes and 0<len(manifest_bytes)<=records.MAX_BYTES)
    manifest=records.strict_json(manifest_bytes.decode('utf8'))
    source_context.validate_manifest(manifest,'release');records.validate_controller(controller)
    require(type(controller['run_attempt']) is int and controller['run_attempt']==1)
    return manifest

def signature(info):
    return (info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_gid,info.st_nlink,
            info.st_size,info.st_mtime_ns,info.st_ctime_ns)

def private_directory(path,uid):
    require(isinstance(path,Path) and path.is_absolute() and path.resolve(strict=True)==path)
    info=path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid==uid and stat.S_IMODE(info.st_mode)==0o700)

def private_bytes(path,uid,limit=MAX_BUNDLE):
    require(isinstance(path,Path) and path.is_absolute() and path.resolve(strict=True)==path)
    descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
    with os.fdopen(descriptor,'rb') as file:
        before=os.fstat(file.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_uid==uid and before.st_nlink==1
                and stat.S_IMODE(before.st_mode)==0o600 and 0<before.st_size<=limit)
        raw=file.read(limit+1)
        require(len(raw)==before.st_size and signature(os.fstat(file.fileno()))==signature(before)
                and signature(path.lstat())==signature(before))
        return raw

def expected_sources():
    names={'sources/'+owner+'/'+path for owner,paths in migration_plan.REVIEWED_FILES.items() for path in paths}
    names.add('sources/xynes-infra/docs/DATABASE.md')
    require(len(names)==87)
    return names

def expected_files():return expected_sources()|set(METADATA)|{RECORD}

def safe_name(name):
    migration_inventory.safe_path(name)
    require(name in expected_files())

def remember(path,owned,directory):
    owned.append((path,path.lstat(),directory))

def cleanup(owned):
    # Never recursively delete unknown/replaced descendants or existing paths.
    for path,before,directory in reversed(owned):
        try:
            if os.path.samestat(before,path.lstat()):path.rmdir() if directory else path.unlink()
        except OSError:pass

def put(root,name,raw,owned):
    safe_name(name);require(type(raw) is bytes and 0<=len(raw)<=MAX_FILE)
    file=root/name;parent=root
    for component in Path(name).parts[:-1]:
        parent=parent/component
        if not parent.exists():
            parent.mkdir(mode=0o700);remember(parent,owned,True)
        info=parent.lstat();require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
                                   and info.st_uid==os.getuid() and stat.S_IMODE(info.st_mode)==0o700)
    descriptor=os.open(file,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    remember(file,owned,False)
    with os.fdopen(descriptor,'wb') as out:
        os.fchmod(out.fileno(),0o600);out.write(raw);out.flush();os.fsync(out.fileno())

def transport_record(files,manifest,controller,inventory,plan):
    require(set(files)==expected_sources()|set(METADATA))
    result={'schema_version':1,'artifact_kind':'immutable-migration-transport',
            'controller':copy.deepcopy(controller),'source_manifest_sha256':records.canonical_sha256(manifest),
            'source_manifest_bytes_sha256':sha(files['source-manifest.json']),
            'inventory_sha256':records.canonical_sha256(inventory),'plan_sha256':records.canonical_sha256(plan),
            'files':[{'path':name,'sha256':sha(files[name]),'size':len(files[name])} for name in sorted(files)],
            'approved':False,'deployable':False}
    records.bounded_tree(result);return result

def create(*,manifest_bytes,controller,repositories,output):
    """Write one deterministic migration-bundle.tar from exact Git objects.

    repositories contains xynes-infra plus the seven backend checkouts. Their HEADs
    must be the controller/source commits; dirty working files are never read.
    Output's parent must be private and current-UID-owned. Existing output refuses.
    """
    owned=[];temporary=[]
    try:
        manifest=inputs(manifest_bytes,controller)
        require(isinstance(output,Path) and output.is_absolute())
        private_directory(output.parent,os.getuid())
        require(not output.exists() and not output.is_symlink()
                and type(repositories) is dict and set(repositories)==set(migration_inventory.OWNERS))
        commits={'xynes-infra':controller['commit'],**{owner:manifest['sources'][owner]['commit'] for owner in source_context.BACKENDS}}
        for owner,repo in repositories.items():
            require(isinstance(repo,Path) and repo.is_absolute() and repo.resolve(strict=True)==repo
                    and source_context.git(repo,'rev-parse','HEAD').strip().decode()==commits[owner])
        inventory=migration_inventory.inventory(manifest,repositories,controller['commit'])
        workspace=Path(tempfile.mkdtemp(prefix='migration-export-',dir=output.parent))
        remember(workspace,temporary,True)
        roots={owner:workspace/'sources'/owner for owner in migration_inventory.OWNERS}
        files={}
        for owner in migration_inventory.OWNERS:
            tree=migration_inventory.tree(repositories[owner],commits[owner])
            bindings=list(inventory['migrations'][owner])
            if owner=='xynes-infra':bindings.append(inventory['ownership_reference'])
            for binding in bindings:
                name='sources/'+owner+'/'+binding['path'];safe_name(name)
                raw=migration_inventory.blob(repositories[owner],tree,binding['path'])
                require(sha(raw)==binding['sha256'] and name not in files)
                files[name]=raw;put(workspace,name,raw,temporary)
        require(set(files)==expected_sources() and sum(map(len,files.values()))<=MAX_BUNDLE//2)
        plan=migration_plan.plan(manifest,inventory,roots)
        require(len(plan['units'])==40)
        files.update({'source-manifest.json':manifest_bytes,'controller.json':encode(controller),
                      'migration-inventory.json':encode(inventory),'migration-plan.json':encode(plan)})
        record=transport_record(files,manifest,controller,inventory,plan);files[RECORD]=encode(record)
        require(set(files)==expected_files() and sum(map(len,files.values()))+len(files)*1024<MAX_BUNDLE)
        descriptor=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
        remember(output,owned,False)
        with os.fdopen(descriptor,'wb') as out:
            os.fchmod(out.fileno(),0o600)
            with tarfile.open(fileobj=out,mode='w',format=tarfile.USTAR_FORMAT) as archive:
                for name in sorted(files):
                    raw=files[name];item=tarfile.TarInfo(name);item.size=len(raw);item.mode=0o600
                    item.uid=item.gid=item.mtime=0;item.uname=item.gname=''
                    archive.addfile(item,io.BytesIO(raw))
            out.flush();os.fsync(out.fileno())
        raw=private_bytes(output,os.getuid())
        return {'schema_version':1,'artifact_kind':'immutable-migration-bundle-receipt',
                'controller':copy.deepcopy(controller),'bundle_sha256':sha(raw),'bundle_size':len(raw),
                'source_manifest_sha256':record['source_manifest_sha256'],
                'source_manifest_bytes_sha256':record['source_manifest_bytes_sha256'],
                'inventory_sha256':record['inventory_sha256'],'plan_sha256':record['plan_sha256'],
                'files':sorted(files),'approved':False,'deployable':False}
    except Exception:
        cleanup(owned);raise TransportError(ERROR) from None
    finally:cleanup(temporary)

def tar_payloads(raw):
    """Parse bounded uncompressed USTAR directly, without extract/PAX handling."""
    require(type(raw) is bytes and 1024<=len(raw)<=MAX_BUNDLE and len(raw)%512==0)
    result={};offset=0;names=[]
    while offset+512<=len(raw):
        block=raw[offset:offset+512]
        if not block.strip(b'\0'):
            require(len(raw)-offset>=1024 and not raw[offset:].strip(b'\0'));break
        require(len(result)<len(expected_files()) and block[257:265]==b'ustar\x0000')
        item=tarfile.TarInfo.frombuf(block,'utf-8','strict')
        require(item.type==tarfile.REGTYPE and item.mode==0o600 and item.uid==item.gid==item.mtime==0
                and item.uname==item.gname==item.linkname=='' and not item.pax_headers
                and 0<=item.size<=MAX_FILE)
        safe_name(item.name);require(item.name not in result)
        start=offset+512;end=start+item.size;next_offset=start+((item.size+511)//512)*512
        require(next_offset<=len(raw) and not raw[end:next_offset].strip(b'\0'))
        result[item.name]=raw[start:end];names.append(item.name);offset=next_offset
    else:require(False)
    require(set(result)==expected_files() and names==sorted(result))
    return result

@dataclass(frozen=True)
class Verified:
    directory:Path=field(repr=False)
    roots:dict=field(repr=False)
    plan:dict=field(repr=False)
    inventory:dict=field(repr=False)
    files:dict=field(repr=False)
    controller:dict
    source_manifest_sha256:str
    deployable:bool=False

def extract(*,bundle,expected_sha256,manifest_bytes,controller,work):
    """Verify trusted artifact bytes and extract private data; no execution occurs."""
    owned=[]
    try:
        manifest=inputs(manifest_bytes,controller);records.hash_value(expected_sha256)
        require(os.getuid()==EXTRACT_UID)
        private_directory(work,EXTRACT_UID)
        raw=private_bytes(bundle,EXTRACT_UID);require(sha(raw)==expected_sha256)
        files=tar_payloads(raw)
        require(files['source-manifest.json']==manifest_bytes
                and records.strict_json(files['controller.json'].decode())==controller)
        inventory=records.strict_json(files['migration-inventory.json'].decode())
        plan=records.strict_json(files['migration-plan.json'].decode())
        require(inventory['infra_commit']==controller['commit'] and len(plan['units'])==40)
        record=records.strict_json(files[RECORD].decode())
        require(record==transport_record({name:data for name,data in files.items() if name!=RECORD},
                                        manifest,controller,inventory,plan))
        # Hash every SQL and prerequisite before creating any output directory.
        for owner,bindings in inventory['migrations'].items():
            require(owner in migration_inventory.OWNERS)
            for binding in bindings:
                name='sources/'+owner+'/'+binding['path'];safe_name(name)
                require(sha(files[name])==binding['sha256'])
        reference=inventory['ownership_reference']
        require(reference['path']=='docs/DATABASE.md'
                and sha(files['sources/xynes-infra/'+reference['path']])==reference['sha256'])
        destination=work/'migration';destination.mkdir(mode=0o700);remember(destination,owned,True)
        for name,data in files.items():put(destination,name,data,owned)
        roots={owner:destination/'sources'/owner for owner in migration_inventory.OWNERS}
        require(migration_plan.plan(manifest,inventory,roots)==plan)
        return Verified(destination,roots,copy.deepcopy(plan),copy.deepcopy(inventory),
                        {name:{'size':len(data),'sha256':sha(data)} for name,data in files.items()},
                        copy.deepcopy(controller),records.canonical_sha256(manifest))
    except Exception:
        cleanup(owned);raise TransportError(ERROR) from None

def application_inputs(verified):
    """Recheck extracted data and return (40 ordered units, exact SQL payload map).

    Parent passes these to migration_sql.render(stage, 'postgres', units, payloads)
    and applies its result through a fixed direct-DB runner. The separately bound
    seed_requirements still require their owned seed implementation and acceptance.
    """
    try:
        require(type(verified) is Verified and verified.deployable is False and os.getuid()==EXTRACT_UID)
        private_directory(verified.directory,EXTRACT_UID)
        require(verified.roots=={owner:verified.directory/'sources'/owner for owner in migration_inventory.OWNERS})
        actual={path.relative_to(verified.directory).as_posix() for path in verified.directory.rglob('*') if not path.is_dir()}
        require(actual==expected_files() and set(verified.files)==actual)
        files={name:private_bytes(verified.directory/name,EXTRACT_UID,MAX_FILE) for name in actual}
        require(all({'size':len(data),'sha256':sha(data)}==verified.files[name] for name,data in files.items()))
        manifest=inputs(files['source-manifest.json'],verified.controller)
        inventory=records.strict_json(files['migration-inventory.json'].decode())
        plan=records.strict_json(files['migration-plan.json'].decode())
        require(plan==verified.plan and inventory==verified.inventory
                and records.canonical_sha256(manifest)==verified.source_manifest_sha256
                and migration_plan.plan(manifest,inventory,verified.roots)==plan)
        units=copy.deepcopy(plan['units'])
        payloads={(unit['owner'],unit['path']):files['sources/'+unit['owner']+'/'+unit['path']] for unit in units}
        require(len(units)==len(payloads)==40)
        return units,payloads
    except Exception:raise TransportError(ERROR) from None
