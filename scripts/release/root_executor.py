#!/usr/bin/env python3
"""Root orchestration library and entry to the supervised fixed host runtime.

NativeAdapters and Operations are constructed ONLY by installed root runtime
code from protected fixed configuration, never requests. The parent runtime must
supervise the entire process tree for 45 minutes, constrain OS/sudo identity and
provide actual target audits, warm leases, offsite backups, product smoke and
atomic root-owned artifact persistence. Local fixture callbacks establish none
of those hosted facts. This library's concrete defaults acquire native candidates,
refresh stage admission, hold real host/stage locks, qualify Compose, apply the
immutable migration catalog and invoke original source seed implementations.

Current/previous assets AND the first accepted migration origin bundle must be
retained. Failed schema work is forward-only. Unknown initial, rollback, lease or
ledger durability state stays pending for explicit root reconciliation; there is
no broad removal, database reset, caller-selected operation or synthetic CLI ack.
"""
import copy
from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import re
import stat
import sys
import time

try:
    from . import deploy_admission, deploy_request, deployment_candidate, host_compose
    from . import host_migrate, host_release_state, host_seed, host_quiescence, migration_transport, publication_record
    from . import release_manifest as records
except ImportError:
    import deploy_admission, deploy_request, deployment_candidate, host_compose
    import host_migrate, host_release_state, host_seed, host_quiescence, migration_transport, publication_record
    import release_manifest as records

ERROR='Root deployment execution refused'
ROOT_UID=0


class ExecutorError(ValueError):
    def __init__(self,*,reconciliation_required=False):
        super().__init__(ERROR)
        self.reconciliation_required=reconciliation_required


def require(value):
    if not value:raise ExecutorError()


@dataclass(frozen=True)
class NativeAdapters:
    """Protected root native clients, not caller-supplied verification results.

    read(path), read_graphql(fixed_query), download(native_reference, ...),
    verify(image=..., controller=...), parse_workflow(raw) use the existing
    bounded native adapter contracts. The installed runtime binds credentials.
    """
    read:object
    read_graphql:object
    download:object
    verify:object
    parse_workflow:object


@dataclass(frozen=True)
class ReleaseAssets:
    """Requalifiable root-owned release data, never a stored approval boolean.

    prepared points at protected rendered Compose; migration is the current
    transport; origin is the retained first accepted transport that proves the
    original applied SQL commits. manifest/public/receipt are exact native build
    data; controller/publication bind the corresponding durable release record.
    """
    prepared:host_compose.Prepared
    migration:migration_transport.Verified
    controller:dict
    publication:dict
    manifest:dict
    public:dict
    receipt:dict
    origin:object=None


@dataclass(frozen=True)
class Operations:
    """Concrete root runtime operation contracts (fixtures do not prove them).

    target_audit(stage, current, previous) is READ-ONLY and returns exactly
    stage, database_state (fresh/existing), database_image, database_container_id
    (None for fresh; exact CID for existing), proof_sha256, observed_at. Fresh
    requires an actual owned empty-data/no-container audit, not absence of an
    accepted release. Runtime must retain the underlying private audit evidence.

    warm(stage, current, previous) returns a synchronous context that holds real
    warm/drain protection across recreation and rollback. __enter__ must clean
    partial acquisitions on failure; __exit__ must verify all leases are released.

    backup(stage, *, phase, current, previous, state, token) runs a NEW encrypted offsite
    backup. phase is before-bootstrap or before-sql; return the actual existing
    backup_recovery.backup result, including uploaded=True and its native upload
    receipt. Snapshot time must be at/after this attempt's root start time.

    product_smoke(assets, *, phase) executes real product flows for current or
    rollback. Return exactly stage, publication, source_sha256 (sha256: prefix),
    result='passed', observed_at. Retain detailed private flow evidence separately.

    load_release(stage, accepted_record) reads protected retained ReleaseAssets.
    persist(assets, *, token, proofs, state) atomically/fsyncs copies and returns
    durable ReleaseAssets OUTSIDE temporary work, preserving migration AND origin.
    state is for read-only binding; callbacks must never begin/accept/fail it.
    Persist immutable preparation before accept; only the ledger defines accepted.

    seed_counts(stage, fixed_query) supplies host_seed's six numeric conditions.
    Compose/seed factories default to the real existing drivers; overrides are
    trusted test/integration boundaries, never request fields.

    clock() returns trusted root UTC Unix seconds as an INT. All local observations
    share this clock: fresh smoke must fall between attempt start and accept;
    backup ISO timestamps must be aware and no later than root now plus 300s.
    Lease release precedes accept; a later revocation reacquires recovery warmth.
    The runtime's 45-minute process-tree supervisor remains independently required.
    """
    target_audit:object
    warm:object
    backup:object
    product_smoke:object
    load_release:object
    persist:object
    seed_counts:object
    persist_rollback:object
    compose_factory:object=host_compose.Driver
    seed_factory:object=None
    clock:object=lambda:int(time.time())
    guard_factory:object=host_quiescence.Guard


