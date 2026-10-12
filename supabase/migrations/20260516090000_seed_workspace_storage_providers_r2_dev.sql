-- STORAGE-LIVE-2: Seed `platform.workspace_storage_providers` row for R2 dev
--
-- Plan: docs/plans/2026-05-14-storage-live-provider-rollout.md §5
-- Predecessor: docs/runbooks/universal-storage-r2-dev-provisioning-evidence-2026-05-16.md
--
-- Idempotently seeds ONE Cloudflare R2 dev provider row per existing local
-- workspace. The migration is conditional on `platform.workspaces`
-- containing rows. A freshly-reset Supabase DB (`supabase db reset`) starts
-- with zero workspaces — this migration is a safe no-op in that case.
-- When workspaces are added later (sign-up flow), the bootstrap script
-- re-applies the same seed via `bootstrap-local-db.sh` so the dev R2 row
-- always exists for every local workspace that needs it.
--
-- Idempotency contract:
--   - `ON CONFLICT (workspace_id, provider_kind, bucket) WHERE status <> 'disabled'`
--     matches the partial unique index
--     `workspace_storage_providers_workspace_kind_bucket_uidx` from
--     `20260513090000_universal_storage_platform_schema.sql`.
--   - Re-running the migration after the row exists is a no-op except for
--     a refreshed `display_name` / `region` / `endpoint` / `credential_ref` /
--     `is_default` / `updated_at` (so config drift is corrected on replay).
--
-- Security invariants (also enforced by `scripts/test/universal-storage-r2-dev-seed.test.sh`):
--   - The `credential_ref` column carries a secret-manager POINTER URI
--     (`secret://xynes/storage/r2-dev`), never a raw token. The pointer is
--     resolved at runtime by `xynes-storage-service` (local-dev:
--     `EnvSecretManagerClient`; hosted: a real SecretManagerClient).
--   - The endpoint hostname is the R2 account-scoped host
--     `<accountId>.r2.cloudflarestorage.com` — non-secret, safe to commit.
--   - The bucket name is non-secret (`xynes-storage-dev`).
--   - The canonical schema migration
--     (`20260513090000_universal_storage_platform_schema.sql`) is the
--     single source of truth for the credential-storage rules; the
--     contract test for this seed rejects any raw credential value,
--     access-key prefix, or signed-URL signature parameter from
--     appearing in this file.
--
-- DB safety: strictly additive. No DROP, no ALTER, no DELETE, no TRUNCATE.

DO $$
DECLARE
  ws_record record;
BEGIN
  -- Guard: skip silently when there are no workspaces yet (clean reset).
  IF NOT EXISTS (SELECT 1 FROM platform.workspaces LIMIT 1) THEN
    RAISE NOTICE 'STORAGE-LIVE-2: no platform.workspaces rows yet; skipping R2 dev provider seed. Re-run bootstrap-local-db.sh after a workspace is created to land the row.';
    RETURN;
  END IF;

  FOR ws_record IN
    SELECT id FROM platform.workspaces ORDER BY created_at, id
  LOOP
    INSERT INTO platform.workspace_storage_providers
      (workspace_id, provider_kind, display_name, bucket, region, endpoint, credential_ref, status, is_default)
    VALUES
      (
        ws_record.id,
        'r2',
        'Cloudflare R2 — dev',
        'xynes-storage-dev',
        'auto',
        'https://11287695f5abfc0f001f8215034619b2.r2.cloudflarestorage.com',
        'secret://xynes/storage/r2-dev',
        'active',
        true
      )
    ON CONFLICT (workspace_id, provider_kind, bucket)
      WHERE status <> 'disabled'
    DO UPDATE SET
      display_name = EXCLUDED.display_name,
      region = EXCLUDED.region,
      endpoint = EXCLUDED.endpoint,
      credential_ref = EXCLUDED.credential_ref,
      is_default = EXCLUDED.is_default,
      updated_at = now();
  END LOOP;
END
$$;
