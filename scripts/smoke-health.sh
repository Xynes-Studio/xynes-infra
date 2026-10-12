#!/usr/bin/env bash
set -euo pipefail

MODE="${MODE:-localhost}" # localhost | env
ENV_FILE="${XYNES_INFRA_ENV_FILE:-.env.dev}"
CURL_TIMEOUT_SECS="${CURL_TIMEOUT_SECS:-10}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=./scripts/lib/colors.sh
source "${SCRIPT_DIR}/lib/colors.sh"
# shellcheck source=./scripts/lib/env.sh
source "${SCRIPT_DIR}/lib/env.sh"
# shellcheck source=./scripts/lib/http.sh
source "${SCRIPT_DIR}/lib/http.sh"

usage() {
  cat <<'EOF'
Usage: ./scripts/smoke-health.sh [options]

Checks platform liveness/readiness endpoints for all services:
- GET /health (no DB, liveness)
- GET /ready  (DB connectivity, readiness)

Options:
  --mode <localhost|env>   localhost: http://localhost:<PORT> (default)
                           env: use *_SERVICE_URL variables as-is
  --env-file <path>        env file to load for ports/urls (default: .env.dev)
  --timeout <seconds>      curl timeout seconds (default: 5)
  -h, --help               show this help

Environment overrides:
  MODE, XYNES_INFRA_ENV_FILE, CURL_TIMEOUT_SECS
  GATEWAY_URL, DOC_SERVICE_URL, CMS_CORE_URL, AUTHZ_SERVICE_URL, TELEMETRY_SERVICE_URL
  GATEWAY_PORT, DOC_SERVICE_PORT, CMS_CORE_PORT, AUTHZ_SERVICE_PORT, TELEMETRY_SERVICE_PORT

Examples:
  ./scripts/smoke-health.sh
  ./scripts/smoke-health.sh --mode env
  ./scripts/smoke-health.sh --timeout 2
EOF
}

maybe_pretty_json() {
  local body="$1"
  if command -v jq >/dev/null 2>&1; then
    echo "$body" | jq -c . 2>/dev/null || echo "$body"
  else
    echo "$body"
  fi
}


curl_json() {
  local url="$1"
  local tmp
  tmp="$(mktemp)"
  local http_code="000"
  local curl_exit=0

  set +e
  http_code="$(
    curl -sS -m "$CURL_TIMEOUT_SECS" \
      -H "Accept: application/json" \
      -o "$tmp" \
      -w "%{http_code}" \
      "$url"
  )"
  curl_exit=$?
  set -e

  local body
  body="$(cat "$tmp" 2>/dev/null || true)"
  rm -f "$tmp"

  if [ "$curl_exit" -ne 0 ]; then
    echo "CURL_ERROR|$curl_exit|$http_code|$body"
    return 0
  fi

  echo "OK|0|$http_code|$body"
}

expect_health_body() {
  local expected_service="$1"
  local body="$2"

  if command -v jq >/dev/null 2>&1; then
    local status service
    status="$(echo "$body" | jq -r '.status // empty' 2>/dev/null || true)"
    service="$(echo "$body" | jq -r '.service // empty' 2>/dev/null || true)"
    [ "$status" = "ok" ] && [ "$service" = "$expected_service" ]
    return $?
  fi

  echo "$body" | grep -q "\"status\"" && echo "$body" | grep -q "\"ok\"" && echo "$body" | grep -q "$expected_service"
}

expect_ready_body() {
  local expected_status="$1" # ready | not_ready
  local body="$2"

  if command -v jq >/dev/null 2>&1; then
    local status
    status="$(echo "$body" | jq -r '.status // empty' 2>/dev/null || true)"
    [ "$status" = "$expected_status" ]
    return $?
  fi

  echo "$body" | grep -q "\"status\"" && echo "$body" | grep -q "\"$expected_status\""
}

check_health() {
  local label="$1"
  local base_url="$2"
  local expected_service="$3"
  local full_url="${base_url%/}/health"
  local result
  result="$(curl_json "$full_url")"
  local kind curl_exit http_code body
  kind="$(echo "$result" | cut -d'|' -f1)"
  curl_exit="$(echo "$result" | cut -d'|' -f2)"
  http_code="$(echo "$result" | cut -d'|' -f3)"
  body="$(echo "$result" | cut -d'|' -f4-)"

  if [ "$kind" != "OK" ]; then
    echo -e "${RED}FAIL${NC} $label /health - curl_exit=$curl_exit http=$http_code"
    echo "  url: $full_url"
    echo "  body: $(maybe_pretty_json "$body")"
    return 1
  fi

  if [ "$http_code" != "200" ]; then
    echo -e "${RED}FAIL${NC} $label /health - expected http=200 got http=$http_code"
    echo "  url: $full_url"
    echo "  body: $(maybe_pretty_json "$body")"
    return 1
  fi

  if ! expect_health_body "$expected_service" "$body"; then
    echo -e "${RED}FAIL${NC} $label /health - response body did not match expectation"
    echo "  url: $full_url"
    echo "  body: $(maybe_pretty_json "$body")"
    return 1
  fi

  echo -e "${GREEN}PASS${NC} $label /health - http=200"
  return 0
}

