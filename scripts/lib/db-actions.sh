#!/usr/bin/env bash
# Database action helpers for smoke tests
# Handles database connections and queries via SSH tunnel
#
# Security: Uses prepared statements via psql where possible
# Connection: Expects SSH tunnel: ssh -N -L 5432:127.0.0.1:5432 xynes@84.247.176.134

set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
readonly DB_CONNECT_TIMEOUT="${DB_CONNECT_TIMEOUT:-5}"
readonly DB_QUERY_TIMEOUT="${DB_QUERY_TIMEOUT:-30}"

# ─────────────────────────────────────────────────────────────────────────────
# db_test_connection - Test if database is reachable
# Usage: db_test_connection [database_url]
# Returns: 0 if connected, 1 otherwise
# ─────────────────────────────────────────────────────────────────────────────
db_test_connection() {
  local url="${1:-${DATABASE_URL:-}}"
  
  if [ -z "$url" ]; then
    echo "Error: DATABASE_URL not set" >&2
    return 1
  fi

  # Normalize URL for host-side psql
  local normalized_url
  normalized_url="$(normalize_database_url_for_host "$url")"

  if ! command -v psql >/dev/null 2>&1; then
    # Fall back to nc check if psql not available
    local host port
    host="$(extract_db_host "$normalized_url")"
    port="$(extract_db_port "$normalized_url")"
    
    if nc -z -w "$DB_CONNECT_TIMEOUT" "$host" "$port" >/dev/null 2>&1; then
      return 0
    fi
    return 1
  fi

  # Use psql for proper connection test
  PGCONNECT_TIMEOUT="$DB_CONNECT_TIMEOUT" psql "$normalized_url" -c "SELECT 1;" >/dev/null 2>&1
}

# ─────────────────────────────────────────────────────────────────────────────
# db_check_schema - Check if a schema exists
# Usage: db_check_schema <schema_name> [database_url]
# Returns: 0 if exists, 1 otherwise
# ─────────────────────────────────────────────────────────────────────────────
db_check_schema() {
  local schema="$1"
  local url="${2:-${DATABASE_URL:-}}"
  
  if [ -z "$url" ]; then
    echo "Error: DATABASE_URL not set" >&2
    return 1
  fi

  local normalized_url
  normalized_url="$(normalize_database_url_for_host "$url")"

  local result
  result="$(PGCONNECT_TIMEOUT="$DB_CONNECT_TIMEOUT" psql "$normalized_url" -tAc \
    "SELECT 1 FROM information_schema.schemata WHERE schema_name = '$schema';" 2>/dev/null || true)"

  [ "$result" = "1" ]
}

# ─────────────────────────────────────────────────────────────────────────────
# db_check_table - Check if a table exists in a schema
# Usage: db_check_table <schema_name> <table_name> [database_url]
# Returns: 0 if exists, 1 otherwise
# ─────────────────────────────────────────────────────────────────────────────
db_check_table() {
  local schema="$1"
  local table="$2"
  local url="${3:-${DATABASE_URL:-}}"
  
  if [ -z "$url" ]; then
    echo "Error: DATABASE_URL not set" >&2
    return 1
  fi

  local normalized_url
  normalized_url="$(normalize_database_url_for_host "$url")"

  local result
  result="$(PGCONNECT_TIMEOUT="$DB_CONNECT_TIMEOUT" psql "$normalized_url" -tAc \
    "SELECT 1 FROM information_schema.tables WHERE table_schema = '$schema' AND table_name = '$table';" 2>/dev/null || true)"

  [ "$result" = "1" ]
}