def private_work(work):
    require(isinstance(work,Path) and work.is_absolute() and work.resolve(strict=True)==work)
    info=work.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid==ROOT_UID and stat.S_IMODE(info.st_mode)==0o700)


def clock(operations):
    value=operations.clock()
    require(type(value) is int and 0<=value<=9007199254740991)
    return value


def admission(stage,request,native,*,source_sha256):
    result=deploy_admission.validate(stage,request['controller'],request['deployment'],
        read=native.read,read_graphql=native.read_graphql,parse_workflow=native.parse_workflow)
    require(type(result) is dict and result.get('artifact_kind')=='native-deployment-admission'
            and result.get('stage')==stage and result.get('controller')==request['controller']
            and result.get('deployment')==request['deployment'] and result.get('admitted') is True
            and result.get('deployable') is False
            and type(result.get('source_approval')) is dict
            and result['source_approval'].get('source_manifest_sha256')==source_sha256)
    return result


def validate_assets(assets,*,stage,driver,record=None,expected=None):
    require(type(assets) is ReleaseAssets and type(assets.prepared) is host_compose.Prepared
            and assets.prepared.stage==stage and assets.prepared.host_approved is False
            and type(assets.migration) is migration_transport.Verified and assets.migration.deployable is False)
    records.validate_controller(assets.controller)
    require(assets.migration.controller==assets.controller
            and records.canonical_sha256(assets.manifest)==assets.migration.source_manifest_sha256)
    migration_transport.application_inputs(assets.migration)
    publication_record.qualify(receipt=assets.receipt,manifest=assets.manifest,public=assets.public,controller=assets.controller)
    pointers=host_compose.image_pointers(stage=stage,receipt=assets.receipt,manifest=assets.manifest,
        public=assets.public,controller=assets.controller)
    require(assets.prepared.images==pointers)
    driver.config(assets.prepared)
    if record is not None:
        require(record['status']=='accepted' and record['request']['controller']==assets.controller
                and record['request']['publication']==assets.publication
                and record['source_sha256']=='sha256:'+assets.migration.source_manifest_sha256)
    if expected is not None:
        require(assets.controller==expected.controller and assets.publication==expected.publication
                and assets.manifest==expected.manifest and assets.public==expected.public and assets.receipt==expected.receipt
                and assets.prepared.images==expected.prepared.images
                and assets.prepared.storage_generation==expected.prepared.storage_generation
                and assets.migration.source_manifest_sha256==expected.migration.source_manifest_sha256
                and type(assets.origin) is migration_transport.Verified
                and assets.origin.controller==expected.origin.controller
                and assets.origin.source_manifest_sha256==expected.origin.source_manifest_sha256)
        migration_transport.application_inputs(assets.origin)
    return assets


def origin_bundle(assets,state,current,publication=None):
    # A failed initial origin has no acceptance authority. Only the fixed,
    # separately root-reviewed QA packet can bridge it to one real successor.
    try:
        from . import host_assets
    except ImportError:
        import host_assets
    if assets is None:
        recovery=host_assets.Store(state.stage).initial_origin(state,current,publication)
        return recovery[0] if recovery is not None else None
    origin=assets.origin if assets.origin is not None else assets.migration
    require(type(origin) is migration_transport.Verified and origin.deployable is False)
    migration_transport.application_inputs(origin)
    accepted=any(record['status']=='accepted' and record['request']['controller']==origin.controller
                and record['source_sha256']=='sha256:'+origin.source_manifest_sha256
                for record in state.data['attempts'])
    if not accepted:
        recovery=host_assets.Store(state.stage).initial_origin(state,current,publication,expected_prior=origin)
        require(recovery is not None)
    old={(unit['owner'],unit['path']):unit['sha256'] for unit in origin.plan['units']}
    now={(unit['owner'],unit['path']):unit['sha256'] for unit in current.plan['units']}
    require(len(old)==len(now)==40 and old==now)
    return origin


