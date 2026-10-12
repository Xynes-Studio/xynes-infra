-- STORAGE-FU-2-FU-1: Partial unique index for ACTIVE processing jobs
--
-- Plan: docs/plans/2026-05-27-storage-followups-combined.md §10
-- Predecessor: 20260513090000_universal_storage_platform_schema.sql (STORAGE-2)
--
-- Adds a workspace-agnostic PARTIAL UNIQUE INDEX on
-- `platform.storage_processing_jobs (object_id, job_kind)` covering ONLY
-- the non-terminal `status` values `queued` / `running`. This is the
-- belt-and-braces DB-layer enforcement for the transaction-level
-- duplicate check that already lives in
-- `xynes-storage-service/src/infra/db/repositories/variant-job-usage-repository.ts`
-- (`PostgresProcessingJobQueueRepository.enqueueBatch` →
-- `DuplicateActiveJobError`, STORAGE-FU-2 commit 7de407b).
--
-- Why a PARTIAL index (and not a global one):
--   - STORAGE-7 explicitly REQUIRES retries to land after terminal
--     statuses (`failed` / `succeeded` / `cancelled`). A global
--     UNIQUE index on `(object_id, job_kind)` would forever block
--     a re-run after a transient failure.
--   - The partial predicate `status IN ('queued', 'running')` covers
--     EXACTLY the rule the transaction-level check enforces — no more,
--     no less.
--   - Worker fan-out under STORAGE-FU-6 polls and CLAIMS jobs via
--     `SELECT … FOR UPDATE SKIP LOCKED`. Without a DB-side guard, two
--     transactions in different worker processes could in theory both
--     pass the in-tx pre-check (`SELECT … status IN ('queued','running')`)
--     before either INSERT, leading to a duplicate active row. The
--     partial unique index closes that window.
--
-- BACKWARD-COMPATIBILITY CONTRACT:
--   - The transaction-level check in `enqueueBatch` STAYS. It returns a
--     clean `DuplicateActiveJobError` envelope (statusHint=409,
--     code='DUPLICATE_ACTIVE_JOB') WITHOUT requiring callers to catch
--     a raw Postgres `unique_violation` error. The DB index is purely
--     defensive — it catches the narrow race-window the transactional
--     check cannot cover, and the repo translates the resulting
--     `unique_violation` (SQLSTATE 23505) into the same closed-set
--     `DuplicateActiveJobError` so callers see ONE consistent error
--     shape regardless of which layer detected the duplicate.
--
-- SECURITY INVARIANTS:
--   - The index keys on `(object_id, job_kind)` — both columns are
--     already present in `storage_processing_jobs` from STORAGE-2.
--     No new columns, no credentials, no provider material.
--   - Tenant isolation is enforced by the parent `object_id` FK to
--     `platform.storage_objects(workspace_id)` plus the
--     workspace-ownership checks in the storage-service handlers.
--     The index is workspace-agnostic ONLY because `object_id` is
--     globally unique (uuid PK) — a single `object_id` can never
--     belong to two workspaces, so a partial unique index keyed on
--     `(object_id, job_kind)` is structurally workspace-scoped via
--     the FK chain.
--   - NO RLS — auth is enforced by the gateway action-key system +
--     storage-service handler workspace-ownership checks (same posture
--     as `platform.workspace_domains` / `platform.workspace_api_keys` /
--     `platform.storage_objects`).
--
-- DB SAFETY:
--   - Strictly additive. NO destructive DDL (no table removal, no
--     column removal, no constraint removal, no table truncation,
--     no row deletion).
--   - `IF NOT EXISTS` on the index → reset-safe + replay-safe.
--   - Existing terminal rows (`succeeded` / `failed` / `cancelled`)
--     are NOT covered by the partial predicate and remain untouched.
--   - Existing `queued` / `running` rows on a healthy stack are
--     UNIQUE by `(object_id, job_kind)` per the existing transaction-
--     level check, so the index creation succeeds without reconciliation
--     on any environment that ran the FU-2 enqueueBatch fix from
--     commit 7de407b onwards. Defensive pre-index reconciliation
--     (below) handles the corner case where a legacy stack — pre-FU-2
--     or running multiple workers without the in-tx check — accumulated
--     duplicate `queued` / `running` rows.
--   - Rollback: drop the index by name. The application-level transaction
--     check from STORAGE-FU-2 remains as the primary line of defense,
--     so removing the index does not regress the duplicate-rejection
--     contract.

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS platform;

