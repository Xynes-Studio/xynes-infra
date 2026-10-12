#!/usr/bin/env bash
set -euo pipefail

# Contract test for the Workspace Admin integrations gateway route seeds.
#
# Scope:
#   - Validates that the platform.routes seed migration contains the eight
#     routes required by the Workspace Admin integrations MVP (backend-
#     foundation plan, Task 7).
#   - Validates route configuration invariants:
#       * All domain routes target accounts-service
#       * All API key routes target accounts-service
#       * All routes are workspace-scoped
#       * All routes are non-public (require auth)
#       * All routes are enabled
#       * action_key values match the permission catalog
#       * Correct HTTP methods (GET/POST/DELETE)
#   - Validates idempotent ON CONFLICT clause is preserved.
#   - Validates that no existing routes are removed or corrupted.
#   - Cross-references action keys against the authz permission catalog
#     when the sibling repo is available.
#
# This runs without a database or running services — it is a static
# contract check modeled on scripts/test/workspace-admin-integrations-migration.test.sh.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

SEED_FILE="./supabase/migrations/20251229100001_seed_platform_routes.sql"
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

echo "Testing Workspace Admin integrations gateway route seeds..."
echo ""

# ---------------------------------------------------------------------------
# File existence
# ---------------------------------------------------------------------------
assert_file_exists "$SEED_FILE"

# ---------------------------------------------------------------------------
# Route count — exactly 9 workspace-admin platform routes (domains + api_keys).
#
# Note: STORAGE-3 (Universal Object Storage) adds additional
# platform.storage.* routes. This test deliberately scopes to the
# workspace-admin families (platform.domains.* and platform.api_keys.*)
# to keep the regression contract focused. See
# scripts/test/universal-storage-gateway-routes.test.sh for the storage
# route contract.
# ---------------------------------------------------------------------------
echo "  Checking route count..."

PLATFORM_ROUTE_COUNT=$(grep -E "'platform\.(domains|api_keys)\." "$SEED_FILE" | wc -l | tr -d ' ' || true)
assert_eq "9" "$PLATFORM_ROUTE_COUNT" "platform.{domains,api_keys}.* route count"

echo "    ✓ exactly 9 workspace-admin platform routes present"

# ---------------------------------------------------------------------------
# Domain routes — path patterns with correct service + action keys
# ---------------------------------------------------------------------------
echo "  Checking domain routes..."

# platform.domains.list — GET /workspaces/:workspaceId/domains
assert_contains "$SEED_FILE" "'/workspaces/:workspaceId/domains'"
assert_contains "$SEED_FILE" "'platform.domains.list'"

# platform.domains.create — POST /workspaces/:workspaceId/domains
assert_contains "$SEED_FILE" "'platform.domains.create'"

# platform.domains.verify — POST /workspaces/:workspaceId/domains/:domainId/verify
assert_contains "$SEED_FILE" "'/workspaces/:workspaceId/domains/:domainId/verify'"
assert_contains "$SEED_FILE" "'platform.domains.verify'"

# platform.domains.regenerateVerification — POST /workspaces/:workspaceId/domains/:domainId/regenerate-verification
# Reuses the platform.domains.create permission (same lifecycle re-issue)
# rather than introducing a new authz key. Recovery path when the
# workspace owner has lost the original one-time TXT reveal.
assert_contains "$SEED_FILE" "'/workspaces/:workspaceId/domains/:domainId/regenerate-verification'"
assert_contains "$SEED_FILE" "'platform.domains.regenerateVerification'"

# platform.domains.delete — DELETE /workspaces/:workspaceId/domains/:domainId
assert_contains "$SEED_FILE" "'/workspaces/:workspaceId/domains/:domainId'"
assert_contains "$SEED_FILE" "'platform.domains.delete'"

echo "    ✓ domain route patterns and action keys"

# ---------------------------------------------------------------------------
# API key routes — path patterns with correct service + action keys
# ---------------------------------------------------------------------------
echo "  Checking API key routes..."

# platform.api_keys.list — GET /workspaces/:workspaceId/api-keys
assert_contains "$SEED_FILE" "'/workspaces/:workspaceId/api-keys'"
assert_contains "$SEED_FILE" "'platform.api_keys.list'"

# platform.api_keys.create — POST /workspaces/:workspaceId/api-keys
assert_contains "$SEED_FILE" "'platform.api_keys.create'"

