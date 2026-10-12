#!/usr/bin/env bash
set -euo pipefail

# SMOKE-AUTH-1 – E2E Smoke Test: Supabase Auth → /me → workspace
#
# Validates:
# - GET /me with Authorization: Bearer <SUPABASE_ACCESS_TOKEN>
# - DB: identity.users contains the user
# - POST /workspaces creates a workspace
# - DB: platform.workspaces + platform.workspace_members updated
# - DB: authz.user_roles contains workspace_owner
# - GET /workspaces lists the created workspace
#
# Security: this script never echoes the Supabase access token.

MODE="${MODE:-localhost}" # localhost | env
ENV_FILE="${XYNES_INFRA_ENV_FILE:-}"
CURL_TIMEOUT_SECS="${CURL_TIMEOUT_SECS:-10}"
TOKEN_FILE="${TOKEN_FILE:-}"
START_STACK="${START_STACK:-0}"
START_DB_TUNNEL="${START_DB_TUNNEL:-0}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.dev.yml}"
DB_TUNNEL_SSH_HOST="${DB_TUNNEL_SSH_HOST:-xynes@84.247.176.134}"
DB_TUNNEL_REMOTE_HOST="${DB_TUNNEL_REMOTE_HOST:-127.0.0.1}"
DB_TUNNEL_REMOTE_PORT="${DB_TUNNEL_REMOTE_PORT:-5432}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=./scripts/lib/colors.sh
source "${SCRIPT_DIR}/lib/colors.sh"
# shellcheck source=./scripts/lib/env.sh
source "${SCRIPT_DIR}/lib/env.sh"
# shellcheck source=./scripts/lib/http.sh
source "${SCRIPT_DIR}/lib/http.sh"

