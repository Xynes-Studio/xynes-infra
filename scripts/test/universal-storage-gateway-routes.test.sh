#!/usr/bin/env bash
set -euo pipefail

# Contract test for the Universal Object Storage gateway route seeds (STORAGE-3).
#
# Scope:
#   - Validates that the platform.routes seed migration contains the eight
#     storage routes described in
#     `xynes/xynes-infra/docs/plans/2026-05-10-universal-object-storage-file-upload-api.md` §7
#   - Validates route configuration invariants:
#       * All storage routes target storage-service
#       * All routes are workspace-scoped
#       * All routes are non-public (require auth)
#       * All routes are enabled
#       * action_key values match the storage permission catalog
#       * Correct HTTP methods (GET / POST / DELETE)
#   - Validates idempotent ON CONFLICT clause is preserved.
#   - Validates that no existing routes are removed or corrupted.
#   - Cross-references action keys against the authz permission catalog
#     when the sibling repo is available.
#
# This runs without a database or running services — it is a static
# contract check modeled on
# scripts/test/workspace-admin-integrations-gateway-routes.test.sh.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# Original combined seed (fresh-env bootstrap path)
SEED_FILE="./supabase/migrations/20251229100001_seed_platform_routes.sql"
# Forward-only migration (already-applied-env replay path) — required so
# environments that already ran SEED_FILE before STORAGE-3 still receive
# the eight storage routes. Mirrors the precedent set by
# 20260508130000_seed_platform_domain_regenerate_route.sql.
FORWARD_SEED_FILE="./supabase/migrations/20260513120000_seed_platform_storage_routes.sql"
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

echo "Testing Universal Object Storage gateway route seeds..."
echo ""

# ---------------------------------------------------------------------------
# File existence
# ---------------------------------------------------------------------------
assert_file_exists "$SEED_FILE"
assert_file_exists "$FORWARD_SEED_FILE"

# ---------------------------------------------------------------------------
# Route count — exactly 8 platform.storage.* action keys expected (in BOTH
# the original combined seed AND the forward-only migration; both files
# must agree so fresh-env bootstrap and already-applied-env replay both
# land the same eight routes).
# ---------------------------------------------------------------------------
echo "  Checking route count..."

STORAGE_ROUTE_COUNT=$(grep -c "'platform\.storage\." "$SEED_FILE" || true)
assert_eq "8" "$STORAGE_ROUTE_COUNT" "platform.storage.* route count in $SEED_FILE"

FORWARD_STORAGE_ROUTE_COUNT=$(grep -c "'platform\.storage\." "$FORWARD_SEED_FILE" || true)
assert_eq "8" "$FORWARD_STORAGE_ROUTE_COUNT" "platform.storage.* route count in $FORWARD_SEED_FILE"

echo "    ✓ exactly 8 storage routes present in both seed files"

# ---------------------------------------------------------------------------
# Upload session lifecycle routes
# ---------------------------------------------------------------------------
echo "  Checking upload session routes..."

# POST /workspaces/:workspaceId/storage/uploads — create
assert_contains "$SEED_FILE" "'/workspaces/:workspaceId/storage/uploads'"
assert_contains "$SEED_FILE" "'platform.storage.objects.upload'"

# POST .../uploads/:uploadId/complete
assert_contains "$SEED_FILE" "'/workspaces/:workspaceId/storage/uploads/:uploadId/complete'"

# POST .../uploads/:uploadId/abort
assert_contains "$SEED_FILE" "'/workspaces/:workspaceId/storage/uploads/:uploadId/abort'"

# All three upload routes share the same action key — verify exactly 3 occurrences
UPLOAD_ACTION_KEY_COUNT=$(grep -c "'platform.storage.objects.upload'" "$SEED_FILE" || true)
assert_eq "3" "$UPLOAD_ACTION_KEY_COUNT" "platform.storage.objects.upload action key usage count"

echo "    ✓ upload session route patterns and action keys"

