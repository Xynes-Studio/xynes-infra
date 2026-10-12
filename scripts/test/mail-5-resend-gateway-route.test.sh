#!/usr/bin/env bash
set -euo pipefail

# MAIL-5 — Contract test for the `accounts.invites.resend` gateway
# route seed.
#
# Scope:
#   - Validates that the dedicated forward-only seed migration
#     `20260603090000_seed_platform_invite_resend_route.sql` contains
#     the single new resend route required by MAIL-5.
#   - Validates the baseline seed `20251229100001_seed_platform_routes.sql`
#     does NOT carry the resend row (regression guard against the
#     Codex-flagged "edit the already-applied baseline seed" anti-pattern,
#     PR #89 review on 2026-06-03).
#   - Validates route configuration invariants:
#       * Targets accounts-service
#       * Workspace-scoped
#       * Non-public (requires auth)
#       * Enabled
#       * action_key === 'accounts.invites.resend'
#       * HTTP method POST
#   - Validates idempotent ON CONFLICT clause is preserved.
#   - Cross-references the action key against the authz permission
#     catalog when the sibling repo is available.
#   - Sweeps for forbidden secret patterns.
#
# Runs without a database or running services — static contract
# check. Modeled on workspace-admin-integrations-gateway-routes.test.sh
# and the precedent forward-only migration from BUG-WAU-1
# (20260508130000_seed_platform_domain_regenerate_route.sql).

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# Canonical location for the resend route — a NEW forward-only seed
# migration. Editing the already-applied baseline seed would not seed
# the row on any environment that has already bootstrapped (see the
# header comment in the migration for the full rationale).
SEED_FILE="./supabase/migrations/20260603090000_seed_platform_invite_resend_route.sql"

# Baseline seed file. We assert this file does NOT carry the resend
# row so future contributors don't silently regress back to the
# anti-pattern.
BASELINE_SEED_FILE="./supabase/migrations/20251229100001_seed_platform_routes.sql"

AUTHZ_PERMISSIONS_FILE="../xynes-authz-service/src/db/seed/permissions.config.ts"

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

assert_eq() {
  local expected="$1"
  local actual="$2"
  local msg="$3"
  if [ "$expected" = "$actual" ]; then
    pass
  else
    fail "$msg: expected $expected, got $actual"
  fi
}

echo "MAIL-5 — testing accounts.invites.resend gateway route seed..."
echo ""

# ---------------------------------------------------------------------------
# File existence
# ---------------------------------------------------------------------------
assert_file_exists "$SEED_FILE"
assert_file_exists "$BASELINE_SEED_FILE"

# ---------------------------------------------------------------------------
# Anti-regression: baseline seed must NOT carry the resend row.
#
# Supabase tracks migrations by filename; editing 20251229100001 only
# seeds fresh DBs and silently misses every previously-bootstrapped
# environment. This guard fails the build if a future contributor
# re-introduces the anti-pattern that Codex flagged in PR #89.
# ---------------------------------------------------------------------------
echo "  Checking baseline seed is free of the resend row..."

BASELINE_RESEND_COUNT=$(grep -c "accounts\.invites\.resend" "$BASELINE_SEED_FILE" || true)
assert_eq "0" "$BASELINE_RESEND_COUNT" \
  "baseline seed $BASELINE_SEED_FILE must NOT contain accounts.invites.resend (use the forward-only migration $SEED_FILE instead)"

# Sanity: the baseline must STILL carry the pre-MAIL-5 invite routes —
# the revert must only have removed the new line, nothing else.
assert_contains "$BASELINE_SEED_FILE" "'accounts.invites.create'"
assert_contains "$BASELINE_SEED_FILE" "'accounts.invites.resolve'"
assert_contains "$BASELINE_SEED_FILE" "'accounts.invites.accept'"

echo "    ✓ baseline seed clean; pre-MAIL-5 invite routes intact"

# ---------------------------------------------------------------------------
# Resend route presence — exactly one row for accounts.invites.resend
# ---------------------------------------------------------------------------
echo "  Checking resend route presence..."

RESEND_ROW_COUNT=$(grep -c "'accounts\.invites\.resend'" "$SEED_FILE" || true)
assert_eq "1" "$RESEND_ROW_COUNT" "accounts.invites.resend row count"

echo "    ✓ exactly one accounts.invites.resend row present"

# ---------------------------------------------------------------------------
# Path pattern + verb
# ---------------------------------------------------------------------------
echo "  Checking path pattern and verb..."

# Workspace-scoped path so the gateway can extract :workspaceId from
# the URL. We deviated from the plan §9.3 string
# `/workspace-invites/:inviteId/resend` because that pattern has no
# workspaceId segment and cannot be workspace-scoped through the
# gateway.
assert_contains "$SEED_FILE" "'/workspaces/:workspaceId/invites/:inviteId/resend'"

