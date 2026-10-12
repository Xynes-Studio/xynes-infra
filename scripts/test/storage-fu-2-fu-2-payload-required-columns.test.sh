#!/usr/bin/env bash
set -euo pipefail

# Contract test for the STORAGE-FU-2-FU-2 migration.
#
# Validates the SQL file at
# `supabase/migrations/20260529090000_storage_processing_jobs_payload_and_required.sql`
# carries every required DDL element from
# `docs/plans/2026-05-27-storage-followups-combined.md` §14 — without
# needing a running database.
#
# Pattern mirrors `storage-fu-2-fu-1-active-jobs-index.test.sh`:
#   - file-existence + grep-based contract assertions
#   - additive `ADD COLUMN IF NOT EXISTS payload jsonb NOT NULL DEFAULT '{}'::jsonb`
#   - additive `ADD COLUMN IF NOT EXISTS required boolean NOT NULL DEFAULT true`
#   - reset-safe via `IF NOT EXISTS`
#   - backfill UPDATE flips ONLY the 4 known non-required job kinds
#   - additive-only migration (no `DROP` / `ALTER … DROP` / `TRUNCATE` /
#     `DELETE FROM`)
#   - no raw credentials (defense in depth — this migration touches no
#     credential surface but the regex sweep is uniform across STORAGE-*
#     migrations)
#   - documentation `COMMENT` on each new column
#   - plan reference embedded in the migration header

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

MIGRATION="./supabase/migrations/20260529090000_storage_processing_jobs_payload_and_required.sql"

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

echo "Testing STORAGE-FU-2-FU-2 payload + required columns migration..."
echo ""

# ---------------------------------------------------------------------------
# File existence
# ---------------------------------------------------------------------------
assert_file_exists "$MIGRATION"

if [ ! -f "$MIGRATION" ]; then
  echo ""
  echo "Results: $PASS_COUNT passed, $FAIL_COUNT failed"
  echo "FAIL: storage-fu-2-fu-2-payload-required-columns.test.sh (migration file missing)"
  exit 1
fi

# ---------------------------------------------------------------------------
# Schema + extensions (defense-in-depth — match sibling STORAGE-* migrations)
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "CREATE EXTENSION IF NOT EXISTS pgcrypto"
assert_contains "$MIGRATION" "CREATE SCHEMA IF NOT EXISTS platform"

# ---------------------------------------------------------------------------
# `payload` column — additive, fail-closed default, reset-safe
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "ALTER TABLE platform.storage_processing_jobs"
assert_contains "$MIGRATION" "ADD COLUMN IF NOT EXISTS payload jsonb NOT NULL DEFAULT '{}'::jsonb"

# ---------------------------------------------------------------------------
# `required` column — additive, fail-closed default `true`, reset-safe
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "ADD COLUMN IF NOT EXISTS required boolean NOT NULL DEFAULT true"
# fail-closed contract: the default MUST be `true` so unknown / new job
# kinds are treated as required by default and cannot silently dead-letter.
assert_not_contains_regex "$MIGRATION" "ADD COLUMN IF NOT EXISTS required boolean NOT NULL DEFAULT false"

# Both columns are `IF NOT EXISTS` so reset is replay-safe.
assert_contains_regex "$MIGRATION" "ADD COLUMN IF NOT EXISTS payload"
assert_contains_regex "$MIGRATION" "ADD COLUMN IF NOT EXISTS required"

# ---------------------------------------------------------------------------
# Backfill — flip ONLY the 4 known non-required job kinds to `required=false`
# ---------------------------------------------------------------------------
# The UPDATE must reference all four documented non-required kinds and
# none of the required-by-default kinds.
assert_contains "$MIGRATION" "UPDATE platform.storage_processing_jobs"
assert_contains "$MIGRATION" "SET required = false"

# All four non-required kinds present in the WHERE IN clause.
for non_required_kind in 'image_optimize' 'video_thumbnail' 'video_transcode' 'document_preview'; do
  assert_contains "$MIGRATION" "$non_required_kind"
done

# Defense in depth: the backfill MUST NOT flip required-by-default
# kinds (`scan_validation`, `video_probe`) to false. A regex sweep
# would be too noisy because both names appear in the documentation
# header; assert the backfill SET clause is exactly `required = false`
# and does NOT carry a `true` variant.
assert_not_contains_regex "$MIGRATION" "SET[[:space:]]+required[[:space:]]*=[[:space:]]*true"

# Idempotency guard: replay must be a strict no-op. The WHERE clause
# must include an `IS DISTINCT FROM false` (or equivalent) guard so
# a re-run touches zero rows.
assert_contains_regex "$MIGRATION" "required[[:space:]]+IS[[:space:]]+DISTINCT[[:space:]]+FROM[[:space:]]+false"

# ---------------------------------------------------------------------------
# Documentation COMMENT on each new column
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "COMMENT ON COLUMN platform.storage_processing_jobs.payload"
assert_contains "$MIGRATION" "COMMENT ON COLUMN platform.storage_processing_jobs.required"
# The payload COMMENT must call out the security contract so future
# maintainers don't accidentally relax the validator.
assert_contains "$MIGRATION" "credentials"
assert_contains "$MIGRATION" "Zod"
# The required COMMENT must call out the fail-closed default semantic.
assert_contains "$MIGRATION" "fail-closed"

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
  assert_not_contains_regex "$MIGRATION" "^[[:space:]]+${forbidden}[[:space:]]+(text|uuid|bigint|integer|boolean|timestamptz|date|jsonb)"
done

# No raw credential values masquerading as defaults or fixture data.
assert_not_contains_regex "$MIGRATION" "AKIA[A-Z0-9]+"
assert_not_contains_regex "$MIGRATION" "xynes_live_[a-f0-9]+"
assert_not_contains_regex "$MIGRATION" "X-Amz-Signature"

# ---------------------------------------------------------------------------
# Plan reference (every storage migration links the plan that authored it).
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "docs/plans/2026-05-27-storage-followups-combined.md"
assert_contains "$MIGRATION" "STORAGE-FU-2-FU-2"

# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
echo ""
echo "Results: $PASS_COUNT passed, $FAIL_COUNT failed"

if [ "$FAIL_COUNT" -gt 0 ]; then
  echo "FAIL: storage-fu-2-fu-2-payload-required-columns.test.sh"
  exit 1
fi

echo "PASS: storage-fu-2-fu-2-payload-required-columns.test.sh"
exit 0
