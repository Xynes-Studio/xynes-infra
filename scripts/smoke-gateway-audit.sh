#!/usr/bin/env bash
set -euo pipefail

# SMOKE-GATEWAY-AUDIT – Canonical gateway audit log smoke tests
#
# Validates gateway-wide logging via telemetry.gateway.logs.ingest by generating
# static, dynamic, and not-found traffic through gateway and asserting persisted
# rows in telemetry.gateway_request_logs / telemetry.v_gateway_request_logs.

MODE="${MODE:-localhost}"
ENV_FILE="${XYNES_INFRA_ENV_FILE:-}"
CURL_TIMEOUT_SECS="${CURL_TIMEOUT_SECS:-10}"
DB_TIMEOUT_SECS="${DB_TIMEOUT_SECS:-30}"
POLL_INTERVAL_SECS="${POLL_INTERVAL_SECS:-1}"
VERBOSE="${VERBOSE:-0}"
DYNAMIC_PATH_OVERRIDE="${DYNAMIC_PATH_OVERRIDE:-}"
DYNAMIC_METHOD_OVERRIDE="${DYNAMIC_METHOD_OVERRIDE:-GET}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=./scripts/lib/colors.sh
source "${SCRIPT_DIR}/lib/colors.sh"
# shellcheck source=./scripts/lib/env.sh
source "${SCRIPT_DIR}/lib/env.sh"
# shellcheck source=./scripts/lib/http.sh
source "${SCRIPT_DIR}/lib/http.sh"

PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0

HTTP_CODE=""
HTTP_BODY=""
HTTP_REQ_ID=""

HOST_DATABASE_URL=""
GATEWAY_URL=""
SMOKE_USER_AGENT="Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 xynes-gateway-audit-smoke/1.0"

usage() {
  cat <<'EOF'
SMOKE-GATEWAY-AUDIT – Gateway-wide canonical logging smoke tests

Usage: ./scripts/smoke-gateway-audit.sh [options]

Validates:
  • telemetry.gateway.logs.ingest path is active
  • static route log capture (/health)
  • dynamic route log capture (DB-backed route)
  • not-found log capture (404)
  • canonical storage objects: telemetry.gateway_request_logs + telemetry.v_gateway_request_logs
  • core fields (route/service/action/status/error/path/request_id)
  • privacy constraints (hashed IP only, no query in path, no sensitive snippet leakage)
  • retention target is configured to 180 days

Options:
  --mode <localhost|env>      localhost: use http://localhost:<PORT> (default)
                              env: use GATEWAY_URL as-is
  --env-file <path>           env file to load (default: .env.localhost for localhost, .env.dev for env)
  --timeout <seconds>         curl timeout in seconds (default: 10)
  --db-timeout <seconds>      wait timeout for async log ingestion (default: 30)
  --poll-interval <seconds>   DB polling interval (default: 1)
  --dynamic-path <path>       force dynamic route path (defaults to auto-discovered GET route)
  --dynamic-method <METHOD>   dynamic route method when --dynamic-path is set (default: GET)
  --verbose                   print request/response debug lines
  -h, --help                  show this help

Environment:
  DATABASE_URL                Required for DB assertions
  TELEMETRY_GATEWAY_LOG_RETENTION_DAYS  Optional (default runtime fallback: 180)

Examples:
  ./scripts/smoke-gateway-audit.sh --env-file .env.localhost
  ./scripts/smoke-gateway-audit.sh --mode env --env-file .env.dev
  ./scripts/smoke-gateway-audit.sh --dynamic-path /me --db-timeout 45 --verbose
EOF
}

log_pass() { echo -e "${GREEN}PASS${NC} $1"; ((PASS_COUNT++)) || true; }
log_fail() { echo -e "${RED}FAIL${NC} $1"; ((FAIL_COUNT++)) || true; }
log_skip() { echo -e "${YELLOW}SKIP${NC} $1"; ((SKIP_COUNT++)) || true; }
log_info() { echo -e "${BLUE}INFO${NC} $1"; }
log_section() { echo -e "\n${BOLD}${CYAN}━━━ $1 ━━━${NC}"; }

log_verbose() {
  if [ "${VERBOSE}" = "1" ]; then
    echo -e "  ${BLUE}→${NC} $1"
  fi
}