usage() {
  cat <<'EOF'
Usage: ./scripts/smoke-auth.sh [options]

Options:
  --mode <localhost|env>   localhost: http://localhost:<PORT> (default)
                           env: use GATEWAY_URL as-is (default http://gateway:<PORT>)
  --env-file <path>        env file to load (default: .env.localhost for localhost, .env.dev for env)
  --token-file <path>      read SUPABASE_ACCESS_TOKEN from a file (preferred; avoids shell history)
  --start-stack            if Docker Compose stack isn't running, start it (uses docker-compose.dev.yml)
  --start-db-tunnel         if DB isn't reachable, attempt to start an SSH tunnel automatically (requires key-based SSH)
  --timeout <seconds>      curl timeout seconds (default: 10)
  -h, --help               show this help

Required env:
  SUPABASE_ACCESS_TOKEN    Supabase access token (JWT). Must be provided via env var.

Or:
  --token-file <path>      reads token from a file (e.g. .tmp/supabase_access_token)

Optional env:
  GATEWAY_URL / GATEWAY_PORT
  DATABASE_URL             Used for DB assertions (identity/users/workspaces/roles)

Notes:
  - For DB assertions, start the SSH tunnel first:
      ssh -N -L 5432:127.0.0.1:5432 xynes@84.247.176.134
  - If you're running services via Docker (.env.dev), your DB tunnel must bind to a non-loopback interface:
      ssh -N -L 0.0.0.0:5432:<REMOTE_DB_HOST>:5432 <SSH_HOST>
  - You can override tunnel params via env:
      DB_TUNNEL_SSH_HOST, DB_TUNNEL_REMOTE_HOST, DB_TUNNEL_REMOTE_PORT
  - This script does NOT print secrets.
EOF
}

docker_compose() {
  # Always use an explicit --env-file so compose-time interpolation works (ports, URLs, etc.)
  local env_file="$1"
  shift
  docker compose --env-file "$env_file" -f "$COMPOSE_FILE" "$@"
}

http_is_listening_local() {
  local host="$1"
  local port="$2"
  if command -v nc >/dev/null 2>&1; then
    nc -z "$host" "$port" >/dev/null 2>&1
    return $?
  fi

  # Fallback: try curl for HTTP-ish ports.
  if command -v curl >/dev/null 2>&1; then
    curl -sS -m 1 "http://${host}:${port}/health" >/dev/null 2>&1
    return $?
  fi

  return 1
}

check_ssh_tunnel_binding_hint() {
  # When running services in Docker dev mode, containers connect to the host gateway.
  # If the SSH tunnel only binds to 127.0.0.1:5432, containers will see ECONNREFUSED.
  local database_url="${DATABASE_URL:-}"
  local host
  host="$(extract_db_host "$database_url")"

  if [ -z "$host" ]; then
    return 0
  fi

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

  # If any listener is non-loopback, Docker can potentially reach it.
  if echo "$listeners" | awk '{print $9}' | grep -vqE '(^127\.0\.0\.1:5432$|^\[::1\]:5432$)'; then
    return 0
  fi

  echo -e "${YELLOW}Note:${NC} Your SSH tunnel appears bound to loopback only (127.0.0.1:5432)."
  echo "      Docker containers will hit the host gateway (e.g. 192.168.x.x:5432) and DB calls will fail."
  echo "      Restart the tunnel bound to all interfaces, e.g.:"
  echo "        ssh -N -L 0.0.0.0:5432:${DB_TUNNEL_REMOTE_HOST}:${DB_TUNNEL_REMOTE_PORT} ${DB_TUNNEL_SSH_HOST}"
  echo
}

maybe_start_stack() {
  local env_file="$1"

  if [ "${START_STACK:-0}" != "1" ]; then
    return 0
  fi

  if ! command -v docker >/dev/null 2>&1; then
    return 0
  fi

  if [ ! -f "$COMPOSE_FILE" ]; then
    return 0
  fi

  # If gateway port is already listening, assume stack is up.
  if http_is_listening_local "127.0.0.1" "${GATEWAY_PORT:-4100}"; then
    return 0
  fi

  echo -e "${YELLOW}Info:${NC} Starting local docker compose stack ($COMPOSE_FILE) using $env_file ..."
  docker_compose "$env_file" up -d --build >/dev/null
}

maybe_start_db_tunnel() {
  if [ "${START_DB_TUNNEL:-0}" != "1" ]; then
    return 0
  fi

  if ! command -v ssh >/dev/null 2>&1; then
    return 0
  fi
  if ! command -v nc >/dev/null 2>&1; then
    return 0
  fi

  local database_url="${DATABASE_URL:-}"
  local raw_host
  raw_host="$(extract_db_host "$database_url")"

  local bind_addr="127.0.0.1"
  if [ "$raw_host" = "db.local" ] || [ "$raw_host" = "host.docker.internal" ]; then
    bind_addr="0.0.0.0"
  fi

  # If already reachable, nothing to do.
  if nc -z "${bind_addr}" 5432 >/dev/null 2>&1 || nc -z 127.0.0.1 5432 >/dev/null 2>&1; then
    return 0
  fi

  echo -e "${YELLOW}Info:${NC} Starting SSH DB tunnel (${bind_addr}:5432 -> ${DB_TUNNEL_REMOTE_HOST}:${DB_TUNNEL_REMOTE_PORT} on ${DB_TUNNEL_SSH_HOST})..."
  set +e
  ssh -fN \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -L "${bind_addr}:5432:${DB_TUNNEL_REMOTE_HOST}:${DB_TUNNEL_REMOTE_PORT}" \
    "${DB_TUNNEL_SSH_HOST}" >/dev/null 2>&1
  local ssh_exit=$?
  set -e

  if [ "$ssh_exit" -ne 0 ]; then
    echo -e "${YELLOW}Note:${NC} Could not start SSH tunnel automatically (ssh exit=$ssh_exit)."
    return 0
  fi

  # Wait briefly for listener.
  local _attempt
  for _attempt in 1 2 3 4 5; do
    if nc -z 127.0.0.1 5432 >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
}

diagnose_downstream_on_failure() {
  local label="$1"

  # Only attempt targeted docker diagnostics when using localhost mode and docker is available.
  if [ "$MODE" != "localhost" ]; then
    return 0
  fi
  if ! command -v docker >/dev/null 2>&1; then
    return 0
  fi

  # For /me, the downstream is accounts-service.
  if echo "$label" | grep -q "GET /me"; then
    local container
    container="$(docker ps --format '{{.Names}}' | grep -E '(^|-)accounts-service-1$' || true)"
    if [ -n "$container" ]; then
      echo
      echo -e "${YELLOW}Hint:${NC} Recent accounts-service logs (likely root cause for /me failures):"
      docker logs --since=5m "$container" 2>/dev/null | tail -n 200 || true
      echo
      echo -e "${YELLOW}Hint:${NC} Accounts-service error lines (last 5m):"
      docker logs --since=5m "$container" 2>/dev/null \
        | grep -E 'ECONNREFUSED|CONNECT_TIMEOUT|Unhandled Error|\[ERROR\]|Failed query|DrizzleQueryError' \
        | tail -n 50 || true
    fi
  fi
}


http_request() {
  local method="$1"
  local url="$2"
  local data="${3:-}"

  HTTP_CODE="000"
  HTTP_BODY=""

  local curl_exit=0

  local tmp
  tmp="$(mktemp)"

  set +e
  if [ "$method" = "GET" ]; then
    HTTP_CODE="$(
      curl -sS -m "$CURL_TIMEOUT_SECS" \
        -H "Accept: application/json" \
        -H "Authorization: Bearer ${SUPABASE_ACCESS_TOKEN}" \
        -o "$tmp" \
        -w "%{http_code}" \
        "$url"
    )"
    curl_exit=$?
  else
    HTTP_CODE="$(
      curl -sS -m "$CURL_TIMEOUT_SECS" \
        -H "Accept: application/json" \
        -H "Authorization: Bearer ${SUPABASE_ACCESS_TOKEN}" \
        -H "Content-Type: application/json" \
        -o "$tmp" \
        -w "%{http_code}" \
        -X "$method" \
        -d "$data" \
        "$url"
    )"
    curl_exit=$?
  fi
  set -e

  if [ "$curl_exit" -ne 0 ]; then
    if [ "$curl_exit" -eq 6 ] && echo "$url" | grep -qE '://(gateway|cms-core|doc-service|authz-service|telemetry-service)(:|/|$)'; then
      echo -e "${YELLOW}Hint:${NC} DNS failed resolving a Docker-network hostname. If you're running this from the host, use --mode localhost."
      echo -e "${YELLOW}Hint:${NC} If you're running from inside a Docker container on the compose network, use --mode env."
    fi

    echo -e "${RED}FAIL${NC} curl exited with code=$curl_exit calling: $url"
    exit 1
  fi

  HTTP_BODY="$(cat "$tmp" 2>/dev/null || true)"
  rm -f "$tmp" 2>/dev/null || true
}

