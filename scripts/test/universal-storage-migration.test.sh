#!/usr/bin/env bash
set -euo pipefail

# Contract test for the Universal Object Storage platform schema migration.
#
# Validates the SQL file at
# `supabase/migrations/20260513090000_universal_storage_platform_schema.sql`
# contains every required DDL element from STORAGE-2 of
# `docs/plans/2026-05-10-universal-object-storage-file-upload-api.md` §9
# — without needing a running database.
#
# Pattern mirrors `workspace-admin-integrations-migration.test.sh`:
#   - file-existence + grep-based contract assertions
#   - status check constraints (object / upload-session / variant / job)
#   - uniqueness constraints preventing duplicate provider/object keys
#     within a workspace/provider
#   - indexes for the four documented read paths
#   - explicit security invariants: NO raw credential columns, NO raw
#     access keys, NO secret keys, NO signed URLs, NO bucket names in
#     any storage table
#   - additive-only migration (no DROP TABLE / DROP COLUMN / ALTER TABLE … DROP)
#   - grants consistent with sibling platform migrations
#   - documentation COMMENTs explaining credential-reference-only behaviour

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

MIGRATION="./supabase/migrations/20260513090000_universal_storage_platform_schema.sql"

PASS_COUNT=0
FAIL_COUNT=0

fail() {
  echo "FAIL: $1" >&2
  FAIL_COUNT=$((FAIL_COUNT + 1))
}

pass() {
  PASS_COUNT=$((PASS_COUNT + 1))
}

assert_file_exists() {
  local path="$1"
  if [ -f "$path" ]; then
    pass
  else
    fail "expected file to exist: $path"
  fi
}

assert_contains() {
  local path="$1"
  local needle="$2"
  if grep -Fq -- "$needle" "$path"; then
    pass
  else
    fail "expected $path to contain: $needle"
  fi
}

assert_not_contains() {
  local path="$1"
  local needle="$2"
  if grep -Fq -- "$needle" "$path"; then
    fail "expected $path NOT to contain: $needle"
  else
    pass
  fi
}

assert_not_contains_regex() {
  local path="$1"
  local pattern="$2"
  if grep -Eiq -- "$pattern" "$path"; then
    fail "expected $path NOT to match regex: $pattern"
  else
    pass
  fi
}

echo "Testing Universal Object Storage platform schema migration..."
echo ""

# ---------------------------------------------------------------------------
# File existence
# ---------------------------------------------------------------------------
assert_file_exists "$MIGRATION"

# Bail early if the file is missing — the rest of the asserts would just
# produce noise.
if [ ! -f "$MIGRATION" ]; then
  echo ""
  echo "Results: $PASS_COUNT passed, $FAIL_COUNT failed"
  echo "FAIL: universal-storage-migration.test.sh (migration file missing)"
  exit 1
fi

# ---------------------------------------------------------------------------
# Schema + extensions
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "CREATE EXTENSION IF NOT EXISTS pgcrypto;"
assert_contains "$MIGRATION" "CREATE SCHEMA IF NOT EXISTS platform;"

# ---------------------------------------------------------------------------
# Additive-only safety: NO destructive DDL
# ---------------------------------------------------------------------------
assert_not_contains_regex "$MIGRATION" '(^|[^a-z_])DROP[[:space:]]+(TABLE|COLUMN|CONSTRAINT|INDEX|SCHEMA|TYPE)'
assert_not_contains_regex "$MIGRATION" '(^|[^a-z_])ALTER[[:space:]]+TABLE[[:space:]]+[a-z_\.]+[[:space:]]+DROP'
assert_not_contains_regex "$MIGRATION" '(^|[^a-z_])TRUNCATE[[:space:]]+'

# ---------------------------------------------------------------------------
# platform.workspace_storage_providers
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "CREATE TABLE IF NOT EXISTS platform.workspace_storage_providers"
assert_contains "$MIGRATION" "workspace_id uuid NOT NULL REFERENCES platform.workspaces(id) ON DELETE CASCADE"
assert_contains "$MIGRATION" "provider_kind text NOT NULL"
assert_contains "$MIGRATION" "credential_ref text NOT NULL"
# Provider state machine
assert_contains "$MIGRATION" "CHECK (provider_kind IN ('r2', 'minio', 's3_compatible'))"
assert_contains "$MIGRATION" "CHECK (status IN ('active', 'disabled'))"
# A workspace cannot register the same provider+bucket twice
assert_contains "$MIGRATION" "workspace_storage_providers_workspace_kind_bucket_uidx"
# Not-blank guards
assert_contains "$MIGRATION" "CONSTRAINT workspace_storage_providers_credential_ref_not_blank CHECK"
assert_contains "$MIGRATION" "CONSTRAINT workspace_storage_providers_bucket_not_blank CHECK"