# HTTP verb
assert_contains "$SEED_FILE" "('POST', '/workspaces/:workspaceId/invites/:inviteId/resend'"

echo "    ✓ workspace-scoped POST /workspaces/:workspaceId/invites/:inviteId/resend"

# ---------------------------------------------------------------------------
# Service routing — must target accounts-service
# ---------------------------------------------------------------------------
echo "  Checking service routing..."

# The full row must target 'accounts-service'. We grep the full INSERT
# tuple to verify both the action_key and the service_key live on the
# same row (defense in depth — a stray service_key elsewhere with the
# same action_key would fool a simple contains check).
RESEND_ON_ACCOUNTS=$(grep -cE "'accounts.invites.resend'.*'accounts-service'|'accounts-service'.*'accounts.invites.resend'" "$SEED_FILE" || true)
assert_eq "1" "$RESEND_ON_ACCOUNTS" "resend row must target accounts-service"

# Defense in depth — the resend action key must NEVER target any
# other backend service.
assert_not_contains "$SEED_FILE" "cms-core.*accounts.invites.resend"
assert_not_contains "$SEED_FILE" "doc-service.*accounts.invites.resend"
assert_not_contains "$SEED_FILE" "telemetry-service.*accounts.invites.resend"
assert_not_contains "$SEED_FILE" "storage-service.*accounts.invites.resend"
assert_not_contains "$SEED_FILE" "authz-service.*accounts.invites.resend"

echo "    ✓ resend route targets accounts-service exclusively"

# ---------------------------------------------------------------------------
# Workspace scoping + auth requirement
# ---------------------------------------------------------------------------
echo "  Checking workspace scoping + auth..."

# Tuple shape:
#   (method, path, service, target, workspace_scoped, action_key, is_public, enabled)
#
# Expected for the resend row: workspace_scoped=true, is_public=false,
# enabled=true. We verify by grepping for the action_key + the
# trailing flag pair.
assert_contains "$SEED_FILE" "'accounts.invites.resend', false, true)"

# Cross-check: the resend route must NOT be marked public.
assert_not_contains "$SEED_FILE" "'accounts.invites.resend', true, true)"
assert_not_contains "$SEED_FILE" "'accounts.invites.resend', true, false)"
# And it must NOT be marked disabled.
assert_not_contains "$SEED_FILE" "'accounts.invites.resend', false, false)"

echo "    ✓ workspace-scoped, non-public, enabled"

# ---------------------------------------------------------------------------
# ON CONFLICT clause preserved — must UPSERT on replay
# ---------------------------------------------------------------------------
echo "  Checking ON CONFLICT idempotency..."

assert_contains "$SEED_FILE" "ON CONFLICT (method, path_pattern) DO UPDATE SET"
assert_contains "$SEED_FILE" "service_key = EXCLUDED.service_key"
assert_contains "$SEED_FILE" "action_key = EXCLUDED.action_key"

echo "    ✓ ON CONFLICT clause preserved"

# ---------------------------------------------------------------------------
# Security — no raw secrets, no placeholder values
# ---------------------------------------------------------------------------
echo "  Checking security invariants..."

assert_not_contains "$SEED_FILE" "raw_key"

# Resend API key shape (re_<base64>) — MAIL-4 redaction target. Should
# never appear in any route seed.
if grep -E "re_[A-Za-z0-9]{8,}" "$SEED_FILE" > /dev/null 2>&1; then
  fail "seed file contains a possible Resend API key shape (re_...)"
else
  pass
fi

# AWS access key shape — defense in depth.
if grep -E "AKIA[A-Z0-9]{8,}" "$SEED_FILE" > /dev/null 2>&1; then
  fail "seed file contains a possible AWS access key shape (AKIA...)"
else
  pass
fi

echo "    ✓ no raw secrets or placeholder credentials"

# ---------------------------------------------------------------------------
# Cross-reference: action key in authz permission catalog
# ---------------------------------------------------------------------------
echo "  Checking authz permission catalog cross-reference..."

if [ -f "$AUTHZ_PERMISSIONS_FILE" ]; then
  if grep -Fq "key: \"accounts.invites.resend\"" "$AUTHZ_PERMISSIONS_FILE"; then
    pass
    echo "    ✓ accounts.invites.resend present in authz permission catalog"
  else
    fail "accounts.invites.resend in route seed but missing from authz catalog at $AUTHZ_PERMISSIONS_FILE"
  fi
else
  echo "    (skipping authz cross-reference: $AUTHZ_PERMISSIONS_FILE not found)"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "PASS: $PASS_COUNT"
echo "FAIL: $FAIL_COUNT"

if [ "$FAIL_COUNT" -ne 0 ]; then
  echo ""
  echo "FAIL: MAIL-5 gateway route seed contract test"
  exit 1
fi

echo ""
echo "All MAIL-5 resend route contract checks passed."