expect_http_success() {
  local label="$1"
  local url="$2"
  if [ "$HTTP_CODE" != "200" ] && [ "$HTTP_CODE" != "201" ]; then
    echo -e "${RED}FAIL${NC} $label - expected http=200 or 201 got http=$HTTP_CODE"
    echo "  url: $url"
    echo "  body: $HTTP_BODY"
    diagnose_downstream_on_failure "$label"
    exit 1
  fi
}

diagnose_jwt_on_401() {
  # Best-effort, safe diagnostics only. Never prints token or secret.
  if [ "${HTTP_CODE:-}" != "401" ]; then
  return 0
  fi

  if ! command -v python3 >/dev/null 2>&1; then
  echo -e "${YELLOW}Hint:${NC} /me returned 401. This usually means the gateway cannot verify the token signature (JWT_SECRET mismatch)."
  return 0
  fi

  python3 - <<'PY' || true
import base64, hashlib, hmac, json, os, re

jwt = (os.environ.get('SUPABASE_ACCESS_TOKEN') or '').strip()
secret = (os.environ.get('JWT_SECRET') or '').strip()

def b64url_decode(s: str) -> bytes:
  s += '=' * (-len(s) % 4)
  return base64.urlsafe_b64decode(s.encode())

def safe_get(parts, idx):
  try:
    return parts[idx]
  except Exception:
    return ''

parts = jwt.split('.')
if len(parts) != 3:
  print('Hint: token is not a standard JWT (expected 3 segments).')
  raise SystemExit(0)

try:
  header = json.loads(b64url_decode(parts[0]))
  payload = json.loads(b64url_decode(parts[1]))
except Exception:
  print('Hint: token segments are not valid base64url JSON.')
  raise SystemExit(0)

alg = header.get('alg')
aud = payload.get('aud')
iss = payload.get('iss')
sub = payload.get('sub')

print(f"Hint: token alg={alg} aud={aud} iss={'<missing>' if iss is None else iss} sub={sub}")

if alg != 'HS256':
  print('Hint: gateway is currently configured for HS256 via JWT_SECRET; set JWT_JWKS_URL/JWT_PUBLIC_KEY if using RS256.')
  raise SystemExit(0)

if not secret:
  print('Hint: JWT_SECRET is not set in the gateway environment; set it to your Supabase project JWT secret.')
  raise SystemExit(0)

msg = (parts[0] + '.' + parts[1]).encode()
sig = b64url_decode(parts[2])

raw_ok = hmac.compare_digest(hmac.new(secret.encode(), msg, hashlib.sha256).digest(), sig)

b64_ok = False
b64_tried = False
if re.fullmatch(r'[A-Za-z0-9+/=]+', secret) and len(secret) % 4 == 0:
  try:
    key = base64.b64decode(secret)
    b64_tried = True
    b64_ok = hmac.compare_digest(hmac.new(key, msg, hashlib.sha256).digest(), sig)
  except Exception:
    b64_tried = True
    b64_ok = False

if raw_ok or b64_ok:
  print('Hint: JWT_SECRET appears to match this token signature; 401 likely comes from another auth requirement.')
else:
  print('Hint: JWT_SECRET does NOT match this token signature. Copy the Supabase project JWT secret into xynes-infra/.env.dev (JWT_SECRET) and restart gateway.')
  print('Hint: Supabase Cloud: Project Settings → API → JWT Secret (do not commit it).')
  print('Hint: Self-hosted Supabase: use the JWT secret configured for the auth service (often an env var named JWT_SECRET).')
  if b64_tried:
    print('Hint: tried both raw and base64-decoded forms of JWT_SECRET.')
PY
}

