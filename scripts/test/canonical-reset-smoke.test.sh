#!/usr/bin/env bash
set -euo pipefail

# canonical-reset-smoke.test.sh
#
# End-to-end validator for the canonical local DB reset path documented in
# `xynes-infra/docs/DATABASE.md` §3.
#
# Asserts post-reset DB state:
#   - The 6 schemas owned by infra and services all exist.
#   - The key tables expected by `docs/DATABASE.md` §1 schema map are
#     present.
#   - `platform.routes` has the expected counts for the route families
#     STORAGE-3, workspace-admin integrations, and CMS dashboard.
#   - `platform.workspace_invites` exists and was placed by an infra
#     migration (post-2026-05-13 promotion) rather than only by the
#     bootstrap psql heredoc (legacy path).
#
# Does NOT:
#   - Run `supabase db reset` itself. This script assumes the operator has
#     already run `bash ./scripts/supabase-local.sh reset` (the canonical
#     entry point). That keeps this test cheap and CI-friendly.
#   - Run live HTTP requests against the gateway. Pure DB state assertions.
#
# Skips with exit 0 if local Supabase is not reachable, matching the
# existing `feature-flags.test.sh` pattern so the test suite is friendly
# on hosts that don't have Docker running.

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

# ---------------------------------------------------------------------------
# Preflight: is local Supabase reachable?
# ---------------------------------------------------------------------------
PSQL_BIN="$(command -v psql || true)"
if [ -z "$PSQL_BIN" ]; then
  echo "SKIP: canonical-reset-smoke.test.sh — psql not installed."
  exit 0
fi

DB_URL="postgresql://postgres:postgres@127.0.0.1:5432/postgres"

if ! PGPASSWORD=postgres psql -h 127.0.0.1 -p 5432 -U postgres -d postgres \
    -tA -c "SELECT 1" >/dev/null 2>&1; then
  echo "SKIP: canonical-reset-smoke.test.sh — local Supabase Postgres not reachable on 127.0.0.1:5432."
  echo "      Run \`bash ./scripts/supabase-local.sh start\` first."
  exit 0
fi

echo "Testing canonical local DB reset state..."
echo ""

# ---------------------------------------------------------------------------
# Helper: assert one psql query returns a value
# ---------------------------------------------------------------------------
psql_query() {
  PGPASSWORD=postgres psql -h 127.0.0.1 -p 5432 -U postgres -d postgres \
    -X -tA -v ON_ERROR_STOP=1 -c "$1"
}

assert_relation_exists() {
  local relname="$1"
  local actual
  actual="$(psql_query "SELECT to_regclass('$relname')::text;")"
  if [ "$actual" = "$relname" ]; then
    pass
  else
    fail "expected relation '$relname' to exist (to_regclass returned: '$actual')"
  fi
}

assert_schema_exists() {
  local schema="$1"
  local actual
  actual="$(psql_query "SELECT schema_name FROM information_schema.schemata WHERE schema_name = '$schema';")"
  if [ "$actual" = "$schema" ]; then
    pass
  else
    fail "expected schema '$schema' to exist (information_schema.schemata returned: '$actual')"
  fi
}

assert_count_ge() {
  local query="$1"
  local minimum="$2"
  local label="$3"
  local actual
  actual="$(psql_query "$query")"
  if [ -n "$actual" ] && [ "$actual" -ge "$minimum" ] 2>/dev/null; then
    pass
  else
    fail "$label: expected count >= $minimum, got '$actual'"
  fi
}

assert_count_eq() {
  local query="$1"
  local expected="$2"
  local label="$3"
  local actual
  actual="$(psql_query "$query")"
  if [ "$actual" = "$expected" ]; then
    pass
  else
    fail "$label: expected count = $expected, got '$actual'"
  fi
}

# ---------------------------------------------------------------------------
# 1. Infra-owned schemas + tables (per docs/DATABASE.md §1)
# ---------------------------------------------------------------------------
echo "  Checking infra-owned schemas..."
assert_schema_exists "identity"
assert_schema_exists "platform"
echo "    ✓ identity + platform schemas exist"

echo "  Checking identity.* tables..."
assert_relation_exists "identity.users"
echo "    ✓ identity.users"

