-- STORAGE-2: Universal Object Storage platform schema
--
-- Plan: docs/plans/2026-05-10-universal-object-storage-file-upload-api.md §9
-- Architecture: infra/architecture/epics/universal-object-storage.md
--
-- Creates the six `platform.storage_*` tables that back the new
-- `xynes-storage-service`. All tables are workspace-scoped, additive,
-- and follow the same conventions as `platform.workspace_domains` and
-- `platform.workspace_api_keys`:
--   - `IF NOT EXISTS` for idempotent local resets (`supabase db reset`).
--   - Workspace FKs use `ON DELETE CASCADE` because workspace deletion
--     must clear workspace-owned platform metadata.
--   - User FKs use `ON DELETE SET NULL` for audit columns so an
--     identity row deletion does not destroy storage history; this is
--     the same posture cms.content_entries.created_by/updated_by uses
--     after CMS-API-KEY-ACTOR-1 Story C (api_key actors leave
--     created_by NULL by design).
--   - Status check constraints follow the plan §9 state machines
--     verbatim so the gateway, service, and workers share one source
--     of truth.
--   - NO raw provider credentials, NO raw API keys, NO secret keys,
--     NO signed URLs, and NO bucket names appear in `storage_objects`
--     rows: bucket/credentials live on `workspace_storage_providers`
--     and are stored as credential REFERENCES (secret-manager key or
--     env alias), never raw values.
--   - NO Row Level Security policies — auth is enforced by the gateway
--     action-key system + storage-service handler workspace-ownership
--     checks (same posture as `platform.workspace_domains` /
--     `platform.workspace_api_keys`).
--
-- Migration safety: this is an additive migration. No destructive DDL
-- (no table removal, no column removal, no constraint removal, and no
-- table truncation). Re-running against a populated database is a
-- no-op (every CREATE is `IF NOT EXISTS`).

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS platform;

-- ============================================================================
-- platform.workspace_storage_providers
-- ----------------------------------------------------------------------------
-- One row per provider configuration a workspace has registered. The MVP
-- only supports `r2` (Cloudflare R2 Standard) and `minio` (local dev),
-- but `s3_compatible` is reserved for future Backblaze B2 / Tigris /
-- AWS S3 / customer-owned-bucket onboarding without another migration.
--
-- `credential_ref` is a REFERENCE (secret-manager key or env alias),
-- never a raw access key, secret key, R2 token, or signed URL. The
-- column-level COMMENT below makes this rule explicit so future agents
-- cannot accidentally store raw credentials.
-- ============================================================================
CREATE TABLE IF NOT EXISTS platform.workspace_storage_providers (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES platform.workspaces(id) ON DELETE CASCADE,
  provider_kind text NOT NULL,
  display_name text NOT NULL,
  bucket text NOT NULL,
  region text,
  endpoint text,
  credential_ref text NOT NULL,
  status text NOT NULL DEFAULT 'active',
  is_default boolean NOT NULL DEFAULT false,
  created_by uuid REFERENCES identity.users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT workspace_storage_providers_kind_check
    CHECK (provider_kind IN ('r2', 'minio', 's3_compatible')),
  CONSTRAINT workspace_storage_providers_status_check
    CHECK (status IN ('active', 'disabled')),
  CONSTRAINT workspace_storage_providers_credential_ref_not_blank CHECK (btrim(credential_ref) <> ''),
  CONSTRAINT workspace_storage_providers_bucket_not_blank CHECK (btrim(bucket) <> ''),
  CONSTRAINT workspace_storage_providers_display_name_not_blank CHECK (btrim(display_name) <> '')
);

-- A workspace cannot register the same provider+bucket twice. Same-bucket
-- collisions across workspaces are allowed (different credentials, different
-- key prefixes; bucket isolation is a deployment concern, not a DB concern).
CREATE UNIQUE INDEX IF NOT EXISTS workspace_storage_providers_workspace_kind_bucket_uidx
  ON platform.workspace_storage_providers (workspace_id, provider_kind, bucket)
  WHERE status <> 'disabled';

CREATE INDEX IF NOT EXISTS workspace_storage_providers_workspace_idx
  ON platform.workspace_storage_providers (workspace_id, status, created_at DESC);

-- At most one default provider per workspace.
CREATE UNIQUE INDEX IF NOT EXISTS workspace_storage_providers_workspace_default_uidx
  ON platform.workspace_storage_providers (workspace_id)
  WHERE is_default = true AND status <> 'disabled';