expect_ok_true() {
  local label="$1"
  if ! echo "$HTTP_BODY" | jq -e '.ok == true' >/dev/null 2>&1; then
    echo -e "${RED}FAIL${NC} $label - expected { ok: true }"
    echo "  body: $HTTP_BODY"
    exit 1
  fi
}

trim_ws() {
  local s="${1:-}"
  echo "$s" | tr -d '[:space:]'
}

# Convert docker-internal hostnames to localhost for host-side queries.
# e.g. host.docker.internal -> localhost, db.local -> localhost
normalize_database_url_for_host() {
  local url="${1:-}"
  [ -n "$url" ] || { echo ""; return 0; }
  echo "$url" | sed -E 's#(postgres(ql)?://[^@]+@)(host\.docker\.internal|db\.local)([:/?])#\1localhost\4#'
}

extract_db_host() {
  local url="${1:-}"
  [ -n "$url" ] || { echo ""; return 0; }
  echo "$url" | sed -E 's#^[^@]+@([^:/?]+).*#\1#' 2>/dev/null || echo ""
}

extract_db_port() {
  local url="${1:-}"
  if [ -z "$url" ]; then
    echo ""
    return 0
  fi

  # Tries to match :<port>/ after host
  local port
  port="$(echo "$url" | sed -E 's#^.*@[^:/?]+:([0-9]+).*$#\1#' 2>/dev/null || true)"
  if [[ "$port" =~ ^[0-9]+$ ]]; then
    echo "$port"
    return 0
  fi

  echo ""
}

check_db_reachable_hint() {
  local database_url="${DATABASE_URL:-}"
  local host_db_url
  host_db_url="$(normalize_database_url_for_host "$database_url")"
  local host port
  host="$(extract_db_host "$host_db_url")"
  port="$(extract_db_port "$host_db_url")"
  [ -n "$port" ] || port="5432"

  local raw_host
  raw_host="$(extract_db_host "$database_url")"

  # Only provide SSH tunnel hints for local-ish hosts.
  if [ "$host" != "127.0.0.1" ] && [ "$host" != "localhost" ] && [ "$host" != "db.local" ] && [ "$host" != "host.docker.internal" ]; then
    return 0
  fi

  if command -v nc >/dev/null 2>&1; then
    if ! nc -z "$host" "$port" >/dev/null 2>&1; then
      echo -e "${YELLOW}Hint:${NC} DB is not reachable at ${host}:${port}."

      # If services run in Docker, containers connect via the host gateway; bind tunnel to 0.0.0.0.
      if [ "$raw_host" = "db.local" ] || [ "$raw_host" = "host.docker.internal" ]; then
        echo "  Start the SSH tunnel (Docker-friendly bind):"
        echo "    ssh -N -L 0.0.0.0:5432:${DB_TUNNEL_REMOTE_HOST}:${DB_TUNNEL_REMOTE_PORT} ${DB_TUNNEL_SSH_HOST}"
      else
        echo "  Start the SSH tunnel:"
        echo "    ssh -N -L 5432:${DB_TUNNEL_REMOTE_HOST}:${DB_TUNNEL_REMOTE_PORT} ${DB_TUNNEL_SSH_HOST}"
      fi

      echo "  See ENV_GUIDE.md for tunnel notes."
    fi
  fi
}