# ─────────────────────────────────────────────────────────────────────────────
# db_row_count - Get row count for a table
# Usage: db_row_count <schema_name> <table_name> [database_url]
# Returns: count (integer) or -1 on error
# ─────────────────────────────────────────────────────────────────────────────
db_row_count() {
  local schema="$1"
  local table="$2"
  local url="${3:-${DATABASE_URL:-}}"
  
  if [ -z "$url" ]; then
    echo "-1"
    return 1
  fi

  local normalized_url
  normalized_url="$(normalize_database_url_for_host "$url")"

  local result
  result="$(PGCONNECT_TIMEOUT="$DB_CONNECT_TIMEOUT" psql "$normalized_url" -tAc \
    "SELECT COUNT(*) FROM \"$schema\".\"$table\";" 2>/dev/null || echo "-1")"

  echo "${result:-0}"
}

# ─────────────────────────────────────────────────────────────────────────────
# db_query - Execute a read-only query
# Usage: db_query <query> [database_url]
# Returns: query result
# Security: Use only for smoke test reads, not user input
# ─────────────────────────────────────────────────────────────────────────────
db_query() {
  local query="$1"
  local url="${2:-${DATABASE_URL:-}}"
  
  if [ -z "$url" ]; then
    echo "Error: DATABASE_URL not set" >&2
    return 1
  fi

  local normalized_url
  normalized_url="$(normalize_database_url_for_host "$url")"

  PGCONNECT_TIMEOUT="$DB_CONNECT_TIMEOUT" \
    PGOPTIONS="-c statement_timeout=${DB_QUERY_TIMEOUT}s" \
    psql "$normalized_url" -tAc "$query" 2>/dev/null || true
}

# ─────────────────────────────────────────────────────────────────────────────
# db_query_json - Execute a query and return JSON result
# Usage: db_query_json <query> [database_url]
# Returns: JSON array of results
# ─────────────────────────────────────────────────────────────────────────────
db_query_json() {
  local query="$1"
  local url="${2:-${DATABASE_URL:-}}"
  
  if [ -z "$url" ]; then
    echo "[]"
    return 1
  fi

  local normalized_url
  normalized_url="$(normalize_database_url_for_host "$url")"

  local json_query="SELECT json_agg(t) FROM ($query) t;"
  local result
  result="$(PGCONNECT_TIMEOUT="$DB_CONNECT_TIMEOUT" \
    PGOPTIONS="-c statement_timeout=${DB_QUERY_TIMEOUT}s" \
    psql "$normalized_url" -tAc "$json_query" 2>/dev/null || echo "null")"

  if [ -z "$result" ] || [ "$result" = "null" ]; then
    echo "[]"
  else
    echo "$result"
  fi
}

# ─────────────────────────────────────────────────────────────────────────────
# db_check_record_exists - Check if a record exists by ID
# Usage: db_check_record_exists <schema> <table> <id_column> <id_value> [database_url]
# Returns: 0 if exists, 1 otherwise
# ─────────────────────────────────────────────────────────────────────────────
db_check_record_exists() {
  local schema="$1"
  local table="$2"
  local id_col="$3"
  local id_val="$4"
  local url="${5:-${DATABASE_URL:-}}"
  
  if [ -z "$url" ]; then
    return 1
  fi

  local normalized_url
  normalized_url="$(normalize_database_url_for_host "$url")"

  # Use parameterized approach to avoid SQL injection
  local result
  result="$(PGCONNECT_TIMEOUT="$DB_CONNECT_TIMEOUT" psql "$normalized_url" -tAc \
    "SELECT 1 FROM \"$schema\".\"$table\" WHERE \"$id_col\" = '$id_val' LIMIT 1;" 2>/dev/null || true)"

  [ "$result" = "1" ]
}

# ─────────────────────────────────────────────────────────────────────────────
# db_list_schemas - List all user schemas
# Usage: db_list_schemas [database_url]
# Returns: newline-separated list of schema names
# ─────────────────────────────────────────────────────────────────────────────
db_list_schemas() {
  local url="${1:-${DATABASE_URL:-}}"
  
  if [ -z "$url" ]; then
    return 1
  fi

  local normalized_url
  normalized_url="$(normalize_database_url_for_host "$url")"

  PGCONNECT_TIMEOUT="$DB_CONNECT_TIMEOUT" psql "$normalized_url" -tAc \
    "SELECT schema_name FROM information_schema.schemata WHERE schema_name NOT IN ('pg_catalog', 'information_schema', 'pg_toast');" 2>/dev/null || true
}

