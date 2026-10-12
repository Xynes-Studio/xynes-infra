-- STORAGE-FU-2-FU-2: Add `payload` jsonb + `required` boolean columns to
-- `platform.storage_processing_jobs`.
--
-- Plan: docs/plans/2026-05-27-storage-followups-combined.md §14
-- Predecessors:
--   - 20260513090000_universal_storage_platform_schema.sql (STORAGE-2 base schema)
--   - 20260528100000_storage_processing_jobs_active_unique_index.sql (FU-2-FU-1 partial unique index)
--
-- WHY THIS MIGRATION
--   STORAGE-FU-2 (production Drizzle repositories) documented two
--   accepted gaps in the STORAGE-2 base schema:
--     1. No `payload` column — `PostgresProcessingJobQueueRepository`
--        returned `{}` for every `ClaimedJob.payload`. STORAGE-8 runners
--        consume the parent object via `JobRunnerContext.object` so this
--        was safe BUT meant the planner's `{ contentType, byteSize? }`
--        payload was silently discarded.
--     2. No `required` column — `mapProcessingJobRow` derived `required`
--        from a TS-only `REQUIRED_BY_JOB_TYPE` lookup table in
--        `mappers.ts`. Safe today (the planner is the only writer) but a
--        drift hazard if a future job kind is added to the planner
--        without updating the lookup table.
--
--   This migration closes both gaps by adding the two columns as the
--   canonical source of truth. The TS-side derivation is removed in the
--   companion PR on xynes-storage-service.
--
-- BACKWARD-COMPATIBILITY CONTRACT
--   - Both columns are NOT NULL with a fail-CLOSED default:
--       payload  → '{}'::jsonb (empty object)
--       required → true (matches `deriveJobRequired` fail-closed default
--                        for unknown job types)
--     So existing rows pre-migration are auto-populated to the
--     fail-closed default without violating NOT NULL.
--   - The backfill UPDATE flips ONLY the 4 known non-required job
--     kinds to `required = false` so the column value matches the
--     planner's intent for legacy rows. New rows from the post-migration
--     code path always carry the planner's emitted `required` flag.
--   - Old-replica compatibility during a rolling deploy: pre-FU-2-FU-2
--     code never references `payload` or `required` columns, so it
--     INSERTs without them and the NOT NULL DEFAULT auto-populates the
--     fail-closed default. Post-FU-2-FU-2 code reads + writes both
--     columns. Safe in both directions.
--
-- SECURITY INVARIANTS
--   - The planner contract (`xynes-storage-service/src/actions/handlers/processing/planner.ts`)
--     explicitly excludes credentials, provider config, and signed URLs
--     from payloads — runners resolve providers via the resolver at run
--     time, NOT from the job payload.
--   - The xynes-storage-service repository wiring adds a per-jobType
--     Zod `.strict()` payload validator BEFORE the INSERT (defense in
--     depth) so hostile keys cannot sneak in.
--   - The `payload` jsonb column has NO indexes — operators cannot
--     accidentally enable a GIN index on hostile content.
--   - The `required` boolean is a closed-set discriminator (true/false)
--     — no enum drift possible.
--   - NO RLS — auth is enforced by the gateway action-key system +
--     storage-service handler workspace-ownership checks (same posture
--     as every other `platform.storage_*` table).
--
-- DB SAFETY
--   - Strictly additive. NO destructive DDL (no table removal, no
--     column removal, no constraint removal, no table truncation,
--     no row deletion).
--   - `ADD COLUMN IF NOT EXISTS` → reset-safe + replay-safe.
--   - Backfill UPDATE is also idempotent: on replay it touches zero
--     rows because the `required` value already matches the desired
--     value for every row.
--   - Rollback: drop the two columns by name. The xynes-storage-service
--     code change would need to roll back in lockstep (it deletes the
--     `deriveJobRequired` derivation). For an emergency rollback of
--     just the DB layer, the pre-FU-2-FU-2 code path still works
--     because it never references these columns.

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS platform;

-- ============================================================================
-- Add the `payload` jsonb column with fail-closed default
-- ----------------------------------------------------------------------------
-- `NOT NULL DEFAULT '{}'::jsonb` so existing rows auto-populate without
-- violating the NOT NULL constraint. Postgres 11+ rewrites the default
-- into a single fast `ALTER TABLE` (no full table rewrite) for an
-- immutable scalar default.
-- ============================================================================
ALTER TABLE platform.storage_processing_jobs
  ADD COLUMN IF NOT EXISTS payload jsonb NOT NULL DEFAULT '{}'::jsonb;

-- ============================================================================
-- Add the `required` boolean column with fail-closed default
-- ----------------------------------------------------------------------------
-- `NOT NULL DEFAULT true` matches the TS-side `deriveJobRequired`
-- fail-closed default: any job kind not explicitly listed as
-- non-required is treated as required. A new worker that's not yet
-- wired to the planner cannot accidentally be treated as best-effort
-- and silently dropped on a transient failure.
-- ============================================================================
ALTER TABLE platform.storage_processing_jobs
  ADD COLUMN IF NOT EXISTS required boolean NOT NULL DEFAULT true;

-- ============================================================================
-- Backfill `required` for the 4 known non-required job kinds
-- ----------------------------------------------------------------------------
-- The TS-side `REQUIRED_BY_JOB_TYPE` lookup table marks these four
-- kinds as `required = false`:
--   - `image_optimize`     (image variants are best-effort)
--   - `video_thumbnail`    (poster is best-effort)
--   - `video_transcode`    (re-encode is best-effort)
--   - `document_preview`   (preview is best-effort)
--
-- Every other kind defaults to `required = true` (fail-closed). The
-- backfill flips ONLY these four kinds. Required-by-default kinds
-- (`scan_validation`, `video_probe`, unknown) keep the column default
-- of `true` set by the ADD COLUMN above.
--
-- IDEMPOTENCY: on replay, the WHERE clause already matches the
-- target values, so the UPDATE touches zero rows. The
-- `required IS DISTINCT FROM false` guard makes replay a strict
-- no-op (no UPDATE row count noise).
-- ============================================================================
UPDATE platform.storage_processing_jobs
SET required = false
WHERE job_kind IN ('image_optimize', 'video_thumbnail', 'video_transcode', 'document_preview')
  AND required IS DISTINCT FROM false;

-- ============================================================================
-- Documentation comments
-- ============================================================================
COMMENT ON COLUMN platform.storage_processing_jobs.payload IS
  'STORAGE-FU-2-FU-2: planner-emitted job payload. Must NEVER contain credentials, provider config, or signed URLs — runners resolve providers at run time via the resolver. Validated by per-jobType Zod .strict() schemas in xynes-storage-service before INSERT.';

COMMENT ON COLUMN platform.storage_processing_jobs.required IS
  'STORAGE-FU-2-FU-2: whether this job is REQUIRED for the parent object to flip from `processing` to `ready`. Required jobs that fail terminally flip the parent to `failed`; non-required (best-effort) failures do not. Closed set: scan_validation/video_probe = true; image_optimize/video_thumbnail/video_transcode/document_preview = false; unknown = true (fail-closed).';
