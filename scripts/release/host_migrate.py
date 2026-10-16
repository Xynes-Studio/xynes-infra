#!/usr/bin/env python3
"""Apply only verified catalog SQL while holding a root-owned pending release.

The coordinator supplies migration_transport.Verified after native artifact/trust
qualification and holds host locks, warm leases and backup/admission checks. No
CLI, caller SQL, caller paths, approval booleans, hosted reset or schema rollback
exists here. Failure leaves the pending/accepted release records unchanged; root
operations must reconcile app rollback and forward-only database progress.

For unchanged SQL with advancing source commits, prior_verified may be a retained
immutable origin bundle from ANY accepted attempt in this stage's durable state.
Its controller and canonical manifest hash must match that accepted attempt; all
40 owner/path/hash bindings must match the current baseline. Retain the first
accepted origin bundle alongside current/previous releases. Unknown origins fail
closed rather than rewriting provenance or adopting caller-provided ledger rows.
"""
import copy
import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import time

try:
    from . import host_release_state, migration_sql, migration_transport, pg_container_bridge
    from . import release_manifest as records
except ImportError:
    import host_release_state, migration_sql, migration_transport, pg_container_bridge
    import release_manifest as records

ERROR='Hosted migration refused'
ROOT_UID=0
TIMEOUT=900
MAX_SQL=16*1024*1024
MAX_OUTPUT=65536
BASE=pg_container_bridge.BASE.copy()
PG_FLAGS=['-X','-qAt','--no-password','--set=ON_ERROR_STOP=1']
PG_ENV=('PGHOST=127.0.0.1','PGPORT=5432','PGDATABASE=postgres','PGUSER=postgres',
        'PGSERVICE=','PGSERVICEFILE=','PGHOSTADDR=','PGOPTIONS=',
        'PGPASSFILE=/nonexistent','PGSSLMODE=disable','PGCONNECT_TIMEOUT=15')
LEDGER_QUERY="""SELECT json_build_object(
 'database',current_database(), 'user',current_user, 'in_recovery',pg_is_in_recovery(),
 'migration_count',(SELECT count(*) FROM xynes_release.migrations),
 'bootstrap',(SELECT coalesce(json_agg(json_build_object('environment',environment,
   'baseline_sha256',baseline_sha256,'completed',completed)), '[]'::json) FROM xynes_release.bootstrap),
 'migrations',(SELECT coalesce(json_agg(json_build_object('owner',owner,'path',path,'sha256',sha256,
   'source_commit',source_commit) ORDER BY owner,path), '[]'::json) FROM xynes_release.migrations));"""


class MigrationError(ValueError):
    """Refusals contain no SQL, provider information or subprocess diagnostics."""


def require(value):
    if not value:raise MigrationError(ERROR)


def exact(value,keys):
    require(type(value) is dict and set(value)==set(keys))