def target(value,*,stage,current,previous,now):
    keys={'stage','database_state','database_image','database_container_id','proof_sha256','observed_at'}
    require(type(value) is dict and set(value)==keys and value['stage']==stage
            and value['database_image']==current.prepared.images['IMAGE_SUPABASE_DB']
            and type(value['proof_sha256']) is str and re.fullmatch(r'[a-f0-9]{64}',value['proof_sha256'])
            and type(value['observed_at']) is int and now-300<=value['observed_at']<=now+300)
    if value['database_state']=='fresh':require(previous is None and value['database_container_id'] is None)
    else:require(value['database_state']=='existing' and type(value['database_container_id']) is str
                 and re.fullmatch(r'[a-f0-9]{64}',value['database_container_id']))
    return value


def backup(value,*,stage,started_at,now):
    require(type(value) is dict and value.get('status')=='backup-created'
            and type(value.get('version')) is int and value['version']==1 and value.get('environment')==stage
            and value.get('uploaded') is True and type(value.get('bytes')) is int and 0<value['bytes']<=32*1024**3
            and type(value.get('sha256')) is str and re.fullmatch(r'[a-f0-9]{64}',value['sha256']))
    times=[]
    for key in ('snapshot_at','completed_at'):
        require(type(value.get(key)) is str)
        parsed=datetime.fromisoformat(value[key]);require(parsed.tzinfo is not None);times.append(parsed.timestamp())
    require(started_at<=times[0]<=times[1]<=now+300)
    uploaded=value.get('upload')
    require(type(uploaded) is dict and set(uploaded)=={'file_id','name','sha1'}
            and type(uploaded['file_id']) is str and 0<len(uploaded['file_id'])<=200
            and type(uploaded['name']) is str and uploaded['name'].startswith(stage+'/')
            and uploaded['name'].endswith('/backup.tar.age') and len(uploaded['name'])<=1024
            and type(uploaded['sha1']) is str and re.fullmatch(r'[a-f0-9]{40}',uploaded['sha1']))
    return copy.deepcopy(value)


def smoke(value,assets,*,stage,started_at,now):
    keys={'stage','publication','source_sha256','result','observed_at'}
    require(type(value) is dict and set(value)==keys and value['stage']==stage
            and value['publication']==assets.publication
            and value['source_sha256']=='sha256:'+assets.migration.source_manifest_sha256
            and value['result']=='passed' and type(value['observed_at']) is int
            and started_at<=value['observed_at']<=now)
    return copy.deepcopy(value)


def rollback(driver,previous,current,operations,*,stage,started_at,state,token):
    result=driver.rollback(previous.prepared,current=current.prepared)
    require(type(result) is dict and result.get('stage')==stage and result.get('services_ready')==14
            and result.get('database_rolled_back') is False)
    rebound=result.get('prepared')
    require(type(rebound) is host_compose.Prepared and rebound.stage==stage
            and rebound.images==previous.prepared.images and rebound.host_approved is False)
    driver.config(rebound)
    recovered=operations.persist_rollback(previous,rebound,state=state,token=token)
    require(type(recovered) is ReleaseAssets and recovered.controller==previous.controller
            and recovered.publication==previous.publication and recovered.manifest==previous.manifest
            and recovered.public==previous.public and recovered.receipt==previous.receipt
            and recovered.prepared.storage_generation==rebound.storage_generation
            and recovered.prepared.images==rebound.images and recovered.prepared.sha256==rebound.sha256
            and recovered.migration.source_manifest_sha256==previous.migration.source_manifest_sha256)
    driver.config(recovered.prepared)
    smoke(operations.product_smoke(recovered,phase='rollback'),recovered,stage=stage,
          started_at=started_at,now=clock(operations))


