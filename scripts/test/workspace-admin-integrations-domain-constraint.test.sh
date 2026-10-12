#!/usr/bin/env bash
set -euo pipefail

# Live-DB constraint test for `platform.workspace_domains.workspace_domains_hostname_shape`.
#
# Why this test exists
# --------------------
# `workspace-admin-integrations-migration.test.sh` is a string-`grep`
# contract test against the migration file source. It can NOT detect
# semantic regex bugs in CHECK constraints — a regression demonstrated by
# the `'\\*'` POSIX-pattern that rejected every hostname (fixed by
# `20260508120000_fix_workspace_domains_hostname_shape.sql`).
#
# This test inserts real rows under savepoints to verify each shape rule:
#  - `example.com`       → ACCEPT (regression for the asterisk-regex bug)
#  - `EXAMPLE.com`       → REJECT (lowercase constraint)
#  - `https://example.com` → REJECT (`://`)
#  - `example.com/path`  → REJECT (`/`)
#  - `example.com:8080`  → REJECT (`:`)
#  - `*.example.com`     → REJECT (`*`)
#  - `   `               → REJECT (blank)
#
# Behaviour
# ---------
# The test SKIPs (exit 0) when no live database is reachable so it stays
# friendly to environments without a Supabase stack. It runs hard when a
# DB IS reachable so the bug class can never regress silently.
#
# DB safety
# ---------
# Every insert is wrapped in a SAVEPOINT and the outer transaction is
# always rolled back, so this test never persists rows. We also rely on a
# pre-existing workspace + identity user ID for the FK targets — see the
# `workspace_id` / `user_id` discovery block below.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

PASS_COUNT=0
FAIL_COUNT=0

fail() {
  echo "FAIL: $1" >&2
  FAIL_COUNT=$((FAIL_COUNT + 1))
}

pass() {
  PASS_COUNT=$((PASS_COUNT + 1))
}

# ─────────────────────────────────────────────────────────────────────
# Resolve DATABASE_URL
# ─────────────────────────────────────────────────────────────────────

DATABASE_URL_RESOLVED="${DATABASE_URL:-}"

if [ -z "$DATABASE_URL_RESOLVED" ]; then
  for env_file in .env.localhost.local .env.localhost .env.dev.local .env.dev; do
    if [ -f "$env_file" ]; then
      candidate="$(grep -E '^DATABASE_URL=' "$env_file" | head -n1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
      if [ -n "$candidate" ]; then
        DATABASE_URL_RESOLVED="$candidate"
        break
      fi
    fi
  done
fi

if [ -z "$DATABASE_URL_RESOLVED" ]; then
  echo "SKIP: workspace-admin-integrations-domain-constraint.test.sh (no DATABASE_URL and no .env file)"
  exit 0
fi

if ! command -v psql >/dev/null 2>&1; then
  echo "SKIP: workspace-admin-integrations-domain-constraint.test.sh (psql not on PATH)"
  exit 0
fi

# Probe reachability with a 3-second timeout so a misconfigured URL doesn't hang the suite.
if ! PGCONNECT_TIMEOUT=3 psql "$DATABASE_URL_RESOLVED" -c "SELECT 1;" >/dev/null 2>&1; then
  echo "SKIP: workspace-admin-integrations-domain-constraint.test.sh (DB unreachable: ${DATABASE_URL_RESOLVED%%@*}@…)"
  exit 0
fi

# Confirm the table exists (otherwise the migration hasn't been applied).
if ! psql "$DATABASE_URL_RESOLVED" -tAc "SELECT to_regclass('platform.workspace_domains') IS NOT NULL;" 2>/dev/null | grep -q '^t$'; then
  echo "SKIP: workspace-admin-integrations-domain-constraint.test.sh (platform.workspace_domains not present; run migrations first)"
  exit 0
fi

echo "Testing platform.workspace_domains hostname-shape constraint live..."
echo ""

# ─────────────────────────────────────────────────────────────────────
# Discover an existing workspace + user to satisfy FKs
# ─────────────────────────────────────────────────────────────────────

WORKSPACE_ID="$(psql "$DATABASE_URL_RESOLVED" -tAc "SELECT id FROM platform.workspaces ORDER BY created_at LIMIT 1;" 2>/dev/null || true)"
USER_ID="$(psql "$DATABASE_URL_RESOLVED" -tAc "SELECT id FROM identity.users ORDER BY created_at LIMIT 1;" 2>/dev/null || true)"