# ---------------------------------------------------------------------------
# platform.storage_objects — canonical object metadata
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "CREATE TABLE IF NOT EXISTS platform.storage_objects"
assert_contains "$MIGRATION" "provider_id uuid NOT NULL REFERENCES platform.workspace_storage_providers(id)"
assert_contains "$MIGRATION" "provider_object_key text NOT NULL"
assert_contains "$MIGRATION" "filename text NOT NULL"
assert_contains "$MIGRATION" "content_type text NOT NULL"
assert_contains "$MIGRATION" "byte_size bigint NOT NULL"
assert_contains "$MIGRATION" "sha256 text"
assert_contains "$MIGRATION" "purpose text NOT NULL"
assert_contains "$MIGRATION" "visibility text NOT NULL"
# Object lifecycle states per plan §9
assert_contains "$MIGRATION" "CHECK (status IN ('pending_upload', 'uploaded', 'processing', 'ready', 'failed', 'deleted'))"
assert_contains "$MIGRATION" "CHECK (visibility IN ('private', 'public'))"
# Audit + actor columns (api_key actors leave created_by NULL — see CMS-API-KEY-ACTOR-1 Story C)
assert_contains "$MIGRATION" "created_by uuid REFERENCES identity.users(id) ON DELETE SET NULL"
assert_contains "$MIGRATION" "created_at timestamptz NOT NULL DEFAULT now()"
assert_contains "$MIGRATION" "updated_at timestamptz NOT NULL DEFAULT now()"
# Not-blank guards
assert_contains "$MIGRATION" "CONSTRAINT storage_objects_filename_not_blank CHECK"
assert_contains "$MIGRATION" "CONSTRAINT storage_objects_content_type_not_blank CHECK"
assert_contains "$MIGRATION" "CONSTRAINT storage_objects_byte_size_non_negative CHECK"
assert_contains "$MIGRATION" "CONSTRAINT storage_objects_provider_object_key_not_blank CHECK"
# Uniqueness: per workspace+provider, provider_object_key is unique (prevents cross-workspace pollution)
assert_contains "$MIGRATION" "storage_objects_workspace_provider_key_uidx"
# Listing index per plan §9: workspace + purpose + status
assert_contains "$MIGRATION" "storage_objects_workspace_purpose_status_idx"

# ---------------------------------------------------------------------------
# platform.storage_upload_sessions
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "CREATE TABLE IF NOT EXISTS platform.storage_upload_sessions"
assert_contains "$MIGRATION" "object_id uuid NOT NULL REFERENCES platform.storage_objects(id) ON DELETE CASCADE"
assert_contains "$MIGRATION" "upload_method text NOT NULL"
assert_contains "$MIGRATION" "provider_upload_id text"
assert_contains "$MIGRATION" "expires_at timestamptz NOT NULL"
assert_contains "$MIGRATION" "completed_at timestamptz"
assert_contains "$MIGRATION" "aborted_at timestamptz"
# Plan §9 status set
assert_contains "$MIGRATION" "CHECK (status IN ('pending', 'completed', 'aborted', 'expired'))"
assert_contains "$MIGRATION" "CHECK (upload_method IN ('single', 'multipart'))"
# Plan §9 index: lookup by workspace/upload id
assert_contains "$MIGRATION" "storage_upload_sessions_workspace_idx"

# ---------------------------------------------------------------------------
# platform.storage_object_variants
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "CREATE TABLE IF NOT EXISTS platform.storage_object_variants"
assert_contains "$MIGRATION" "object_id uuid NOT NULL REFERENCES platform.storage_objects(id) ON DELETE CASCADE"
assert_contains "$MIGRATION" "variant_kind text NOT NULL"
assert_contains "$MIGRATION" "provider_object_key text NOT NULL"
assert_contains "$MIGRATION" "content_type text NOT NULL"
assert_contains "$MIGRATION" "byte_size bigint NOT NULL"
assert_contains "$MIGRATION" "CHECK (status IN ('pending', 'ready', 'failed'))"
assert_contains "$MIGRATION" "CONSTRAINT storage_object_variants_byte_size_non_negative CHECK"
# A given (object, variant_kind) is unique
assert_contains "$MIGRATION" "storage_object_variants_object_kind_uidx"

