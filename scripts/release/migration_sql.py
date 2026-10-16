#!/usr/bin/env python3
"""Render a verified fresh-bootstrap batch; no connections, CLI or authorization."""
import hashlib
import json
import re
try:
    from . import migration_inventory
except ImportError:
    import migration_inventory
LOCK_KEY=67930274466224818
MAX_SQL=1024*1024
MAX_BATCH_SQL=16*1024*1024

def require(value):
    if not value:raise ValueError('Migration SQL refused: incomplete or unsafe binding')

def render(environment, database, units, payloads):
    """Caller must first validate immutable migration_plan/source admission.

    Only this exact baseline may resume/replay. Existing unrelated app schemas
    refuse, and a new baseline on an initialized target requires adoption review.
    A returned SQL string is neither freshness evidence nor permission to apply.
    """
    require(environment in ('qa','staging','prod') and isinstance(database,str)
            and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,62}',database))
    require(isinstance(units,list) and 0<len(units)<=512 and isinstance(payloads,dict))
    selected=set();bindings=[];bodies=[];total_bytes=0
    for unit in units:
        require(isinstance(unit,dict) and set(unit)=={'owner','path','sha256','source_commit'})
        require(unit['owner'] in migration_inventory.OWNERS)
        migration_inventory.safe_path(unit['path'])
        require(unit['path'].endswith('.sql') and isinstance(unit['sha256'],str)
                and re.fullmatch(r'[a-f0-9]{64}',unit['sha256']) and isinstance(unit['source_commit'],str)
                and re.fullmatch(r'[a-f0-9]{40}',unit['source_commit']))
        key=(unit['owner'],unit['path']);require(key not in selected);selected.add(key)
        data=payloads.get(key);require(isinstance(data,bytes) and 0<len(data)<=MAX_SQL
                    and hashlib.sha256(data).hexdigest()==unit['sha256'] and b'\0' not in data)
        total_bytes+=len(data);require(total_bytes<=MAX_BATCH_SQL)
        body=data.decode('utf8')
        # Known reviewed catalog only: psql commands and explicit transaction
        # boundaries are refused; this is not a parser for arbitrary SQL.
        require(not re.search(r'^\s*(?:\\|(?:BEGIN|COMMIT|ROLLBACK|END)\s*;|START\s+TRANSACTION|CREATE\s+INDEX\s+CONCURRENTLY)',body,re.I|re.M))
        bindings.append({k:unit[k] for k in ('owner','path','sha256')});bodies.append(body)
    require(selected==set(payloads))
    fingerprint=hashlib.sha256(json.dumps(bindings,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    statements=[f"""SET lock_timeout = '5s';
SET statement_timeout = '120s';
SELECT pg_advisory_lock({LOCK_KEY});
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';
SET LOCAL search_path = public, extensions;
DO $apex_init$
BEGIN
  IF current_database() <> '{database}' OR current_user <> 'postgres' OR pg_is_in_recovery() THEN
    RAISE EXCEPTION 'migration database identity refused';
  END IF;
  IF to_regprocedure('gen_random_uuid()') IS NULL THEN
    RAISE EXCEPTION 'migration UUID prerequisite unavailable';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'xynes_release') THEN
    IF to_regclass('xynes_release.bootstrap') IS NULL OR to_regclass('xynes_release.migrations') IS NULL THEN
      RAISE EXCEPTION 'migration ledger adoption required';
    END IF;
    IF (SELECT count(*) FROM xynes_release.bootstrap) <> 1 OR NOT EXISTS (
      SELECT 1 FROM xynes_release.bootstrap WHERE environment = '{environment}' AND baseline_sha256 = '{fingerprint}'
    ) THEN
      RAISE EXCEPTION 'migration environment or baseline mismatch';
    END IF;
    IF EXISTS (SELECT 1 FROM xynes_release.migrations m WHERE NOT EXISTS (
      SELECT 1 FROM jsonb_to_recordset('{json.dumps(bindings,separators=(',',':'))}'::jsonb)
        AS b(owner text,path text,sha256 text)
      WHERE b.owner=m.owner AND b.path=m.path AND b.sha256=m.sha256
    )) THEN
      RAISE EXCEPTION 'migration ledger content mismatch';
    END IF;
  ELSE
    IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname IN
      ('identity','platform','authz','cms','docs','telemetry','cms_migration_backups','comments','drizzle')) OR EXISTS (
      SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname='public' AND c.relname IN ('roles','permissions','role_permissions','user_roles')
    ) THEN
      RAISE EXCEPTION 'nonempty application target requires adoption review';
    END IF;
    CREATE SCHEMA xynes_release;
    CREATE TABLE xynes_release.bootstrap (
      environment text PRIMARY KEY, baseline_sha256 text NOT NULL, completed boolean NOT NULL DEFAULT false
    );
    CREATE TABLE xynes_release.migrations (
      owner text NOT NULL, path text NOT NULL, sha256 text NOT NULL, source_commit text NOT NULL,
      applied_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(owner,path)
    );
    INSERT INTO xynes_release.bootstrap VALUES ('{environment}','{fingerprint}',false);
  END IF;
END $apex_init$;
COMMIT;"""]
    for unit,body in zip(units,bodies):
        tag='$apex_source_'+unit['sha256']+'$';require(tag not in body)
        statements.append(f"""BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';
SET LOCAL search_path = public, extensions;
DO $apex_unit$
DECLARE prior_hash text;
BEGIN
  SELECT sha256 INTO prior_hash FROM xynes_release.migrations
    WHERE owner='{unit['owner']}' AND path='{unit['path']}';
  IF FOUND THEN
    IF prior_hash <> '{unit['sha256']}' THEN RAISE EXCEPTION 'migration hash mismatch'; END IF;
  ELSE
    EXECUTE {tag}{body}{tag};
    INSERT INTO xynes_release.migrations(owner,path,sha256,source_commit)
      VALUES ('{unit['owner']}','{unit['path']}','{unit['sha256']}','{unit['source_commit']}');
  END IF;
END $apex_unit$;
COMMIT;""")
    statements.append(f"""BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';
DO $apex_complete$
BEGIN
  IF (SELECT count(*) FROM xynes_release.migrations) <> {len(units)} OR EXISTS (
    SELECT 1 FROM jsonb_to_recordset('{json.dumps(bindings,separators=(',',':'))}'::jsonb)
      AS b(owner text,path text,sha256 text)
    WHERE NOT EXISTS (SELECT 1 FROM xynes_release.migrations m
      WHERE m.owner=b.owner AND m.path=b.path AND m.sha256=b.sha256)
  ) THEN
    RAISE EXCEPTION 'migration ledger incomplete';
  END IF;
END $apex_complete$;
UPDATE xynes_release.bootstrap SET completed=true WHERE environment='{environment}' AND baseline_sha256='{fingerprint}';
COMMIT;
SELECT pg_advisory_unlock({LOCK_KEY});""")
    return '\n'.join(statements)+'\n'
