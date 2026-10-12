#!/usr/bin/env bash
set -euo pipefail

# Contract test for the STORAGE-LIVE-2 R2 dev provider seed migration.
#
# Validates the SQL file at
# `supabase/migrations/20260516090000_seed_workspace_storage_providers_r2_dev.sql`
# satisfies the STORAGE-LIVE-2 acceptance criteria from
# `docs/plans/2026-05-14-storage-live-provider-rollout.md` §5 — without
# needing a running database.
#
# Pattern mirrors `universal-storage-migration.test.sh` (file-existence +
# grep-based contract assertions).
#
# Coverage:
#   - Migration file exists at the expected path.
#   - No raw `accessKeyId` / `secretAccessKey` / `R2_TOKEN` / `AKIA*` /
#     `xynes_live_*` / `X-Amz-Signature` / `signed_url` substrings.
#   - Inserts the R2 dev row with the non-secret values from the
#     STORAGE-LIVE-1 evidence runbook (account ID, bucket, region).
#   - References `secret://xynes/storage/r2-dev` (the STORAGE-FU-3
#     `EnvSecretManagerClient` pointer) — NOT a raw credential.
#   - Idempotent via `ON CONFLICT (workspace_id, provider_kind, bucket)
#     DO UPDATE` against the partial unique index from the STORAGE-2
#     schema migration.
#   - Conditional / safe-no-op when `platform.workspaces` is empty.
#   - Additive-only (no DROP / ALTER … DROP / DELETE / TRUNCATE).
#   - Provider kind is `r2`.
#   - Provider is marked default (`is_default = true`).
#   - Active status (`status = 'active'`).

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

MIGRATION="./supabase/migrations/20260516090000_seed_workspace_storage_providers_r2_dev.sql"

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
  if grep -Eq -- "$pattern" "$path"; then
    fail "expected $path NOT to match regex: $pattern"
  else
    pass
  fi
}

echo "Testing STORAGE-LIVE-2 R2 dev workspace storage providers seed migration..."
echo ""

# ---------------------------------------------------------------------------
# File existence
# ---------------------------------------------------------------------------
assert_file_exists "$MIGRATION"

if [ ! -f "$MIGRATION" ]; then
  echo ""
  echo "Results: $PASS_COUNT passed, $FAIL_COUNT failed"
  echo "FAIL: universal-storage-r2-dev-seed.test.sh (migration file missing)"
  exit 1
fi

# ---------------------------------------------------------------------------
# Security invariants — no raw credentials, no signed URLs, no token leakage
# ---------------------------------------------------------------------------
# AWS-style access key id prefix (must NEVER appear anywhere in the seed)
assert_not_contains_regex "$MIGRATION" 'AKIA[A-Z0-9]{16,}'
# Raw secret access key column names — values shouldn't appear, names also shouldn't
# (the schema reserves `credential_ref` as the only credential column).
assert_not_contains "$MIGRATION" "accessKeyId"
assert_not_contains "$MIGRATION" "access_key_id"
assert_not_contains "$MIGRATION" "secretAccessKey"
assert_not_contains "$MIGRATION" "secret_access_key"
assert_not_contains "$MIGRATION" "r2Token"
assert_not_contains "$MIGRATION" "R2_TOKEN"
assert_not_contains "$MIGRATION" "signedUrl"
assert_not_contains "$MIGRATION" "signed_url"
assert_not_contains "$MIGRATION" "presignedUrl"
assert_not_contains "$MIGRATION" "presigned_url"
# Workspace API key prefix (a different feature; never legitimate in storage seed)
assert_not_contains_regex "$MIGRATION" 'xynes_live_[a-f0-9]+'
# SigV4 signature query parameters from a signed URL
assert_not_contains "$MIGRATION" "X-Amz-Signature"
assert_not_contains "$MIGRATION" "X-Amz-Credential"
assert_not_contains "$MIGRATION" "X-Amz-Security-Token"

# ---------------------------------------------------------------------------
# Additive-only safety: NO destructive DDL or destructive DML
# ---------------------------------------------------------------------------
assert_not_contains_regex "$MIGRATION" '(^|[^a-z_])DROP[[:space:]]+(TABLE|COLUMN|CONSTRAINT|INDEX|SCHEMA|TYPE)'
assert_not_contains_regex "$MIGRATION" '(^|[^a-z_])ALTER[[:space:]]+TABLE[[:space:]]+[a-z_\.]+[[:space:]]+DROP'
assert_not_contains_regex "$MIGRATION" '(^|[^a-z_])TRUNCATE[[:space:]]+'
assert_not_contains_regex "$MIGRATION" '(^|[^a-z_])DELETE[[:space:]]+FROM'

# ---------------------------------------------------------------------------
# Targets the right table
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "INSERT INTO platform.workspace_storage_providers"

# ---------------------------------------------------------------------------
# Non-secret R2 dev values from STORAGE-LIVE-1 evidence
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "'r2'"
assert_contains "$MIGRATION" "'xynes-storage-dev'"
assert_contains "$MIGRATION" "'auto'"
assert_contains "$MIGRATION" "11287695f5abfc0f001f8215034619b2.r2.cloudflarestorage.com"
assert_contains "$MIGRATION" "'Cloudflare R2 — dev'"

# ---------------------------------------------------------------------------
# Credential reference is the secret-manager POINTER, never a raw value
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "'secret://xynes/storage/r2-dev'"
# Pointer must look exactly like a `secret://` URI (no raw token nearby).
# The credential_ref column NEVER carries an `http`/`https` URL or a raw key.
assert_not_contains_regex "$MIGRATION" "credential_ref[[:space:]]*=[[:space:]]*'(http|https|AKIA)"

# ---------------------------------------------------------------------------
# Idempotent UPSERT against the partial unique index from STORAGE-2
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "ON CONFLICT (workspace_id, provider_kind, bucket)"
assert_contains "$MIGRATION" "WHERE status <> 'disabled'"
assert_contains "$MIGRATION" "DO UPDATE SET"
assert_contains "$MIGRATION" "updated_at = now()"

# ---------------------------------------------------------------------------
# Conditional / safe no-op when platform.workspaces is empty
# ---------------------------------------------------------------------------
# Must guard on workspaces existing — otherwise a fresh `supabase db reset`
# would error or silently insert nothing in a way that hides logic bugs.
assert_contains "$MIGRATION" "NOT EXISTS (SELECT 1 FROM platform.workspaces"

# ---------------------------------------------------------------------------
# Seeds for every existing workspace (iterates platform.workspaces)
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "FROM platform.workspaces"
assert_contains "$MIGRATION" "LOOP"

# ---------------------------------------------------------------------------
# Active + default
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "'active'"
assert_contains "$MIGRATION" "true"  # is_default

# ---------------------------------------------------------------------------
# Documentation surface: explicit comment about credential_ref being a pointer
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "secret-manager"
assert_contains "$MIGRATION" "EnvSecretManagerClient"

echo ""
echo "Results: $PASS_COUNT passed, $FAIL_COUNT failed"

if [ "$FAIL_COUNT" -gt 0 ]; then
  echo "FAIL: universal-storage-r2-dev-seed.test.sh"
  exit 1
fi

echo "PASS: universal-storage-r2-dev-seed.test.sh"