# ---------------------------------------------------------------------------
# Object metadata + delivery routes
# ---------------------------------------------------------------------------
echo "  Checking object metadata + delivery routes..."

# GET /workspaces/:workspaceId/storage/objects — list
assert_contains "$SEED_FILE" "'/workspaces/:workspaceId/storage/objects'"
# GET .../objects/:objectId
assert_contains "$SEED_FILE" "'/workspaces/:workspaceId/storage/objects/:objectId'"
# POST .../objects/:objectId/download-url
assert_contains "$SEED_FILE" "'/workspaces/:workspaceId/storage/objects/:objectId/download-url'"

# All three share platform.storage.objects.read — verify exactly 3 occurrences
READ_ACTION_KEY_COUNT=$(grep -c "'platform.storage.objects.read'" "$SEED_FILE" || true)
assert_eq "3" "$READ_ACTION_KEY_COUNT" "platform.storage.objects.read action key usage count"

echo "    ✓ object metadata + delivery route patterns and action keys"

# ---------------------------------------------------------------------------
# Object delete + processing retry
# ---------------------------------------------------------------------------
echo "  Checking object delete + processing retry routes..."

# DELETE .../objects/:objectId
assert_contains "$SEED_FILE" "'platform.storage.objects.delete'"
# POST .../objects/:objectId/process/retry
assert_contains "$SEED_FILE" "'/workspaces/:workspaceId/storage/objects/:objectId/process/retry'"
assert_contains "$SEED_FILE" "'platform.storage.objects.process.retry'"

echo "    ✓ delete + retry route patterns and action keys"

# ---------------------------------------------------------------------------
# Service key — all storage routes must target storage-service
# ---------------------------------------------------------------------------
echo "  Checking service routing..."

STORAGE_SERVICE_COUNT=$(grep "'platform\.storage\." "$SEED_FILE" | grep -c "'storage-service'" || true)

if [ "$STORAGE_ROUTE_COUNT" -gt 0 ] && [ "$STORAGE_ROUTE_COUNT" -eq "$STORAGE_SERVICE_COUNT" ]; then
  pass
else
  fail "all platform.storage.* routes must target storage-service (found $STORAGE_SERVICE_COUNT of $STORAGE_ROUTE_COUNT targeting storage-service; need exactly 8)"
fi

echo "    ✓ all storage routes target storage-service"

# Defense in depth: storage routes must NEVER target another service
# (regression guard against accidentally pointing storage routes at
# cms-core / accounts-service / doc-service / telemetry-service).
for wrong_target in 'accounts-service' 'cms-core' 'doc-service' 'telemetry-service'; do
  WRONG_COUNT=$(grep "'platform\.storage\." "$SEED_FILE" | grep -c "'$wrong_target'" || true)
  if [ "$WRONG_COUNT" -eq 0 ]; then
    pass
  else
    fail "no platform.storage.* route may target $wrong_target (found $WRONG_COUNT)"
  fi
done

echo "    ✓ no storage routes target a non-storage service"

# ---------------------------------------------------------------------------
# Workspace scoping — all storage routes must be under /workspaces/:workspaceId
# ---------------------------------------------------------------------------
echo "  Checking workspace scoping..."

# A simpler structural check: every platform.storage.* row uses
# /workspaces/:workspaceId prefix.
NON_WORKSPACE_STORAGE=$(grep "'platform\.storage\." "$SEED_FILE" | grep -cv "/workspaces/:workspaceId/storage/" || true)
assert_eq "0" "$NON_WORKSPACE_STORAGE" "non-workspace-scoped storage routes"

echo "    ✓ all storage routes are workspace-scoped"

# ---------------------------------------------------------------------------
# Auth requirement — all storage routes must be non-public (is_public=false)
# ---------------------------------------------------------------------------
echo "  Checking auth requirement..."