run_sql_scalar() {
  local query="$1"

  local host_db_url
  host_db_url="$(normalize_database_url_for_host "${DATABASE_URL:-}")"

  # Method 1: psql (if available on host)
  if command -v psql >/dev/null 2>&1; then
    # -t: tuples only, -A: unaligned, -q: quiet
    psql "$host_db_url" -tAq -c "$query" 2>/dev/null || return 1
    return 0
  fi

  # Method 2: Docker + Bun (using cms-core container)
  if command -v docker >/dev/null 2>&1; then
    local result
    result=$(docker_compose "${ENV_FILE:-.env.dev}" exec -T \
      -e SQL_QUERY="$query" \
      cms-core bun -e "
        import postgres from 'postgres';
        const sql = postgres(process.env.DATABASE_URL);
        try {
          const rows = await sql.unsafe(process.env.SQL_QUERY);
          if (rows.length > 0) console.log(Object.values(rows[0])[0] ?? '');
          await sql.end();
          process.exit(0);
        } catch {
          process.exit(1);
        }
      " 2>/dev/null | grep -v 'WARN\|obsolete\|version')

    echo "$result"
    return 0
  fi

  return 1
}

assert_db_count_eq() {
  local label="$1"
  local query="$2"
  local expected="$3"

  local value
  value="$(trim_ws "$(run_sql_scalar "$query" 2>/dev/null || true)")"

  if [ "$value" != "$expected" ]; then
    echo -e "${RED}FAIL${NC} DB assert: $label - expected $expected got '${value:-<empty>}'"
    echo "  query: $query"
    exit 1
  fi

  echo -e "${GREEN}PASS${NC} DB assert: $label"
}

cd "$ROOT_DIR"

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
    --token-file)
      TOKEN_FILE="${2:-}"
      shift 2
      ;;
    --timeout)
      CURL_TIMEOUT_SECS="${2:-}"
      shift 2
      ;;
    --start-stack)
      START_STACK=1
      shift 1
      ;;
    --start-db-tunnel)
      START_DB_TUNNEL=1
      shift 1
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
require_bin jq

if [ -z "$ENV_FILE" ]; then
  if [ "$MODE" = "localhost" ]; then
    ENV_FILE=".env.localhost"
  else
    ENV_FILE=".env.dev"
  fi
fi

load_env_file_no_override "$ENV_FILE"
load_env_file_no_override ".env"
load_env_file_no_override ".env.localhost"
load_env_file_no_override ".env.local"

if [ -z "${SUPABASE_ACCESS_TOKEN:-}" ] && [ -n "${TOKEN_FILE:-}" ]; then
  load_token_file "$TOKEN_FILE"
fi

if [ -z "${SUPABASE_ACCESS_TOKEN:-}" ]; then
  echo -e "${RED}Error:${NC} SUPABASE_ACCESS_TOKEN is required."
  echo "Tip: use --token-file .tmp/supabase_access_token (preferred)"
  exit 1
fi

GATEWAY_PORT="${GATEWAY_PORT:-4100}"
if [ "$MODE" = "localhost" ]; then
  GATEWAY_URL="http://localhost:${GATEWAY_PORT}"
else
  GATEWAY_URL="${GATEWAY_URL:-http://gateway:${GATEWAY_PORT}}"
fi

# Fallback DATABASE_URL if missing (still give a hint for SSH tunnel)
if [ -z "${DATABASE_URL:-}" ]; then
  echo -e "${YELLOW}Warning:${NC} DATABASE_URL not set; DB assertions may fail."
  echo "Tip: set DATABASE_URL in $ENV_FILE (or export it)."
fi

check_db_reachable_hint
check_ssh_tunnel_binding_hint

maybe_start_db_tunnel

maybe_start_stack "$ENV_FILE"

echo -e "${GREEN}Starting SMOKE-AUTH-1 (Supabase Auth → /me → workspace)...${NC}"

# 1) GET /me
ME_URL="${GATEWAY_URL%/}/me"
http_request "GET" "$ME_URL"
diagnose_jwt_on_401
expect_http_success "gateway GET /me" "$ME_URL"
expect_ok_true "gateway GET /me"

