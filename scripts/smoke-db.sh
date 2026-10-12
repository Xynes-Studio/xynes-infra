#!/usr/bin/env bash
set -euo pipefail

# SMOKE-DB – Database connection and schema verification smoke tests
#
# This script validates database connectivity and schema integrity:
# - SSH tunnel connectivity check
# - Database connection test
# - Schema existence verification
# - Table structure validation
# - Seed data operations
#
# Security: Uses SSH tunnel for all database connections
# Usage: ./scripts/smoke-db.sh --env-file .env.localhost
#
# Prerequisites:
#   SSH tunnel must be active: ssh -N -L 5432:127.0.0.1:5432 xynes@84.247.176.134

MODE="${MODE:-localhost}"
ENV_FILE="${XYNES_INFRA_ENV_FILE:-.env.localhost}"
VERBOSE="${VERBOSE:-0}"
SEED_DATA="${SEED_DATA:-0}"
CLEANUP="${CLEANUP:-0}"
VERIFY_ONLY="${VERIFY_ONLY:-0}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=./scripts/lib/colors.sh
source "${SCRIPT_DIR}/lib/colors.sh"
# shellcheck source=./scripts/lib/env.sh
source "${SCRIPT_DIR}/lib/env.sh"
# shellcheck source=./scripts/lib/db.sh
source "${SCRIPT_DIR}/lib/db.sh"
# shellcheck source=./scripts/lib/db-actions.sh
source "${SCRIPT_DIR}/lib/db-actions.sh"
# shellcheck source=./scripts/lib/seed.sh
source "${SCRIPT_DIR}/lib/seed.sh"

PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

usage() {
  cat <<'EOF'
Usage: ./scripts/smoke-db.sh [options]

Database connection and schema verification smoke tests.

Options:
  --env-file <path>        env file to load (default: .env.localhost)
  --seed                   seed smoke test data
  --cleanup                cleanup smoke test data
  --verify-only            only verify prerequisites (quick check)
  --verbose                show detailed output
  -h, --help               show this help

Environment:
  DATABASE_URL             Required - PostgreSQL connection string

Prerequisites:
  SSH tunnel must be active for remote database access:
    ssh -N -L 5432:127.0.0.1:5432 xynes@84.247.176.134

Examples:
  # Quick connectivity check
  ./scripts/smoke-db.sh --verify-only

  # Full smoke test with seed data
  ./scripts/smoke-db.sh --seed

  # Cleanup after testing
  ./scripts/smoke-db.sh --cleanup
EOF
}

log_pass() { echo -e "${GREEN}PASS${NC} $1"; ((PASS_COUNT++)) || true; }
log_fail() { echo -e "${RED}FAIL${NC} $1"; ((FAIL_COUNT++)) || true; }
log_skip() { echo -e "${YELLOW}SKIP${NC} $1"; ((SKIP_COUNT++)) || true; }
log_info() { echo -e "${BLUE}INFO${NC} $1"; }
log_section() { echo -e "\n${BLUE}━━━ $1 ━━━${NC}"; }

# ─────────────────────────────────────────────────────────────────────────────
# Parse args
# ─────────────────────────────────────────────────────────────────────────────

cd "$ROOT_DIR"

while [ "${1:-}" != "" ]; do
  case "$1" in
    --env-file) ENV_FILE="${2:-}"; shift 2 ;;
    --seed) SEED_DATA=1; shift ;;
    --cleanup) CLEANUP=1; shift ;;
    --verify-only) VERIFY_ONLY=1; shift ;;
    --verbose) VERBOSE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo -e "${RED}Error:${NC} unknown arg: $1"; usage; exit 2 ;;
  esac
done

# Load environment
load_env_file_no_override "$ENV_FILE"
load_env_file_no_override ".env"

echo "═══════════════════════════════════════════════════════════════════════════"
echo "  SMOKE-DB: Database Connection & Schema Tests"
echo "═══════════════════════════════════════════════════════════════════════════"
echo "Env: $ENV_FILE"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# 1. Prerequisites Check
# ─────────────────────────────────────────────────────────────────────────────

log_section "1. Prerequisites Check"

# Check required tools
if command -v psql >/dev/null 2>&1; then
  log_pass "psql available"
else
  log_fail "psql not found - install postgresql-client"
  echo ""
  echo "Install with:"
  echo "  macOS: brew install postgresql"
  echo "  Ubuntu: apt install postgresql-client"
  exit 1
fi

if command -v nc >/dev/null 2>&1; then
  log_pass "nc (netcat) available"
else
  log_skip "nc not available (optional)"
fi

# Check DATABASE_URL
if [ -z "${DATABASE_URL:-}" ]; then
  log_fail "DATABASE_URL not set"
  echo ""
  echo "Set DATABASE_URL in $ENV_FILE or environment:"
  echo "  export DATABASE_URL=\"postgresql://user:pass@localhost:5432/postgres\""
  exit 1
