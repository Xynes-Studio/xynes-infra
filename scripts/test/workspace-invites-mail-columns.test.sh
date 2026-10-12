#!/usr/bin/env bash
set -euo pipefail

# Contract test for the MAIL-3 migration.
#
# Validates the SQL file at
# `supabase/migrations/20260601090000_workspace_invites_mail_columns.sql`
# carries every required DDL element from
# `docs/plans/2026-05-30-invite-mail-delivery.md` §7 — without
# needing a running database.
#
# Pattern mirrors `storage-fu-2-fu-2-payload-required-columns.test.sh`:
#   - file-existence + grep-based contract assertions
#   - additive `ADD COLUMN IF NOT EXISTS email_sent_at timestamptz`
#   - additive `ADD COLUMN IF NOT EXISTS email_attempts integer NOT NULL DEFAULT 0`
#   - additive `ADD COLUMN IF NOT EXISTS last_email_error_code text`
#   - reset-safe via `IF NOT EXISTS`
#   - additive-only migration (no `DROP` / `ALTER … DROP` / `TRUNCATE` /
#     `DELETE FROM`)
#   - no raw credentials (defense in depth — this migration touches no
#     credential surface but the regex sweep is uniform across MAIL-*
#     and STORAGE-* migrations)
#   - documentation `COMMENT` on each new column
#   - plan reference embedded in the migration header

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

MIGRATION="./supabase/migrations/20260601090000_workspace_invites_mail_columns.sql"

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

echo "Testing MAIL-3 workspace_invites mail columns migration..."
echo ""

# ---------------------------------------------------------------------------
# File existence
# ---------------------------------------------------------------------------
assert_file_exists "$MIGRATION"

if [ ! -f "$MIGRATION" ]; then
  echo ""
  echo "Results: $PASS_COUNT passed, $FAIL_COUNT failed"
  echo "FAIL: workspace-invites-mail-columns.test.sh (migration file missing)"
  exit 1
fi

# ---------------------------------------------------------------------------
# Schema + extensions (defense-in-depth — match sibling platform.* migrations)
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "CREATE SCHEMA IF NOT EXISTS platform"

# ---------------------------------------------------------------------------
# `email_sent_at` column — additive, nullable, reset-safe
# ---------------------------------------------------------------------------
# Note: nullable BY DESIGN — a NULL value means "no mail has been
# successfully sent for this invite yet" (e.g. pre-MAIL-3 rows, or
# rows where MAIL-5 hasn't run yet, or rows that errored out).
assert_contains "$MIGRATION" "ALTER TABLE platform.workspace_invites"
assert_contains_regex "$MIGRATION" "ADD COLUMN IF NOT EXISTS email_sent_at timestamptz"
# Defense in depth: must NOT be NOT NULL (NULL is the documented "no send yet" state).
assert_not_contains_regex "$MIGRATION" "ADD COLUMN IF NOT EXISTS email_sent_at timestamptz[[:space:]]+NOT[[:space:]]+NULL"

# ---------------------------------------------------------------------------
# `email_attempts` column — additive, non-null with fail-closed default 0
# ---------------------------------------------------------------------------
assert_contains_regex "$MIGRATION" "ADD COLUMN IF NOT EXISTS email_attempts integer NOT NULL DEFAULT 0"
# Defense in depth: the default MUST be 0 so pre-MAIL-3 rows auto-populate to "no attempts made".
assert_not_contains_regex "$MIGRATION" "ADD COLUMN IF NOT EXISTS email_attempts integer NOT NULL DEFAULT [^0]"

# ---------------------------------------------------------------------------
# `last_email_error_code` column — additive, nullable, closed-set surface
# ---------------------------------------------------------------------------
# Nullable BY DESIGN — a NULL value means "no error on the most recent
# send" (including pre-MAIL-3 rows that have never been sent at all).
assert_contains_regex "$MIGRATION" "ADD COLUMN IF NOT EXISTS last_email_error_code text"
# Defense in depth: must NOT be NOT NULL.
assert_not_contains_regex "$MIGRATION" "ADD COLUMN IF NOT EXISTS last_email_error_code text[[:space:]]+NOT[[:space:]]+NULL"