# ---------------------------------------------------------------------------
# platform.storage_processing_jobs
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "CREATE TABLE IF NOT EXISTS platform.storage_processing_jobs"
assert_contains "$MIGRATION" "object_id uuid NOT NULL REFERENCES platform.storage_objects(id) ON DELETE CASCADE"
assert_contains "$MIGRATION" "job_kind text NOT NULL"
assert_contains "$MIGRATION" "attempts integer NOT NULL DEFAULT 0"
assert_contains "$MIGRATION" "scheduled_at timestamptz NOT NULL DEFAULT now()"
assert_contains "$MIGRATION" "started_at timestamptz"
assert_contains "$MIGRATION" "finished_at timestamptz"
assert_contains "$MIGRATION" "error_code text"
assert_contains "$MIGRATION" "CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled'))"
# Plan §9 index: queue polling by status + scheduled time
assert_contains "$MIGRATION" "storage_processing_jobs_status_scheduled_idx"

# ---------------------------------------------------------------------------
# platform.storage_usage_daily
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "CREATE TABLE IF NOT EXISTS platform.storage_usage_daily"
assert_contains "$MIGRATION" "workspace_id uuid NOT NULL REFERENCES platform.workspaces(id) ON DELETE CASCADE"
assert_contains "$MIGRATION" "provider_id uuid REFERENCES platform.workspace_storage_providers(id)"
assert_contains "$MIGRATION" "usage_date date NOT NULL"
assert_contains "$MIGRATION" "bytes_stored bigint NOT NULL DEFAULT 0"
assert_contains "$MIGRATION" "bytes_egress bigint NOT NULL DEFAULT 0"
assert_contains "$MIGRATION" "operations_class_a bigint NOT NULL DEFAULT 0"
assert_contains "$MIGRATION" "operations_class_b bigint NOT NULL DEFAULT 0"
# Plan §9 index: daily usage by workspace/date/provider
assert_contains "$MIGRATION" "storage_usage_daily_workspace_date_provider_uidx"

# ---------------------------------------------------------------------------
# Security invariants — NO raw credentials, NO raw access/secret keys,
# NO signed URLs, NO bucket names should appear in storage_objects rows.
# ---------------------------------------------------------------------------
# Forbidden raw-credential column names anywhere in the file.
assert_not_contains "$MIGRATION" "raw_key"
assert_not_contains "$MIGRATION" "raw_secret"
assert_not_contains "$MIGRATION" "access_key_id"
assert_not_contains "$MIGRATION" "access_key"
assert_not_contains "$MIGRATION" "secret_access_key"
assert_not_contains "$MIGRATION" "secret_key"
assert_not_contains "$MIGRATION" "r2_token"
assert_not_contains "$MIGRATION" "signed_url"
assert_not_contains "$MIGRATION" "presigned_url"
# Object keys never re-encode the workspace credential; only references.
assert_not_contains "$MIGRATION" "provider_credentials"

# ---------------------------------------------------------------------------
# Grants — consistent with sibling platform migrations
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "GRANT SELECT, INSERT, UPDATE, DELETE ON platform.workspace_storage_providers TO postgres;"
assert_contains "$MIGRATION" "GRANT SELECT, INSERT, UPDATE, DELETE ON platform.storage_objects TO postgres;"
assert_contains "$MIGRATION" "GRANT SELECT, INSERT, UPDATE, DELETE ON platform.storage_upload_sessions TO postgres;"
assert_contains "$MIGRATION" "GRANT SELECT, INSERT, UPDATE, DELETE ON platform.storage_object_variants TO postgres;"
assert_contains "$MIGRATION" "GRANT SELECT, INSERT, UPDATE, DELETE ON platform.storage_processing_jobs TO postgres;"
assert_contains "$MIGRATION" "GRANT SELECT, INSERT, UPDATE, DELETE ON platform.storage_usage_daily TO postgres;"

# ---------------------------------------------------------------------------
# Documentation comments — credential-reference-only behaviour must be
# called out at the column level so future agents cannot miss it.
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "COMMENT ON TABLE platform.workspace_storage_providers"
assert_contains "$MIGRATION" "COMMENT ON COLUMN platform.workspace_storage_providers.credential_ref"
assert_contains "$MIGRATION" "COMMENT ON TABLE platform.storage_objects"
assert_contains "$MIGRATION" "COMMENT ON TABLE platform.storage_upload_sessions"
assert_contains "$MIGRATION" "COMMENT ON TABLE platform.storage_object_variants"
assert_contains "$MIGRATION" "COMMENT ON TABLE platform.storage_processing_jobs"
assert_contains "$MIGRATION" "COMMENT ON TABLE platform.storage_usage_daily"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Results: $PASS_COUNT passed, $FAIL_COUNT failed"
echo ""
if [ "$FAIL_COUNT" -gt 0 ]; then
  echo "FAIL: universal-storage-migration.test.sh"
  exit 1
fi
echo "PASS: universal-storage-migration.test.sh"