else
  # Mask password in output
  local_url="${DATABASE_URL}"
  masked_url="$(echo "$local_url" | sed -E 's/:[^:@]+@/:****@/')"
  log_pass "DATABASE_URL set: $masked_url"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 2. SSH Tunnel Check
# ─────────────────────────────────────────────────────────────────────────────

log_section "2. SSH Tunnel Check"

DB_PORT="$(extract_db_port "${DATABASE_URL}")"
DB_HOST="$(extract_db_host "${DATABASE_URL}")"

# Normalize for localhost
case "$DB_HOST" in
  localhost|127.0.0.1)
    if db_ssh_tunnel_check "$DB_PORT"; then
      log_pass "SSH tunnel active on port $DB_PORT"
    else
      log_fail "SSH tunnel not detected on port $DB_PORT"
      echo ""
      db_ssh_tunnel_hint
      exit 1
    fi
    ;;
  host.docker.internal|db.local)
    log_info "Docker host: $DB_HOST"
    if db_ssh_tunnel_check "$DB_PORT"; then
      log_pass "SSH tunnel active on port $DB_PORT"
    else
      log_fail "SSH tunnel not detected on port $DB_PORT"
      echo ""
      echo "For Docker mode, bind SSH tunnel to all interfaces:"
      echo "  ssh -N -L 0.0.0.0:$DB_PORT:127.0.0.1:5432 xynes@84.247.176.134"
      exit 1
    fi
    ;;
  *)
    log_info "Using remote host: $DB_HOST:$DB_PORT"
    ;;
esac

# ─────────────────────────────────────────────────────────────────────────────
# 3. Database Connection Test
# ─────────────────────────────────────────────────────────────────────────────

log_section "3. Database Connection Test"

if db_test_connection "$DATABASE_URL"; then
  log_pass "Database connection successful"
else
  log_fail "Database connection failed"
  echo ""
  echo "Verify:"
  echo "  1. SSH tunnel is active"
  echo "  2. DATABASE_URL credentials are correct"
  echo "  3. Database server is running"
  exit 1
fi

# Quick version check
DB_VERSION="$(db_query "SELECT version();" "$DATABASE_URL" | head -1)"
if [ -n "$DB_VERSION" ]; then
  log_info "PostgreSQL: $(echo "$DB_VERSION" | cut -d' ' -f1-2)"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 4. Schema Verification
# ─────────────────────────────────────────────────────────────────────────────

log_section "4. Schema Verification"

REQUIRED_SCHEMAS=("platform" "identity" "cms" "docs" "authz" "telemetry")

for schema in "${REQUIRED_SCHEMAS[@]}"; do
  if db_check_schema "$schema" "$DATABASE_URL"; then
    log_pass "Schema: $schema"
  else
    log_fail "Schema: $schema (missing)"
  fi
done

# If verify-only mode, stop here
if [ "$VERIFY_ONLY" = "1" ]; then
  echo ""
  echo "═══════════════════════════════════════════════════════════════════════════"
  echo "  VERIFY-ONLY SUMMARY"
  echo "═══════════════════════════════════════════════════════════════════════════"
  echo -e "  ${GREEN}PASS${NC}: $PASS_COUNT"
  echo -e "  ${RED}FAIL${NC}: $FAIL_COUNT"
  [ "$FAIL_COUNT" -gt 0 ] && exit 1
  exit 0
fi

# ─────────────────────────────────────────────────────────────────────────────
# 5. Table Verification
# ─────────────────────────────────────────────────────────────────────────────

log_section "5. Table Verification"

# Platform tables
PLATFORM_TABLES=("workspaces" "workspace_members")
for table in "${PLATFORM_TABLES[@]}"; do
  if db_check_table "platform" "$table" "$DATABASE_URL"; then
    count="$(db_row_count "platform" "$table" "$DATABASE_URL")"
    log_pass "platform.$table ($count rows)"
  else
    log_fail "platform.$table (missing)"
  fi
done

# Identity tables
IDENTITY_TABLES=("users")
for table in "${IDENTITY_TABLES[@]}"; do
  if db_check_table "identity" "$table" "$DATABASE_URL"; then
    count="$(db_row_count "identity" "$table" "$DATABASE_URL")"
    log_pass "identity.$table ($count rows)"
  else
    log_fail "identity.$table (missing)"
  fi
done

# CMS tables
CMS_TABLES=("global_content_templates" "content_types" "content_entries" "comments")
for table in "${CMS_TABLES[@]}"; do
  if db_check_table "cms" "$table" "$DATABASE_URL"; then
    count="$(db_row_count "cms" "$table" "$DATABASE_URL")"
    log_pass "cms.$table ($count rows)"
  else
    log_fail "cms.$table (missing)"
  fi