if [ -z "$WORKSPACE_ID" ] || [ -z "$USER_ID" ]; then
  echo "SKIP: workspace-admin-integrations-domain-constraint.test.sh (no seed workspace + user; run bootstrap-local-db.sh first)"
  exit 0
fi

# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

# assert_insert_accepts <hostname>
#   Runs the insert under a savepoint and rolls it back; a non-zero exit
#   from psql indicates a CHECK violation.
assert_insert_accepts() {
  local hostname="$1"
  local sql
  sql="$(cat <<EOF
BEGIN;
SAVEPOINT s;
INSERT INTO platform.workspace_domains (
  workspace_id, hostname, verification_name, verification_value_hash, created_by
) VALUES (
  '${WORKSPACE_ID}', \$\$${hostname}\$\$, '_xynes.test', 'h', '${USER_ID}'
);
ROLLBACK TO SAVEPOINT s;
ROLLBACK;
EOF
)"
  if psql "$DATABASE_URL_RESOLVED" -v ON_ERROR_STOP=1 -q -c "$sql" >/dev/null 2>&1; then
    echo "  ✓ ACCEPT: $hostname"
    pass
  else
    echo "  ✗ ACCEPT (got rejected): $hostname" >&2
    fail "expected hostname to be accepted: $hostname"
  fi
}

# assert_insert_rejects <hostname> <constraint_name_substring>
#   Runs the insert under a savepoint; expects a CHECK / NOT NULL
#   violation that mentions <constraint_name_substring> so we know the
#   right rule fired.
assert_insert_rejects() {
  local hostname="$1"
  local expected_constraint="$2"
  local sql
  sql="$(cat <<EOF
BEGIN;
SAVEPOINT s;
INSERT INTO platform.workspace_domains (
  workspace_id, hostname, verification_name, verification_value_hash, created_by
) VALUES (
  '${WORKSPACE_ID}', \$\$${hostname}\$\$, '_xynes.test', 'h', '${USER_ID}'
);
ROLLBACK TO SAVEPOINT s;
ROLLBACK;
EOF
)"
  local out
  out="$(psql "$DATABASE_URL_RESOLVED" -v ON_ERROR_STOP=1 -q -c "$sql" 2>&1 || true)"
  if echo "$out" | grep -qiE "violates (check|not-null) constraint"; then
    if echo "$out" | grep -Fq -- "$expected_constraint"; then
      echo "  ✓ REJECT ($expected_constraint): $hostname"
      pass
    else
      echo "  ✗ REJECT (wrong constraint fired for $hostname): $out" >&2
      fail "expected $expected_constraint to fire for $hostname"
    fi
  else
    echo "  ✗ REJECT (insert was accepted): $hostname" >&2
    fail "expected hostname to be rejected: $hostname"
  fi
}

# ─────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────

# Regression for the asterisk-regex bug: a plain hostname must be accepted.
assert_insert_accepts "example.com"
assert_insert_accepts "sub.example.co.uk"
assert_insert_accepts "a.b"

# Lowercase rule.
assert_insert_rejects "EXAMPLE.com"          "workspace_domains_hostname_lower"
assert_insert_rejects "Example.com"          "workspace_domains_hostname_lower"

# Shape rules: each fragment fires the hostname_shape constraint.
assert_insert_rejects "https://example.com"  "workspace_domains_hostname_shape"
assert_insert_rejects "example.com/path"     "workspace_domains_hostname_shape"
assert_insert_rejects "example.com:8080"     "workspace_domains_hostname_shape"
assert_insert_rejects "*.example.com"        "workspace_domains_hostname_shape"

# Blank rule.
assert_insert_rejects "   "                  "workspace_domains_hostname_not_blank"

# ─────────────────────────────────────────────────────────────────────
# Final tally
# ─────────────────────────────────────────────────────────────────────

echo ""
echo "PASS: $PASS_COUNT"
echo "FAIL: $FAIL_COUNT"

if [ "$FAIL_COUNT" -gt 0 ]; then
  exit 1
fi

echo "PASS: workspace-admin-integrations-domain-constraint.test.sh"
