-- DEDUP-1: Content-hash dedup schema for Universal Object Storage
--
-- Plan: docs/plans/2026-05-27-storage-followups-combined.md §8
-- Predecessor: 20260513090000_universal_storage_platform_schema.sql (STORAGE-2)
--
-- Adds:
--   1. `platform.storage_object_references` — reference-counting join
--      table. Tracks which owners (CMS entries, comments, doc-service
--      rows, user avatars, workspace logos, generic platform owners)
--      currently hold a reference to a storage object. Reference-counted
--      soft-delete: a `storage_objects` row is only soft-deleted when
--      the last reference is removed.
--   2. A workspace-scoped PARTIAL UNIQUE INDEX on
--      `platform.storage_objects (workspace_id, sha256)` covering
--      active statuses only (`uploaded` / `processing` / `ready`). The
--      upload-create handler (DEDUP-2) probes this index to short-circuit
--      duplicate uploads — same workspace + same `sha256` → return the
--      existing `objectId` instead of minting a new provider URL.
--
-- SECURITY INVARIANTS:
--   - Dedup is WORKSPACE-SCOPED only. The partial unique index keys on
--     `(workspace_id, sha256)`, NOT `(sha256)` alone, so a hostile
--     workspace cannot probe content existence in another workspace via
--     timing or response-size differences. Cross-tenant leaks are
--     structurally impossible at the DB layer.
--   - The reference table carries NO provider material, NO credentials,
--     and NO signed URLs. Only `object_id` (FK to `storage_objects`),
--     `owner_kind` (closed set), `owner_id` (uuid), `created_at`.
--   - `owner_kind` is a closed CHECK set. New owner kinds require an
--     additive migration that ALTERs the constraint — they cannot be
--     introduced by application code.
--   - NO foreign key on `owner_id`. Different owner kinds target
--     different schemas (`cms.content_entries`, `cms.comments`,
--     `docs.documents`, `identity.users`, etc.). Validation of the
--     owner identity is the upstream handler's job (documented in
--     `xynes-storage-service/DEVELOPER.md`).
--   - NO RLS — auth is enforced by the gateway action-key system +
--     storage-service handler workspace-ownership checks (same posture
--     as `platform.workspace_domains` / `platform.workspace_api_keys` /
--     `platform.storage_objects`).
--
-- DB SAFETY:
--   - Strictly additive. NO destructive DDL (no table removal, no
--     column removal, no constraint removal, no table truncation).
--   - `IF NOT EXISTS` everywhere → reset-safe + replay-safe.
--   - `sha256` column on `platform.storage_objects` is NOT made
--     `NOT NULL`. Pre-existing rows uploaded without a sha256 are
--     preserved as-is. New uploads SHOULD set sha256 (DEDUP-2 makes it
--     strongly-encouraged via response semantics) but the column itself
--     stays nullable.
--   - The partial unique index uses the `(workspace_id, sha256)` keyset
--     PLUS a predicate excluding terminal states (`pending_upload` /
--     `failed` / `deleted`) so legitimate retries after an aborted
--     multipart OR after a soft-delete still succeed.
--   - Rollback: drop the index + table. No app-side rollback required
--     (DEDUP-2 handler change is gated on the index's presence via
--     try/catch; absence means dedup is silently disabled — fail-open
--     is acceptable because the worst case is "we miss a dedup hit,"
--     not a correctness bug).

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS platform;

-- ============================================================================
-- platform.storage_object_references
-- ----------------------------------------------------------------------------
-- One row per (object, owner_kind, owner_id) triple. Multiple rows per
-- object are expected — that is the entire point of reference-counting.
-- The composite PRIMARY KEY enforces idempotency: re-inserting the same
-- triple is a no-op (handlers use `ON CONFLICT (object_id, owner_kind,
-- owner_id) DO NOTHING`).
--
-- `ON DELETE CASCADE` on `object_id` so a hard-deleted storage object
-- (rare; soft-delete is the norm) automatically clears its references.
--
-- Closed-set `owner_kind` CHECK constraint kept on a single line so the
-- grep-based contract test in
-- `xynes-infra/scripts/test/universal-storage-dedup-schema.test.sh`
-- can match it.
-- ============================================================================
CREATE TABLE IF NOT EXISTS platform.storage_object_references (
  object_id uuid NOT NULL REFERENCES platform.storage_objects(id) ON DELETE CASCADE,
  owner_kind text NOT NULL,
  owner_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT storage_object_references_pkey PRIMARY KEY (object_id, owner_kind, owner_id),
  CONSTRAINT storage_object_references_owner_kind_check CHECK (owner_kind IN ('cms_entry', 'comment', 'doc_service', 'user_avatar', 'workspace_logo', 'platform_generic'))
);

-- Plan §8 read-path index: lookup all references for a given object.
-- Used by the DEDUP-2 delete handler to decide whether to soft-delete
-- (last reference removed) or short-circuit with `referencesRemaining`.
-- The PK is `(object_id, owner_kind, owner_id)` so an index on
-- `object_id` alone is genuinely useful — the PK only helps when the
-- query also constrains `owner_kind` + `owner_id`.
CREATE INDEX IF NOT EXISTS storage_object_references_object_idx
  ON platform.storage_object_references (object_id);