def baseline(units):
    bindings=[{key:unit[key] for key in ('owner','path','sha256')} for unit in units]
    return hashlib.sha256(json.dumps(bindings,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def bound_state(state,verified):
    require(os.geteuid()==ROOT_UID and type(state) is host_release_state.ReleaseState
            and state.owner_uid==ROOT_UID and type(verified) is migration_transport.Verified
            and verified.deployable is False)
    pending=state.read_pending()
    # Compare the held in-memory view with the protected on-disk ledger too;
    # mutable object attributes cannot supply an invented accepted history.
    require(state._read()==state.data and pending is not None
            and pending['request']['controller']==verified.controller
            and pending['source_sha256']=='sha256:'+verified.source_manifest_sha256)
    return pending


def origins(prior_verified,state,units):
    if prior_verified is None:return {}
    require(type(prior_verified) is migration_transport.Verified and prior_verified.deployable is False)
    accepted=[record for record in state.data['attempts'] if record['status']=='accepted'
              and record['request']['controller']==prior_verified.controller
              and record['source_sha256']=='sha256:'+prior_verified.source_manifest_sha256]
    require(accepted)
    prior_units,_=migration_transport.application_inputs(prior_verified)
    require(len(prior_units)==40)
    old={(unit['owner'],unit['path']):unit for unit in prior_units}
    current={(unit['owner'],unit['path']):unit for unit in units}
    require(len(old)==len(current)==40 and set(old)==set(current)
            and all(old[key]['sha256']==current[key]['sha256'] for key in current))
    return old


def _execute(container,password,*,sql=None):
    """Private implementation: stdin is rendered SQL; no arbitrary command API."""
    process=None
    try:
        require(type(container) is str and re.fullmatch(r'[a-f0-9]{64}',container)
                and type(password) is str and 0<len(password)<=256
                and not any(char in password for char in '\x00\r\n')
                and type(TIMEOUT) in (int,float) and 0<TIMEOUT<=900)
        if sql is not None:require(type(sql) is bytes and 0<len(sql)<=MAX_SQL)
        argv=BASE+['exec','-i','--env','PGPASSWORD']
        for value in PG_ENV:argv+=['--env',value]
        argv += [container,'psql',*PG_FLAGS]
        if sql is None:argv+=['--command',LEDGER_QUERY]
        process=subprocess.Popen(argv,stdin=subprocess.PIPE if sql is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,env={**pg_container_bridge.CLEAN_ENV,'PGPASSWORD':password},
            cwd='/',start_new_session=True)
        deadline=time.monotonic()+TIMEOUT;output=bytearray();errors=0;offset=0
        selector=selectors.DefaultSelector()
        for stream in (process.stdout,process.stderr):os.set_blocking(stream.fileno(),False)
        selector.register(process.stdout,selectors.EVENT_READ,'out')
        selector.register(process.stderr,selectors.EVENT_READ,'err')
        if sql is not None:
            os.set_blocking(process.stdin.fileno(),False)
            selector.register(process.stdin,selectors.EVENT_WRITE,'in')
        with selector:
            while selector.get_map():
                require(time.monotonic()<deadline)
                for key,_ in selector.select(min(1,max(0,deadline-time.monotonic()))):
                    if key.data=='in':
                        count=os.write(key.fileobj.fileno(),memoryview(sql)[offset:offset+65536])
                        require(count>0);offset+=count
                        if offset==len(sql):selector.unregister(key.fileobj);process.stdin.close()
                    else:
                        data=os.read(key.fileobj.fileno(),65536)
                        if not data:selector.unregister(key.fileobj);continue
                        if key.data=='out':output.extend(data);require(len(output)<=MAX_OUTPUT)
                        else:errors+=len(data);require(errors<=MAX_OUTPUT)
            require(process.wait(timeout=max(.001,deadline-time.monotonic()))==0
                    and (sql is None or offset==len(sql)))
        return bytes(output)
    except Exception:
        raise MigrationError(ERROR) from None
    finally:
        if process:
            if process.poll() is None:os.killpg(process.pid,signal.SIGKILL);process.wait()
            for stream in (process.stdin,process.stdout,process.stderr):
                if stream and not stream.closed:stream.close()


def ledger(raw,*,stage,units,prior):
    require(type(raw) is bytes and 0<len(raw)<=MAX_OUTPUT)
    value=records.strict_json(raw.decode('utf-8'))
    exact(value,{'database','user','in_recovery','migration_count','bootstrap','migrations'})
    require(value['database']=='postgres' and value['user']=='postgres' and value['in_recovery'] is False
            and type(value['migration_count']) is int and value['migration_count']==40
            and type(value['bootstrap']) is list and len(value['bootstrap'])==1
            and type(value['migrations']) is list and len(value['migrations'])==40)
    bootstrap=value['bootstrap'][0]
    exact(bootstrap,{'environment','baseline_sha256','completed'})
    require(bootstrap['environment']==stage and bootstrap['completed'] is True
            and bootstrap['baseline_sha256']==baseline(units))
    expected={(unit['owner'],unit['path']):unit for unit in units};seen=set();rows=[]
    require(len(expected)==40)
    for row in value['migrations']:
        exact(row,{'owner','path','sha256','source_commit'})
        require(all(type(item) is str for item in row.values()))
        key=(row['owner'],row['path']);require(key in expected and key not in seen);seen.add(key)
        unit=expected[key];allowed={unit['source_commit']}
        if key in prior:allowed.add(prior[key]['source_commit'])
        require(row['sha256']==unit['sha256'] and row['source_commit'] in allowed)
        rows.append(copy.deepcopy(row))
    require(seen==set(expected))
    return sorted(rows,key=lambda unit:(unit['owner'],unit['path']))


def verify_existing(verified,state,*,prior_verified):
    """Read-only qualification of an unchanged, independently accepted catalog.

    Never bootstrap or repair an unknown ledger. The coordinator can use this
    native readback before maintenance as evidence that no schema SQL is needed.
    """
    require(type(prior_verified) is migration_transport.Verified)
    return apply(verified,state,prior_verified=prior_verified)


def apply(verified,state,*,prior_verified=None):
    """Bootstrap the immutable catalog, or read back an accepted unchanged one.

    Native release/stage authorization, independent approvals, backup, SQL seed
    runners and product smoke remain coordinator responsibilities. This function
    never begins/accepts/fails a release or changes any prior accepted pointer.
    """
    try:
        pending=bound_state(state,verified);stage=state.stage
        units,payloads=migration_transport.application_inputs(verified)
        require(len(units)==len(payloads)==40)
        prior=origins(prior_verified,state,units)
        environment=pg_container_bridge.stage_environment(stage)
        require(type(environment) is dict and environment.get('XYNES_ENV')==stage)
        password=environment.get('POSTGRES_PASSWORD')
        container=pg_container_bridge.inspected_container(stage)
        require(bound_state(state,verified)==pending)
        if prior_verified is None:
            sql=migration_sql.render(stage,'postgres',units,payloads).encode('utf-8')
            require(0<len(sql)<=MAX_SQL)
            _execute(container,password,sql=sql)
            require(pg_container_bridge.inspected_container(stage)==container
                    and bound_state(state,verified)==pending)
        rows=ledger(_execute(container,password),stage=stage,units=units,prior=prior)
        require(pg_container_bridge.inspected_container(stage)==container
                and bound_state(state,verified)==pending)
        return {'schema_version':1,'artifact_kind':'host-migration-application-result','stage':stage,
                'controller':copy.deepcopy(verified.controller),
                'source_manifest_sha256':verified.source_manifest_sha256,
                'baseline_sha256':baseline(units),'migration_count':40,'source_origins':rows,
                'migration_ledger_verified':True,'schema_sql_applied':prior_verified is None,
                'database_container_id':container,'deployable':False}
    except Exception:
        raise MigrationError(ERROR) from None