-- ============================================================================
-- platform.storage_objects
-- ----------------------------------------------------------------------------
-- Canonical metadata for every uploaded object. Apps persist `id`
-- (objectId) only; they MUST NOT persist `provider_object_key` or any
-- provider URL. Signed read URLs are minted on demand by the
-- storage-service.
--
-- `status` follows the plan §9 state machine:
--   pending_upload → uploaded → processing → (ready | failed | deleted)
-- `deleted` is a soft-delete marker so audit history survives.
-- ============================================================================
CREATE TABLE IF NOT EXISTS platform.storage_objects (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES platform.workspaces(id) ON DELETE CASCADE,
  provider_id uuid NOT NULL REFERENCES platform.workspace_storage_providers(id),
  provider_object_key text NOT NULL,
  filename text NOT NULL,
  content_type text NOT NULL,
  byte_size bigint NOT NULL,
  sha256 text,
  purpose text NOT NULL DEFAULT 'platform_generic',
  visibility text NOT NULL DEFAULT 'private',
  status text NOT NULL DEFAULT 'pending_upload',
  compression_requested boolean NOT NULL DEFAULT true,
  created_by uuid REFERENCES identity.users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  uploaded_at timestamptz,
  ready_at timestamptz,
  deleted_at timestamptz,
  failure_code text,
  failure_message text,

  CONSTRAINT storage_objects_status_check CHECK (status IN ('pending_upload', 'uploaded', 'processing', 'ready', 'failed', 'deleted')),
  CONSTRAINT storage_objects_visibility_check CHECK (visibility IN ('private', 'public')),
  CONSTRAINT storage_objects_filename_not_blank CHECK (btrim(filename) <> ''),
  CONSTRAINT storage_objects_content_type_not_blank CHECK (btrim(content_type) <> ''),
  CONSTRAINT storage_objects_purpose_not_blank CHECK (btrim(purpose) <> ''),
  CONSTRAINT storage_objects_byte_size_non_negative CHECK (byte_size >= 0),
  CONSTRAINT storage_objects_provider_object_key_not_blank CHECK (btrim(provider_object_key) <> ''),
  -- Soft-delete consistency: once status='deleted', deleted_at must be set.
  CONSTRAINT storage_objects_deleted_consistency CHECK (
    (status = 'deleted' AND deleted_at IS NOT NULL) OR status <> 'deleted'
  )
);

-- Prevent duplicate provider keys within a workspace/provider pair.
-- Different workspaces with different providers (or different bucket
-- prefixes) can collide on key strings; that's prevented at the bucket
-- layer, not here.
CREATE UNIQUE INDEX IF NOT EXISTS storage_objects_workspace_provider_key_uidx
  ON platform.storage_objects (workspace_id, provider_id, provider_object_key);

-- Plan §9 listing index: workspace + purpose + status pagination.
CREATE INDEX IF NOT EXISTS storage_objects_workspace_purpose_status_idx
  ON platform.storage_objects (workspace_id, purpose, status, created_at DESC);

-- Secondary listing index: workspace + status alone (for usage queries).
CREATE INDEX IF NOT EXISTS storage_objects_workspace_status_idx
  ON platform.storage_objects (workspace_id, status, created_at DESC);

-- ============================================================================
-- platform.storage_upload_sessions
-- ----------------------------------------------------------------------------
-- One row per upload attempt. The `provider_upload_id` is the
-- S3-compatible multipart upload identifier returned by the provider
-- when `upload_method = 'multipart'`; it is NOT a credential.
--
-- `status` follows the plan §9 state machine:
--   pending → (completed | aborted | expired)
-- ============================================================================
CREATE TABLE IF NOT EXISTS platform.storage_upload_sessions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES platform.workspaces(id) ON DELETE CASCADE,
  object_id uuid NOT NULL REFERENCES platform.storage_objects(id) ON DELETE CASCADE,
  upload_method text NOT NULL,
  provider_upload_id text,
  status text NOT NULL DEFAULT 'pending',
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  completed_at timestamptz,
  aborted_at timestamptz,
  created_by uuid REFERENCES identity.users(id) ON DELETE SET NULL,

  CONSTRAINT storage_upload_sessions_method_check
    CHECK (upload_method IN ('single', 'multipart')),
  CONSTRAINT storage_upload_sessions_status_check
    CHECK (status IN ('pending', 'completed', 'aborted', 'expired')),
  CONSTRAINT storage_upload_sessions_completed_consistency CHECK (
    (status = 'completed' AND completed_at IS NOT NULL) OR status <> 'completed'
  ),
  CONSTRAINT storage_upload_sessions_aborted_consistency CHECK (
    (status = 'aborted' AND aborted_at IS NOT NULL) OR status <> 'aborted'
  )
);