-- ============================================================================
-- platform.storage_objects — workspace-scoped partial unique index for dedup
-- ----------------------------------------------------------------------------
-- Predicate rationale:
--   - `sha256 IS NOT NULL` — pre-existing rows uploaded without a
--     hash are excluded so the index creation does not fail on legacy
--     data. New uploads in DEDUP-2 always carry `sha256`.
--   - `status IN ('uploaded', 'processing', 'ready')` — excludes
--     `pending_upload` (the upload hasn't actually committed bytes
--     yet; client may abort or fail; a fresh retry with the same
--     `sha256` must succeed), `failed` (bytes never persisted
--     correctly), and `deleted` (soft-deleted rows MUST NOT block a
--     fresh upload of the same content).
--
-- Workspace-scoping (NOT global) is the tenant-isolation invariant.
-- Cross-workspace dedup is structurally forbidden at the DB layer.
-- ============================================================================
-- Pre-index reconciliation — demote legacy duplicates BEFORE creating
-- the partial unique index.
-- ----------------------------------------------------------------------------
-- This migration is meant to address Bug 2 (same file uploaded N times
-- is stored N times). On any environment that already accumulated
-- legacy duplicates — i.e. multiple rows with the SAME
-- `(workspace_id, sha256)` and status in
-- ('uploaded', 'processing', 'ready') — the
-- `CREATE UNIQUE INDEX` below would otherwise abort with a
-- duplicate-key error and block the migration.
--
-- We reconcile by SOFT-DELETING every duplicate except the OLDEST per
-- `(workspace_id, sha256)` group:
--   - Oldest row (lowest created_at, then lowest id as a tiebreaker)
--     keeps its original status and remains the dedup winner.
--   - All later duplicates flip to status='deleted' with
--     deleted_at=now() (the soft-delete pattern STORAGE-2 already
--     uses). This is NON-destructive: the rows + provider object
--     keys + audit columns are preserved for forensic / billing
--     review. Subsequent DEDUP-2 upload attempts that match the
--     winner's `sha256` will hit the dedup short-circuit.
--
-- IDEMPOTENCY: when re-run, the partial unique index already exists
-- (or there are no more duplicates to demote); the UPDATE matches
-- zero rows and is a no-op. Replay-safe.
--
-- DB SAFETY: this is an UPDATE only — no destructive DDL (no `DROP`,
-- no `ALTER ... DROP`, no `TRUNCATE...`, no `DELETE FROM...`). The
-- soft-delete `deleted_at = now()` satisfies the
-- `storage_objects_deleted_consistency` CHECK from STORAGE-2.
--
-- AUDIT TRAIL: `failure_code` is set to 'DEDUP_RECONCILED' so
-- operators can identify rows demoted by this migration vs rows
-- soft-deleted by the regular delete handler. `failure_message` is a
-- stable English sentence — no provider material.
-- ============================================================================
WITH ranked_duplicates AS (
  SELECT
    id,
    workspace_id,
    sha256,
    ROW_NUMBER() OVER (
      PARTITION BY workspace_id, sha256
      ORDER BY created_at ASC, id ASC
    ) AS rn
  FROM platform.storage_objects
  WHERE sha256 IS NOT NULL
    AND status IN ('uploaded', 'processing', 'ready')
)
UPDATE platform.storage_objects
SET status = 'deleted',
    deleted_at = now(),
    failure_code = 'DEDUP_RECONCILED',
    failure_message = 'Soft-deleted by DEDUP-1 migration; older duplicate is the dedup winner.',
    updated_at = now()
WHERE id IN (
  SELECT id FROM ranked_duplicates WHERE rn > 1
);

CREATE UNIQUE INDEX IF NOT EXISTS storage_objects_workspace_sha256_uidx
  ON platform.storage_objects (workspace_id, sha256)
  WHERE sha256 IS NOT NULL AND status IN ('uploaded', 'processing', 'ready');

-- ============================================================================
-- Grants — consistent with sibling platform migrations
-- ============================================================================
GRANT SELECT, INSERT, UPDATE, DELETE ON platform.storage_object_references TO postgres;

-- ============================================================================
-- Documentation — reference-counting + dedup semantics
-- ============================================================================
COMMENT ON TABLE platform.storage_object_references IS
  'Reference-counting join table for storage objects. Each row marks one (object_id, owner_kind, owner_id) reference. Storage objects are soft-deleted only when the last reference is removed (DEDUP-2 delete handler). The table carries NO provider material, NO credentials, NO signed URLs.';
COMMENT ON COLUMN platform.storage_object_references.owner_kind IS
  'Closed-set discriminator. New owner kinds require an additive migration that ALTERs the constraint; application code MUST NOT introduce new values.';
COMMENT ON COLUMN platform.storage_object_references.owner_id IS
  'Owner row id. NOT a foreign key — owner kinds target different schemas (cms.content_entries, cms.comments, docs.documents, identity.users). Validation lives in the upstream handler that mints the reference.';

COMMENT ON INDEX platform.storage_objects_workspace_sha256_uidx IS
  'DEDUP-1: workspace-scoped partial unique index for content-hash deduplication. Predicate excludes pending_upload / failed / deleted statuses so legitimate retries after abort / failure / soft-delete still succeed. Cross-workspace dedup is structurally forbidden (the key is (workspace_id, sha256), not sha256 alone).';
