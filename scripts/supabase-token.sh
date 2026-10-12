#!/usr/bin/env bash
set -euo pipefail

# Helper: obtain a Supabase access token (JWT) via Auth REST API.
#
# Security properties:
# - Does NOT print the access token.
# - Prompts for password with no-echo.
# - Writes token to .tmp/supabase_access_token with 0600 permissions.
#
# Usage:
#   ./scripts/supabase-token.sh --base-url https://<host>:<port> --anon-key <anon>
#   ./scripts/supabase-token.sh --env-file .env.localhost --base-url https://<host>:<port>
#
# Then:
#   export SUPABASE_ACCESS_TOKEN="$(cat .tmp/supabase_access_token)"

ENV_FILE="${XYNES_INFRA_ENV_FILE:-}"
CURL_TIMEOUT_SECS="${CURL_TIMEOUT_SECS:-10}"
INSECURE_TLS="${INSECURE_TLS:-0}"

# Curl TLS flag (avoid bash array edge-cases under `set -u` on macOS)
CURL_TLS_ARG=""

SUPABASE_BASE_URL="${SUPABASE_BASE_URL:-}"
SUPABASE_ANON_KEY="${SUPABASE_ANON_KEY:-${ANON_KEY:-}}"
EMAIL_INPUT="${EMAIL:-}"
PASSWORD_FILE="${SUPABASE_PASSWORD_FILE:-}"
SUPABASE_SERVICE_ROLE_KEY="${SUPABASE_SERVICE_ROLE_KEY:-${SERVICE_ROLE_KEY:-}}"

TOKEN_OUT_FILE=".tmp/supabase_access_token"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=./scripts/lib/env.sh
source "${SCRIPT_DIR}/lib/env.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

usage() {
  cat <<'EOF'
Usage: ./scripts/supabase-token.sh [options]

Options:
  --env-file <path>     Load env vars from file (default: none)
  --base-url <url>      Supabase API base URL (e.g. http://127.0.0.1:54321 or https://<host>:8444)
  --anon-key <key>      Supabase anon key (or set SUPABASE_ANON_KEY / ANON_KEY)
  --email <email>       Email to sign up/sign in with (optional; if omitted, you will be prompted)
  --password-file <path> Read password from a file (preferred for automation; avoids interactive prompt)
  --service-role-key <k> Service role key for Admin API fallback (or set SUPABASE_SERVICE_ROLE_KEY / SERVICE_ROLE_KEY)
  --out <path>          Output file for token (default: .tmp/supabase_access_token)
  --timeout <seconds>   curl timeout (default: 10)
  --insecure             Allow insecure TLS (curl -k). Use only for self-signed dev setups.
  -h, --help            Show help

What it does:
  1) Prompts for email + password.
  2) Attempts signup (ignored if user already exists).
  3) Signs in using password grant and writes access_token to output file.

Security:
  - The token is never printed.
  - The output file is chmod 600.

Notes:
  - For VPS Supabase (self-hosted), Kong HTTPS may be on :8444.
  - For local Supabase CLI, API is typically http://127.0.0.1:54321.
EOF
}

require_bin() {
  local bin="$1"
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo -e "${RED}Error:${NC} '$bin' is required"
    exit 1
  fi
}


while [ "${1:-}" != "" ]; do
  case "$1" in
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    --base-url)
      SUPABASE_BASE_URL="${2:-}"
      shift 2
      ;;
    --anon-key)
      SUPABASE_ANON_KEY="${2:-}"
      shift 2
      ;;
    --email)
      EMAIL_INPUT="${2:-}"
      shift 2
      ;;
    --password-file)
      PASSWORD_FILE="${2:-}"
      shift 2
      ;;
    --service-role-key)
      SUPABASE_SERVICE_ROLE_KEY="${2:-}"
      shift 2
      ;;
    --out)
      TOKEN_OUT_FILE="${2:-}"
      shift 2
      ;;
    --timeout)
      CURL_TIMEOUT_SECS="${2:-}"
      shift 2
      ;;
    --insecure)
      INSECURE_TLS="1"
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

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ -n "$ENV_FILE" ]; then
  load_env_file_no_override "$ENV_FILE"
fi