-- Plan §9 lookup index: by workspace + session id (id is PK so workspace
-- is enough to scope the read pattern).
CREATE INDEX IF NOT EXISTS storage_upload_sessions_workspace_idx
  ON platform.storage_upload_sessions (workspace_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS storage_upload_sessions_object_idx
  ON platform.storage_upload_sessions (object_id, status);

-- ============================================================================
-- platform.storage_object_variants
-- ----------------------------------------------------------------------------
-- Derived assets (thumbnails, responsive images, video posters,
-- transcodes, document previews). Originals never appear here — they
-- live on `storage_objects` directly. A given (object, variant_kind)
-- pair is unique so re-running a processing job can upsert in place.
-- ============================================================================
CREATE TABLE IF NOT EXISTS platform.storage_object_variants (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  object_id uuid NOT NULL REFERENCES platform.storage_objects(id) ON DELETE CASCADE,
  variant_kind text NOT NULL,
  provider_object_key text NOT NULL,
  content_type text NOT NULL,
  byte_size bigint NOT NULL,
  width integer,
  height integer,
  duration_ms integer,
  status text NOT NULL DEFAULT 'pending',
  created_at timestamptz NOT NULL DEFAULT now(),
  ready_at timestamptz,

  CONSTRAINT storage_object_variants_status_check CHECK (status IN ('pending', 'ready', 'failed')),
  CONSTRAINT storage_object_variants_kind_not_blank CHECK (btrim(variant_kind) <> ''),
  CONSTRAINT storage_object_variants_byte_size_non_negative CHECK (byte_size >= 0),
  CONSTRAINT storage_object_variants_provider_key_not_blank CHECK (btrim(provider_object_key) <> '')
);

CREATE UNIQUE INDEX IF NOT EXISTS storage_object_variants_object_kind_uidx
  ON platform.storage_object_variants (object_id, variant_kind);

CREATE INDEX IF NOT EXISTS storage_object_variants_object_idx
  ON platform.storage_object_variants (object_id, status);

-- ============================================================================
-- platform.storage_processing_jobs
-- ----------------------------------------------------------------------------
-- Async job state for scan / validate / image-optimize / probe / poster /
-- transcode / preview workers. `attempts` and `error_code` support the
-- retry-with-dead-letter pattern described in plan §STORAGE-7.
--
-- `status` follows the plan §9 state machine:
--   queued → running → (succeeded | failed | cancelled)
-- ============================================================================
CREATE TABLE IF NOT EXISTS platform.storage_processing_jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  object_id uuid NOT NULL REFERENCES platform.storage_objects(id) ON DELETE CASCADE,
  workspace_id uuid NOT NULL REFERENCES platform.workspaces(id) ON DELETE CASCADE,
  job_kind text NOT NULL,
  status text NOT NULL DEFAULT 'queued',
  attempts integer NOT NULL DEFAULT 0,
  scheduled_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz,
  error_code text,
  error_message text,
  created_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT storage_processing_jobs_status_check
    CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
  CONSTRAINT storage_processing_jobs_kind_not_blank CHECK (btrim(job_kind) <> ''),
  CONSTRAINT storage_processing_jobs_attempts_non_negative CHECK (attempts >= 0)
);

-- Plan §9 queue-polling index: workers poll by status + scheduled time.
CREATE INDEX IF NOT EXISTS storage_processing_jobs_status_scheduled_idx
  ON platform.storage_processing_jobs (status, scheduled_at);

CREATE INDEX IF NOT EXISTS storage_processing_jobs_object_idx
  ON platform.storage_processing_jobs (object_id, status, scheduled_at);

CREATE INDEX IF NOT EXISTS storage_processing_jobs_workspace_idx
  ON platform.storage_processing_jobs (workspace_id, status, created_at DESC);

-- ============================================================================
-- platform.storage_usage_daily
-- ----------------------------------------------------------------------------
-- Pre-aggregated daily counters. The usage-read action (STORAGE-6 +
-- `platform.storage.usage.read` permission) MUST read from this table —
-- live full-table scans of `storage_objects` are explicitly forbidden
-- in normal request paths (architecture epic §7).
-- ============================================================================
CREATE TABLE IF NOT EXISTS platform.storage_usage_daily (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES platform.workspaces(id) ON DELETE CASCADE,
  provider_id uuid REFERENCES platform.workspace_storage_providers(id),
  usage_date date NOT NULL,
  bytes_stored bigint NOT NULL DEFAULT 0,
  bytes_egress bigint NOT NULL DEFAULT 0,
  operations_class_a bigint NOT NULL DEFAULT 0,
  operations_class_b bigint NOT NULL DEFAULT 0,
  object_count bigint NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT storage_usage_daily_bytes_stored_non_negative CHECK (bytes_stored >= 0),
  CONSTRAINT storage_usage_daily_bytes_egress_non_negative CHECK (bytes_egress >= 0),
  CONSTRAINT storage_usage_daily_ops_class_a_non_negative CHECK (operations_class_a >= 0),
  CONSTRAINT storage_usage_daily_ops_class_b_non_negative CHECK (operations_class_b >= 0),
  CONSTRAINT storage_usage_daily_object_count_non_negative CHECK (object_count >= 0)
);

