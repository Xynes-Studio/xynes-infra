#!/usr/bin/env python3
"""Private durable release assets; only the separate ledger grants acceptance.

The root coordinator validates native candidates and qualifies these assets both
before and after persistence. No code, SQL, Docker operation or request-selected
path executes here. Current/previous and the first migration origin remain data.
Interrupted writes retain a private incomplete directory for reconciliation.
"""
import copy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import stat

try:
    from . import host_compose, host_release_state, migration_transport, root_executor
    from . import release_manifest as records
except ImportError:
    import host_compose, host_release_state, migration_transport, root_executor
    import release_manifest as records

ROOT_UID=0
ERROR='Durable release assets refused'
META_KEYS={'schema_version','artifact_kind','stage','token','controller','publication',
           'source_manifest_sha256','prepared','files','proofs'}
RECOVERY_KEYS={'schema_version','artifact_kind','stage','accepted_token','attempt_token',
               'accepted_binding_sha256','accepted_asset_sha256','pending_request_sha256',
               'previous_token','previous_record_sha256','previous_compose_sha256','prepared','files'}
HEAD_KEYS={'schema_version','artifact_kind','stage','accepted_token','latest_token','record_sha256'}

class AssetError(ValueError):pass

def require(value):
    if not value:raise AssetError(ERROR)

def stamp(info):
    return (info.st_dev,info.st_ino,info.st_uid,info.st_mode,info.st_nlink,
            info.st_size,info.st_mtime_ns,info.st_ctime_ns)