# Re-read after env-file load
SUPABASE_ANON_KEY="${SUPABASE_ANON_KEY:-${ANON_KEY:-}}"
SUPABASE_SERVICE_ROLE_KEY="${SUPABASE_SERVICE_ROLE_KEY:-${SERVICE_ROLE_KEY:-}}"

if [ -z "$SUPABASE_BASE_URL" ]; then
  echo -e "${RED}Error:${NC} --base-url is required (no SUPABASE_BASE_URL set)."
  echo "Examples:"
  echo "  --base-url http://127.0.0.1:54321"
  echo "  --base-url https://84.247.176.134:8444"
  exit 1
fi

if [ -z "$SUPABASE_ANON_KEY" ]; then
  echo -e "${RED}Error:${NC} Supabase anon key is required."
  echo "Set SUPABASE_ANON_KEY (or ANON_KEY) or pass --anon-key."
  exit 1
fi

# Normalize base URL (strip trailing slash)
SUPABASE_BASE_URL="${SUPABASE_BASE_URL%/}"
AUTH_BASE="${SUPABASE_BASE_URL}/auth/v1"

if [ "$INSECURE_TLS" = "1" ] || [ "$INSECURE_TLS" = "true" ]; then
  CURL_TLS_ARG="-k"
fi

preflight_check_auth_upstream() {
  # Validate that the provided base URL can actually reach GoTrue via Kong.
  # We intentionally avoid printing any secrets.
  local url="${AUTH_BASE}/settings"

  local tmp http_code body curl_exit
  tmp="$(mktemp)"

  set +e
  http_code="$(curl -sS -m "$CURL_TIMEOUT_SECS" \
    ${CURL_TLS_ARG:+$CURL_TLS_ARG} \
    -H "apikey: ${SUPABASE_ANON_KEY}" \
    -H "Authorization: Bearer ${SUPABASE_ANON_KEY}" \
    -o "$tmp" \
    -w "%{http_code}" \
    "$url")"
  curl_exit=$?
  set -e

  body="$(cat "$tmp" 2>/dev/null || true)"
  rm -f "$tmp" 2>/dev/null || true

  if [ "$curl_exit" -ne 0 ]; then
    echo -e "${RED}Error:${NC} Unable to reach Supabase Auth via: ${url} (curl exit=$curl_exit)"
    echo -e "${YELLOW}Hint:${NC} If using a private VPS, ensure your SSH tunnel is up and points to the correct Kong host ports."
    echo -e "${YELLOW}Hint:${NC} If using HTTPS with self-signed certs, pass --insecure."
    exit 1
  fi

  if [ "$http_code" = "200" ]; then
    return 0
  fi

  if [ "$http_code" = "401" ] || [ "$http_code" = "403" ]; then
    echo -e "${RED}Error:${NC} Supabase Auth preflight failed (http=$http_code) calling: ${url}"
    echo -e "${YELLOW}Hint:${NC} This usually means your anon key is missing/invalid for this Supabase instance."
    echo -e "${YELLOW}Hint:${NC} Ensure ANON_KEY (or SUPABASE_ANON_KEY) in the selected env-file matches the Supabase instance behind the tunnel."
    exit 1
  fi

  if [ "$http_code" = "503" ] && echo "$body" | grep -qi "name resolution failed"; then
    echo -e "${RED}Error:${NC} Supabase Auth is unreachable through Kong (http=503: name resolution failed)"
    echo -e "${YELLOW}Hint:${NC} This is a server-side routing/DNS issue inside the VPS Supabase stack (Kong cannot resolve the auth upstream)."
    echo -e "${YELLOW}Hint:${NC} On the VPS, check: 'docker ps' shows supabase-auth is running, and inspect logs: 'docker logs --tail 200 supabase-kong' and 'docker logs --tail 200 supabase-auth'."
    exit 1
  fi

  echo -e "${RED}Error:${NC} Supabase Auth preflight failed (http=$http_code) calling: ${url}"
  if echo "$body" | jq -e . >/dev/null 2>&1; then
    echo "$body" | jq -c '{error, error_description, msg, message, code}' 2>/dev/null || true
  else
    echo "$body"
  fi
  exit 1
}

preflight_check_auth_upstream