sql_escape_literal() {
  local input="${1:-}"
  printf '%s' "$input" | sed "s/'/''/g"
}

psql_query() {
  local sql="$1"
  PGCONNECT_TIMEOUT=5 psql -X -v ON_ERROR_STOP=1 -tA "$HOST_DATABASE_URL" -c "$sql"
}

http_request_capture() {
  local method="$1"
  local url="$2"
  local body="${3:-}"
  shift 3 || true
  local extra_headers=()
  if [ $# -gt 0 ]; then
    extra_headers=("$@")
  fi

  local tmp_headers tmp_body
  tmp_headers="$(mktemp)"
  tmp_body="$(mktemp)"

  local curl_args=(
    -sS
    -m "${CURL_TIMEOUT_SECS}"
    -X "$method"
    -H "Accept: application/json"
    -H "User-Agent: ${SMOKE_USER_AGENT}"
    -D "$tmp_headers"
    -o "$tmp_body"
    -w "%{http_code}"
  )

  if [ -n "$body" ]; then
    curl_args+=(-H "Content-Type: application/json" -d "$body")
  fi

  if [ ${#extra_headers[@]} -gt 0 ]; then
    for header in "${extra_headers[@]}"; do
      curl_args+=(-H "$header")
    done
  fi

  set +e
  HTTP_CODE="$(curl "${curl_args[@]}" "$url" 2>/dev/null)"
  local exit_code=$?
  set -e

  HTTP_BODY="$(cat "$tmp_body" 2>/dev/null || true)"
  HTTP_REQ_ID="$(
    awk 'BEGIN{IGNORECASE=1} /^x-request-id:/{sub(/\r$/,"",$2); print $2; exit}' "$tmp_headers"
  )"

  rm -f "$tmp_headers" "$tmp_body"

  if [ "$exit_code" -ne 0 ]; then
    HTTP_CODE="000"
  fi

  log_verbose "${method} ${url} -> ${HTTP_CODE} (requestId=${HTTP_REQ_ID:-<none>})"
}

assert_hex_hash() {
  local value="${1:-}"
  local label="$2"
  if [ -z "$value" ]; then
    log_fail "$label - client_ip_hash missing"
    return
  fi
  if [[ "$value" =~ ^[A-Fa-f0-9]{8,128}$ ]]; then
    log_pass "$label - client_ip_hash is hashed"
  else
    log_fail "$label - client_ip_hash is not a valid hash: $value"
  fi
}

assert_no_sensitive_snippets() {
  local json="$1"
  local label="$2"
  local request_snippet response_snippet
  request_snippet="$(echo "$json" | jq -r '.request_snippet // ""')"
  response_snippet="$(echo "$json" | jq -r '.response_snippet // ""')"

  if echo "$request_snippet$response_snippet" | grep -Eiq 'authorization|x-internal-service-token|set-cookie'; then
    log_fail "$label - sensitive header content leaked in snippets"
  else
    log_pass "$label - snippets are free of sensitive header content"
  fi
}

wait_for_log_json() {
  local request_id="$1"
  local request_id_escaped
  request_id_escaped="$(sql_escape_literal "$request_id")"

  local started_at now
  started_at="$(date +%s)"

  while true; do
    local row
    set +e
    row="$(
      psql_query "
        select row_to_json(t)::text
        from (
          select
            request_id,
            method,
            path,
            path_pattern,
            route_id,
            service_key,
            action_key,
            status_code,
            duration_ms,
            workspace_id,
            user_id,
            client_ip_hash,
            geo_country,
            geo_region,
            geo_city,
            geo_source,
            device_type,
            device_browser,
            device_os,
            user_agent,
            error_code,
            request_snippet,
            response_snippet,
            request_size_bytes,
            response_size_bytes,
            occurred_at,
            created_at
          from telemetry.gateway_request_logs
          where request_id = '${request_id_escaped}'
          order by created_at desc
          limit 1
        ) t;
      " 2>/dev/null | tr -d '\r'
    )"
    local code=$?
    set -e

    if [ "$code" -eq 0 ] && [ -n "$row" ] && [ "$row" != "null" ]; then
      echo "$row"
      return 0
    fi

    now="$(date +%s)"
    if [ $((now - started_at)) -ge "${DB_TIMEOUT_SECS}" ]; then
      return 1
    fi
    sleep "${POLL_INTERVAL_SECS}"
  done
}

assert_path_is_canonical() {
  local path="$1"
  local label="$2"
  if [[ "$path" == *\?* ]] || [[ "$path" == *\#* ]]; then
    log_fail "$label - path includes query/hash: $path"
  else
    log_pass "$label - path excludes query/hash"
  fi
}

build_urls() {
  if [ "$MODE" = "localhost" ]; then
    GATEWAY_URL="http://localhost:${GATEWAY_PORT:-4100}"
  else
    GATEWAY_URL="${GATEWAY_URL:-http://gateway:4100}"
  fi
}

resolve_db_url() {
  if [ -z "${DATABASE_URL:-}" ]; then
    log_fail "DATABASE_URL is required for DB assertions"
    echo "Set DATABASE_URL in ${ENV_FILE} or current environment."
    exit 1
  fi

  HOST_DATABASE_URL="$(normalize_database_url_for_host "${DATABASE_URL}")"
  if [ -z "$HOST_DATABASE_URL" ]; then
    log_fail "Could not normalize DATABASE_URL for host checks"
    exit 1
  fi
}

validate_runtime_contract() {
  log_section "Runtime Contract Checks"

  local retention_days="${TELEMETRY_GATEWAY_LOG_RETENTION_DAYS:-180}"
  if [ "$retention_days" = "180" ]; then
    log_pass "Retention target is 180 days (effective=${retention_days})"
  else
    log_fail "Retention target must be 180 days (effective=${retention_days})"
  fi

  local table_reg view_reg
  table_reg="$(psql_query "select to_regclass('telemetry.gateway_request_logs');" | tr -d '\r')"
  view_reg="$(psql_query "select to_regclass('telemetry.v_gateway_request_logs');" | tr -d '\r')"

  if [ "$table_reg" = "telemetry.gateway_request_logs" ]; then
    log_pass "Table exists: telemetry.gateway_request_logs"
  else
    log_fail "Table missing: telemetry.gateway_request_logs"
  fi

  if [ "$view_reg" = "telemetry.v_gateway_request_logs" ]; then
    log_pass "View exists: telemetry.v_gateway_request_logs"
  else
    log_fail "View missing: telemetry.v_gateway_request_logs"
  fi
}

resolve_dynamic_route() {
  local row=""
  local escaped_path=""
  local method_upper
  method_upper="$(echo "${DYNAMIC_METHOD_OVERRIDE}" | tr '[:lower:]' '[:upper:]')"

  if [ -n "$DYNAMIC_PATH_OVERRIDE" ]; then
    escaped_path="$(sql_escape_literal "$DYNAMIC_PATH_OVERRIDE")"
    row="$(
      psql_query "
        select id || '|' || method || '|' || path_pattern || '|' ||
          coalesce(service_key, '') || '|' || coalesce(action_key, '')
        from platform.routes
        where upper(method) = '${method_upper}'
          and path_pattern = '${escaped_path}'
        order by id
        limit 1;
      " | tr -d '\r'
    )"
  else
    row="$(
      psql_query "
        select id || '|' || method || '|' || path_pattern || '|' ||
          coalesce(service_key, '') || '|' || coalesce(action_key, '')
        from platform.routes
        where upper(method) = 'GET'
          and path_pattern not like '%:%'
        order by
          case when path_pattern = '/me' then 0 else 1 end,
          length(path_pattern),
          path_pattern
        limit 1;
      " | tr -d '\r'
    )"
  fi

  if [ -z "$row" ]; then
    log_fail "Could not resolve a dynamic GET route from platform.routes"
    return 1
  fi

  echo "$row"
}

validate_log_row() {
  local json="$1"
  local label="$2"
  local expected_status="$3"
  local expected_path="$4"
  local expected_route_id="$5"
  local expected_service_key="$6"
  local expected_action_key="$7"
  local expected_error_code="$8"

  local status path route_id service_key action_key error_code client_ip_hash user_agent
  status="$(echo "$json" | jq -r '.status_code')"
  path="$(echo "$json" | jq -r '.path')"
  route_id="$(echo "$json" | jq -r '.route_id // ""')"
  service_key="$(echo "$json" | jq -r '.service_key // ""')"
  action_key="$(echo "$json" | jq -r '.action_key // ""')"
  error_code="$(echo "$json" | jq -r '.error_code // ""')"
  client_ip_hash="$(echo "$json" | jq -r '.client_ip_hash // ""')"
  user_agent="$(echo "$json" | jq -r '.user_agent // ""')"

  if [ "$status" = "$expected_status" ]; then
    log_pass "$label - status_code=$expected_status"
  else
    log_fail "$label - expected status_code=$expected_status got $status"
  fi

  if [ "$path" = "$expected_path" ]; then
    log_pass "$label - path=$expected_path"
  else
    log_fail "$label - expected path=$expected_path got $path"
  fi
  assert_path_is_canonical "$path" "$label"

  if [ "$route_id" = "$expected_route_id" ]; then
    log_pass "$label - route_id=$expected_route_id"
  else
    log_fail "$label - expected route_id=$expected_route_id got ${route_id:-<null>}"
  fi

  if [ "$service_key" = "$expected_service_key" ]; then
    log_pass "$label - service_key=$expected_service_key"
  else
    log_fail "$label - expected service_key=$expected_service_key got ${service_key:-<null>}"
  fi

  if [ "$action_key" = "$expected_action_key" ]; then
    log_pass "$label - action_key=$expected_action_key"
  else
    log_fail "$label - expected action_key=$expected_action_key got ${action_key:-<null>}"
  fi

  if [ "$expected_error_code" = "__ANY__" ]; then
    if [ -n "$error_code" ]; then
      log_pass "$label - error_code present ($error_code)"
    else
      log_fail "$label - error_code expected but missing"
    fi
  elif [ "$expected_error_code" = "__NONE__" ]; then
    if [ -z "$error_code" ]; then
      log_pass "$label - error_code absent as expected"
    else
      log_fail "$label - expected no error_code, got $error_code"
    fi
  elif [ "$error_code" = "$expected_error_code" ]; then
    log_pass "$label - error_code=$expected_error_code"
  else
    log_fail "$label - expected error_code=$expected_error_code got ${error_code:-<null>}"
  fi

  assert_hex_hash "$client_ip_hash" "$label"
  assert_no_sensitive_snippets "$json" "$label"

  if echo "$user_agent" | grep -q "xynes-gateway-audit-smoke"; then
    log_pass "$label - user_agent captured"
  else
    log_fail "$label - expected smoke user_agent marker"
  fi
}

cd "$ROOT_DIR"

while [ "${1:-}" != "" ]; do
  case "$1" in
    --mode) MODE="${2:-}"; shift 2 ;;
    --env-file) ENV_FILE="${2:-}"; shift 2 ;;
    --timeout) CURL_TIMEOUT_SECS="${2:-}"; shift 2 ;;
    --db-timeout) DB_TIMEOUT_SECS="${2:-}"; shift 2 ;;
    --poll-interval) POLL_INTERVAL_SECS="${2:-}"; shift 2 ;;
    --dynamic-path) DYNAMIC_PATH_OVERRIDE="${2:-}"; shift 2 ;;
    --dynamic-method) DYNAMIC_METHOD_OVERRIDE="${2:-}"; shift 2 ;;
    --verbose) VERBOSE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo -e "${RED}Error:${NC} unknown arg: $1"; usage; exit 2 ;;
  esac
