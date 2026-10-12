#!/usr/bin/env bash
set -euo pipefail

# Contract test for the DEDUP-1 schema migration.
#
# Validates the SQL file at
# `supabase/migrations/20260528090000_storage_object_references_and_dedup_index.sql`
# contains every required DDL element from DEDUP-1 of
# `docs/plans/2026-05-27-storage-followups-combined.md` §8
# — without needing a running database.
#
# Pattern mirrors `universal-storage-migration.test.sh`:
#   - file-existence + grep-based contract assertions
#   - reference-counting table with the documented columns + PK
#   - closed-set CHECK constraint on `owner_kind`
#   - workspace-scoped partial unique index on storage_objects
#   - read-path index on storage_object_references.object_id
#   - explicit security invariants: NO raw credential columns,
#     NO foreign key on owner_id, NO non-additive DDL
#   - additive-only migration (no DROP / ALTER … DROP / TRUNCATE / DELETE)
#   - grants consistent with sibling platform migrations
#   - documentation COMMENTs explaining reference-counting + dedup semantics

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

MIGRATION="./supabase/migrations/20260528090000_storage_object_references_and_dedup_index.sql"

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

assert_contains_regex() {
  local path="$1"
  local pattern="$2"
  if grep -Eq -- "$pattern" "$path"; then
    pass
  else
    fail "expected $path to match regex: $pattern"
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

echo "Testing DEDUP-1 content-hash dedup schema migration..."
echo ""

# ---------------------------------------------------------------------------
# File existence
# ---------------------------------------------------------------------------
assert_file_exists "$MIGRATION"

if [ ! -f "$MIGRATION" ]; then
  echo ""
  echo "Results: $PASS_COUNT passed, $FAIL_COUNT failed"
  echo "FAIL: universal-storage-dedup-schema.test.sh (migration file missing)"
  exit 1
fi

# ---------------------------------------------------------------------------
# Schema + extensions
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "CREATE EXTENSION IF NOT EXISTS pgcrypto"
assert_contains "$MIGRATION" "CREATE SCHEMA IF NOT EXISTS platform"

# ---------------------------------------------------------------------------
# Reference-counting table — platform.storage_object_references
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "CREATE TABLE IF NOT EXISTS platform.storage_object_references"

# Required columns
assert_contains_regex "$MIGRATION" "object_id uuid NOT NULL REFERENCES platform\\.storage_objects\\(id\\) ON DELETE CASCADE"
assert_contains_regex "$MIGRATION" "owner_kind text NOT NULL"
assert_contains_regex "$MIGRATION" "owner_id uuid NOT NULL"
assert_contains_regex "$MIGRATION" "created_at timestamptz NOT NULL DEFAULT now\\(\\)"

# Composite PRIMARY KEY
assert_contains "$MIGRATION" "PRIMARY KEY (object_id, owner_kind, owner_id)"

# Closed-set CHECK on owner_kind — kept on a single line for grep matching.
assert_contains "$MIGRATION" "CHECK (owner_kind IN ('cms_entry', 'comment', 'doc_service', 'user_avatar', 'workspace_logo', 'platform_generic'))"

# Each documented owner_kind must appear in the CHECK allowlist.
for owner_kind in cms_entry comment doc_service user_avatar workspace_logo platform_generic; do
  assert_contains "$MIGRATION" "'${owner_kind}'"
done

# ---------------------------------------------------------------------------
# Index on storage_object_references.object_id (read-path)
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "CREATE INDEX IF NOT EXISTS storage_object_references_object_idx"
assert_contains "$MIGRATION" "ON platform.storage_object_references (object_id)"

# ---------------------------------------------------------------------------
# Workspace-scoped partial unique index on storage_objects(workspace_id, sha256)
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "CREATE UNIQUE INDEX IF NOT EXISTS storage_objects_workspace_sha256_uidx"
assert_contains "$MIGRATION" "ON platform.storage_objects (workspace_id, sha256)"
assert_contains "$MIGRATION" "WHERE sha256 IS NOT NULL AND status IN ('uploaded', 'processing', 'ready')"

# Each active status appears in the predicate.
for active_status in 'uploaded' 'processing' 'ready'; do
  assert_contains "$MIGRATION" "${active_status}"
done

# The predicate MUST exclude terminal statuses (pending_upload / failed / deleted)
# from the unique constraint so retries after abort / failure / soft-delete
# do not collide with a stale row. Verified indirectly: the predicate clause
# names ONLY the three active statuses, and a regex that matches the active
# status `IN` clause containing any terminal status would fail.
assert_not_contains_regex "$MIGRATION" "status IN \\([^)]*pending_upload[^)]*\\)\\s*$"
assert_not_contains_regex "$MIGRATION" "WHERE sha256 IS NOT NULL AND status IN \\([^)]*'failed'"
assert_not_contains_regex "$MIGRATION" "WHERE sha256 IS NOT NULL AND status IN \\([^)]*'deleted'"

# ---------------------------------------------------------------------------
# Workspace-scoping invariant (tenant isolation)
# ---------------------------------------------------------------------------
# The unique index MUST key on (workspace_id, sha256). A unique index keyed
# on (sha256) alone would be a cross-tenant leak.
assert_not_contains_regex "$MIGRATION" "CREATE UNIQUE INDEX[^(]*\\(sha256\\)"

# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "GRANT SELECT, INSERT, UPDATE, DELETE ON platform.storage_object_references TO postgres"

# ---------------------------------------------------------------------------
# Documentation COMMENTs
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "COMMENT ON TABLE platform.storage_object_references"
assert_contains "$MIGRATION" "COMMENT ON COLUMN platform.storage_object_references.owner_kind"
assert_contains "$MIGRATION" "COMMENT ON COLUMN platform.storage_object_references.owner_id"
assert_contains "$MIGRATION" "COMMENT ON INDEX platform.storage_objects_workspace_sha256_uidx"

# The owner_id comment MUST document the NO-FK choice + upstream validation rule.
assert_contains "$MIGRATION" "NOT a foreign key"

# ---------------------------------------------------------------------------
# Security invariants — no raw credentials, no forbidden columns
# ---------------------------------------------------------------------------
# Same forbidden-column list as the STORAGE-2 migration test. None of these
# may appear in a DEDUP-1 column declaration.
for forbidden in raw_key raw_secret access_key_id access_key secret_access_key secret_key r2_token signed_url presigned_url provider_credentials; do
  # Match `forbidden TYPE` column declarations only — we tolerate the name
  # appearing inside a security-comment paragraph.
  assert_not_contains_regex "$MIGRATION" "^[[:space:]]+${forbidden}[[:space:]]+(text|uuid|bigint|integer|boolean|timestamptz|date)"
done

# No raw credential values masquerading as defaults.
assert_not_contains_regex "$MIGRATION" "AKIA[A-Z0-9]+"
assert_not_contains_regex "$MIGRATION" "xynes_live_[a-f0-9]+"
assert_not_contains_regex "$MIGRATION" "X-Amz-Signature"

# ---------------------------------------------------------------------------
# NO foreign key on owner_id (cross-schema validation lives in handlers)
# ---------------------------------------------------------------------------
# The `owner_id` declaration MUST NOT carry a REFERENCES clause; doing so
# would force all owner kinds into a single schema and break the design.
assert_not_contains_regex "$MIGRATION" "owner_id uuid NOT NULL REFERENCES"

# ---------------------------------------------------------------------------
# Additive-only invariant
# ---------------------------------------------------------------------------
# No DROP TABLE / COLUMN / CONSTRAINT / INDEX / SCHEMA / TYPE.
assert_not_contains_regex "$MIGRATION" "(^|[^a-z_])DROP[[:space:]]+(TABLE|COLUMN|CONSTRAINT|INDEX|SCHEMA|TYPE)"

# No ALTER TABLE … DROP …
assert_not_contains_regex "$MIGRATION" "ALTER[[:space:]]+TABLE[[:space:]]+\\S+[[:space:]]+DROP"

# No TRUNCATE.
assert_not_contains_regex "$MIGRATION" "(^|[^a-z_])TRUNCATE[[:space:]]+"

# No DELETE FROM (data mutation).
assert_not_contains_regex "$MIGRATION" "(^|[^a-z_])DELETE[[:space:]]+FROM[[:space:]]+"

# Index uses IF NOT EXISTS so reset is idempotent.
assert_contains "$MIGRATION" "CREATE UNIQUE INDEX IF NOT EXISTS storage_objects_workspace_sha256_uidx"
assert_contains "$MIGRATION" "CREATE TABLE IF NOT EXISTS platform.storage_object_references"
assert_contains "$MIGRATION" "CREATE INDEX IF NOT EXISTS storage_object_references_object_idx"

# ---------------------------------------------------------------------------
# Pre-index reconciliation block (Codex P1 — handle legacy duplicates)
# ---------------------------------------------------------------------------
# The migration MUST demote pre-existing duplicate `(workspace_id, sha256)`
# rows BEFORE creating the partial unique index, otherwise the
# `CREATE UNIQUE INDEX` aborts on any environment that already accumulated
# legacy duplicates (which is exactly Bug 2's signature). The reconciliation
# is a soft-delete (status='deleted', deleted_at=now()), keeping the oldest
# row per group as the dedup winner. See migration §"Pre-index reconciliation".
assert_contains "$MIGRATION" "Pre-index reconciliation"
assert_contains_regex "$MIGRATION" "ROW_NUMBER\\(\\)[[:space:]]+OVER[[:space:]]*\\("
assert_contains_regex "$MIGRATION" "PARTITION[[:space:]]+BY[[:space:]]+workspace_id,[[:space:]]+sha256"
assert_contains_regex "$MIGRATION" "ORDER[[:space:]]+BY[[:space:]]+created_at[[:space:]]+ASC"
assert_contains "$MIGRATION" "DEDUP_RECONCILED"
# The reconciliation MUST run BEFORE the partial unique index — assert
# byte offsets in the file.
RECONCILE_LINE=$(grep -n "^WITH ranked_duplicates AS" "$MIGRATION" | head -n1 | cut -d: -f1)
INDEX_LINE=$(grep -n "^CREATE UNIQUE INDEX IF NOT EXISTS storage_objects_workspace_sha256_uidx" "$MIGRATION" | head -n1 | cut -d: -f1)
if [ -n "$RECONCILE_LINE" ] && [ -n "$INDEX_LINE" ] && [ "$RECONCILE_LINE" -lt "$INDEX_LINE" ]; then
  pass
else
  fail "expected reconciliation CTE (line $RECONCILE_LINE) to appear BEFORE the partial unique index creation (line $INDEX_LINE)"
fi
# Winner selection MUST use `rn > 1` (keep rn=1 = the oldest = winner).
assert_contains_regex "$MIGRATION" "WHERE[[:space:]]+rn[[:space:]]*>[[:space:]]*1"
# Soft-delete must set deleted_at to satisfy the STORAGE-2 consistency CHECK.
assert_contains_regex "$MIGRATION" "deleted_at[[:space:]]*=[[:space:]]*now\\(\\)"

# ---------------------------------------------------------------------------
# Plan reference (every storage migration links the plan that authored it).
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "docs/plans/2026-05-27-storage-followups-combined.md"
assert_contains "$MIGRATION" "DEDUP-1"

# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
echo ""
echo "Results: $PASS_COUNT passed, $FAIL_COUNT failed"

if [ "$FAIL_COUNT" -gt 0 ]; then
  echo "FAIL: universal-storage-dedup-schema.test.sh"
  exit 1
fi

echo "PASS: universal-storage-dedup-schema.test.sh"
exit 0