# Tuple format:
#   (method, path, service, target, workspace_scoped, action_key, is_public, enabled)
# Storage routes must have ', false, true)' at the end (is_public=false, enabled=true).
STORAGE_FALSE_TRUE=$(grep "'platform\.storage\." "$SEED_FILE" | grep -c "false, true)" || true)

if [ "$STORAGE_FALSE_TRUE" -eq "$STORAGE_ROUTE_COUNT" ]; then
  pass
else
  fail "all platform.storage.* routes must have is_public=false, enabled=true (found $STORAGE_FALSE_TRUE of $STORAGE_ROUTE_COUNT matching)"
fi

echo "    ✓ all storage routes require authentication and are enabled"

# ---------------------------------------------------------------------------
# HTTP methods — correct verbs for each action
# ---------------------------------------------------------------------------
echo "  Checking HTTP methods..."

# Uploads
assert_contains "$SEED_FILE" "('POST',   '/workspaces/:workspaceId/storage/uploads'"
assert_contains "$SEED_FILE" "('POST',   '/workspaces/:workspaceId/storage/uploads/:uploadId/complete'"
assert_contains "$SEED_FILE" "('POST',   '/workspaces/:workspaceId/storage/uploads/:uploadId/abort'"

# Object reads / signed URL
assert_contains "$SEED_FILE" "('GET',    '/workspaces/:workspaceId/storage/objects'"
assert_contains "$SEED_FILE" "('GET',    '/workspaces/:workspaceId/storage/objects/:objectId'"
assert_contains "$SEED_FILE" "('POST',   '/workspaces/:workspaceId/storage/objects/:objectId/download-url'"

# Delete + retry
assert_contains "$SEED_FILE" "('DELETE', '/workspaces/:workspaceId/storage/objects/:objectId'"
assert_contains "$SEED_FILE" "('POST',   '/workspaces/:workspaceId/storage/objects/:objectId/process/retry'"

echo "    ✓ HTTP methods correct for all 8 routes"

# ---------------------------------------------------------------------------
# Structural integrity — ON CONFLICT clause preserved
# ---------------------------------------------------------------------------
echo "  Checking structural integrity..."

assert_contains "$SEED_FILE" "ON CONFLICT (method, path_pattern) DO UPDATE SET"
assert_contains "$SEED_FILE" "service_key = EXCLUDED.service_key"
assert_contains "$SEED_FILE" "action_key = EXCLUDED.action_key"
assert_contains "$SEED_FILE" "enabled = EXCLUDED.enabled"

# Forward-only migration: must also be idempotent via ON CONFLICT so
# replaying in environments that already applied 20251229100001 is safe.
assert_contains "$FORWARD_SEED_FILE" "ON CONFLICT (method, path_pattern) DO UPDATE SET"
assert_contains "$FORWARD_SEED_FILE" "service_key = EXCLUDED.service_key"
assert_contains "$FORWARD_SEED_FILE" "action_key = EXCLUDED.action_key"
assert_contains "$FORWARD_SEED_FILE" "enabled = EXCLUDED.enabled"

# Forward-only migration: must only INSERT (no DROP / DELETE / TRUNCATE)
# and must only touch platform.routes. Same additive-safety posture as
# the workspace-admin migrations (workspace-admin-integrations.sql etc.).
assert_not_contains "$FORWARD_SEED_FILE" "DROP TABLE"
assert_not_contains "$FORWARD_SEED_FILE" "DROP COLUMN"
assert_not_contains "$FORWARD_SEED_FILE" "TRUNCATE"
assert_not_contains "$FORWARD_SEED_FILE" "DELETE FROM"
assert_contains "$FORWARD_SEED_FILE" "INSERT INTO platform.routes"