if [ -n "$EMAIL_INPUT" ]; then
  EMAIL="$EMAIL_INPUT"
else
  read -r -p "Email: " EMAIL
fi

if [ -n "${PASSWORD_FILE:-}" ]; then
  if [ ! -f "$PASSWORD_FILE" ]; then
    echo -e "${RED}Error:${NC} password file not found: $PASSWORD_FILE"
    exit 1
  fi
  PASSWORD="$(cat "$PASSWORD_FILE" 2>/dev/null || true)"
  PASSWORD="$(echo "$PASSWORD" | tr -d '\r\n')"
else
  read -r -s -p "Password (hidden): " PASSWORD
  echo
fi

if [ -z "$EMAIL" ] || [ -z "$PASSWORD" ]; then
  echo -e "${RED}Error:${NC} Email and password are required."
  exit 1
fi

mkdir -p "$(dirname "$TOKEN_OUT_FILE")"

SIGNUP_PAYLOAD="$(jq -nc --arg email "$EMAIL" --arg password "$PASSWORD" '{email:$email, password:$password}')"
LOGIN_PAYLOAD="$SIGNUP_PAYLOAD"

admin_create_user_if_possible() {
  local reason="$1"
  if [ -z "${SUPABASE_SERVICE_ROLE_KEY:-}" ]; then
    return 1
  fi

  echo -e "${YELLOW}Note:${NC} Attempting Admin API user creation (no email) due to: ${reason}"

  local tmp http_code body
  tmp="$(mktemp)"

  # GoTrue Admin API: create user and mark email confirmed.
  # Docs vary by version; the stable endpoint in Supabase is /auth/v1/admin/users.
  http_code="$(curl -sS -m "$CURL_TIMEOUT_SECS" \
    ${CURL_TLS_ARG:+$CURL_TLS_ARG} \
    -H "apikey: ${SUPABASE_SERVICE_ROLE_KEY}" \
    -H "Authorization: Bearer ${SUPABASE_SERVICE_ROLE_KEY}" \
    -H "Content-Type: application/json" \
    -o "$tmp" \
    -w "%{http_code}" \
    -X POST "${AUTH_BASE}/admin/users" \
    -d "$(jq -nc --arg email "$EMAIL" --arg password "$PASSWORD" '{email:$email, password:$password, email_confirm:true}')")"

  body="$(cat "$tmp" 2>/dev/null || true)"
  rm -f "$tmp" 2>/dev/null || true

  if [ "$http_code" = "200" ] || [ "$http_code" = "201" ]; then
    echo -e "${GREEN}OK${NC} Admin API user created/confirmed"
    return 0
  fi

  # If user already exists, treat as ok.
  if echo "$body" | grep -qi "already\|exists\|duplicate"; then
    echo -e "${YELLOW}Note:${NC} Admin API user likely already exists; continuing to login"
    return 0
  fi

  echo -e "${YELLOW}Warning:${NC} Admin API user creation failed (http=$http_code)"
  if echo "$body" | jq -e . >/dev/null 2>&1; then
    echo "$body" | jq -c '{error, error_description, msg, message, code}' 2>/dev/null || echo "$body"
  else
    echo "$body"
  fi
  return 1
}

# Attempt signup first (safe for dev; enable_confirmations=false in config.toml).
# Ignore errors like "User already registered".
SIGNUP_TMP="$(mktemp)"
set +e
SIGNUP_HTTP_CODE="$(curl -sS -m "$CURL_TIMEOUT_SECS" \
  ${CURL_TLS_ARG:+$CURL_TLS_ARG} \
  -H "apikey: ${SUPABASE_ANON_KEY}" \
  -H "Authorization: Bearer ${SUPABASE_ANON_KEY}" \
  -H "Content-Type: application/json" \
  -o "$SIGNUP_TMP" \
  -w "%{http_code}" \
  -X POST "${AUTH_BASE}/signup" \
  -d "$SIGNUP_PAYLOAD")"
SIGNUP_BODY="$(cat "$SIGNUP_TMP" 2>/dev/null || true)"
rm -f "$SIGNUP_TMP" 2>/dev/null || true
set -e

