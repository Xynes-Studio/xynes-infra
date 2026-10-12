#!/usr/bin/env bash
set -euo pipefail

# wipe-identity.sh
#
# Dev-only helper to remove a single identity (user) and related data.
# Default behavior:
# - Deletes workspaces owned by the user (platform.workspaces.created_by = user)
# - Deletes the user's memberships/roles (user_id = user)
# - Deletes identity.users row (identity.users.id = user)
# - Does NOT touch Supabase-managed schemas (auth, storage) unless explicitly enabled

ENV_FILE="${XYNES_INFRA_ENV_FILE:-.env.localhost}"
YES=0
PLAN_ONLY=0
DRY_RUN=0
USER_ID=""
EMAIL=""
WIPE_OWNED_WORKSPACES=1
INCLUDE_SUPABASE_MANAGED=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=./scripts/lib/colors.sh
source "${SCRIPT_DIR}/lib/colors.sh"
# shellcheck source=./scripts/lib/env.sh
source "${SCRIPT_DIR}/lib/env.sh"
# shellcheck source=./scripts/lib/db.sh
source "${SCRIPT_DIR}/lib/db.sh"
# shellcheck source=./scripts/lib/http.sh
source "${SCRIPT_DIR}/lib/http.sh"

usage() {
  cat <<'USAGE'
Usage: ./scripts/wipe-identity.sh [options]

Destructively deletes a single user (identity) from the dev database.

Options:
  --user-id <uuid>            User UUID (preferred)
  --email <email>             Resolve UUID via identity.users.email
  --no-owned-workspaces       Do NOT delete owned workspaces (will error if user owns any)
  --include-supabase-managed  ALSO delete from auth.users (very dangerous)
  --env-file <path>           Env file to load (default: .env.localhost)
  --yes                       Skip interactive confirmation (required in CI/non-tty)
  --plan-only                 Print what would be deleted and exit (no DB access)
  --dry-run                   Show counts + affected IDs and exit (requires DB access)
  -h, --help                  Show this help

Environment:
  DATABASE_URL_ADMIN          Preferred admin connection string
  DATABASE_URL                Fallback connection string

Examples:
  # Resolve by email and wipe
  ./scripts/wipe-identity.sh --env-file .env.localhost --email alice@example.com --yes

  # Resolve by UUID and dry-run
  ./scripts/wipe-identity.sh --user-id <uuid> --yes --dry-run
USAGE
}

mask_db_url() {
  local url="${1:-}"
  if [ -z "$url" ]; then
    echo ""
    return 0
  fi
  echo "$url" | sed -E 's#(postgres(ql)?://[^:]+):[^@]+@#\1:****@#'
}

is_uuid() {
  local v="${1:-}"
  [[ "$v" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]
}

print_plan() {
  echo "═══════════════════════════════════════════════════════════════════════════"
  echo "  WIPE-IDENTITY: PLAN"
  echo "═══════════════════════════════════════════════════════════════════════════"
  echo "Env file: ${ENV_FILE}"
  echo ""

  echo "Deletes (default):"
  echo "  - identity.users row for the user"
  echo "  - rows with user_id = user in application schemas"
  echo "  - owned workspaces (platform.workspaces.created_by = user) and all workspace_id-linked rows"
  echo ""

  if [ "$INCLUDE_SUPABASE_MANAGED" = "1" ]; then
    echo -e "${YELLOW}WARNING:${NC} will also delete from auth.users (Supabase-managed)"
  else
    echo "Supabase-managed schemas preserved (auth, storage, etc.)"
  fi
  echo ""
}

cd "$ROOT_DIR"

while [ "${1:-}" != "" ]; do
  case "$1" in
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    --yes)
      YES=1
      shift
      ;;
    --plan-only)
      PLAN_ONLY=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --user-id)
      USER_ID="${2:-}"
      shift 2
      ;;
    --email)
      EMAIL="${2:-}"
      shift 2
      ;;
    --no-owned-workspaces)
      WIPE_OWNED_WORKSPACES=0
      shift
      ;;
    --include-supabase-managed)
      INCLUDE_SUPABASE_MANAGED=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo -e "${RED}Error:${NC} unknown arg: $1"
      usage
      exit 2
      ;;
  esac