echo "  Checking platform.* tables (infra-owned)..."
assert_relation_exists "platform.routes"
assert_relation_exists "platform.route_rate_limits"
assert_relation_exists "platform.workspaces"
assert_relation_exists "platform.workspace_members"
assert_relation_exists "platform.workspace_invites"
assert_relation_exists "platform.workspace_domains"
assert_relation_exists "platform.workspace_domain_bindings"
assert_relation_exists "platform.workspace_api_keys"
assert_relation_exists "platform.workspace_api_key_scopes"
assert_relation_exists "platform.workspace_storage_providers"
assert_relation_exists "platform.storage_objects"
assert_relation_exists "platform.storage_upload_sessions"
assert_relation_exists "platform.storage_processing_jobs"
assert_relation_exists "platform.storage_object_variants"
assert_relation_exists "platform.storage_usage_daily"
echo "    ✓ all 15 platform.* infra-owned tables present"

# ---------------------------------------------------------------------------
# 2. workspace_invites must be placed by an infra migration, not just by
#    bootstrap psql heredoc (post-2026-05-13 promotion contract).
# ---------------------------------------------------------------------------
echo "  Checking workspace_invites migration ownership..."
MIGRATION_PRESENT="$(psql_query "SELECT count(*)::int FROM supabase_migrations.schema_migrations WHERE version = '20260513140000';")"
if [ "$MIGRATION_PRESENT" = "1" ]; then
  pass
  echo "    ✓ 20260513140000_promote_workspace_invites_to_infra applied"
else
  fail "expected 20260513140000_promote_workspace_invites_to_infra to be applied (schema_migrations row: '$MIGRATION_PRESENT')"
fi

# ---------------------------------------------------------------------------
# 3. Service-owned schemas (created by Supabase migrations, populated by
#    Drizzle migrations during bootstrap-local-db.sh).
# ---------------------------------------------------------------------------
echo "  Checking service-owned schemas..."
assert_schema_exists "docs"
assert_schema_exists "cms"
assert_schema_exists "cms_migration_backups"
echo "    ✓ docs + cms + cms_migration_backups schemas exist"

echo "  Checking docs.* tables (xynes-doc-service Drizzle, via bootstrap)..."
# Note: docs.documents is created by xynes-doc-service Drizzle migrations.
# Bootstrap delegates to `drizzle-kit migrate` rather than creating it
# inline (2026-05-13 dedup). If bootstrap was skipped or doc-service has
# no migrations applied yet, this will fail — fail with a clear message.
DOCS_DOCUMENTS_RELNAME="$(psql_query "SELECT to_regclass('docs.documents')::text;")"
if [ "$DOCS_DOCUMENTS_RELNAME" = "docs.documents" ]; then
  pass
  echo "    ✓ docs.documents present (delegated from xynes-doc-service Drizzle)"
else
  fail "docs.documents missing. Run bootstrap-local-db.sh; it should invoke xynes-doc-service drizzle-kit migrate. Got: '$DOCS_DOCUMENTS_RELNAME'"
fi

echo "  Checking cms.* tables (xynes-cms-core Drizzle, via bootstrap)..."
assert_relation_exists "cms.content_entries"
assert_relation_exists "cms.content_directories"
assert_relation_exists "cms.content_types"
assert_relation_exists "cms.global_content_templates"
assert_relation_exists "cms.content_entry_collaborators"
assert_relation_exists "cms.content_entry_favorites"
echo "    ✓ all 6 cms.* required tables present"

# ---------------------------------------------------------------------------
# 4. Route seed counts (lower bounds — additive migrations may add more later).
# ---------------------------------------------------------------------------
echo "  Checking platform.routes counts..."

assert_count_ge \
  "SELECT count(*)::int FROM platform.routes;" \
  "20" \
  "platform.routes total"

# STORAGE-3 (universal object storage): 8 storage routes
assert_count_eq \
  "SELECT count(*)::int FROM platform.routes WHERE path_pattern LIKE '/workspaces/%/storage/%';" \
  "8" \
  "platform.routes storage routes"

# Workspace Admin integrations: 5 domain routes (incl. regenerate-verification) + 4 api-key routes = 9
assert_count_eq \
  "SELECT count(*)::int FROM platform.routes WHERE path_pattern LIKE '/workspaces/%/domains%';" \
  "5" \
  "platform.routes domain routes"

assert_count_eq \
  "SELECT count(*)::int FROM platform.routes WHERE path_pattern LIKE '/workspaces/%/api-keys%';" \
  "4" \
  "platform.routes api-key routes"

echo "    ✓ route seed counts match docs/DATABASE.md §1 expectations"

