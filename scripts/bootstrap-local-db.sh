#!/usr/bin/env bash
set -euo pipefail

# Bootstraps local DB tables required for smoke tests and local resets.
#
# Why this exists:
# - In local dev, some schemas (docs/cms/platform/authz) may exist but tables may not.
# - Supabase resets replay infra SQL migrations, but service-owned Drizzle migrations
#   still need to be applied afterward.
# - This script restores the minimal missing tables/seeds/constraints for local flows.
#
# Usage:
#   ./scripts/bootstrap-local-db.sh --env-file .env.dev.local

ENV_FILE="${XYNES_INFRA_ENV_FILE:-.env.dev.local}"
RESTART_GATEWAY=0
SKIP_CMS_SEED=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=./scripts/lib/env.sh
source "${SCRIPT_DIR}/lib/env.sh"

usage() {
  cat <<'EOF'
Usage: ./scripts/bootstrap-local-db.sh [options]

Options:
  --env-file <path>    env file to load (default: .env.dev.local)
  --restart-gateway    restart running gateway containers after route/bootstrap work
  --skip-cms-seed      skip CMS default content seed for platform workspaces
  -h, --help           show this help

Requires:
  - psql available on host
  - bun available on host
  - DATABASE_URL points at your local Postgres (db.local/host.docker.internal ok)

Behavior:
  - applies service-owned CMS Core Drizzle migrations
  - applies service-owned AUTHZ Drizzle migrations and seeds the RBAC catalog
  - seeds CMS defaults for existing platform.workspaces unless skipped
  - re-applies the STORAGE-LIVE-2 R2 dev workspace_storage_providers seed
  - verifies CMS tables and directory-first dashboard routes exist
  - verifies authz catalog is seeded (5 roles, ≥1 permission, ≥1 role-permission)
  - verifies every workspace has the R2 dev provider row
  - optionally restarts the gateway so DB-backed routes are reloaded
EOF
}

require_bin() {
  local bin="$1"
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "Error: '$bin' is required" >&2
    exit 1
  fi
}