# Handle both single envelope (.data.user.id) and double envelope (.data.data.user.id)
USER_ID="$(echo "$HTTP_BODY" | jq -r '.data.data.user.id // .data.user.id // empty')"
if [ -z "$USER_ID" ] || [ "$USER_ID" = "null" ]; then
  echo -e "${RED}FAIL${NC} gateway GET /me - missing user.id in response"
  echo "  body: $HTTP_BODY"
  exit 1
fi

echo -e "${GREEN}PASS${NC} /me: userId=$USER_ID"

# 2) DB assert identity.users row exists
if [ -n "${DATABASE_URL:-}" ]; then
  assert_db_count_eq "identity.users has user" "SELECT COUNT(*)::text FROM identity.users WHERE id = '${USER_ID}';" "1"
fi

# 3) POST /workspaces
NOW_SECS="$(date +%s)"
WORKSPACE_SLUG="smoke-ws-${NOW_SECS}"
WORKSPACE_NAME="Smoke Workspace ${NOW_SECS}"

CREATE_URL="${GATEWAY_URL%/}/workspaces"
CREATE_PAYLOAD="$(jq -nc --arg name "$WORKSPACE_NAME" --arg slug "$WORKSPACE_SLUG" '{name:$name, slug:$slug}')"

http_request "POST" "$CREATE_URL" "$CREATE_PAYLOAD"
expect_http_success "gateway POST /workspaces" "$CREATE_URL"
expect_ok_true "gateway POST /workspaces"

# Handle both single envelope (.data.id) and double envelope (.data.data.id)
WORKSPACE_ID="$(echo "$HTTP_BODY" | jq -r '.data.data.id // .data.id // empty')"
if [ -z "$WORKSPACE_ID" ] || [ "$WORKSPACE_ID" = "null" ]; then
  echo -e "${RED}FAIL${NC} gateway POST /workspaces - missing workspace id in response"
  echo "  body: $HTTP_BODY"
  exit 1
fi

echo -e "${GREEN}PASS${NC} /workspaces created: workspaceId=$WORKSPACE_ID slug=$WORKSPACE_SLUG"

# 4) DB asserts for workspace + membership + role
if [ -n "${DATABASE_URL:-}" ]; then
  assert_db_count_eq "platform.workspaces has workspace" "SELECT COUNT(*)::text FROM platform.workspaces WHERE id = '${WORKSPACE_ID}';" "1"
  assert_db_count_eq "platform.workspaces.created_by matches user" "SELECT COUNT(*)::text FROM platform.workspaces WHERE id = '${WORKSPACE_ID}' AND created_by = '${USER_ID}';" "1"
  assert_db_count_eq "platform.workspace_members has membership" "SELECT COUNT(*)::text FROM platform.workspace_members WHERE workspace_id = '${WORKSPACE_ID}' AND user_id = '${USER_ID}' AND status = 'active';" "1"
  assert_db_count_eq "authz.user_roles has workspace_owner" "SELECT COUNT(*)::text FROM authz.user_roles WHERE workspace_id = '${WORKSPACE_ID}' AND user_id = '${USER_ID}' AND role_key = 'workspace_owner';" "1"
fi

# 5) GET /workspaces lists created workspace
LIST_URL="${GATEWAY_URL%/}/workspaces"
http_request "GET" "$LIST_URL"
expect_http_success "gateway GET /workspaces" "$LIST_URL"
expect_ok_true "gateway GET /workspaces"

# Handle both single envelope (.data.workspaces) and double envelope (.data.data.workspaces)
if ! echo "$HTTP_BODY" | jq -e --arg wid "$WORKSPACE_ID" '(.data.data.workspaces // .data.workspaces) | type == "array" and (map(select(.id == $wid)) | length) == 1' >/dev/null 2>&1; then
  echo -e "${RED}FAIL${NC} gateway GET /workspaces - created workspace not found"
  echo "  url: $LIST_URL"
  echo "  body: $HTTP_BODY"
  exit 1
fi

echo -e "${GREEN}PASS${NC} /workspaces list contains created workspace"

echo
cat <<EOF
Smoke completed.

export XS_USER_ID="${USER_ID}"
export WORKSPACE_ID="${WORKSPACE_ID}"
export WORKSPACE_SLUG="${WORKSPACE_SLUG}"
EOF