check_ready() {
  local label="$1"
  local base_url="$2"
  local full_url="${base_url%/}/ready"

  if [ "${DOCKER_DB_LOOPBACK_ONLY:-0}" -eq 1 ]; then
    echo -e "${YELLOW}SKIP${NC} $label /ready - DB tunnel is loopback-only; Docker cannot reach it"
    echo "  url: $full_url"
    echo "  hint: DATABASE_URL uses db.local/host.docker.internal but local 5432 listens only on 127.0.0.1"
    return 0
  fi

  local result
  result="$(curl_json "$full_url")"
  local kind curl_exit http_code body
  kind="$(echo "$result" | cut -d'|' -f1)"
  curl_exit="$(echo "$result" | cut -d'|' -f2)"
  http_code="$(echo "$result" | cut -d'|' -f3)"
  body="$(echo "$result" | cut -d'|' -f4-)"

  if [ "$kind" != "OK" ]; then
    echo -e "${RED}FAIL${NC} $label /ready - curl_exit=$curl_exit http=$http_code"
    echo "  url: $full_url"
    echo "  body: $(maybe_pretty_json "$body")"
    return 1
  fi

  if [ "$http_code" != "200" ]; then
    echo -e "${RED}FAIL${NC} $label /ready - expected http=200 got http=$http_code"
    echo "  url: $full_url"
    echo "  body: $(maybe_pretty_json "$body")"
    return 1
  fi

  if ! expect_ready_body "ready" "$body"; then
    echo -e "${RED}FAIL${NC} $label /ready - response body did not match expectation"
    echo "  url: $full_url"
    echo "  body: $(maybe_pretty_json "$body")"
    return 1
  fi

  echo -e "${GREEN}PASS${NC} $label /ready - http=200"
  return 0
}

while [ "${1:-}" != "" ]; do
  case "$1" in
    --mode)
      MODE="${2:-}"
      shift 2
      ;;
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    --timeout)
      CURL_TIMEOUT_SECS="${2:-}"
      shift 2
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

require_bin curl

load_env_file_no_override "$ENV_FILE"

GATEWAY_PORT="${GATEWAY_PORT:-4100}"
DOC_SERVICE_PORT="${DOC_SERVICE_PORT:-4201}"
CMS_CORE_PORT="${CMS_CORE_PORT:-4202}"
AUTHZ_SERVICE_PORT="${AUTHZ_SERVICE_PORT:-4300}"
TELEMETRY_SERVICE_PORT="${TELEMETRY_SERVICE_PORT:-4400}"

extract_db_host() {
  local url="${1:-}"
  if [ -z "$url" ]; then
    echo ""
    return 0
  fi

  # Handles: postgresql://user:pass@host:port/db?params
  echo "$url" | sed -E 's#^[^@]+@([^:/?]+).*#\1#' 2>/dev/null || echo ""
}

DOCKER_DB_LOOPBACK_ONLY=0

compute_docker_db_loopback_only() {
  local database_url="${DATABASE_URL:-}"
  local host
  host="$(extract_db_host "$database_url")"

  if [ -z "$host" ]; then
    DOCKER_DB_LOOPBACK_ONLY=0
    return 0
  fi

  # Only relevant when containers connect to the host gateway.
  if [ "$host" != "db.local" ] && [ "$host" != "host.docker.internal" ]; then
    DOCKER_DB_LOOPBACK_ONLY=0
    return 0
  fi

  if ! command -v lsof >/dev/null 2>&1; then
    DOCKER_DB_LOOPBACK_ONLY=0
    return 0
  fi

  local listeners
  listeners="$(lsof -nP -iTCP:5432 -sTCP:LISTEN 2>/dev/null | tail -n +2 || true)"
  if [ -z "$listeners" ]; then
    DOCKER_DB_LOOPBACK_ONLY=0
    return 0
  fi

  # If any listener is non-loopback, Docker can potentially reach it.
  if echo "$listeners" | awk '{print $9}' | grep -vqE '(^127\.0\.0\.1:5432$|^\[::1\]:5432$)'; then
    DOCKER_DB_LOOPBACK_ONLY=0
    return 0
  fi

  DOCKER_DB_LOOPBACK_ONLY=1
  return 0
}