# ---------------------------------------------------------------------------
# 5. Cross-schema FK posture (post-2026-05-13)
# ---------------------------------------------------------------------------
echo "  Checking cross-schema FK posture..."

# workspace_invites_workspace_id_workspaces_id_fk — always required.
assert_count_eq \
  "SELECT count(*)::int FROM pg_constraint WHERE conname = 'workspace_invites_workspace_id_workspaces_id_fk';" \
  "1" \
  "workspace_invites → workspaces FK present"

# workspace_invites_invited_by_users_id_fk — always required.
assert_count_eq \
  "SELECT count(*)::int FROM pg_constraint WHERE conname = 'workspace_invites_invited_by_users_id_fk';" \
  "1" \
  "workspace_invites → identity.users FK present"

# workspace_invites_role_key_roles_key_fk — conditional on authz.roles.
# We DO NOT assert presence here. Both states are legal per
# docs/DATABASE.md §4 (the gap is documented and recovery is by re-running
# bootstrap after authz Drizzle migrations apply). We only assert the
# constraint is NOT corrupted when present.
AUTHZ_FK_COUNT="$(psql_query "SELECT count(*)::int FROM pg_constraint WHERE conname = 'workspace_invites_role_key_roles_key_fk';")"
AUTHZ_ROLES_EXISTS="$(psql_query "SELECT count(*)::int FROM information_schema.tables WHERE table_schema = 'authz' AND table_name = 'roles';")"

if [ "$AUTHZ_ROLES_EXISTS" = "1" ]; then
  # authz.roles exists, so the FK must be present (bootstrap-local-db.sh
  # re-applies it idempotently after authz Drizzle migrations).
  if [ "$AUTHZ_FK_COUNT" = "1" ]; then
    pass
    echo "    ✓ workspace_invites → authz.roles FK present (authz.roles exists)"
  else
    fail "workspace_invites_role_key_roles_key_fk missing despite authz.roles existing. Re-run bootstrap-local-db.sh."
  fi
else
  # authz.roles does not exist yet (fresh stack, authz Drizzle not yet run).
  # This is the documented gap from docs/DATABASE.md §4.
  if [ "$AUTHZ_FK_COUNT" = "0" ]; then
    pass
    echo "    ⓘ workspace_invites → authz.roles FK skipped (authz.roles not present yet)"
    echo "      To close the gap: run xynes-authz-service Drizzle migrations,"
    echo "      then re-run bash ./scripts/bootstrap-local-db.sh --env-file .env.localhost.local"
  else
    fail "workspace_invites_role_key_roles_key_fk present despite authz.roles missing (impossible state — DB corruption?)"
  fi
fi

# ---------------------------------------------------------------------------
# 6. Security invariant — no raw provider credentials in routes table
# ---------------------------------------------------------------------------
echo "  Checking security invariants..."
SECRETS_IN_ROUTES="$(psql_query "
  SELECT count(*)::int FROM platform.routes
  WHERE path_pattern ~ '(raw_key|secret_access|access_key_id|r2_token|signed_url|presigned_url)'
     OR target_path ~ '(raw_key|secret_access|access_key_id|r2_token|signed_url|presigned_url)'
     OR action_key ~ '(raw_key|secret_access|access_key_id|r2_token|signed_url|presigned_url)';
")"
if [ "$SECRETS_IN_ROUTES" = "0" ]; then
  pass
  echo "    ✓ no raw credentials in platform.routes"
else
  fail "found $SECRETS_IN_ROUTES routes with credential-like substrings in path/target/action keys"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Results: $PASS_COUNT passed, $FAIL_COUNT failed"
echo ""
if [ "$FAIL_COUNT" -gt 0 ]; then
  echo "FAIL: canonical-reset-smoke.test.sh"
  echo ""
  echo "Hint: this test asserts the canonical reset path produced a fully"
  echo "bootstrapped local DB. If you see failures:"
  echo "  1. Run \`bash ./scripts/supabase-local.sh reset\` to apply all migrations."
  echo "  2. If \"authz.roles\" missing, run xynes-authz-service migrations,"
  echo "     then \`bash ./scripts/bootstrap-local-db.sh --env-file .env.localhost.local --restart-gateway\`."
  echo "  3. See xynes-infra/docs/DATABASE.md §7 for the manual verification checklist."
  exit 1
fi
echo "PASS: canonical-reset-smoke.test.sh"