if [ "$SIGNUP_HTTP_CODE" = "200" ] || [ "$SIGNUP_HTTP_CODE" = "201" ]; then
  echo -e "${GREEN}OK${NC} Supabase signup succeeded"
else
  if [ "$SIGNUP_HTTP_CODE" = "503" ] && echo "$SIGNUP_BODY" | grep -qi "name resolution failed"; then
    echo -e "${RED}Error:${NC} Supabase signup failed (http=503: name resolution failed)"
    echo -e "${YELLOW}Hint:${NC} This is a server-side routing/DNS issue inside the VPS Supabase stack (Kong cannot resolve the auth upstream)."
    echo -e "${YELLOW}Hint:${NC} On the VPS, check logs: 'docker logs --tail 200 supabase-kong' and 'docker logs --tail 200 supabase-auth'."
    exit 1
  fi
  # Common expected cases:
  # - 400/422: user already exists
  # - 403: signups disabled
  if echo "$SIGNUP_BODY" | grep -qi "already registered\|already exists\|user already"; then
    echo -e "${YELLOW}Note:${NC} Supabase signup skipped (user likely already exists)"
  else
    echo -e "${YELLOW}Warning:${NC} Supabase signup did not succeed (http=$SIGNUP_HTTP_CODE). Continuing to login."
    if echo "$SIGNUP_BODY" | jq -e . >/dev/null 2>&1; then
      echo "$SIGNUP_BODY" | jq -c '{error, error_description, msg, message, code}' 2>/dev/null || echo "$SIGNUP_BODY"
    else
      echo "$SIGNUP_BODY"
    fi

    # Fallback: if signup fails due to email/confirmation delivery, try Admin API.
    if echo "$SIGNUP_BODY" | grep -qi "confirmation email\|sending confirmation\|smtp"; then
      admin_create_user_if_possible "signup error related to confirmation email" || true
    fi
  fi
fi

# Password grant login
TMP="$(mktemp)"
HTTP_CODE="$(curl -sS -m "$CURL_TIMEOUT_SECS" \
  ${CURL_TLS_ARG:+$CURL_TLS_ARG} \
  -H "apikey: ${SUPABASE_ANON_KEY}" \
  -H "Authorization: Bearer ${SUPABASE_ANON_KEY}" \
  -H "Content-Type: application/json" \
  -o "$TMP" \
  -w "%{http_code}" \
  -X POST "${AUTH_BASE}/token?grant_type=password" \
  -d "$LOGIN_PAYLOAD")"
BODY="$(cat "$TMP" 2>/dev/null || true)"
rm -f "$TMP" 2>/dev/null || true

if [ "$HTTP_CODE" != "200" ]; then
  echo -e "${RED}FAIL${NC} Supabase login - expected http=200 got http=$HTTP_CODE"
  # Print only non-secret error fields
  if echo "$BODY" | jq -e . >/dev/null 2>&1; then
    echo "$BODY" | jq -c '{error, error_description, msg, message}' 2>/dev/null || echo "$BODY"
  else
    echo "$BODY"
  fi

  if echo "$BODY" | grep -qi "Invalid login credentials"; then
    echo
    echo -e "${YELLOW}Hint:${NC} This usually means one of:"
    echo "  - The user does not exist (signup failed/disabled)"
    echo "  - The password does not match an existing user"
    echo "Try again with a brand-new email like: smoke+$(date +%s)@example.com"
    echo "If signups are disabled, create a user via Supabase Studio or Admin API first."
  fi
  exit 1
fi

ACCESS_TOKEN="$(echo "$BODY" | jq -r '.access_token // empty')"
if [ -z "$ACCESS_TOKEN" ] || [ "$ACCESS_TOKEN" = "null" ]; then
  echo -e "${RED}FAIL${NC} Supabase login - missing access_token"
  exit 1
fi

# Write token securely
umask 077
printf '%s' "$ACCESS_TOKEN" >"$TOKEN_OUT_FILE"
chmod 600 "$TOKEN_OUT_FILE" 2>/dev/null || true

echo -e "${GREEN}OK${NC} Access token written to: $TOKEN_OUT_FILE"
echo "Next:"
echo "  export SUPABASE_ACCESS_TOKEN=\"\$(cat $TOKEN_OUT_FILE)\""
