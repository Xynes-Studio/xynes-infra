#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

assert_contains() {
  local haystack="$1"
  local needle="$2"
  echo "$haystack" | grep -Fq -- "$needle" || fail "expected output to contain: $needle"
}

# 1) --help should mention secure reset options
OUT="$(./scripts/bootstrap-local-db.sh --help)"
assert_contains "$OUT" "--env-file"
assert_contains "$OUT" "--restart-gateway"
assert_contains "$OUT" "--skip-cms-seed"

# 2) Script should use the cms-core migrator and seeder rather than replaying raw SQL files
SCRIPT_CONTENT="$(cat ./scripts/bootstrap-local-db.sh)"
assert_contains "$SCRIPT_CONTENT" 'bun --env-file="$CMS_ENV_FILE" run src/infra/db/migrate.ts >/dev/null'
assert_contains "$SCRIPT_CONTENT" 'cd "${ROOT_DIR}/../xynes-cms-core"'
assert_contains "$SCRIPT_CONTENT" 'DEFAULT_WORKSPACE_ID="$workspace_id" bun --env-file="$CMS_ENV_FILE" run src/infra/db/seed.ts >/dev/null'

# 2a) Script should apply xynes-authz-service Drizzle migrations + seed the
# RBAC catalog before the workspace_invites FK guard runs. This closes the
# `relation "authz.roles" does not exist` gap that 502'd POST /workspaces
# on a fresh `supabase db reset`. Asserts the exact invocation pattern
# (matches the cms-core / doc-service convention) plus the catalog
# verification step.
assert_contains "$SCRIPT_CONTENT" 'cd "${ROOT_DIR}/../xynes-authz-service"'
assert_contains "$SCRIPT_CONTENT" 'bun --env-file="$AUTHZ_ENV_FILE" x drizzle-kit migrate >/dev/null'
assert_contains "$SCRIPT_CONTENT" 'bun --env-file="$AUTHZ_ENV_FILE" run src/scripts/seed.ts >/dev/null'
assert_contains "$SCRIPT_CONTENT" 'SELECT count(*)::int FROM authz.roles;'
assert_contains "$SCRIPT_CONTENT" 'SELECT count(*)::int FROM authz.permissions;'
assert_contains "$SCRIPT_CONTENT" 'SELECT count(*)::int FROM authz.role_permissions;'
assert_contains "$SCRIPT_CONTENT" 'expected 5 roles'

# 2b) Step 2a must run BEFORE the workspace_invites FK guard (step 4) so
# `authz.roles` exists by the time the FK is added on a fresh reset.
# Defensive ordering check via line numbers.
AUTHZ_STEP_LINE="$(grep -nE '^# 2a\) AUTHZ tables' ./scripts/bootstrap-local-db.sh | head -1 | cut -d: -f1)"
INVITES_STEP_LINE="$(grep -nE '^# 4\) Workspace invites table' ./scripts/bootstrap-local-db.sh | head -1 | cut -d: -f1)"
if [ -z "$AUTHZ_STEP_LINE" ] || [ -z "$INVITES_STEP_LINE" ]; then
  fail "could not locate step 2a (authz) or step 4 (workspace_invites) header"
fi
if [ "$AUTHZ_STEP_LINE" -ge "$INVITES_STEP_LINE" ]; then
  fail "step 2a (authz, line $AUTHZ_STEP_LINE) must precede step 4 (workspace_invites FK, line $INVITES_STEP_LINE)"
fi

# 3) Script should verify the directory-first CMS dashboard route registry
assert_contains "$SCRIPT_CONTENT" "cms.entry.listByDirectory"
assert_contains "$SCRIPT_CONTENT" "cms.content_directories.listForWorkspace"

# 4) Script should be able to restart a running gateway after route bootstrap
assert_contains "$SCRIPT_CONTENT" "docker restart"

echo "PASS: bootstrap-local-db.test.sh"