def execute_locked(*,stage,request,work,state,native,operations,current,driver):
    require(state._read()==state.data and state.read_pending() is None)
    if stage=='prod':state.read_staging_for_promotion(publication=request['publication'],
        source_sha256='sha256:'+current.migration.source_manifest_sha256,controller=request['controller'])
    previous_record=state.read_accepted()['current']
    previous=None
    if previous_record is not None:
        previous=validate_assets(operations.load_release(stage,previous_record),stage=stage,driver=driver,record=previous_record)
    prior=origin_bundle(previous,state,current.migration,current.publication)
    current=ReleaseAssets(**{**current.__dict__,'origin':prior or current.migration})
    audit=target(operations.target_audit(stage,current,previous),stage=stage,current=current,
                 previous=previous,now=clock(operations))
    # Expensive artifact/config checks precede this fresh write-boundary read.
    proofs={'admission':[admission(stage,request,native,source_sha256=current.migration.source_manifest_sha256)],'target':copy.deepcopy(audit),'backups':[]}
    started_at=clock(operations)
    token=state.begin(request,source_sha256='sha256:'+current.migration.source_manifest_sha256,started_at=started_at)
    context=None;guard=None;entered=False;released=False;mutated=False;warm_uncertain=False;phase='runtime'
    try:
        guard=operations.guard_factory(stage,state)
        require(getattr(guard,'stage',None)==stage and all(callable(getattr(guard,key,None))
                    for key in ('assert_sql_safe','resume')) and callable(getattr(driver,'bind_guard',None)))
        driver.bind_guard(guard)
        proofs['admission'].append(admission(stage,request,native,source_sha256=current.migration.source_manifest_sha256))
        context=operations.warm(stage,current,previous)
        require(hasattr(context,'__enter__') and hasattr(context,'__exit__'))
        context.__enter__();entered=True
        if audit['database_state']=='existing':
            proofs['admission'].append(admission(stage,request,native,source_sha256=current.migration.source_manifest_sha256))
            proofs['backups'].append(backup(operations.backup(stage,phase='before-bootstrap',current=current,previous=previous,state=state,token=token),
                stage=stage,started_at=started_at,now=clock(operations)))
        proofs['admission'].append(admission(stage,request,native,source_sha256=current.migration.source_manifest_sha256));mutated=True
        result=driver.bootstrap(current.prepared,previous=previous.prepared if previous else None)
        require(type(result) is dict and result.get('stage')==stage and result.get('services_ready')==2)
        proofs['admission'].append(admission(stage,request,native,source_sha256=current.migration.source_manifest_sha256))
        proofs['backups'].append(backup(operations.backup(stage,phase='before-sql',current=current,previous=previous,state=state,token=token),
            stage=stage,started_at=started_at,now=clock(operations)))
        phase='migration';proofs['admission'].append(admission(stage,request,native,source_sha256=current.migration.source_manifest_sha256))
        proofs['storage_sql']=guard.assert_sql_safe(current.migration,prior_verified=prior)
        require(type(proofs['storage_sql']) is dict and proofs['storage_sql'].get('stage')==stage)
        migration=host_migrate.apply(current.migration,state,prior_verified=prior)
        require(type(migration) is dict and migration.get('stage')==stage
                and migration.get('controller')==request['controller']
                and migration.get('source_manifest_sha256')==current.migration.source_manifest_sha256
                and migration.get('migration_count')==40 and migration.get('migration_ledger_verified') is True
                and migration.get('deployable') is False)
        proofs['migration']=migration
        proofs['admission'].append(admission(stage,request,native,source_sha256=current.migration.source_manifest_sha256))
        seeder=operations.seed_factory(driver) if operations.seed_factory else host_seed.Driver(driver,verify_counts=operations.seed_counts)
        seeded=seeder.run(current.prepared,current.migration,work=work)
        require(type(seeded) is dict and seeded.get('stage')==stage and seeded.get('seeds_applied') is True)
        proofs['seeds']=seeded
        phase='runtime';proofs['admission'].append(admission(stage,request,native,source_sha256=current.migration.source_manifest_sha256))
        result=driver.start(current.prepared,previous=previous.prepared if previous else None)
        require(type(result) is dict and result.get('stage')==stage and result.get('services_ready')==14)
        phase='smoke'
        observed=smoke(operations.product_smoke(current,phase='current'),current,stage=stage,
                       started_at=started_at,now=clock(operations));proofs['smoke']=observed
        phase='runtime'
        proofs['admission'].append(admission(stage,request,native,source_sha256=current.migration.source_manifest_sha256))
        persisted=validate_assets(operations.persist(current,token=token,proofs=copy.deepcopy(proofs),state=state),
                                   stage=stage,driver=driver,expected=current)
        require(persisted.prepared.file!=current.prepared.file and work not in persisted.prepared.file.parents
                and work!=persisted.migration.directory and work not in persisted.migration.directory.parents
                and work!=persisted.origin.directory and work not in persisted.origin.directory.parents)
        # Recheck after persistence and release while still holding the host lock.
        proofs['admission'].append(admission(stage,request,native,source_sha256=current.migration.source_manifest_sha256))
        try:guard.resume();context.__exit__(None,None,None);released=True
        except Exception:warm_uncertain=True;raise
        proofs['admission'].append(admission(stage,request,native,source_sha256=current.migration.source_manifest_sha256))
        state.accept(token,smoke=observed,accepted_at=clock(operations))
        return {'artifact_kind':'root-deployment-result','stage':stage,'token':token,
                'controller':copy.deepcopy(request['controller']),'publication':copy.deepcopy(request['publication']),
                'source_manifest_sha256':current.migration.source_manifest_sha256,'deployment_accepted':True}
    except Exception:
        # Uncertain ledger replacement/fsync must never trigger a blind rollback
        # of a release that may already be atomically accepted on disk.
        try:pending=state.read_pending()
        except Exception:raise ExecutorError(reconciliation_required=True) from None
        if pending is None or pending['token']!=token:
            raise ExecutorError(reconciliation_required=True) from None
        restored=not mutated
        if mutated and previous is not None:
            try:
                if released:
                    # A revocation after successful lease release still requires
                    # fresh recovery warmth before touching accepted app images.
                    context=operations.warm(stage,current,previous)
                    require(hasattr(context,'__enter__') and hasattr(context,'__exit__'))
                    context.__enter__();entered=True;released=False
                rollback(driver,previous,current,operations,stage=stage,started_at=started_at,state=state,token=token);restored=True
            except Exception:restored=False
        safe_release=not entered and context is None
        if entered and not released:
            try:
                if guard is not None:guard.resume()
                context.__exit__(None,None,None);safe_release=True
            except Exception:warm_uncertain=True;safe_release=False
        elif released:safe_release=True
        # Initial mutations, failed rollback or uncertain lease cleanup are never
        # declared failed/reconciled by guess. Accepted pointers remain untouched.
        reconciled=restored and safe_release and not warm_uncertain
        if reconciled:
            try:state.fail(token,reason=phase,failed_at=clock(operations))
            except Exception:reconciled=False
        raise ExecutorError(reconciliation_required=not reconciled) from None