resolve_path() {
  local file="$1"
  if [[ "$file" = /* ]]; then
    echo "$file"
  else
    echo "$ROOT_DIR/$file"
  fi
}

restart_gateway_if_running() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "[bootstrap-local-db] Docker not found; skipping gateway restart."
    return 0
  fi

  local containers
  containers="$(docker ps --format '{{.Names}}' | grep -E '(^|[-_])xynes-infra-gateway-1$|^xynes-infra-gateway-1$' || true)"
  if [ -z "$containers" ]; then
    echo "[bootstrap-local-db] Gateway container is not running; skipping restart."
    return 0
  fi

  while IFS= read -r container_name; do
    [ -z "$container_name" ] && continue
    echo "[bootstrap-local-db] Restarting gateway container: $container_name"
    docker restart "$container_name" >/dev/null
  done <<< "$containers"
}

while [ "${1:-}" != "" ]; do
  case "$1" in
    --env-file)
      if [ -z "${2:-}" ]; then
        echo "Error: --env-file requires a path" >&2
        usage
        exit 2
      fi
      ENV_FILE="$2"
      shift 2
      ;;
    --restart-gateway) RESTART_GATEWAY=1; shift ;;
    --skip-cms-seed) SKIP_CMS_SEED=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Error: unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

cd "$ROOT_DIR"

load_env_file_no_override "$ENV_FILE"
load_env_file_no_override ".env"

require_bin psql
require_bin bun

if [ -z "${DATABASE_URL:-}" ]; then
  echo "Error: DATABASE_URL is not set (env-file: $ENV_FILE)" >&2
  exit 1
fi

HOST_DB_URL="$(normalize_database_url_for_host "${DATABASE_URL}")"
if [ -z "$HOST_DB_URL" ]; then
  echo "Error: failed to normalize DATABASE_URL for host" >&2
  exit 1
fi

echo "[bootstrap-local-db] Using DB: $(echo "$HOST_DB_URL" | sed -E 's#://[^:]+:[^@]+@#://***:***@#')"

# 1) Docs tables (apply service-managed Drizzle migrations)
#
# As of 2026-05-13 this section delegates to `xynes-doc-service`'s
# Drizzle migrations (`drizzle-kit migrate`) instead of carrying its own
# `CREATE TABLE docs.documents` heredoc. Mirrors the pattern already used
# for `xynes-cms-core` below. Single source of truth: the doc-service repo.
DOC_ENV_FILE="$(resolve_path "$ENV_FILE")"
(
  cd "${ROOT_DIR}/../xynes-doc-service"
  bun --env-file="$DOC_ENV_FILE" x drizzle-kit migrate >/dev/null
)

# 2) CMS tables (apply service-managed Drizzle migrations)
CMS_ENV_FILE="$(resolve_path "$ENV_FILE")"
(
  cd "${ROOT_DIR}/../xynes-cms-core"
  bun --env-file="$CMS_ENV_FILE" run src/infra/db/migrate.ts >/dev/null
)

# 2a) AUTHZ tables (apply service-managed Drizzle migrations + seed RBAC catalog)
#
# Why this exists: a clean `supabase db reset` leaves the `authz` schema EMPTY
# (Supabase migrations don't own it). Without this step, the first
# `POST /workspaces` from the frontend creates a workspace row in the
# accounts-service handler and then fails with `relation "authz.roles"
# does not exist` when it tries to assign the caller `workspace_owner`,
# returning 502 BAD_GATEWAY. The user-facing symptom is the generic
# "Failed to create workspace. Please try again." error.
#
# Schema ownership: `authz.*` is owned by `xynes-authz-service` Drizzle
# migrations (`src/db/migrations/`). Catalog data (5 roles, 62
# permissions, 198 role-permission mappings) is owned by
# `src/db/seed/authz.seed.ts` and applied via `src/scripts/seed.ts`.
# See `docs/DATABASE.md` §2 and §4.
#
# Idempotency: both `drizzle-kit migrate` and `seedAuthz()` are
# idempotent (the seed uses `ON CONFLICT (key) DO NOTHING`). Safe to
# replay on every bootstrap invocation.
AUTHZ_ENV_FILE="$(resolve_path "$ENV_FILE")"
(
  cd "${ROOT_DIR}/../xynes-authz-service"
  bun --env-file="$AUTHZ_ENV_FILE" x drizzle-kit migrate >/dev/null
  bun --env-file="$AUTHZ_ENV_FILE" run src/scripts/seed.ts >/dev/null
)

# 3) CMS default seed for every local workspace
if [ "$SKIP_CMS_SEED" = "1" ]; then
  echo "[bootstrap-local-db] Skipping CMS seed."
else
  workspace_ids="$(psql "$HOST_DB_URL" -X -tA -v ON_ERROR_STOP=1 -c "SELECT id::text FROM platform.workspaces ORDER BY id;")"
  if [ -z "$workspace_ids" ]; then
    echo "[bootstrap-local-db] No platform.workspaces rows found; skipping CMS seed."
  else
    while IFS= read -r workspace_id; do
      [ -z "$workspace_id" ] && continue
      echo "[bootstrap-local-db] Seeding CMS defaults for workspace: $workspace_id"
      (
        cd "${ROOT_DIR}/../xynes-cms-core"
        DEFAULT_WORKSPACE_ID="$workspace_id" bun --env-file="$CMS_ENV_FILE" run src/infra/db/seed.ts >/dev/null
      )
    done <<< "$workspace_ids"
  fi
fi

# 3b) STORAGE-LIVE-2: re-apply the workspace_storage_providers R2 dev seed
#
# The infra Supabase migration `20260516090000_seed_workspace_storage_providers_r2_dev.sql`
# iterates `platform.workspaces` and idempotently UPSERTs one R2 dev row
# per workspace. At `supabase db reset` time there are zero workspaces so
# the migration is a safe no-op; after the sign-up flow creates the local
# workspace we re-play the same SQL here so the R2 dev row lands without
# requiring another `supabase db reset`.
#
# Idempotent via the partial unique index from STORAGE-2:
#   `(workspace_id, provider_kind, bucket) WHERE status <> 'disabled'`.
# Safe to run repeatedly. No-op when workspaces are absent.
STORAGE_LIVE_SEED="$ROOT_DIR/supabase/migrations/20260516090000_seed_workspace_storage_providers_r2_dev.sql"
if [ -f "$STORAGE_LIVE_SEED" ]; then
  echo "[bootstrap-local-db] Re-applying STORAGE-LIVE-2 R2 dev provider seed."
  psql "$HOST_DB_URL" -X -v ON_ERROR_STOP=1 -f "$STORAGE_LIVE_SEED" >/dev/null
else
  echo "[bootstrap-local-db] WARN: STORAGE-LIVE-2 seed migration not found at $STORAGE_LIVE_SEED" >&2
fi

# 4) Workspace invites table (platform.workspace_invites)
#
# As of 2026-05-13 this table is owned by an infra Supabase migration
# (`20260513140000_promote_workspace_invites_to_infra.sql`). The migration
# carries the same CREATE TABLE / FK definitions that previously lived
# here as a psql heredoc, plus the same conditional `authz.roles` FK
# (skipped if `authz.roles` doesn't exist yet).
#
# As of 2026-05-27, step 2a above applies authz Drizzle migrations
# before this block runs, so `authz.roles` always exists by the time
# this FK guard executes and the FK is added on every fresh reset. The
# `IF EXISTS … ELSE NOTICE` branch is retained as defense in depth for
# rare recovery flows (e.g. operator deletes the `authz` schema by
# hand) so the bootstrap stays idempotent and never hard-fails on a
# missing `authz.roles` table.
psql "$HOST_DB_URL" -v ON_ERROR_STOP=1 <<'SQL'
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'authz' AND table_name = 'roles'
  ) THEN
    IF NOT EXISTS (
      SELECT 1 FROM pg_constraint
      WHERE conrelid = 'platform.workspace_invites'::regclass
        AND conname = 'workspace_invites_role_key_roles_key_fk'
    ) THEN
      ALTER TABLE platform.workspace_invites
        ADD CONSTRAINT workspace_invites_role_key_roles_key_fk
        FOREIGN KEY (role_key) REFERENCES authz.roles(key);
    END IF;
  ELSE
    RAISE NOTICE 'workspace_invites_role_key_roles_key_fk still skipped: authz.roles not present. Run xynes-authz-service migrations, then re-run this bootstrap to add it.';
  END IF;
END $$;
SQL

# 5) Gateway body limits column (platform.routes.max_body_bytes)
psql "$HOST_DB_URL" -v ON_ERROR_STOP=1 <<'SQL'
ALTER TABLE platform.routes ADD COLUMN IF NOT EXISTS max_body_bytes integer;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'platform.routes'::regclass
      AND conname = 'max_body_bytes_non_negative'
  ) THEN
    ALTER TABLE platform.routes
      ADD CONSTRAINT max_body_bytes_non_negative
      CHECK (max_body_bytes IS NULL OR max_body_bytes >= 0);
  END IF;
END $$;

COMMENT ON COLUMN platform.routes.max_body_bytes IS
  'SEC-BODYLIMIT-1: Maximum allowed request body size in bytes. NULL = use default (1MB), 0 = reject bodies.';
SQL

# 6) Verify authz catalog is populated.
#
# Step 2a applies authz Drizzle migrations and runs the seed; this
# check is a defense-in-depth gate so a future regression that breaks
# either step fails the bootstrap loudly instead of producing a
# silently-broken stack where `POST /workspaces` 502s on role
# assignment. Expected baseline (per `src/db/seed/authz.seed.ts` +
# `src/db/seed/permissions.config.ts` in `xynes-authz-service`):
#   - exactly 5 roles (workspace_owner, workspace_member, content_editor,
#     read_only, super_admin)
#   - at least 1 permission row
#   - at least 1 role-permission mapping row
AUTHZ_ROLE_COUNT="$(psql "$HOST_DB_URL" -X -tA -v ON_ERROR_STOP=1 -c "SELECT count(*)::int FROM authz.roles;")"
AUTHZ_PERM_COUNT="$(psql "$HOST_DB_URL" -X -tA -v ON_ERROR_STOP=1 -c "SELECT count(*)::int FROM authz.permissions;")"
AUTHZ_ROLE_PERM_COUNT="$(psql "$HOST_DB_URL" -X -tA -v ON_ERROR_STOP=1 -c "SELECT count(*)::int FROM authz.role_permissions;")"

if [ "$AUTHZ_ROLE_COUNT" != "5" ]; then
  echo "Error: authz catalog verification failed; expected 5 roles, found $AUTHZ_ROLE_COUNT. Re-run step 2a of the bootstrap." >&2
  exit 1
fi

if [ "${AUTHZ_PERM_COUNT:-0}" -lt 1 ]; then
  echo "Error: authz catalog verification failed; expected ≥1 permission, found ${AUTHZ_PERM_COUNT:-0}. Re-run step 2a of the bootstrap." >&2
  exit 1
fi

if [ "${AUTHZ_ROLE_PERM_COUNT:-0}" -lt 1 ]; then
  echo "Error: authz catalog verification failed; expected ≥1 role-permission mapping, found ${AUTHZ_ROLE_PERM_COUNT:-0}. Re-run step 2a of the bootstrap." >&2
  exit 1
fi

# 7) Verify reset-critical CMS tables and dashboard routes
CMS_TABLE_COUNT="$(psql "$HOST_DB_URL" -X -tA -v ON_ERROR_STOP=1 <<'SQL'
WITH expected(table_schema, table_name) AS (
  VALUES
    ('cms', 'content_entries'),
    ('cms', 'content_types'),
    ('cms', 'global_content_templates'),
    ('cms', 'content_directories'),
    ('cms', 'content_entry_collaborators'),
    ('cms', 'content_entry_favorites')
)
SELECT count(*)::int
FROM expected e
JOIN information_schema.tables t
  ON t.table_schema = e.table_schema
 AND t.table_name = e.table_name;
SQL
)"

if [ "$CMS_TABLE_COUNT" != "6" ]; then
  echo "Error: CMS bootstrap verification failed; expected 6 required CMS tables, found $CMS_TABLE_COUNT" >&2
  exit 1
fi

CMS_ROUTE_COUNT="$(psql "$HOST_DB_URL" -X -tA -v ON_ERROR_STOP=1 <<'SQL'
WITH expected(action_key) AS (
  VALUES
    ('cms.content_directories.listForWorkspace'),
    ('cms.content_directories.create'),
    ('cms.content_directories.update'),
    ('cms.content_directories.delete'),
    ('cms.entry.listByDirectory'),
    ('cms.entry.create'),
    ('cms.entry.getById'),
    ('cms.entry.update'),
    ('cms.entry.delete'),
    ('cms.entry.publish'),
    ('cms.entry.status.set'),
    ('cms.entry.collaborators.set'),
    ('cms.entry.favorite.toggle'),
    ('cms.entry.favorite.list'),
    ('cms.entry.share.generateInternalLink')
)
SELECT count(*)::int
FROM expected e
JOIN platform.routes r
  ON r.action_key = e.action_key
 WHERE r.service_key = 'cms-core'
   AND r.workspace_scoped IS TRUE
   AND r.enabled IS TRUE;
SQL
)"

if [ "$CMS_ROUTE_COUNT" != "15" ]; then
  echo "Error: CMS route verification failed; expected 15 directory-first CMS routes, found $CMS_ROUTE_COUNT" >&2
  exit 1
fi

# 8) Verify STORAGE-LIVE-2 R2 dev provider row landed for each workspace.
#
# Expectation: every workspace has exactly one active R2 dev provider row
# pointing at bucket `xynes-storage-dev` with `credential_ref` set to the
# secret-manager pointer (NOT a raw token). If no workspaces exist this
# check is skipped (sign-up has not run yet).
STORAGE_PROVIDER_MISMATCH="$(psql "$HOST_DB_URL" -X -tA -v ON_ERROR_STOP=1 <<'SQL'
WITH ws AS (
  SELECT id FROM platform.workspaces
),
ok AS (
  SELECT workspace_id
  FROM platform.workspace_storage_providers
  WHERE provider_kind = 'r2'
    AND bucket = 'xynes-storage-dev'
    AND status = 'active'
    AND credential_ref = 'secret://xynes/storage/r2-dev'
)
SELECT count(*)::int FROM ws
LEFT JOIN ok ON ok.workspace_id = ws.id
WHERE ok.workspace_id IS NULL;
SQL
)"

if [ -z "$STORAGE_PROVIDER_MISMATCH" ]; then
  STORAGE_PROVIDER_MISMATCH=0
fi

if [ "$STORAGE_PROVIDER_MISMATCH" != "0" ]; then
  echo "Error: STORAGE-LIVE-2 verification failed; $STORAGE_PROVIDER_MISMATCH workspace(s) missing the R2 dev provider row" >&2
  exit 1
fi

if [ "$RESTART_GATEWAY" = "1" ]; then
  restart_gateway_if_running
fi

echo "[bootstrap-local-db] Done."