done

print_plan

if [ "$PLAN_ONLY" = "1" ]; then
  exit 0
fi

require_bin psql

# Load env (do not override already-set vars)
load_env_file_no_override "$ENV_FILE"
load_env_file_no_override ".env"
load_env_file_no_override ".env.localhost"
load_env_file_no_override ".env.local"

DB_URL="${DATABASE_URL_ADMIN:-${DATABASE_URL:-}}"
if [ -z "$DB_URL" ]; then
  echo -e "${RED}Error:${NC} DATABASE_URL_ADMIN or DATABASE_URL is required"
  exit 1
fi

NORMALIZED_DB_URL="$(normalize_database_url_for_host "$DB_URL")"

echo ""
echo -e "${YELLOW}Target:${NC} $(mask_db_url "$NORMALIZED_DB_URL")"

# Require non-interactive runs to pass --yes
if [ "$YES" != "1" ] && [ ! -t 0 ]; then
  echo -e "${RED}Error:${NC} non-interactive shell detected; pass --yes"
  exit 1
fi

if [ -z "$USER_ID" ]; then
  if [ -z "$EMAIL" ]; then
    echo -e "${RED}Error:${NC} --user-id or --email is required"
    exit 2
  fi

  USER_ID="$(PGCONNECT_TIMEOUT=5 psql -X -tA "$NORMALIZED_DB_URL" -c "SELECT id::text FROM identity.users WHERE lower(email) = lower('$EMAIL') LIMIT 1;" 2>/dev/null || true)"
  if [ -z "$USER_ID" ]; then
    echo -e "${RED}Error:${NC} no identity.users row found for email: $EMAIL"
    exit 1
  fi
fi

if ! is_uuid "$USER_ID"; then
  echo -e "${RED}Error:${NC} invalid UUID: $USER_ID"
  exit 2
fi

USER_EMAIL="$(PGCONNECT_TIMEOUT=5 psql -X -tA "$NORMALIZED_DB_URL" -c "SELECT email::text FROM identity.users WHERE id = '$USER_ID' LIMIT 1;" 2>/dev/null || true)"
if [ -z "$USER_EMAIL" ]; then
  USER_EMAIL="(not found in identity.users)"
fi

mapfile -t owned_workspaces < <(PGCONNECT_TIMEOUT=5 psql -X -tA "$NORMALIZED_DB_URL" -c "SELECT id::text FROM platform.workspaces WHERE created_by = '$USER_ID' ORDER BY created_at DESC;" 2>/dev/null || true)