done

if [ "$MODE" != "localhost" ] && [ "$MODE" != "env" ]; then
  echo -e "${RED}Error:${NC} invalid --mode '$MODE'. Must be 'localhost' or 'env'"
  exit 2
fi

if ! [[ "$DYNAMIC_METHOD_OVERRIDE" =~ ^[A-Za-z]{3,10}$ ]]; then
  echo -e "${RED}Error:${NC} invalid --dynamic-method '$DYNAMIC_METHOD_OVERRIDE'. Use HTTP verb characters only (e.g. GET, POST)."
  exit 2
fi

if ! [[ "$CURL_TIMEOUT_SECS" =~ ^[0-9]+$ ]] || [ "$CURL_TIMEOUT_SECS" -lt 1 ]; then
  echo -e "${RED}Error:${NC} --timeout must be a positive integer"
  exit 2
fi
if ! [[ "$DB_TIMEOUT_SECS" =~ ^[0-9]+$ ]] || [ "$DB_TIMEOUT_SECS" -lt 1 ]; then
  echo -e "${RED}Error:${NC} --db-timeout must be a positive integer"
  exit 2
fi

require_bin curl
require_bin jq
require_bin psql

if [ -z "$ENV_FILE" ]; then
  if [ "$MODE" = "localhost" ]; then
    ENV_FILE=".env.localhost"
  else
    ENV_FILE=".env.dev"
  fi
