#!/usr/bin/env bash
set -euo pipefail

# Contract test for CMS dashboard directory-first gateway route seeds.
# These routes are required by xynes-cms-console-web dashboard clients.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

MIGRATIONS_DIR="./supabase/migrations"

PASS_COUNT=0
FAIL_COUNT=0

fail() {
  echo "FAIL: $1" >&2
  FAIL_COUNT=$((FAIL_COUNT + 1))
}

pass() {
  PASS_COUNT=$((PASS_COUNT + 1))
}

assert_routes_contain() {
  local needle="$1"
  if grep -R -Fq -- "$needle" "$MIGRATIONS_DIR"; then
    pass
  else
    fail "expected migrations to contain: $needle"
  fi
}

assert_route_tuple() {
  local method="$1"
  local path="$2"
  local action_key="$3"

  if grep -R -F -- "$path" "$MIGRATIONS_DIR" | grep -F -- "'$method'" | grep -Fq -- "'$action_key'"; then
    pass
  else
    fail "expected $method $path to seed $action_key"
  fi
}

echo "Testing CMS dashboard gateway route seeds..."
echo ""

assert_routes_contain "cms.content_directories.listForWorkspace"
assert_routes_contain "cms.content_directories.create"
assert_routes_contain "cms.content_directories.update"
assert_routes_contain "cms.content_directories.delete"
assert_routes_contain "cms.entry.listByDirectory"
assert_routes_contain "cms.entry.create"
assert_routes_contain "cms.entry.getById"
assert_routes_contain "cms.entry.update"
assert_routes_contain "cms.entry.delete"
assert_routes_contain "cms.entry.publish"
assert_routes_contain "cms.entry.status.set"
assert_routes_contain "cms.entry.collaborators.set"
assert_routes_contain "cms.entry.favorite.toggle"
assert_routes_contain "cms.entry.favorite.list"
assert_routes_contain "cms.entry.share.generateInternalLink"

assert_route_tuple "GET" "/workspaces/:workspaceId/content-directories" "cms.content_directories.listForWorkspace"
assert_route_tuple "POST" "/workspaces/:workspaceId/content-directories" "cms.content_directories.create"
assert_route_tuple "PATCH" "/workspaces/:workspaceId/content-directories/:directoryId" "cms.content_directories.update"
assert_route_tuple "DELETE" "/workspaces/:workspaceId/content-directories/:directoryId" "cms.content_directories.delete"

assert_route_tuple "GET" "/workspaces/:workspaceId/content/entries" "cms.entry.listByDirectory"
assert_route_tuple "POST" "/workspaces/:workspaceId/content/entries" "cms.entry.create"
assert_route_tuple "GET" "/workspaces/:workspaceId/content/entries/:entryId" "cms.entry.getById"
assert_route_tuple "PATCH" "/workspaces/:workspaceId/content/entries/:entryId" "cms.entry.update"
assert_route_tuple "DELETE" "/workspaces/:workspaceId/content/entries/:entryId" "cms.entry.delete"
assert_route_tuple "POST" "/workspaces/:workspaceId/content/entries/:entryId/publish" "cms.entry.publish"
assert_route_tuple "POST" "/workspaces/:workspaceId/content/entries/:entryId/status" "cms.entry.status.set"
assert_route_tuple "PUT" "/workspaces/:workspaceId/content/entries/:entryId/collaborators" "cms.entry.collaborators.set"
assert_route_tuple "POST" "/workspaces/:workspaceId/content/entries/:entryId/favorite" "cms.entry.favorite.toggle"
assert_route_tuple "GET" "/workspaces/:workspaceId/content/entries/favorites" "cms.entry.favorite.list"
assert_route_tuple "POST" "/workspaces/:workspaceId/content/entries/:entryId/share-link" "cms.entry.share.generateInternalLink"

if [ "$FAIL_COUNT" -ne 0 ]; then
  echo ""
  echo "FAIL: cms-dashboard-gateway-routes.test.sh ($PASS_COUNT passed, $FAIL_COUNT failed)" >&2
  exit 1
fi

echo "PASS: cms-dashboard-gateway-routes.test.sh ($PASS_COUNT assertions)"
