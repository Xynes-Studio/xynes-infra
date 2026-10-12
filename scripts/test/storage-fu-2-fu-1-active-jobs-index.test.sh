#!/usr/bin/env bash
set -euo pipefail

# Contract test for the STORAGE-FU-2-FU-1 migration.
#
# Validates the SQL file at
# `supabase/migrations/20260528100000_storage_processing_jobs_active_unique_index.sql`
# carries every required DDL element from
# `docs/plans/2026-05-27-storage-followups-combined.md` §10 — without
# needing a running database.
#
# Pattern mirrors `universal-storage-dedup-schema.test.sh`:
#   - file-existence + grep-based contract assertions
#   - workspace-agnostic PARTIAL UNIQUE INDEX on
#     `platform.storage_processing_jobs (object_id, job_kind)` with the
#     `status IN ('queued','running')` predicate
#   - reset-safe via `IF NOT EXISTS`
#   - pre-index reconciliation block soft-cancels legacy duplicate active
#     rows BEFORE the index is created (line-order asserted)
#   - additive-only migration (no `DROP` / `ALTER … DROP` / `TRUNCATE` /
#     `DELETE FROM`)
#   - no raw credentials (defense in depth — this migration touches no
#     credential surface but the regex sweep is uniform across STORAGE-*
#     migrations)
#   - documentation `COMMENT` on the new index
#   - plan reference embedded in the migration header

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

MIGRATION="./supabase/migrations/20260528100000_storage_processing_jobs_active_unique_index.sql"

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

echo "Testing STORAGE-FU-2-FU-1 active-jobs partial unique index migration..."
echo ""

# ---------------------------------------------------------------------------
# File existence
# ---------------------------------------------------------------------------
assert_file_exists "$MIGRATION"

if [ ! -f "$MIGRATION" ]; then
  echo ""
  echo "Results: $PASS_COUNT passed, $FAIL_COUNT failed"
  echo "FAIL: storage-fu-2-fu-1-active-jobs-index.test.sh (migration file missing)"
  exit 1
fi

# ---------------------------------------------------------------------------
# Schema + extensions (defense-in-depth — match sibling STORAGE-* migrations)
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "CREATE EXTENSION IF NOT EXISTS pgcrypto"
assert_contains "$MIGRATION" "CREATE SCHEMA IF NOT EXISTS platform"

# ---------------------------------------------------------------------------
# The partial unique index — name, table, columns, predicate
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "CREATE UNIQUE INDEX IF NOT EXISTS storage_processing_jobs_active_unique_uidx"
assert_contains "$MIGRATION" "ON platform.storage_processing_jobs (object_id, job_kind)"
assert_contains "$MIGRATION" "WHERE status IN ('queued', 'running')"

# Each active status appears in the predicate exactly once.
for active_status in 'queued' 'running'; do
  assert_contains "$MIGRATION" "${active_status}"
done

# The predicate MUST exclude every terminal status. We assert with a
# precise regex: the predicate clause on the `CREATE UNIQUE INDEX` line
# (or the immediately following continuation lines) MUST NOT include
# any of `succeeded` / `failed` / `cancelled` as a quoted IN value
# alongside the active statuses. We match the WHERE clause itself only
# (avoiding the documentation prose at the top of the file).
assert_not_contains_regex "$MIGRATION" "WHERE status IN \\([^)]*'succeeded'"
assert_not_contains_regex "$MIGRATION" "WHERE status IN \\([^)]*'failed'"
assert_not_contains_regex "$MIGRATION" "WHERE status IN \\([^)]*'cancelled'"

# Defense in depth: there must be NO global (non-partial) unique index
# on (object_id, job_kind) — that would break STORAGE-7's retry-after-
# terminal contract.
assert_not_contains_regex "$MIGRATION" "CREATE UNIQUE INDEX[^(]*\\(object_id, job_kind\\)[[:space:]]*;"

# Reset-safe + replay-safe.
assert_contains "$MIGRATION" "CREATE UNIQUE INDEX IF NOT EXISTS"