def encode(value):
    records.bounded_tree(value)
    return (json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode()

def read(path,*,limit=1024*1024):
    require(path.resolve(strict=True)==path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
    with os.fdopen(fd,'rb') as file:
        before=os.fstat(file.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_uid==ROOT_UID
                and before.st_nlink==1 and stat.S_IMODE(before.st_mode)==0o600
                and 0<before.st_size<=limit)
        raw=file.read(limit+1)
        require(len(raw)==before.st_size and stamp(before)==stamp(os.fstat(file.fileno()))
                and stamp(before)==stamp(path.lstat()))
        return raw

def directory(path):
    require(path.is_absolute() and path.resolve(strict=True)==path)
    info=path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid==ROOT_UID and stat.S_IMODE(info.st_mode)==0o700)

def fsync_directory(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:os.fsync(fd)
    finally:os.close(fd)

class Store:
    def __init__(self,stage,*,base=None):
        require(stage in ('qa','staging','prod'))
        self.stage=stage
        # base is an explicit trusted fixture setting, never part of a request.
        self.base=Path('/var/lib/xynes/releases')/stage if base is None else base
        require(isinstance(self.base,Path) and self.base.is_absolute())

    def check(self):
        require(os.geteuid()==ROOT_UID)
        directory(self.base)
        if ROOT_UID==0:
            for path in self.base.parents:
                info=path.lstat()
                require(stat.S_ISDIR(info.st_mode) and info.st_uid==0 and not info.st_mode&0o022)

    def destination(self,token):
        require(type(token) is str and re.fullmatch(r'[a-f0-9]{64}',token))
        return self.base/token

    def write(self,destination,name,raw,catalog):
        require(type(raw) is bytes and 0<len(raw)<=1024*1024)
        parts=Path(name).parts
        require(not name.startswith('/') and '..' not in parts and '.' not in parts)
        current=destination
        for part in parts[:-1]:
            current=current/part
            if not current.exists():current.mkdir(mode=0o700)
            directory(current)
        path=destination/name
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
        with os.fdopen(fd,'wb') as file:
            os.fchmod(file.fileno(),0o600);file.write(raw);file.flush();os.fsync(file.fileno())
        catalog[name]={'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}

    def migration(self,destination,prefix,verified,catalog):
        require(type(verified) is migration_transport.Verified and verified.deployable is False)
        migration_transport.application_inputs(verified)
        for name,binding in sorted(verified.files.items()):
            raw=read(verified.directory/name,limit=migration_transport.MAX_FILE)
            require(len(raw)==binding['size'] and hashlib.sha256(raw).hexdigest()==binding['sha256'])
            self.write(destination,prefix+'/'+name,raw,catalog)

    def persist(self,assets,*,token,proofs,state):
        try:
            self.check()
            require(type(state) is host_release_state.ReleaseState and state.stage==self.stage
                    and state.owner_uid==ROOT_UID and state._read()==state.data
                    and type(assets) is root_executor.ReleaseAssets)
            pending=state.read_pending()
            require(pending is not None and pending['token']==token
                    and pending['request']['controller']==assets.controller
                    and pending['request']['publication']==assets.publication
                    and pending['source_sha256']=='sha256:'+assets.migration.source_manifest_sha256
                    and assets.prepared.stage==self.stage and assets.prepared.host_approved is False
                    and type(assets.prepared.storage_generation) is str
                    and re.fullmatch(r'[a-f0-9]{64}',assets.prepared.storage_generation)
                    and assets.migration.controller==assets.controller
                    and records.canonical_sha256(assets.manifest)==assets.migration.source_manifest_sha256
                    and type(assets.origin) is migration_transport.Verified)
            records.validate_controller(assets.controller)
            compose=read(assets.prepared.file)
            require(hashlib.sha256(compose).hexdigest()==assets.prepared.sha256)
            destination=self.destination(token)
            require(not destination.exists() and not destination.is_symlink())
            destination.mkdir(mode=0o700);catalog={}
            self.write(destination,'compose.json',compose,catalog)
            for name,value in (('manifest.json',assets.manifest),('public.json',assets.public),('receipt.json',assets.receipt)):
                self.write(destination,name,encode(value),catalog)
            self.migration(destination,'migration',assets.migration,catalog)
            self.migration(destination,'origin',assets.origin,catalog)
            meta={'schema_version':1,'artifact_kind':'durable-release-assets','stage':self.stage,'token':token,
                  'controller':assets.controller,'publication':assets.publication,
                  'source_manifest_sha256':assets.migration.source_manifest_sha256,
                  'prepared':{'sha256':assets.prepared.sha256,'images':assets.prepared.images,
                              'storage_generation':assets.prepared.storage_generation},
                  'files':catalog,'proofs':copy.deepcopy(proofs)}
            # All content and directory entries precede the completion record.
            for path in sorted((p for p in destination.rglob('*') if p.is_dir()),key=lambda p:len(p.parts),reverse=True):
                directory(path);fsync_directory(path)
            fsync_directory(destination)
            self.write(destination,'asset-record.json',encode(meta),{})
            fsync_directory(destination);fsync_directory(self.base)
            require(state.read_pending()==pending and state._read()==state.data)
            return self._load(destination,meta)
        except Exception:
            raise AssetError(ERROR) from None

    def _verified(self,destination,prefix):
        root=destination/prefix
        files={name:read(root/name,limit=migration_transport.MAX_FILE) for name in migration_transport.expected_files()}
        controller=records.strict_json(files['controller.json'].decode())
        manifest=migration_transport.inputs(files['source-manifest.json'],controller)
        inventory=records.strict_json(files['migration-inventory.json'].decode())
        plan=records.strict_json(files['migration-plan.json'].decode())
        record=records.strict_json(files['transport-record.json'].decode())
        require(record==migration_transport.transport_record({n:d for n,d in files.items() if n!='transport-record.json'},
                    manifest,controller,inventory,plan))
        roots={owner:root/'sources'/owner for owner in migration_transport.migration_inventory.OWNERS}
        verified=migration_transport.Verified(root,roots,plan,inventory,
            {name:{'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()} for name,raw in files.items()},
            controller,records.canonical_sha256(manifest))
        migration_transport.application_inputs(verified)
        return verified

    def _load(self,destination,meta):
        require(type(meta) is dict and set(meta)==META_KEYS and type(meta['schema_version']) is int
                and meta['schema_version']==1 and meta['artifact_kind']=='durable-release-assets'
                and meta['stage']==self.stage and destination==self.destination(meta['token'])
                and type(meta['files']) is dict)
        directory(destination)
        expected={'compose.json','manifest.json','public.json','receipt.json'}
        expected|={prefix+'/'+name for prefix in ('migration','origin') for name in migration_transport.expected_files()}
        require(set(meta['files'])==expected)
        actual=set()
        expected_directories={str(parent) for name in expected for parent in Path(name).parents if str(parent)!='.'}
        for path in destination.rglob('*'):
            if path.is_dir():
                directory(path);require(path.relative_to(destination).as_posix() in expected_directories)
            else:actual.add(path.relative_to(destination).as_posix())
        require(actual==expected|{'asset-record.json'})
        for name,binding in meta['files'].items():
            require(type(binding) is dict and set(binding)=={'size','sha256'} and type(binding['size']) is int)
            raw=read(destination/name)
            require(len(raw)==binding['size'] and hashlib.sha256(raw).hexdigest()==binding['sha256'])
        manifest=records.read_json(destination/'manifest.json');public=records.read_json(destination/'public.json')
        receipt=records.read_json(destination/'receipt.json');verified=self._verified(destination,'migration')
        origin=self._verified(destination,'origin')
        records.validate_controller(meta['controller'])
        require(verified.controller==meta['controller'] and verified.source_manifest_sha256==meta['source_manifest_sha256']
                and records.canonical_sha256(manifest)==verified.source_manifest_sha256)
        spec=meta['prepared'];require(type(spec) is dict and set(spec)=={'sha256','images','storage_generation'}
                and type(spec['images']) is dict and type(spec['storage_generation']) is str
                and re.fullmatch(r'[a-f0-9]{64}',spec['storage_generation'])
                and hashlib.sha256(read(destination/'compose.json')).hexdigest()==spec['sha256'])
        prepared=host_compose.Prepared(self.stage,destination/'compose.json',spec['sha256'],copy.deepcopy(spec['images']),
                                     storage_generation=spec['storage_generation'])
        return root_executor.ReleaseAssets(prepared,verified,meta['controller'],meta['publication'],manifest,public,receipt,origin)

    def normalized(self,prepared):
        # Qualifying the whole native Compose contract remains the parent Driver's
        # responsibility. This comparison permits exactly two ephemeral values.
        require(type(prepared) is host_compose.Prepared and prepared.stage==self.stage
                and prepared.host_approved is False and type(prepared.storage_generation) is str
                and re.fullmatch(r'[a-f0-9]{64}',prepared.storage_generation))
        raw=read(prepared.file);require(hashlib.sha256(raw).hexdigest()==prepared.sha256)
        value=records.strict_json(raw.decode('utf-8'))
        storage=value['services']['storage-service'];labels=storage['labels']
        require(type(labels) is dict and labels.get('com.xynes.storage-generation')==prepared.storage_generation
                and type(storage['volumes']) is list)
        mounts=[row for row in storage['volumes'] if type(row) is dict and row.get('target')=='/run/xynes/storage-control']
        require(len(mounts)==1 and mounts[0].get('type')=='bind'
                and mounts[0].get('source')=='/run/xynes/storage/'+self.stage+'/'+prepared.storage_generation[:32])
        labels['com.xynes.storage-generation']='fixed-generation-placeholder'
        mounts[0]['source']='/run/xynes/storage/'+self.stage+'/fixed-generation-placeholder'
        return encode(value)

    def accepted_binding(self,record):
        require(type(record) is dict and record.get('status')=='accepted')
        # Soak observations may be appended later; the accepted source/request,
        # acceptance timestamp and smoke identity must remain exactly bound.
        return hashlib.sha256(encode({key:record[key] for key in
            ('token','request','source_sha256','status','started_at','ended_at','smoke')})).hexdigest()

    def original(self,record):
        self.accepted_binding(record)
        destination=self.destination(record['token']);raw=read(destination/'asset-record.json')
        result=self._load(destination,records.strict_json(raw.decode('utf-8')))
        require(record['request']['controller']==result.controller and record['request']['publication']==result.publication
                and record['source_sha256']=='sha256:'+result.migration.source_manifest_sha256)
        return result,hashlib.sha256(raw).hexdigest()

    def recovery(self,record,original,asset_hash):
        root=self.base/'recovery';namespace=root/record['token']
        if not root.exists() and not root.is_symlink():return original,None,None,None,[]
        directory(root);entries=list(root.iterdir())
        require(len(entries)<=host_release_state.MAX_ATTEMPTS)
        for path in entries:
            require(re.fullmatch(r'[a-f0-9]{64}',path.name));directory(path)
        if not namespace.exists() and not namespace.is_symlink():return original,None,None,None,[]
        directory(namespace);paths=list(namespace.iterdir())
        require(1<=len(paths)<=host_release_state.MAX_ATTEMPTS+1)
        head=namespace/'latest.json';head_stamp=stamp(head.lstat());head_raw=read(head)
        require(stamp(head.lstat())==head_stamp)
        pointer=records.strict_json(head_raw.decode('utf-8'))
        require(type(pointer) is dict and set(pointer)==HEAD_KEYS and type(pointer['schema_version']) is int
                and pointer['schema_version']==1 and pointer['artifact_kind']=='rollback-operational-head'
                and pointer['stage']==self.stage and pointer['accepted_token']==record['token']
                and type(pointer['latest_token']) is str and re.fullmatch(r'[a-f0-9]{64}',pointer['latest_token'])
                and type(pointer['record_sha256']) is str and re.fullmatch(r'[a-f0-9]{64}',pointer['record_sha256']))
        names={path.name for path in paths};chain=[];seen=set();token=pointer['latest_token'];expected_hash=pointer['record_sha256']
        while token is not None:
            require(type(token) is str and re.fullmatch(r'[a-f0-9]{64}',token) and token not in seen
                    and token!=record['token'] and len(seen)<host_release_state.MAX_ATTEMPTS)
            seen.add(token);destination=namespace/token;directory(destination)
            require({path.name for path in destination.iterdir()}=={'compose.json','recovery-record.json'})
            raw=read(destination/'recovery-record.json');meta=records.strict_json(raw.decode('utf-8'))
            require(hashlib.sha256(raw).hexdigest()==expected_hash)
            require(type(meta) is dict and set(meta)==RECOVERY_KEYS and type(meta['schema_version']) is int
                    and meta['schema_version']==1 and meta['artifact_kind']=='durable-rollback-assets'
                    and meta['stage']==self.stage and meta['accepted_token']==record['token']
                    and meta['attempt_token']==token and meta['accepted_binding_sha256']==self.accepted_binding(record)
                    and meta['accepted_asset_sha256']==asset_hash
                    and type(meta['pending_request_sha256']) is str and re.fullmatch(r'[a-f0-9]{64}',meta['pending_request_sha256']))
            spec=meta['prepared'];files=meta['files']
            require(type(spec) is dict and set(spec)=={'sha256','images','storage_generation'}
                    and encode(spec['images'])==encode(original.prepared.images)
                    and type(files) is dict and set(files)=={'compose.json'}
                    and type(files['compose.json']) is dict and set(files['compose.json'])=={'size','sha256'})
            compose=read(destination/'compose.json');binding=files['compose.json']
            require(type(binding['size']) is int and binding['size']==len(compose)
                    and binding['sha256']==spec['sha256']==hashlib.sha256(compose).hexdigest())
            prepared=host_compose.Prepared(self.stage,destination/'compose.json',spec['sha256'],copy.deepcopy(spec['images']),
                                          storage_generation=spec['storage_generation'])
            require(self.normalized(prepared)==self.normalized(original.prepared))
            chain.append((meta,prepared));token=meta['previous_token'];expected_hash=meta['previous_record_sha256']
            require((token is None and expected_hash is None) or
                    (token is not None and type(expected_hash) is str and re.fullmatch(r'[a-f0-9]{64}',expected_hash)))
        # Every entry must be reachable from the committed head. Interrupted or
        # unindexed directories cannot silently fall back to an older process.
        require(names==seen|{'latest.json'})
        current=original;generations=[original.prepared.storage_generation]
        for meta,prepared in reversed(chain):
            require(meta['previous_compose_sha256']==current.prepared.sha256
                    and all(prepared.storage_generation[:32]!=old[:32] for old in generations))
            generations.append(prepared.storage_generation);current=replace(original,prepared=prepared)
        return current,namespace,head_raw,head_stamp,generations

    def persist_rollback(self,previous,rebound,*,state,token):
        """Persist a fresh operational generation; never move accepted pointers.

        The parent qualifies native Driver.config and the replacement process.
        This callback only retains matching private data under the held ledger
        lock. Any incomplete/unindexed write leaves an explicit refusal state.
        """
        try:
            self.check()
            require(type(state) is host_release_state.ReleaseState and state.stage==self.stage
                    and state.owner_uid==ROOT_UID and state._read()==state.data)
            pending=state.read_pending();accepted=state.read_accepted()['current']
            require(pending is not None and pending['token']==token and accepted is not None
                    and type(previous) is root_executor.ReleaseAssets and type(rebound) is host_compose.Prepared)
            original,asset_hash=self.original(accepted)
            operational,namespace,head_raw,head_stamp,generations=self.recovery(accepted,original,asset_hash)
            require(previous==operational and encode(rebound.images)==encode(original.prepared.images)
                    and self.normalized(rebound)==self.normalized(operational.prepared)
                    and all(rebound.storage_generation[:32]!=old[:32] for old in
                            (generations or [original.prepared.storage_generation])))
            root=self.base/'recovery'
            if not root.exists():root.mkdir(mode=0o700)
            directory(root);namespace=root/accepted['token']
            if not namespace.exists():namespace.mkdir(mode=0o700)
            directory(namespace)
            destination=namespace/token
            require(not destination.exists() and not destination.is_symlink())
            destination.mkdir(mode=0o700);catalog={}
            self.write(destination,'compose.json',read(rebound.file),catalog)
            prior=None if head_raw is None else records.strict_json(head_raw.decode('utf-8'))['latest_token']
            meta={'schema_version':1,'artifact_kind':'durable-rollback-assets','stage':self.stage,
                  'accepted_token':accepted['token'],'attempt_token':token,
                  'accepted_binding_sha256':self.accepted_binding(accepted),'accepted_asset_sha256':asset_hash,
                  'pending_request_sha256':hashlib.sha256(encode(pending['request'])).hexdigest(),
                  'previous_token':prior,'previous_record_sha256':None if head_raw is None else
                      records.strict_json(head_raw.decode('utf-8'))['record_sha256'],
                  'previous_compose_sha256':operational.prepared.sha256,
                  'prepared':{'sha256':rebound.sha256,'images':copy.deepcopy(rebound.images),
                              'storage_generation':rebound.storage_generation},'files':catalog}
            raw=encode(meta);self.write(destination,'recovery-record.json',raw,{})
            fsync_directory(destination);fsync_directory(namespace);fsync_directory(root);fsync_directory(self.base)
            head=namespace/'latest.json';temporary='head-'+token+'.json'
            pointer={'schema_version':1,'artifact_kind':'rollback-operational-head','stage':self.stage,
                     'accepted_token':accepted['token'],'latest_token':token,'record_sha256':hashlib.sha256(raw).hexdigest()}
            self.write(namespace,temporary,encode(pointer),{})
            require(state.read_pending()==pending and state.read_accepted()['current']==accepted and state._read()==state.data)
            if head_raw is None:require(not head.exists() and not head.is_symlink())
            else:require(stamp(head.lstat())==head_stamp and read(head)==head_raw)
            os.replace(namespace/temporary,head);fsync_directory(namespace)
            require(state.read_pending()==pending and state.read_accepted()['current']==accepted and state._read()==state.data)
            result,_,_,_,_=self.recovery(accepted,original,asset_hash)
            return result
        except Exception:raise AssetError(ERROR) from None

    def load_release(self,stage,record):
        try:
            self.check();require(stage==self.stage and type(record) is dict and record.get('status')=='accepted')
            original,asset_hash=self.original(record)
            result,_,_,_,_=self.recovery(record,original,asset_hash)
            return result
        except Exception:
            raise AssetError(ERROR) from None