fi

load_env_file_no_override "$ENV_FILE"
load_env_file_no_override ".env"

build_urls
resolve_db_url

echo "═══════════════════════════════════════════════════════════════════════════"
echo "  SMOKE-GATEWAY-AUDIT: Canonical Gateway Logging"
echo "═══════════════════════════════════════════════════════════════════════════"
echo "Mode: $MODE | Env: $ENV_FILE | Gateway: $GATEWAY_URL"
echo "HTTP timeout: ${CURL_TIMEOUT_SECS}s | DB wait timeout: ${DB_TIMEOUT_SECS}s"
echo ""

log_section "Pre-flight"
http_request_capture "GET" "${GATEWAY_URL%/}/health" "" "CF-IPCountry: US"
if [ "$HTTP_CODE" = "200" ]; then
  log_pass "Gateway reachable"
else
  log_fail "Gateway not reachable (got HTTP $HTTP_CODE)"
  echo "Body: $(echo "$HTTP_BODY" | head -c 200)"
  echo ""
  exit 1
fi

if psql_query "select 1;" >/dev/null 2>&1; then
  log_pass "Database reachable for assertions"
else
  log_fail "Database not reachable via DATABASE_URL"
  echo "Normalized DATABASE_URL host might need adjustment or local DB may be down."
  exit 1