done

# Docs tables
DOCS_TABLES=("documents")
for table in "${DOCS_TABLES[@]}"; do
  if db_check_table "docs" "$table" "$DATABASE_URL"; then
    count="$(db_row_count "docs" "$table" "$DATABASE_URL")"
    log_pass "docs.$table ($count rows)"
  else
    log_fail "docs.$table (missing)"
  fi
done

# Authz tables
AUTHZ_TABLES=("roles" "permissions" "role_permissions" "user_roles")
for table in "${AUTHZ_TABLES[@]}"; do
  if db_check_table "authz" "$table" "$DATABASE_URL"; then
    count="$(db_row_count "authz" "$table" "$DATABASE_URL")"
    log_pass "authz.$table ($count rows)"
  else
    log_fail "authz.$table (missing)"
  fi
done

# Telemetry tables
TELEMETRY_TABLES=("events")
for table in "${TELEMETRY_TABLES[@]}"; do
  if db_check_table "telemetry" "$table" "$DATABASE_URL"; then
    count="$(db_row_count "telemetry" "$table" "$DATABASE_URL")"
    log_pass "telemetry.$table ($count rows)"
  else
    log_fail "telemetry.$table (missing)"
  fi
done

# ─────────────────────────────────────────────────────────────────────────────
# 6. Data Integrity Checks
# ─────────────────────────────────────────────────────────────────────────────

log_section "6. Data Integrity Checks"

# Check for global templates
TEMPLATE_COUNT="$(db_row_count "cms" "global_content_templates" "$DATABASE_URL")"
if [ "$TEMPLATE_COUNT" -gt 0 ]; then
  log_pass "Global templates seeded ($TEMPLATE_COUNT)"
else
  log_skip "No global templates - run CMS service seed"
fi

# Check for roles
ROLE_COUNT="$(db_row_count "authz" "roles" "$DATABASE_URL")"
if [ "$ROLE_COUNT" -gt 0 ]; then
  log_pass "Authz roles seeded ($ROLE_COUNT)"
else
  log_skip "No authz roles - run authz service seed"
fi

# Check workspace-user relationships
WS_COUNT="$(db_row_count "platform" "workspaces" "$DATABASE_URL")"
if [ "$WS_COUNT" -gt 0 ]; then
  # Verify workspace members exist for each workspace
  ORPHAN_WS="$(db_query "SELECT COUNT(*) FROM platform.workspaces w WHERE NOT EXISTS (SELECT 1 FROM platform.workspace_members wm WHERE wm.workspace_id = w.id);" "$DATABASE_URL")"
  if [ "${ORPHAN_WS:-0}" = "0" ]; then
    log_pass "All workspaces have members"
  else
    log_info "Orphan workspaces (no members): $ORPHAN_WS"
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# 7. Seed Data (if requested)
# ─────────────────────────────────────────────────────────────────────────────

if [ "$SEED_DATA" = "1" ]; then
  log_section "7. Seed Smoke Test Data"
  seed_smoke_test_data "$DATABASE_URL"
  
  echo ""
  echo "To use seeded data in other scripts:"
  get_smoke_test_ids "$DATABASE_URL"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 8. Cleanup (if requested)
# ─────────────────────────────────────────────────────────────────────────────

if [ "$CLEANUP" = "1" ]; then
  log_section "8. Cleanup Smoke Test Data"
  cleanup_smoke_test_data "$DATABASE_URL"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────

echo ""
echo "═══════════════════════════════════════════════════════════════════════════"
echo "  SUMMARY"
echo "═══════════════════════════════════════════════════════════════════════════"
echo -e "  ${GREEN}PASS${NC}: $PASS_COUNT"
echo -e "  ${RED}FAIL${NC}: $FAIL_COUNT"
echo -e "  ${YELLOW}SKIP${NC}: $SKIP_COUNT"
echo ""

if [ "$FAIL_COUNT" -gt 0 ]; then
  echo -e "${RED}Some checks failed!${NC}"
  exit 1
fi

echo -e "${GREEN}All database checks passed!${NC}"

# Export useful variables
if [ "$WS_COUNT" -gt 0 ] 2>/dev/null; then
  echo ""
  echo "Quick IDs for further testing:"
  FIRST_WS="$(db_get_workspace_id "$DATABASE_URL")"
  FIRST_USER="$(db_get_user_id "$DATABASE_URL")"
  [ -n "$FIRST_WS" ] && echo "export WORKSPACE_ID=\"$FIRST_WS\""
  [ -n "$FIRST_USER" ] && echo "export USER_ID=\"$FIRST_USER\""
fi

exit 0