-- Plan §9 uniqueness: one daily row per workspace+date+provider. `provider_id`
-- is nullable so a workspace-wide aggregate (across providers) can also
-- be stored — we differentiate the NULL bucket from any concrete provider
-- via a partial index.
CREATE UNIQUE INDEX IF NOT EXISTS storage_usage_daily_workspace_date_provider_uidx
  ON platform.storage_usage_daily (workspace_id, usage_date, provider_id)
  WHERE provider_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS storage_usage_daily_workspace_date_total_uidx
  ON platform.storage_usage_daily (workspace_id, usage_date)
  WHERE provider_id IS NULL;

-- ============================================================================
-- Grants — consistent with sibling platform migrations
-- ============================================================================
GRANT SELECT, INSERT, UPDATE, DELETE ON platform.workspace_storage_providers TO postgres;
GRANT SELECT, INSERT, UPDATE, DELETE ON platform.storage_objects TO postgres;
GRANT SELECT, INSERT, UPDATE, DELETE ON platform.storage_upload_sessions TO postgres;
GRANT SELECT, INSERT, UPDATE, DELETE ON platform.storage_object_variants TO postgres;
GRANT SELECT, INSERT, UPDATE, DELETE ON platform.storage_processing_jobs TO postgres;
GRANT SELECT, INSERT, UPDATE, DELETE ON platform.storage_usage_daily TO postgres;

-- ============================================================================
-- Documentation — credential-reference-only behaviour
-- ============================================================================
COMMENT ON TABLE platform.workspace_storage_providers IS
  'Workspace-scoped object-storage provider configuration. Stores credential REFERENCES only (secret-manager keys or env aliases); raw access keys, secret keys, R2 tokens, S3 secrets, and signed URLs MUST NEVER be persisted here.';
COMMENT ON COLUMN platform.workspace_storage_providers.credential_ref IS
  'Reference to a credential held in the platform secret manager / env alias system. NEVER a raw access key, secret key, R2 token, or signed URL.';
COMMENT ON COLUMN platform.workspace_storage_providers.endpoint IS
  'Optional S3-compatible endpoint URL (e.g. MinIO local dev). Not a credential.';
COMMENT ON COLUMN platform.workspace_storage_providers.is_default IS
  'At most one default provider per workspace (enforced by workspace_storage_providers_workspace_default_uidx).';

COMMENT ON TABLE platform.storage_objects IS
  'Canonical metadata for every uploaded object. Apps persist the id (objectId) only; provider_object_key, bucket names, and signed URLs MUST NOT be persisted in app data (CMS entry bodies, doc service rows, etc.).';
COMMENT ON COLUMN platform.storage_objects.provider_object_key IS
  'Provider-side object key inside the bound bucket. Never returned to browsers and never persisted in app content bodies.';
COMMENT ON COLUMN platform.storage_objects.created_by IS
  'Identity user who owns this upload. NULL when the upload was performed by an api_key actor (per CMS-API-KEY-ACTOR-1 Story C audit posture).';

COMMENT ON TABLE platform.storage_upload_sessions IS
  'Upload lifecycle state per object. Tracks single/multipart method, provider multipart upload id, and pending/completed/aborted/expired status. Provider_upload_id is NOT a credential.';

COMMENT ON TABLE platform.storage_object_variants IS
  'Derived assets (thumbnails, responsive images, posters, transcodes, previews). Originals live on storage_objects and are NEVER overwritten.';

COMMENT ON TABLE platform.storage_processing_jobs IS
  'Async storage processing job state (scan, validate, image-optimize, probe, poster, transcode, preview). Supports retry-with-dead-letter via attempts + error_code.';

COMMENT ON TABLE platform.storage_usage_daily IS
  'Pre-aggregated daily usage counters per workspace (and optionally per provider). The usage-read action MUST read from this table; live full-table scans of storage_objects are forbidden in normal request paths.';