-- ============================================================================
-- Pre-index reconciliation — defensively demote legacy duplicate ACTIVE rows
-- ----------------------------------------------------------------------------
-- Mirrors the DEDUP-1 reconciliation pattern (see migration
-- `20260528090000_storage_object_references_and_dedup_index.sql`). On any
-- environment that pre-dates the STORAGE-FU-2 transaction-level
-- duplicate check (commit 7de407b) — OR ran under buggy multi-worker
-- fan-out before the FU-1 index landed — there may be multiple
-- `queued` / `running` rows for the same `(object_id, job_kind)`.
-- The `CREATE UNIQUE INDEX` below would otherwise abort with a
-- duplicate-key error and block the migration.
--
-- We reconcile by CANCELLING every duplicate except the OLDEST per
-- `(object_id, job_kind)` group:
--   - Oldest row (lowest `created_at`, then lowest `id` as a
--     deterministic tiebreaker) keeps its `queued` / `running` status
--     and remains the legitimate active row.
--   - All later duplicates flip to `status='cancelled'` with
--     `finished_at=now()` and `error_code='DEDUP_RECONCILED_FU_1'` so
--     operators can identify rows demoted by this migration vs rows
--     cancelled by the regular worker path. `error_message` is a
--     stable English sentence — no provider material.
--
-- This is NON-destructive: the rows + their audit columns are
-- preserved. `cancelled` is a terminal status per the STORAGE-7
-- state machine and matches the existing `CHECK status IN (...)`
-- constraint from STORAGE-2.
--
-- IDEMPOTENCY: when re-run, the partial unique index already exists
-- (or there are no more duplicates to demote); the UPDATE matches
-- zero rows and is a no-op. Replay-safe.
-- ============================================================================
WITH ranked_duplicates AS (
  SELECT
    id,
    object_id,
    job_kind,
    ROW_NUMBER() OVER (
      PARTITION BY object_id, job_kind
      ORDER BY created_at ASC, id ASC
    ) AS rn
  FROM platform.storage_processing_jobs
  WHERE status IN ('queued', 'running')
)
UPDATE platform.storage_processing_jobs
SET status = 'cancelled',
    finished_at = now(),
    error_code = 'DEDUP_RECONCILED_FU_1',
    error_message = 'Cancelled by STORAGE-FU-2-FU-1 migration; older duplicate is the active winner.'
WHERE id IN (
  SELECT id FROM ranked_duplicates WHERE rn > 1
);

-- ============================================================================
-- Partial unique index on (object_id, job_kind) for ACTIVE rows only
-- ----------------------------------------------------------------------------
-- Predicate rationale:
--   - `status IN ('queued', 'running')` — covers the exact set of
--     non-terminal statuses. Terminal rows (`succeeded` / `failed` /
--     `cancelled`) are intentionally NOT covered so retries can
--     enqueue fresh rows for the same `(object_id, job_kind)` after
--     a terminal outcome.
--
-- Naming:
--   - `storage_processing_jobs_active_unique_uidx` — matches the
--     plan §10 spec verbatim. The `_uidx` suffix mirrors the DEDUP-1
--     `storage_objects_workspace_sha256_uidx` partial unique index
--     convention.
-- ============================================================================
CREATE UNIQUE INDEX IF NOT EXISTS storage_processing_jobs_active_unique_uidx
  ON platform.storage_processing_jobs (object_id, job_kind)
  WHERE status IN ('queued', 'running');

-- ============================================================================
-- Documentation — partial unique index semantics
-- ============================================================================
COMMENT ON INDEX platform.storage_processing_jobs_active_unique_uidx IS
  'STORAGE-FU-2-FU-1: partial unique index ensuring at most one ACTIVE (queued/running) processing job per (object_id, job_kind). Terminal rows (succeeded / failed / cancelled) are intentionally NOT covered so retries land. Belt-and-braces for the transaction-level duplicate check in PostgresProcessingJobQueueRepository.enqueueBatch.';