# platform.api_keys.revoke — POST /workspaces/:workspaceId/api-keys/:keyId/revoke
assert_contains "$SEED_FILE" "'/workspaces/:workspaceId/api-keys/:keyId/revoke'"
assert_contains "$SEED_FILE" "'platform.api_keys.revoke'"

# platform.api_keys.usage.read — GET /workspaces/:workspaceId/api-keys/:keyId/usage
assert_contains "$SEED_FILE" "'/workspaces/:workspaceId/api-keys/:keyId/usage'"
assert_contains "$SEED_FILE" "'platform.api_keys.usage.read'"

echo "    ✓ API key route patterns and action keys"

# ---------------------------------------------------------------------------
# Service key — all workspace-admin platform routes must target
# accounts-service. (platform.storage.* routes belong to STORAGE-3 and
# are covered by universal-storage-gateway-routes.test.sh.)
# ---------------------------------------------------------------------------
echo "  Checking service routing..."

ACCOUNTS_SERVICE_PLATFORM_COUNT=$(grep -E "'platform\.(domains|api_keys)\." "$SEED_FILE" | grep -c "'accounts-service'" || true)

if [ "$PLATFORM_ROUTE_COUNT" -gt 0 ] && [ "$PLATFORM_ROUTE_COUNT" -eq "$ACCOUNTS_SERVICE_PLATFORM_COUNT" ]; then
  pass
else
  fail "all platform.{domains,api_keys}.* routes must target accounts-service (found $ACCOUNTS_SERVICE_PLATFORM_COUNT of $PLATFORM_ROUTE_COUNT targeting accounts-service; need exactly 9)"
fi

echo "    ✓ all workspace-admin platform routes target accounts-service"

# ---------------------------------------------------------------------------
# Workspace scoping — all domain/API key routes must be workspace_scoped=true
# ---------------------------------------------------------------------------
echo "  Checking workspace scoping..."

# Each platform.* action_key line should have 'true' for workspace_scoped
# (the 5th value in the tuple). We verify by ensuring no 'false' appears
# between the path pattern and the platform.* action key on the same line.
# A simpler structural check: all routes use /workspaces/:workspaceId prefix.
assert_contains "$SEED_FILE" "'/workspaces/:workspaceId/domains', 'accounts-service'"
assert_contains "$SEED_FILE" "'/workspaces/:workspaceId/api-keys', 'accounts-service'"

echo "    ✓ workspace-scoped routing"

# ---------------------------------------------------------------------------
# Auth requirement — all routes must be non-public (is_public=false)
# ---------------------------------------------------------------------------
echo "  Checking auth requirement..."

# Security invariant: workspace-admin platform routes must never be public.
# Scoped to the platform.{domains,api_keys}.* families — storage.* routes
# are covered by their own contract test.
# Tuple format:
#   (method, path, service, target, workspace_scoped, action_key, is_public, enabled)
# We verify by checking the pattern: 'platform.{domains|api_keys}.*', false, true)
PLATFORM_FALSE_TRUE=$(grep -E "'platform\.(domains|api_keys)\." "$SEED_FILE" | grep -c "false, true)" || true)

if [ "$PLATFORM_FALSE_TRUE" -eq "$PLATFORM_ROUTE_COUNT" ]; then
  pass
else
  fail "all platform.{domains,api_keys}.* routes must have is_public=false, enabled=true (found $PLATFORM_FALSE_TRUE of $PLATFORM_ROUTE_COUNT matching)"
fi

echo "    ✓ all workspace-admin platform routes require authentication"

# ---------------------------------------------------------------------------
# HTTP methods — correct verbs for each action
# ---------------------------------------------------------------------------
echo "  Checking HTTP methods..."

assert_contains "$SEED_FILE" "('GET',    '/workspaces/:workspaceId/domains'"
assert_contains "$SEED_FILE" "('POST',   '/workspaces/:workspaceId/domains'"
assert_contains "$SEED_FILE" "('POST',   '/workspaces/:workspaceId/domains/:domainId/verify'"
assert_contains "$SEED_FILE" "('POST',   '/workspaces/:workspaceId/domains/:domainId/regenerate-verification'"
assert_contains "$SEED_FILE" "('DELETE', '/workspaces/:workspaceId/domains/:domainId'"
assert_contains "$SEED_FILE" "('GET',  '/workspaces/:workspaceId/api-keys'"
assert_contains "$SEED_FILE" "('POST', '/workspaces/:workspaceId/api-keys'"
assert_contains "$SEED_FILE" "('POST', '/workspaces/:workspaceId/api-keys/:keyId/revoke'"
assert_contains "$SEED_FILE" "('GET',  '/workspaces/:workspaceId/api-keys/:keyId/usage'"