# Forward-only migration: ensure all 8 path patterns + action keys also
# present in the forward-only migration. Defense-in-depth so the two
# files cannot drift in either direction.
assert_contains "$FORWARD_SEED_FILE" "'/workspaces/:workspaceId/storage/uploads'"
assert_contains "$FORWARD_SEED_FILE" "'/workspaces/:workspaceId/storage/uploads/:uploadId/complete'"
assert_contains "$FORWARD_SEED_FILE" "'/workspaces/:workspaceId/storage/uploads/:uploadId/abort'"
assert_contains "$FORWARD_SEED_FILE" "'/workspaces/:workspaceId/storage/objects'"
assert_contains "$FORWARD_SEED_FILE" "'/workspaces/:workspaceId/storage/objects/:objectId'"
assert_contains "$FORWARD_SEED_FILE" "'/workspaces/:workspaceId/storage/objects/:objectId/download-url'"
assert_contains "$FORWARD_SEED_FILE" "'/workspaces/:workspaceId/storage/objects/:objectId/process/retry'"
assert_contains "$FORWARD_SEED_FILE" "'platform.storage.objects.upload'"
assert_contains "$FORWARD_SEED_FILE" "'platform.storage.objects.read'"
assert_contains "$FORWARD_SEED_FILE" "'platform.storage.objects.delete'"
assert_contains "$FORWARD_SEED_FILE" "'platform.storage.objects.process.retry'"

# Forward-only migration: every row must target storage-service.
FORWARD_STORAGE_SERVICE_COUNT=$(grep -c "'storage-service'" "$FORWARD_SEED_FILE" || true)
assert_eq "8" "$FORWARD_STORAGE_SERVICE_COUNT" "storage-service occurrences in $FORWARD_SEED_FILE"

# Forward-only migration: no provider credentials may appear.
assert_not_contains "$FORWARD_SEED_FILE" "raw_key"
assert_not_contains "$FORWARD_SEED_FILE" "secret_access_key"
assert_not_contains "$FORWARD_SEED_FILE" "access_key_id"
assert_not_contains "$FORWARD_SEED_FILE" "r2_token"
assert_not_contains "$FORWARD_SEED_FILE" "signed_url"
assert_not_contains "$FORWARD_SEED_FILE" "presigned_url"
assert_not_contains "$FORWARD_SEED_FILE" "password"

echo "    ✓ ON CONFLICT clause preserved (both files) and forward-only migration is additive-only"

# ---------------------------------------------------------------------------
# Cross-reference: action keys vs authz permission catalog
# ---------------------------------------------------------------------------
echo "  Checking cross-reference with authz permission catalog..."

if [ -f "$AUTHZ_PERMISSIONS_FILE" ]; then
  EXPECTED_STORAGE_ACTION_KEYS=(
    "platform.storage.objects.upload"
    "platform.storage.objects.read"
    "platform.storage.objects.delete"
    "platform.storage.objects.process.retry"
  )

  for key in "${EXPECTED_STORAGE_ACTION_KEYS[@]}"; do
    if grep -Fq "key: \"${key}\"" "$AUTHZ_PERMISSIONS_FILE"; then
      pass
    else
      fail "action_key '$key' in seed but missing from authz permission catalog"
    fi
  done

  # platform.storage.providers.manage and platform.storage.usage.read
  # are catalog-only for the MVP (no route yet — provider management
  # lands in a later Workspace Admin Integrations story; usage is read
  # via a dedicated daily aggregate endpoint that will be added in
  # STORAGE-6). We still assert they exist in the catalog so that
  # adding their routes later is purely additive.
  if grep -Fq 'key: "platform.storage.providers.manage"' "$AUTHZ_PERMISSIONS_FILE"; then
    pass
  else
    fail "permission 'platform.storage.providers.manage' missing from authz permission catalog"
  fi

  if grep -Fq 'key: "platform.storage.usage.read"' "$AUTHZ_PERMISSIONS_FILE"; then
    pass
  else
    fail "permission 'platform.storage.usage.read' missing from authz permission catalog"
  fi

  echo "    ✓ all route action keys exist in authz permission catalog"
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
  echo "FAIL: universal-storage-gateway-routes.test.sh"
  exit 1
fi
echo "PASS: universal-storage-gateway-routes.test.sh"
