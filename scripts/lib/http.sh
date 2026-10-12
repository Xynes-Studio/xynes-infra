#!/usr/bin/env bash
# Common helper functions for smoke test scripts

# ─────────────────────────────────────────────────────────────────────────────
# require_bin - Check if a binary is available
# Usage: require_bin <binary_name>
# ─────────────────────────────────────────────────────────────────────────────
require_bin() {
  local bin="$1"
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo -e "${RED:-}Error:${NC:-} '$bin' is required"
    exit 1
  fi
}

# ─────────────────────────────────────────────────────────────────────────────
# load_token_file - Load a token from a file
# Usage: load_token_file <file_path>
# Sets: SUPABASE_ACCESS_TOKEN
# ─────────────────────────────────────────────────────────────────────────────
load_token_file() {
  local file="$1"
  if [ -z "$file" ]; then
    return 1
  fi
  if [ ! -f "$file" ]; then
    echo -e "${RED:-}Error:${NC:-} token file not found: $file"
    exit 1
  fi

  local token
  token="$(cat "$file" 2>/dev/null || true)"
  token="$(echo "$token" | tr -d '\r\n')"
  if [ -z "$token" ]; then
    echo -e "${RED:-}Error:${NC:-} token file is empty: $file"
    exit 1
  fi

  export SUPABASE_ACCESS_TOKEN="$token"
  return 0
}

# ─────────────────────────────────────────────────────────────────────────────
# HTTP Request Helpers
# ─────────────────────────────────────────────────────────────────────────────

# HTTP request with auth headers
# Usage: http_request METHOD URL [BODY]
# Sets: HTTP_CODE, HTTP_BODY
HTTP_CODE=""
HTTP_BODY=""

http_request() {
  local method="$1"
  local url="$2"
  local body="${3:-}"
  local tmp
  tmp="$(mktemp)"

  local curl_args=(
    -sS -m "${CURL_TIMEOUT_SECS:-10}"
    -X "$method"
    -H "Content-Type: application/json"
    -H "Accept: application/json"
    -o "$tmp"
    -w "%{http_code}"
  )

  # Add auth header if token available
  if [ -n "${SUPABASE_ACCESS_TOKEN:-}" ]; then
    curl_args+=(-H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN")
  fi

  # Add internal service token if available
  if [ -n "${INTERNAL_SERVICE_TOKEN:-}" ]; then
    curl_args+=(-H "X-Internal-Service-Token: $INTERNAL_SERVICE_TOKEN")
  fi

  # Add workspace header if set
  if [ -n "${CURRENT_WORKSPACE_ID:-}" ]; then
    curl_args+=(-H "X-Workspace-Id: $CURRENT_WORKSPACE_ID")
  fi

  # Add user header if set
  if [ -n "${CURRENT_USER_ID:-}" ]; then
    curl_args+=(-H "X-XS-User-Id: $CURRENT_USER_ID")
  fi

  if [ -n "$body" ]; then
    curl_args+=(-d "$body")
  fi

  set +e
  HTTP_CODE="$(curl "${curl_args[@]}" "$url" 2>/dev/null)"
  local exit_code=$?
  set -e

  HTTP_BODY="$(cat "$tmp" 2>/dev/null || true)"
  rm -f "$tmp"

  if [ "$exit_code" -ne 0 ]; then
    HTTP_CODE="000"
  fi
}

# Internal action helper
# Usage: internal_action SERVICE_URL ACTION_KEY PAYLOAD
internal_action() {
  local url="$1"
  local action_key="$2"
  local payload="$3"
  local body
  body=$(jq -nc --arg ak "$action_key" --argjson p "$payload" '{actionKey: $ak, payload: $p}')
  http_request "POST" "$url" "$body"
}

# ─────────────────────────────────────────────────────────────────────────────
# Assertion Helpers
# ─────────────────────────────────────────────────────────────────────────────

# Check if HTTP response is 2xx
expect_http_ok() {
  local label="$1"
  if [[ "$HTTP_CODE" =~ ^2[0-9][0-9]$ ]]; then
    return 0
  fi
  if command -v log_fail >/dev/null 2>&1; then
    log_fail "$label - expected 2xx got $HTTP_CODE"
  else
    echo -e "${RED:-}FAIL${NC:-} $label - expected 2xx got $HTTP_CODE"
  fi
  echo "  body: $(echo "$HTTP_BODY" | head -c 300)" >&2
  return 1
}

# Check if response has ok:true
expect_ok_true() {
  local label="$1"
  local ok_val
  ok_val="$(echo "$HTTP_BODY" | jq -r '.ok // "false"' 2>/dev/null || true)"
  if [ "$ok_val" = "true" ]; then
    return 0
  fi
  if command -v log_fail >/dev/null 2>&1; then
    log_fail "$label - expected ok:true"
  else
    echo -e "${RED:-}FAIL${NC:-} $label - expected ok:true"
  fi
  return 1
}

# Extract JSON field from HTTP_BODY
extract_json() {
  local path="$1"
  echo "$HTTP_BODY" | jq -r "$path // empty" 2>/dev/null || true
}
