#!/usr/bin/env bash
set -euo pipefail

# Cross-repo contract smoke for the Workspace Admin integrations authz
# permission catalog.
#
# Scope:
#   - Validates that xynes-authz-service/src/db/seed/permissions.config.ts
#     publishes the nine `platform.*` permission keys required by the
#     Workspace Admin integrations MVP (backend-foundation plan, Task 2).
#   - Validates the initial role wiring invariants documented in
#     AGENTS.md and the epic source of truth:
#       * workspace_owner + super_admin are catalog-derived and therefore
#         inherit every permission (we assert the permissions list uses
#         `AUTHZ_PERMISSIONS.map`).
#       * workspace_member, content_editor, read_only are explicit
#         allowlists and MUST NOT list any `platform.*` API-key lifecycle
#         or domain-lifecycle write key.
#
# This runs without a database or running services — it is a static
# contract check modeled on scripts/test/workspace-admin-integrations-migration.test.sh.
#
# Cross-repo behavior:
#   - If the sibling xynes-authz-service repo is not checked out alongside
#     xynes-infra, the test SKIPS (exit 0) to stay friendly to CI
#     environments that build infra in isolation. Local developer checkouts
#     always have the sibling present per AGENTS.md workspace layout.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

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

assert_not_contains_in_block() {
  # Assert that within the explicit allowlist block for a given role,
  # a forbidden permission key does NOT appear.
  #
  # Args:
  #   $1 - file path
  #   $2 - role key (e.g. "workspace_member")
  #   $3 - forbidden permission key (e.g. "platform.api_keys.create")
  local path="$1"
  local role="$2"
  local forbidden="$3"

  # Extract the slice between `key: "<role>"` and the next `key: "` entry.
  local slice
  slice="$(awk -v role_marker="key: \"${role}\"" '
    $0 ~ role_marker { capture=1 }
    capture && /^ {4}key: "/ && $0 !~ role_marker { exit }
    capture { print }
  ' "$path")"

  if [ -z "$slice" ]; then
    fail "could not locate role block for: $role in $path"
    return
  fi

  if printf '%s' "$slice" | grep -Fq -- "\"${forbidden}\""; then
    fail "role $role must not include permission: $forbidden"
  else
    pass
  fi
}

echo "Testing Workspace Admin integrations authz permission contract..."
echo ""

# ---------------------------------------------------------------------------
# Cross-repo preflight
# ---------------------------------------------------------------------------
if [ ! -f "$AUTHZ_PERMISSIONS_FILE" ]; then
  echo "SKIP: sibling authz permissions file not found at $AUTHZ_PERMISSIONS_FILE"
  echo "SKIP: workspace-admin-integrations-authz-permissions.test.sh (no sibling authz repo)"
  exit 0
fi

assert_file_exists "$AUTHZ_PERMISSIONS_FILE"

# ---------------------------------------------------------------------------
# Required platform.* permission keys (MVP)
# ---------------------------------------------------------------------------
REQUIRED_PERMISSION_KEYS=(
  "platform.domains.list"
  "platform.domains.create"
  "platform.domains.verify"
  "platform.domains.regenerateVerification"
  "platform.domains.delete"
  "platform.domain_bindings.manage"
  "platform.api_keys.list"
  "platform.api_keys.create"
  "platform.api_keys.revoke"
  "platform.api_keys.usage.read"
)

for key in "${REQUIRED_PERMISSION_KEYS[@]}"; do
  assert_contains "$AUTHZ_PERMISSIONS_FILE" "key: \"${key}\""
done

# ---------------------------------------------------------------------------
# Role wiring invariants
# ---------------------------------------------------------------------------

# workspace_owner + super_admin are catalog-derived.
# We assert the canonical map expression appears in the role permission list
# so that every new catalog entry auto-flows to both admin roles.
#
# NOTE: the source uses the expression `AUTHZ_PERMISSIONS.map((p) => p.key)`
# both for workspace_owner and super_admin (see permissions.config.ts).
OWNER_MAP_COUNT=$(grep -c "permissions: AUTHZ_PERMISSIONS.map((p) => p.key)" "$AUTHZ_PERMISSIONS_FILE" || true)
if [ "$OWNER_MAP_COUNT" -ge 2 ]; then
  pass
else
  fail "expected AUTHZ_PERMISSIONS.map((p) => p.key) to appear at least twice (workspace_owner + super_admin); found ${OWNER_MAP_COUNT}"
fi

# Security invariant: restricted roles must NOT carry API-key lifecycle
# writes or domain lifecycle writes in their explicit allowlists.
RESTRICTED_ROLES=(
  "workspace_member"
  "content_editor"
  "read_only"
)

FORBIDDEN_KEYS=(
  "platform.api_keys.create"
  "platform.api_keys.revoke"
  "platform.domains.create"
  "platform.domains.verify"
  "platform.domains.regenerateVerification"
  "platform.domains.delete"
  "platform.domain_bindings.manage"
)

for role in "${RESTRICTED_ROLES[@]}"; do
  for key in "${FORBIDDEN_KEYS[@]}"; do
    assert_not_contains_in_block "$AUTHZ_PERMISSIONS_FILE" "$role" "$key"
  done
done

# ---------------------------------------------------------------------------
# Companion unit test presence (keeps the two sides from drifting)
# ---------------------------------------------------------------------------
COMPANION_UNIT_TEST="../xynes-authz-service/test/unit/workspace-admin-integrations-permissions.test.ts"
assert_file_exists "$COMPANION_UNIT_TEST"
assert_contains "$COMPANION_UNIT_TEST" "platform.domains.list"
assert_contains "$COMPANION_UNIT_TEST" "platform.api_keys.usage.read"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Results: $PASS_COUNT passed, $FAIL_COUNT failed"
echo ""
if [ "$FAIL_COUNT" -gt 0 ]; then
  echo "FAIL: workspace-admin-integrations-authz-permissions.test.sh"
  exit 1
fi
echo "PASS: workspace-admin-integrations-authz-permissions.test.sh"
