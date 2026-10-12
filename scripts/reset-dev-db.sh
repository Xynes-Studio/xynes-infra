#!/usr/bin/env bash
set -euo pipefail

# reset-dev-db.sh
#
# DANGEROUS: destructive wipe of application data in the dev Postgres.
# - Preserves Supabase-managed schemas by default (auth, storage, etc.)
# - Preserves platform.routes + platform.route_rate_limits by default

ENV_FILE="${XYNES_INFRA_ENV_FILE:-.env.localhost}"
YES=0
PLAN_ONLY=0
DRY_RUN=0
WIPE_ROUTES=0
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
Usage: ./scripts/reset-dev-db.sh [options]

Destructively wipes dev data from the Postgres database.

Defaults (safe-ish):
- Wipes application schemas: platform, identity, cms, docs, comments, telemetry, authz
- Preserves: platform.routes, platform.route_rate_limits
- Does NOT touch Supabase-managed schemas: auth, storage, supabase_migrations, etc.

Options:
  --env-file <path>           Env file to load (default: .env.localhost)
  --yes                       Skip interactive confirmation (required in CI/non-tty)
  --plan-only                 Print what would be wiped and exit (no DB access)
  --dry-run                   Print the tables that would be truncated and exit (requires DB access)
  --wipe-routes               Also wipe platform.routes + platform.route_rate_limits
  --include-supabase-managed  ALSO wipe auth/storage schemas (very dangerous)
  -h, --help                  Show this help

Environment:
  DATABASE_URL_ADMIN          Preferred admin connection string
  DATABASE_URL                Fallback connection string

Examples:
  # Show plan only
  ./scripts/reset-dev-db.sh --plan-only

  # Dry-run (lists tables)
  ./scripts/reset-dev-db.sh --env-file .env.localhost --yes --dry-run

  # Actually wipe dev data
  ./scripts/reset-dev-db.sh --env-file .env.localhost --yes
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

extract_db_name() {
  local url="${1:-}"
  echo "$url" | sed -E 's#^postgres(ql)?://[^/]+/([^?]+).*$#\2#'
}

print_plan() {
  echo "═══════════════════════════════════════════════════════════════════════════"
  echo "  RESET-DEV-DB: PLAN"
  echo "═══════════════════════════════════════════════════════════════════════════"
  echo "Env file: ${ENV_FILE}"
  echo ""

  echo "Schemas to wipe (application):"
  echo "  - platform"
  echo "  - identity"
  echo "  - cms"
  echo "  - docs"
  echo "  - comments"
  echo "  - telemetry"
  echo "  - authz"
  echo ""

  echo "Preserved tables (default):"
  if [ "$WIPE_ROUTES" = "1" ]; then
    echo "  (none; --wipe-routes enabled)"
  else
    echo "  - platform.routes"
    echo "  - platform.route_rate_limits"
  fi
  echo ""

  echo "Supabase-managed schemas:"
  if [ "$INCLUDE_SUPABASE_MANAGED" = "1" ]; then
    echo "  WARNING: will also wipe auth/storage (enabled)"
  else
    echo "  preserved (auth, storage, supabase_migrations, etc.)"
  fi
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
    --wipe-routes)
      WIPE_ROUTES=1
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
DB_HOST="$(extract_db_host "$NORMALIZED_DB_URL")"
DB_PORT="$(extract_db_port "$NORMALIZED_DB_URL")"
DB_NAME="$(extract_db_name "$NORMALIZED_DB_URL")"

echo ""
echo -e "${YELLOW}Target:${NC} $(mask_db_url "$NORMALIZED_DB_URL")"
echo -e "${YELLOW}Parsed:${NC} host=${DB_HOST} port=${DB_PORT} db=${DB_NAME}"
echo ""

echo -e "${RED}DANGER:${NC} This will permanently delete data."

echo ""

# Require non-interactive runs to pass --yes
if [ "$YES" != "1" ] && [ ! -t 0 ]; then
  echo -e "${RED}Error:${NC} non-interactive shell detected; pass --yes"
  exit 1
fi

if [ "$YES" != "1" ]; then
  echo "Type RESET to continue (or anything else to abort):"
  read -r confirm
  if [ "$confirm" != "RESET" ]; then
    echo "Cancelled."
    exit 1
  fi
fi

schemas=(platform identity cms docs comments telemetry authz)
if [ "$INCLUDE_SUPABASE_MANAGED" = "1" ]; then
  schemas+=(auth storage)
fi

# Build exclusion list (schema.table)
exclude_tables=()
if [ "$WIPE_ROUTES" != "1" ]; then
  exclude_tables+=("platform.routes" "platform.route_rate_limits")
fi

schema_list_sql="'${schemas[0]}'"
for s in "${schemas[@]:1}"; do
  schema_list_sql+=",'${s}'"
done

exclude_sql="''"
if [ ${#exclude_tables[@]} -gt 0 ]; then
  exclude_sql="'${exclude_tables[0]}'"
  for t in "${exclude_tables[@]:1}"; do
    exclude_sql+=",'${t}'"
  done
fi

# Gather all base tables to truncate.
# Note: we intentionally avoid touching supabase_migrations and other system schemas.
list_tables_query=$(cat <<SQL
SELECT quote_ident(table_schema) || '.' || quote_ident(table_name)
FROM information_schema.tables
WHERE table_type = 'BASE TABLE'
  AND table_schema IN (${schema_list_sql})
  AND (table_schema || '.' || table_name) NOT IN (${exclude_sql})
ORDER BY table_schema, table_name;
SQL
)

mapfile -t tables < <(PGCONNECT_TIMEOUT=5 psql -X -tA "$NORMALIZED_DB_URL" -c "$list_tables_query" 2>/dev/null || true)

if [ ${#tables[@]} -eq 0 ]; then
  echo -e "${YELLOW}Nothing to wipe:${NC} no matching tables found (or DB unreachable)."
  exit 0
fi

if [ "$DRY_RUN" = "1" ]; then
  echo -e "${BLUE}Dry run:${NC} would truncate ${#tables[@]} tables"
  for t in "${tables[@]}"; do
    echo "  - $t"
  done
  exit 0
fi

# Join with comma for TRUNCATE.
truncate_list=""
for t in "${tables[@]}"; do
  if [ -z "$truncate_list" ]; then
    truncate_list="$t"
  else
    truncate_list+=" , $t"
  fi
done

echo -e "${BLUE}Executing:${NC} TRUNCATE (${#tables[@]} tables)"

PGCONNECT_TIMEOUT=5 psql -X "$NORMALIZED_DB_URL" -v ON_ERROR_STOP=1 <<SQL
BEGIN;
TRUNCATE TABLE ${truncate_list} RESTART IDENTITY CASCADE;
COMMIT;
SQL

echo -e "${GREEN}Done:${NC} dev data wiped."