def run(raw,*,stage,work,state_directory,native,operations):
    """Only installed root integration code supplies settings and adapters."""
    try:
        require(os.geteuid()==ROOT_UID and type(native) is NativeAdapters and type(operations) is Operations)
        require(all(callable(value) for value in native.__dict__.values())
                and all(callable(getattr(operations,key)) for key in (
                    'target_audit','warm','backup','product_smoke','load_release','persist','persist_rollback','seed_counts','compose_factory','clock','guard_factory'))
                and (operations.seed_factory is None or callable(operations.seed_factory)))
        private_work(work)
        request=deploy_request.parse(raw,stage=stage)
        request={key:value for key,value in request.items() if key!='stage'}
        candidate=deployment_candidate.acquire(raw,stage=stage,work=work,read=native.read,download=native.download,verify=native.verify)
        require(type(candidate) is dict and candidate.get('artifact_kind')=='verified-deployment-candidate'
                and candidate.get('stage')==stage and candidate.get('controller')==request['controller']
                and candidate.get('publication')==request['publication'] and candidate.get('deployable') is False)
        manifest_bytes=deployment_candidate.private_bytes(work/'admission/source-manifest.json')
        manifest=records.strict_json(manifest_bytes.decode('utf-8'))
        public=records.read_json(work/'admission/public-inputs.json')
        receipt=records.read_json(work/'publication/published-receipt.json')
        qualified=publication_record.qualify(receipt=receipt,manifest=manifest,public=public,controller=request['controller'])
        require(qualified['source_manifest_sha256']==candidate['source_manifest_sha256']
                and qualified['receipt_sha256']==candidate['receipt_sha256'])
        verified=deployment_candidate.acquire_migration(controller=request['controller'],manifest_bytes=manifest_bytes,
                work=work,read=native.read,download=native.download)
        require(type(verified) is migration_transport.Verified and verified.controller==request['controller']
                and verified.source_manifest_sha256==candidate['source_manifest_sha256'])
        migration_transport.application_inputs(verified)
        with host_release_state.ReleaseState(state_directory,stage=stage,owner_uid=ROOT_UID) as state:
            driver=operations.compose_factory(stage)
            prepared=driver.prepare(receipt=receipt,manifest=manifest,public=public,controller=request['controller'],work=work)
            current=validate_assets(ReleaseAssets(prepared,verified,request['controller'],request['publication'],manifest,public,receipt),
                                    stage=stage,driver=driver)
            return execute_locked(stage=stage,request=request,work=work,state=state,native=native,
                                  operations=operations,current=current,driver=driver)
    except ExecutorError:raise
    except Exception:raise ExecutorError() from None


def main():
    """The supervisor validates sudo/OS identity and the entire worker cgroup."""
    try:
        from . import host_supervisor
    except ImportError:
        import host_supervisor
    return host_supervisor.main()


if __name__=='__main__':sys.exit(main())