check_ssh_tunnel_binding_hint() {
  local database_url="${DATABASE_URL:-}"
  local host
  host="$(extract_db_host "$database_url")"

  if [ -z "$host" ]; then
    return 0
  fi

  # In Docker dev, db.local / host.docker.internal are expected to resolve to the host gateway.
  # If your SSH tunnel only binds to 127.0.0.1:5432, containers won't be able to reach it.
  if [ "$host" != "db.local" ] && [ "$host" != "host.docker.internal" ]; then
    return 0
  fi

  if ! command -v lsof >/dev/null 2>&1; then
    return 0
  fi

  local listeners
  listeners="$(lsof -nP -iTCP:5432 -sTCP:LISTEN 2>/dev/null | tail -n +2 || true)"
  if [ -z "$listeners" ]; then
    return 0
  fi

  # If all listeners are loopback-only, emit a targeted hint.
  if echo "$listeners" | awk '{print $9}' | grep -vqE '(^127\.0\.0\.1:5432$|^\[::1\]:5432$)'; then
    return 0
  fi

  echo -e "${YELLOW}Note:${NC} Your SSH tunnel appears bound to loopback only (127.0.0.1:5432)."
  echo "      Docker containers will hit the host gateway (e.g. 192.168.x.x:5432) and /ready may fail."
  echo "      Restart the tunnel bound to all interfaces, e.g.:"
  echo "        ssh -N -g -L 5432:<REMOTE_DB_HOST>:5432 <SSH_HOST>"
  echo "      or:"
  echo "        ssh -N -L 0.0.0.0:5432:<REMOTE_DB_HOST>:5432 <SSH_HOST>"
  echo
}

if [ "$MODE" = "env" ]; then
  GATEWAY_URL="${GATEWAY_URL:-http://localhost:${GATEWAY_PORT}}"
  DOC_SERVICE_URL="${DOC_SERVICE_URL:-http://localhost:${DOC_SERVICE_PORT}}"
  CMS_CORE_URL="${CMS_CORE_URL:-http://localhost:${CMS_CORE_PORT}}"
  AUTHZ_SERVICE_URL="${AUTHZ_SERVICE_URL:-http://localhost:${AUTHZ_SERVICE_PORT}}"
  TELEMETRY_SERVICE_URL="${TELEMETRY_SERVICE_URL:-http://localhost:${TELEMETRY_SERVICE_PORT}}"
elif [ "$MODE" = "localhost" ]; then
  GATEWAY_URL="http://localhost:${GATEWAY_PORT}"
  DOC_SERVICE_URL="http://localhost:${DOC_SERVICE_PORT}"
  CMS_CORE_URL="http://localhost:${CMS_CORE_PORT}"
  AUTHZ_SERVICE_URL="http://localhost:${AUTHZ_SERVICE_PORT}"
  TELEMETRY_SERVICE_URL="http://localhost:${TELEMETRY_SERVICE_PORT}"
else
  echo -e "${RED}Error:${NC} invalid MODE: $MODE (expected localhost|env)"
  exit 2
fi

echo -e "${GREEN}Platform Health/Readiness Smoke Test${NC}"
echo "Mode: $MODE"
echo "Env file: $ENV_FILE"
echo "Timeout: ${CURL_TIMEOUT_SECS}s"
echo

compute_docker_db_loopback_only
check_ssh_tunnel_binding_hint

failures=0

check_health "gateway" "$GATEWAY_URL" "xynes-gateway" || failures=$((failures+1))
check_ready "gateway" "$GATEWAY_URL" || failures=$((failures+1))
echo

check_health "doc-service" "$DOC_SERVICE_URL" "xynes-doc-service" || failures=$((failures+1))
check_ready "doc-service" "$DOC_SERVICE_URL" || failures=$((failures+1))
echo

check_health "cms-core" "$CMS_CORE_URL" "xynes-cms-core" || failures=$((failures+1))
check_ready "cms-core" "$CMS_CORE_URL" || failures=$((failures+1))
echo

check_health "authz-service" "$AUTHZ_SERVICE_URL" "xynes-authz-service" || failures=$((failures+1))
check_ready "authz-service" "$AUTHZ_SERVICE_URL" || failures=$((failures+1))
echo

check_health "telemetry-service" "$TELEMETRY_SERVICE_URL" "xynes-telemetry-service" || failures=$((failures+1))
check_ready "telemetry-service" "$TELEMETRY_SERVICE_URL" || failures=$((failures+1))
echo

if [ "$failures" -gt 0 ]; then
  echo -e "${RED}Done:${NC} $failures checks failed"
  echo -e "${YELLOW}Hints:${NC}"
  echo '- If running via Docker: run from xynes-infra/ with docker compose -f docker-compose.dev.yml up'
  echo "- If running on host: ensure each service is listening on the expected ports."
  echo "- /ready requires Postgres reachable via your SSH tunnel."
  exit 1
fi

echo -e "${GREEN}Done:${NC} all checks passed"