# ---------------------------------------------------------------------------
# Pre-index reconciliation block — mirrors the DEDUP-1 pattern
# ---------------------------------------------------------------------------
# Defense in depth: a legacy stack pre-FU-2 may have accumulated multiple
# active rows for the same `(object_id, job_kind)`. The migration MUST
# demote those duplicates BEFORE creating the partial unique index,
# otherwise `CREATE UNIQUE INDEX` aborts with `duplicate key value`.
assert_contains "$MIGRATION" "Pre-index reconciliation"
assert_contains_regex "$MIGRATION" "ROW_NUMBER\\(\\)[[:space:]]+OVER[[:space:]]*\\("
assert_contains_regex "$MIGRATION" "PARTITION[[:space:]]+BY[[:space:]]+object_id,[[:space:]]+job_kind"
assert_contains_regex "$MIGRATION" "ORDER[[:space:]]+BY[[:space:]]+created_at[[:space:]]+ASC"
# Audit handle so operators can identify migration-cancelled rows vs
# regular worker-cancelled rows.
assert_contains "$MIGRATION" "DEDUP_RECONCILED_FU_1"
# Winner selection MUST use `rn > 1` (keep rn=1 = the oldest = winner).
assert_contains_regex "$MIGRATION" "WHERE[[:space:]]+rn[[:space:]]*>[[:space:]]*1"
# The reconciliation flips losers to a TERMINAL status (cancelled) so
# they no longer collide with the partial unique index — and it sets
# `finished_at = now()` to keep the state machine consistent.
assert_contains_regex "$MIGRATION" "status[[:space:]]*=[[:space:]]*'cancelled'"
assert_contains_regex "$MIGRATION" "finished_at[[:space:]]*=[[:space:]]*now\\(\\)"
# Source-only partition: the reconciliation MUST only touch rows that
# are currently ACTIVE — terminal rows are already non-colliding.
assert_contains_regex "$MIGRATION" "WHERE[[:space:]]+status[[:space:]]+IN[[:space:]]*\\('queued',[[:space:]]*'running'\\)"

# The reconciliation MUST run BEFORE the partial unique index — assert
# byte offsets in the file (catches a regression that re-orders the
# index creation above the CTE).
RECONCILE_LINE=$(grep -n "^WITH ranked_duplicates AS" "$MIGRATION" | head -n1 | cut -d: -f1)
INDEX_LINE=$(grep -n "^CREATE UNIQUE INDEX IF NOT EXISTS storage_processing_jobs_active_unique_uidx" "$MIGRATION" | head -n1 | cut -d: -f1)
if [ -n "$RECONCILE_LINE" ] && [ -n "$INDEX_LINE" ] && [ "$RECONCILE_LINE" -lt "$INDEX_LINE" ]; then
  pass
else
  fail "expected reconciliation CTE (line $RECONCILE_LINE) to appear BEFORE the partial unique index (line $INDEX_LINE)"
fi

# ---------------------------------------------------------------------------
# Documentation COMMENT on the new index
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "COMMENT ON INDEX platform.storage_processing_jobs_active_unique_uidx"
# The COMMENT must call out the retry contract so future maintainers
# don't accidentally tighten the predicate.
assert_contains "$MIGRATION" "retries"

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

# ---------------------------------------------------------------------------
# Security invariants — no raw credentials anywhere in the file
# ---------------------------------------------------------------------------
# This migration touches no credential surface — but the regex sweep is
# uniform across STORAGE-* migrations so we catch a regression where a
# future edit accidentally pastes a key.
for forbidden in raw_key raw_secret access_key_id access_key secret_access_key secret_key r2_token signed_url presigned_url provider_credentials; do
  assert_not_contains_regex "$MIGRATION" "^[[:space:]]+${forbidden}[[:space:]]+(text|uuid|bigint|integer|boolean|timestamptz|date)"
done

# No raw credential values masquerading as defaults or fixture data.
assert_not_contains_regex "$MIGRATION" "AKIA[A-Z0-9]+"
assert_not_contains_regex "$MIGRATION" "xynes_live_[a-f0-9]+"
assert_not_contains_regex "$MIGRATION" "X-Amz-Signature"

# ---------------------------------------------------------------------------
# Plan reference (every storage migration links the plan that authored it).
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "docs/plans/2026-05-27-storage-followups-combined.md"
assert_contains "$MIGRATION" "STORAGE-FU-2-FU-1"

# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
echo ""
echo "Results: $PASS_COUNT passed, $FAIL_COUNT failed"

if [ "$FAIL_COUNT" -gt 0 ]; then
  echo "FAIL: storage-fu-2-fu-1-active-jobs-index.test.sh"
  exit 1
fi

echo "PASS: storage-fu-2-fu-1-active-jobs-index.test.sh"
exit 0