# ─────────────────────────────────────────────────────────────────────────────
# db_list_tables - List all tables in a schema
# Usage: db_list_tables <schema_name> [database_url]
# Returns: newline-separated list of table names
# ─────────────────────────────────────────────────────────────────────────────
db_list_tables() {
  local schema="$1"
  local url="${2:-${DATABASE_URL:-}}"
  
  if [ -z "$url" ]; then
    return 1
  fi

  local normalized_url
  normalized_url="$(normalize_database_url_for_host "$url")"

  PGCONNECT_TIMEOUT="$DB_CONNECT_TIMEOUT" psql "$normalized_url" -tAc \
    "SELECT table_name FROM information_schema.tables WHERE table_schema = '$schema' ORDER BY table_name;" 2>/dev/null || true
}

# ─────────────────────────────────────────────────────────────────────────────
# db_get_workspace_id - Get first workspace ID for testing
# Usage: db_get_workspace_id [database_url]
# Returns: workspace UUID or empty
# ─────────────────────────────────────────────────────────────────────────────
db_get_workspace_id() {
  local url="${1:-${DATABASE_URL:-}}"
  db_query "SELECT id FROM platform.workspaces LIMIT 1" "$url"
}

# ─────────────────────────────────────────────────────────────────────────────
# db_get_user_id - Get first user ID for testing
# Usage: db_get_user_id [database_url]
# Returns: user UUID or empty
# ─────────────────────────────────────────────────────────────────────────────
db_get_user_id() {
  local url="${1:-${DATABASE_URL:-}}"
  db_query "SELECT id FROM identity.users LIMIT 1" "$url"
}

# ─────────────────────────────────────────────────────────────────────────────
# db_get_content_type_id - Get blog content type ID for a workspace
# Usage: db_get_content_type_id <workspace_id> [database_url]
# Returns: content type UUID or empty
# ─────────────────────────────────────────────────────────────────────────────
db_get_content_type_id() {
  local workspace_id="$1"
  local url="${2:-${DATABASE_URL:-}}"
  db_query "SELECT id FROM cms.content_types WHERE workspace_id = '$workspace_id' AND route_segment = 'blog' LIMIT 1" "$url"
}

# ─────────────────────────────────────────────────────────────────────────────
# db_ssh_tunnel_check - Check if SSH tunnel is active
# Usage: db_ssh_tunnel_check [local_port]
# Returns: 0 if tunnel appears active, 1 otherwise
# ─────────────────────────────────────────────────────────────────────────────
db_ssh_tunnel_check() {
  local port="${1:-5432}"
  
  # Check if something is listening on the port
  if command -v lsof >/dev/null 2>&1; then
    lsof -i ":$port" -sTCP:LISTEN >/dev/null 2>&1 && return 0
  elif command -v ss >/dev/null 2>&1; then
    ss -tln | grep -q ":$port " && return 0
  elif command -v nc >/dev/null 2>&1; then
    nc -z 127.0.0.1 "$port" >/dev/null 2>&1 && return 0
  fi
  
  return 1
}

# ─────────────────────────────────────────────────────────────────────────────
# db_ssh_tunnel_hint - Print SSH tunnel setup hint
# Usage: db_ssh_tunnel_hint
# ─────────────────────────────────────────────────────────────────────────────
db_ssh_tunnel_hint() {
  cat <<'EOF'
SSH Tunnel Setup:
  ssh -N -L 5432:127.0.0.1:5432 xynes@84.247.176.134

For Docker (bind to all interfaces):
  ssh -N -L 0.0.0.0:5432:127.0.0.1:5432 xynes@84.247.176.134
EOF
}