if [ "$WIPE_OWNED_WORKSPACES" != "1" ] && [ ${#owned_workspaces[@]} -gt 0 ]; then
  echo -e "${RED}Error:${NC} user owns ${#owned_workspaces[@]} workspaces; refusing because --no-owned-workspaces set"
  echo "Owned workspace IDs:"
  for wid in "${owned_workspaces[@]}"; do
    echo "  - $wid"
  done
  exit 1
fi

# Identify all tables in application schemas that carry workspace_id/user_id.
app_schemas=(platform identity cms docs comments telemetry authz)
if [ "$INCLUDE_SUPABASE_MANAGED" = "1" ]; then
  app_schemas+=(auth)
fi

schema_list_sql="'${app_schemas[0]}'"
for s in "${app_schemas[@]:1}"; do
  schema_list_sql+=",'${s}'"
done

workspace_tables_query=$(cat <<SQL
SELECT table_schema || '|' || table_name
FROM information_schema.columns
WHERE column_name = 'workspace_id'
  AND table_schema IN (${schema_list_sql})
  AND table_name <> 'workspaces'
  AND table_type = 'BASE TABLE'
ORDER BY table_schema, table_name;
SQL
)

user_tables_query=$(cat <<SQL
SELECT table_schema || '|' || table_name
FROM information_schema.columns
WHERE column_name = 'user_id'
  AND table_schema IN (${schema_list_sql})
  AND table_type = 'BASE TABLE'
ORDER BY table_schema, table_name;
SQL
)

mapfile -t workspace_tables < <(PGCONNECT_TIMEOUT=5 psql -X -tA "$NORMALIZED_DB_URL" -c "$workspace_tables_query" 2>/dev/null || true)
mapfile -t user_tables < <(PGCONNECT_TIMEOUT=5 psql -X -tA "$NORMALIZED_DB_URL" -c "$user_tables_query" 2>/dev/null || true)

if [ "$DRY_RUN" = "1" ]; then
  echo ""
  echo -e "${BLUE}Dry run:${NC}"
  echo "User: $USER_ID ($USER_EMAIL)"
  echo "Owned workspaces: ${#owned_workspaces[@]}"
  for wid in "${owned_workspaces[@]}"; do
    slug="$(PGCONNECT_TIMEOUT=5 psql -X -tA "$NORMALIZED_DB_URL" -c "SELECT slug::text FROM platform.workspaces WHERE id = '$wid' LIMIT 1;" 2>/dev/null || true)"
    echo "  - $wid (slug=${slug:-?})"
  done
  echo ""
  echo "workspace_id tables to scan: ${#workspace_tables[@]}"
  echo "user_id tables to scan: ${#user_tables[@]}"
  exit 0
fi

echo ""
echo -e "${RED}DANGER:${NC} This will permanently delete data for:"
echo "  userId: $USER_ID"
echo "  email:  $USER_EMAIL"
echo "  owned workspaces to delete: ${#owned_workspaces[@]}"

if [ "$INCLUDE_SUPABASE_MANAGED" = "1" ]; then
  echo -e "${YELLOW}  plus: auth.users row (Supabase-managed)${NC}"
fi

echo ""

if [ "$YES" != "1" ]; then
  echo "Type WIPE to continue (or anything else to abort):"
  read -r confirm
  if [ "$confirm" != "WIPE" ]; then
    echo "Cancelled."
    exit 1
  fi
fi

# Generate SQL
TMP_SQL="$(mktemp)"

{
  echo "BEGIN;"

  # 1) Delete all owned workspace-linked rows, then workspace rows.
  for wid in "${owned_workspaces[@]}"; do
    for entry in "${workspace_tables[@]}"; do
      schema="${entry%%|*}"
      table="${entry##*|}"
      echo "DELETE FROM \"${schema}\".\"${table}\" WHERE workspace_id = '${wid}';"
    done
    echo "DELETE FROM platform.workspaces WHERE id = '${wid}';"
  done

  # 2) Delete user-linked rows (memberships/roles/etc.)
  for entry in "${user_tables[@]}"; do
    schema="${entry%%|*}"
    table="${entry##*|}"
    echo "DELETE FROM \"${schema}\".\"${table}\" WHERE user_id = '${USER_ID}';"
  done

  # 3) Delete identity.users row
  echo "DELETE FROM identity.users WHERE id = '${USER_ID}';"

  # 4) Optional: delete from auth.users (Supabase-managed)
  if [ "$INCLUDE_SUPABASE_MANAGED" = "1" ]; then
    echo "DELETE FROM auth.users WHERE id = '${USER_ID}';"
  fi

  echo "COMMIT;"
} >"$TMP_SQL"

PGCONNECT_TIMEOUT=5 psql -X "$NORMALIZED_DB_URL" -v ON_ERROR_STOP=1 -f "$TMP_SQL"
rm -f "$TMP_SQL"

echo -e "${GREEN}Done:${NC} identity wiped for userId=$USER_ID"