fi

validate_runtime_contract

log_section "Traffic Generation"

dynamic_row="$(resolve_dynamic_route)" || exit 1
IFS='|' read -r dynamic_route_id dynamic_method dynamic_path dynamic_service_key dynamic_action_key <<<"$dynamic_row"

if [ -z "$dynamic_path" ]; then
  log_fail "Resolved dynamic route path is empty"
  exit 1
fi

if [[ "$dynamic_path" != /* ]]; then
  dynamic_path="/${dynamic_path}"
fi

log_info "Resolved dynamic route: ${dynamic_method} ${dynamic_path} (routeId=${dynamic_route_id})"

http_request_capture "GET" "${GATEWAY_URL%/}/health?audit_probe=1" "" "CF-IPCountry: US"
health_status="$HTTP_CODE"
health_req_id="$HTTP_REQ_ID"
if [ "$health_status" = "200" ] && [ -n "$health_req_id" ]; then
  log_pass "Generated static route traffic (/health)"
else
  log_fail "Failed static route generation (/health) status=$health_status requestId=${health_req_id:-<none>}"
fi

http_request_capture "${dynamic_method}" "${GATEWAY_URL%/}${dynamic_path}?audit_probe=1" "" "CF-IPCountry: US"
dynamic_status="$HTTP_CODE"
dynamic_req_id="$HTTP_REQ_ID"
if [ -n "$dynamic_req_id" ] && [ "$dynamic_status" != "404" ]; then
  log_pass "Generated dynamic route traffic (${dynamic_path})"
else
  log_fail "Failed dynamic route generation (${dynamic_path}) status=$dynamic_status requestId=${dynamic_req_id:-<none>}"
fi

not_found_path="/__gateway_audit_smoke_not_found_$(date +%s)"
http_request_capture "GET" "${GATEWAY_URL%/}${not_found_path}?token=secret-probe" "" "CF-IPCountry: US"
not_found_status="$HTTP_CODE"
not_found_req_id="$HTTP_REQ_ID"
if [ "$not_found_status" = "404" ] && [ -n "$not_found_req_id" ]; then
  log_pass "Generated not-found traffic (${not_found_path})"
else
  log_fail "Failed not-found generation status=$not_found_status requestId=${not_found_req_id:-<none>}"
fi

log_section "DB Assertions"

if [ -n "$health_req_id" ]; then
  if health_json="$(wait_for_log_json "$health_req_id")"; then
    validate_log_row \
      "$health_json" \
      "static:/health" \
      "200" \
      "/health" \
      "static.health" \
      "gateway" \
      "gateway.health" \
      "__NONE__"

    health_geo_country="$(echo "$health_json" | jq -r '.geo_country // ""')"
    health_geo_source="$(echo "$health_json" | jq -r '.geo_source // ""')"
    if [ "$health_geo_country" = "US" ] && [ "$health_geo_source" = "cf" ]; then
      log_pass "static:/health - geo enrichment captured (US/cf)"
    else
      log_fail "static:/health - expected geo_country=US and geo_source=cf, got ${health_geo_country:-<null>}/${health_geo_source:-<null>}"
    fi

    health_device_type="$(echo "$health_json" | jq -r '.device_type // ""')"
    if [ -n "$health_device_type" ]; then
      log_pass "static:/health - device enrichment captured (${health_device_type})"
    else
      log_fail "static:/health - device enrichment missing"
    fi
  else
    log_fail "No telemetry row found for static request_id=$health_req_id"
  fi
else
  log_skip "Skipping /health DB assertion because request id missing"
fi

if [ -n "$dynamic_req_id" ]; then
  if dynamic_json="$(wait_for_log_json "$dynamic_req_id")"; then
    dynamic_expected_error="__NONE__"
    if [ "$dynamic_status" -ge 400 ]; then
      dynamic_expected_error="__ANY__"
    fi

    validate_log_row \
      "$dynamic_json" \
      "dynamic:${dynamic_path}" \
      "$dynamic_status" \
      "$dynamic_path" \
      "$dynamic_route_id" \
      "$dynamic_service_key" \
      "$dynamic_action_key" \
      "$dynamic_expected_error"
  else
    log_fail "No telemetry row found for dynamic request_id=$dynamic_req_id"
  fi
else
  log_skip "Skipping dynamic DB assertion because request id missing"
fi

if [ -n "$not_found_req_id" ]; then
  if not_found_json="$(wait_for_log_json "$not_found_req_id")"; then
    validate_log_row \
      "$not_found_json" \
      "not-found:${not_found_path}" \
      "404" \
      "$not_found_path" \
      "" \
      "" \
      "" \
      "NOT_FOUND"
  else
    log_fail "No telemetry row found for not-found request_id=$not_found_req_id"
  fi
else
  log_skip "Skipping not-found DB assertion because request id missing"
fi

log_section "View Assertions"
view_reg_check="$(psql_query "select to_regclass('telemetry.v_gateway_request_logs');" | tr -d '\r')"
if [ "$view_reg_check" != "telemetry.v_gateway_request_logs" ]; then
  log_fail "Skipping view query: telemetry.v_gateway_request_logs is missing"
elif [ -n "$health_req_id" ]; then
  health_req_id_escaped="$(sql_escape_literal "$health_req_id")"
  count_in_view="$(
    psql_query "
      select count(*)::text
      from telemetry.v_gateway_request_logs
      where request_id = '${health_req_id_escaped}';
    " | tr -d '\r'
  )"

  if [ "${count_in_view:-0}" -ge 1 ]; then
    log_pass "telemetry.v_gateway_request_logs returns rows for request_id=$health_req_id"
  else
    log_fail "telemetry.v_gateway_request_logs returned no rows for request_id=$health_req_id"
  fi
else
  log_skip "Skipping view assertion because /health request id missing"
fi

echo ""
echo "═══════════════════════════════════════════════════════════════════════════"
echo "  GATEWAY AUDIT SMOKE SUMMARY"
echo "═══════════════════════════════════════════════════════════════════════════"
echo -e "  ${GREEN}PASS${NC}: $PASS_COUNT"
echo -e "  ${RED}FAIL${NC}: $FAIL_COUNT"
echo -e "  ${YELLOW}SKIP${NC}: $SKIP_COUNT"
echo ""

if [ "$FAIL_COUNT" -gt 0 ]; then
  echo -e "${RED}SMOKE-GATEWAY-AUDIT FAILED${NC}"
  exit 1
fi

echo -e "${GREEN}SMOKE-GATEWAY-AUDIT PASSED${NC}"
exit 0