# Reset-safe: all three columns use `IF NOT EXISTS`.
assert_contains_regex "$MIGRATION" "ADD COLUMN IF NOT EXISTS email_sent_at"
assert_contains_regex "$MIGRATION" "ADD COLUMN IF NOT EXISTS email_attempts"
assert_contains_regex "$MIGRATION" "ADD COLUMN IF NOT EXISTS last_email_error_code"

# ---------------------------------------------------------------------------
# Documentation COMMENT on each new column
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "COMMENT ON COLUMN platform.workspace_invites.email_sent_at"
assert_contains "$MIGRATION" "COMMENT ON COLUMN platform.workspace_invites.email_attempts"
assert_contains "$MIGRATION" "COMMENT ON COLUMN platform.workspace_invites.last_email_error_code"

# The `last_email_error_code` COMMENT must call out the closed-set
# surface (the MAIL-2 MailerErrorCode set) so future maintainers don't
# accidentally relax it into an open string column.
assert_contains "$MIGRATION" "MailerError"
# At least one closed-set code name must appear in the COMMENT so the
# value space is clear without consulting MAIL-2 source.
assert_contains_regex "$MIGRATION" "(RECIPIENT_INVALID|PROVIDER_UNAVAILABLE|RATE_LIMITED|TEMPLATE_RENDER_FAILED|PROVIDER_REJECTED)"

# The `email_sent_at` COMMENT must call out the NULL semantic.
assert_contains_regex "$MIGRATION" "COMMENT ON COLUMN platform\\.workspace_invites\\.email_sent_at[[:space:]]+IS"

# The `email_attempts` COMMENT must call out the rate-limit role (MAIL-5
# uses this column for the resend cap).
assert_contains_regex "$MIGRATION" "COMMENT ON COLUMN platform\\.workspace_invites\\.email_attempts[[:space:]]+IS"

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

# No UPDATE on workspace_invites — MAIL-3 is column-add only; MAIL-5
# owns the runtime writes to these columns.
assert_not_contains_regex "$MIGRATION" "UPDATE[[:space:]]+platform\\.workspace_invites"

# ---------------------------------------------------------------------------
# Security invariants — no raw credentials anywhere in the file
# ---------------------------------------------------------------------------
# This migration touches no credential surface — but the regex sweep is
# uniform across STORAGE-* + MAIL-* migrations so we catch a regression
# where a future edit accidentally pastes a key.
for forbidden in raw_key raw_secret access_key_id access_key secret_access_key secret_key r2_token signed_url presigned_url resend_api_key; do
  assert_not_contains_regex "$MIGRATION" "^[[:space:]]+${forbidden}[[:space:]]+(text|uuid|bigint|integer|boolean|timestamptz|date|jsonb)"
done

# No raw credential values masquerading as defaults or fixture data.
assert_not_contains_regex "$MIGRATION" "AKIA[A-Z0-9]+"
assert_not_contains_regex "$MIGRATION" "xynes_live_[a-f0-9]+"
assert_not_contains_regex "$MIGRATION" "X-Amz-Signature"
# Resend key shape (the MAIL-4 redaction target) — defense in depth.
assert_not_contains_regex "$MIGRATION" "re_[A-Za-z0-9]{16,}"

# ---------------------------------------------------------------------------
# Plan reference (every MAIL-* migration links the plan that authored it).
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "docs/plans/2026-05-30-invite-mail-delivery.md"
assert_contains "$MIGRATION" "MAIL-3"

# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
echo ""
echo "Results: $PASS_COUNT passed, $FAIL_COUNT failed"

if [ "$FAIL_COUNT" -gt 0 ]; then
  echo "FAIL: workspace-invites-mail-columns.test.sh"
  exit 1
fi

echo "PASS: workspace-invites-mail-columns.test.sh"
exit 0