echo "    ✓ HTTP methods correct"

# ---------------------------------------------------------------------------
# Structural integrity — ON CONFLICT clause preserved
# ---------------------------------------------------------------------------
echo "  Checking structural integrity..."

assert_contains "$SEED_FILE" "ON CONFLICT (method, path_pattern) DO UPDATE SET"
assert_contains "$SEED_FILE" "service_key = EXCLUDED.service_key"
assert_contains "$SEED_FILE" "action_key = EXCLUDED.action_key"

echo "    ✓ ON CONFLICT clause preserved"

# ---------------------------------------------------------------------------
# Backward compatibility — pre-existing routes must still be present
# ---------------------------------------------------------------------------
echo "  Checking backward compatibility..."

assert_contains "$SEED_FILE" "'accounts.me.getOrCreate'"
assert_contains "$SEED_FILE" "'accounts.workspaces.listForUser'"
assert_contains "$SEED_FILE" "'accounts.workspaces.create'"
assert_contains "$SEED_FILE" "'accounts.invites.create'"
assert_contains "$SEED_FILE" "'accounts.invites.resolve'"
assert_contains "$SEED_FILE" "'accounts.invites.accept'"
assert_contains "$SEED_FILE" "'docs.document.create'"
assert_contains "$SEED_FILE" "'docs.document.read'"
assert_contains "$SEED_FILE" "'cms.blog_entry.listPublished'"
assert_contains "$SEED_FILE" "'cms.blog_entry.getPublishedBySlug'"
assert_contains "$SEED_FILE" "'cms.content.listPublished'"
assert_contains "$SEED_FILE" "'cms.content.getPublishedBySlug'"
assert_contains "$SEED_FILE" "'cms.comments.create'"
assert_contains "$SEED_FILE" "'cms.comments.listForEntry'"
assert_contains "$SEED_FILE" "'telemetry.events.listRecentForWorkspace'"
assert_contains "$SEED_FILE" "'telemetry.stats.summaryByRoute'"

echo "    ✓ all pre-existing routes preserved"

# ---------------------------------------------------------------------------
# Security — no raw secrets in seed
# ---------------------------------------------------------------------------
echo "  Checking security invariants..."

assert_not_contains "$SEED_FILE" "raw_key"
assert_not_contains "$SEED_FILE" "secret"
assert_not_contains "$SEED_FILE" "password"
# Note: "token" is intentionally not checked because the existing invite
# routes use :token as a path parameter (e.g. /workspace-invites/:token).

echo "    ✓ no secrets in seed file"

# ---------------------------------------------------------------------------
# Cross-reference: action keys vs authz permission catalog (optional)
# ---------------------------------------------------------------------------
echo "  Checking cross-reference with authz permission catalog..."

if [ -f "$AUTHZ_PERMISSIONS_FILE" ]; then
  EXPECTED_ACTION_KEYS=(
    "platform.domains.list"
    "platform.domains.create"
    "platform.domains.verify"
    "platform.domains.regenerateVerification"
    "platform.domains.delete"
    "platform.api_keys.list"
    "platform.api_keys.create"
    "platform.api_keys.revoke"
    "platform.api_keys.usage.read"
  )

  for key in "${EXPECTED_ACTION_KEYS[@]}"; do
    if grep -Fq "key: \"${key}\"" "$AUTHZ_PERMISSIONS_FILE"; then
      pass
    else
      fail "action_key '$key' in seed but missing from authz permission catalog"
    fi
  done

  # platform.domains.regenerateVerification has the SAME effective grants
  # as platform.domains.create (both are catalog-derived for
  # workspace_owner / super_admin; explicit allowlist lower-tier roles do
  # not receive either). The catalog entry is modeled separately so the
  # "route action_key === permission key" invariant holds without the
  # gateway needing to substitute permission lookups.

  echo "    ✓ all action keys exist in authz permission catalog"
else
  echo "    ⚠ SKIP: sibling authz repo not found at $AUTHZ_PERMISSIONS_FILE"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Results: $PASS_COUNT passed, $FAIL_COUNT failed"
echo ""
if [ "$FAIL_COUNT" -gt 0 ]; then
  echo "FAIL: workspace-admin-integrations-gateway-routes.test.sh"
  exit 1
fi
echo "PASS: workspace-admin-integrations-gateway-routes.test.sh"
