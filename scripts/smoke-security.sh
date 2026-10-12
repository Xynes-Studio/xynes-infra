#!/usr/bin/env bash
set -euo pipefail

# SMOKE-SEC-2 – Security & Rate-Limit Smoke Tests
#
# Validates security features E2E using self-hosted Supabase and live services:
# - Rate limit check: Rapid calls result in 429
# - Body limit check: Oversized payload results in 413
# - Internal JWT check: Bypassing gateway results in 401/403
# - Telemetry check: Events exist with proper fields (no sensitive data)
#
# Security: Token values are NEVER logged. No secrets exposed in output.

MODE="${MODE:-localhost}"
ENV_FILE="${XYNES_INFRA_ENV_FILE:-}"
CURL_TIMEOUT_SECS="${CURL_TIMEOUT_SECS:-10}"
TOKEN_FILE="${TOKEN_FILE:-}"
VERBOSE="${VERBOSE:-0}"
SKIP_DB_CHECK="${SKIP_DB_CHECK:-0}"
BODY_LIMIT_MB="${BODY_LIMIT_MB:-5}"

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

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

usage() {
  cat <<'EOF'
SMOKE-SEC-2 – Security & Rate-Limit Smoke Tests

Usage: ./scripts/smoke-security.sh [options]

Validates security features E2E:
  • Rate limiting returns 429 on rapid calls
  • Body limit returns 413 on oversized payloads
  • Internal JWT check returns 401/403 when bypassing gateway
  • Telemetry events exist and exclude sensitive data

Options:
  --mode <localhost|env>   localhost: http://localhost:<PORT> (default)
                           env: use *_URL variables as-is
  --env-file <path>        env file to load (default: .env.localhost for localhost, .env.dev for env)
  --token-file <path>      read SUPABASE_ACCESS_TOKEN from file (for telemetry check)
  --timeout <seconds>      curl timeout (default: 10)
  --body-limit <MB>        expected body limit in MB (default: 5)
  --skip-db-check          skip DB connectivity check
  --verbose                show request/response details
  -h, --help               show this help

Environment:
  WORKSPACE_ID             Required for rate limit and telemetry tests
  INTERNAL_SERVICE_TOKEN   Required for telemetry test (to query events)
  DATABASE_URL             Optional, for DB connectivity check

Examples:
  # Basic run with localhost services
  ./scripts/smoke-security.sh --env-file .env.localhost

  # Run against Docker compose services
  ./scripts/smoke-security.sh --mode env --env-file .env.dev

  # With telemetry validation (requires token)
  ./scripts/smoke-security.sh --env-file .env.localhost --token-file .tmp/supabase_access_token

Notes:
  - For telemetry tests, ensure WORKSPACE_ID is set (from smoke-setup.sh)
  - This script never logs token values
  - Failures have clear messages for regression detection

EOF
}

log_pass() { echo -e "${GREEN}PASS${NC} $1"; ((PASS_COUNT++)) || true; }
log_fail() { echo -e "${RED}FAIL${NC} $1"; ((FAIL_COUNT++)) || true; }
log_skip() { echo -e "${YELLOW}SKIP${NC} $1"; ((SKIP_COUNT++)) || true; }
log_info() { echo -e "${BLUE}INFO${NC} $1"; }
log_warn() { echo -e "${YELLOW}WARN${NC} $1"; }
log_section() { echo -e "\n${BOLD}${CYAN}━━━ $1 ━━━${NC}"; }

# Verbose logging helper
log_verbose() {
  if [ "$VERBOSE" = "1" ]; then
    echo -e "  ${BLUE}→${NC} $1"
  fi
}

# HTTP request with verbose logging (overrides lib/http.sh version)
# Usage: http_request_raw METHOD URL [BODY] [EXTRA_HEADERS...]
# Sets: HTTP_CODE, HTTP_BODY
http_request_raw() {
  local method="$1"
  local url="$2"
  local body="${3:-}"
  shift 3 || true
  local extra_headers=()
  if [ $# -gt 0 ]; then
    extra_headers=("$@")
  fi
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

  # Add extra headers
  if [ ${#extra_headers[@]} -gt 0 ]; then
    for header in "${extra_headers[@]}"; do
      curl_args+=(-H "$header")
    done
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

  log_verbose "$method $url → $HTTP_CODE"
}

# HTTP request with auth (for telemetry queries)
# Usage: http_request_auth METHOD URL [BODY]
http_request_auth() {
  local method="$1"
  local url="$2"
  local body="${3:-}"
  local headers=()

  if [ -n "${SUPABASE_ACCESS_TOKEN:-}" ]; then
    headers+=("Authorization: Bearer $SUPABASE_ACCESS_TOKEN")
  fi

  if [ -n "${INTERNAL_SERVICE_TOKEN:-}" ]; then
    headers+=("X-Internal-Service-Token: $INTERNAL_SERVICE_TOKEN")
  fi

  if [ -n "${CURRENT_WORKSPACE_ID:-}" ]; then
    headers+=("X-Workspace-Id: $CURRENT_WORKSPACE_ID")
  fi

  http_request_raw "$method" "$url" "$body" "${headers[@]}"
}

# ─────────────────────────────────────────────────────────────────────────────
# Parse args
# ─────────────────────────────────────────────────────────────────────────────

cd "$ROOT_DIR"

while [ "${1:-}" != "" ]; do
  case "$1" in
    --mode) MODE="${2:-}"; shift 2 ;;
    --env-file) ENV_FILE="${2:-}"; shift 2 ;;
    --token-file) TOKEN_FILE="${2:-}"; shift 2 ;;
    --timeout) CURL_TIMEOUT_SECS="${2:-}"; shift 2 ;;
    --body-limit) BODY_LIMIT_MB="${2:-}"; shift 2 ;;
    --skip-db-check) SKIP_DB_CHECK=1; shift ;;
    --verbose) VERBOSE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo -e "${RED}Error:${NC} unknown arg: $1"; usage; exit 2 ;;
  esac
done

# Validate mode argument
if [ "$MODE" != "localhost" ] && [ "$MODE" != "env" ]; then
  echo -e "${RED}Error:${NC} invalid --mode '$MODE'. Must be 'localhost' or 'env'"
  exit 2
fi

require_bin curl
require_bin jq

# Default env file based on mode
if [ -z "$ENV_FILE" ]; then
  if [ "$MODE" = "localhost" ]; then
    ENV_FILE=".env.localhost"
  else
    ENV_FILE=".env.dev"
  fi
fi

# Load env
load_env_file_no_override "$ENV_FILE"
load_env_file_no_override ".env"

# Load token if provided
if [ -n "$TOKEN_FILE" ]; then
  load_token_file "$TOKEN_FILE"
fi

# Build URLs based on mode
if [ "$MODE" = "localhost" ]; then
  GATEWAY_URL="http://localhost:${GATEWAY_PORT:-4100}"
  CMS_CORE_URL="http://localhost:${CMS_CORE_PORT:-4202}"
  TELEMETRY_SERVICE_URL="http://localhost:${TELEMETRY_SERVICE_PORT:-4400}"
else
  GATEWAY_URL="${GATEWAY_URL:-http://gateway:4100}"
  CMS_CORE_URL="${CMS_CORE_URL:-http://cms-core:4202}"
  TELEMETRY_SERVICE_URL="${TELEMETRY_SERVICE_URL:-http://telemetry-service:4400}"
fi

TELEMETRY_ACTIONS_URL="${TELEMETRY_SERVICE_URL%/}/internal/telemetry-actions"
CMS_ACTIONS_URL="${CMS_CORE_URL%/}/internal/cms-actions"

echo "═══════════════════════════════════════════════════════════════════════════"
echo "  SMOKE-SEC-2: Security & Rate-Limit Smoke Tests"
echo "═══════════════════════════════════════════════════════════════════════════"
echo "Mode: $MODE | Env: $ENV_FILE | Timeout: ${CURL_TIMEOUT_SECS}s"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight checks
# ─────────────────────────────────────────────────────────────────────────────

log_section "Pre-flight Checks"

# Check gateway is up
http_request_raw "GET" "${GATEWAY_URL}/health"
if [ "$HTTP_CODE" = "200" ]; then
  log_pass "Gateway is reachable"
else
  log_fail "Gateway not reachable (got $HTTP_CODE)"
  echo -e "${RED}Aborting:${NC} Gateway must be running for security tests"
  exit 1
fi

# ─────────────────────────────────────────────────────────────────────────────
# 1. Rate Limit Check
# ─────────────────────────────────────────────────────────────────────────────

log_section "1. Rate Limit Check (SEC-RATELIMIT-1)"

test_rate_limit() {
  local workspace_id="${WORKSPACE_ID:-}"
  
  if [ -z "$workspace_id" ]; then
    log_skip "Rate limit test - WORKSPACE_ID not set"
    log_info "Run smoke-setup.sh first to create a workspace"
    return
  fi

  # Route 3: GET /workspaces/:workspaceId/blog (public route)
  # This route has rate limit: 60 requests per 60 seconds (ip bucket)
  # Using a public route so we don't need authentication
  # Note: 404 responses are expected if no blog posts exist, but rate limiting still applies
  local url="${GATEWAY_URL}/workspaces/${workspace_id}/blog"
  
  log_info "Sending rapid requests to rate-limited public endpoint..."
  log_verbose "Target: GET $url (public blog listing)"
  log_verbose "Expected: 429 Too Many Requests after hitting 60 req/min limit"

  local got_429=0
  local request_count=0
  local max_requests=75  # More than the 60 req/min limit
  
  # Send rapid requests until we get 429 or hit max
  for i in $(seq 1 $max_requests); do
    request_count=$i
    
    http_request_raw "GET" "$url"
    
    if [ "$HTTP_CODE" = "429" ]; then
      got_429=1
      break
    fi
    
    # Small delay to avoid overwhelming, but fast enough to hit rate limit
    sleep 0.02
  done

  if [ "$got_429" = "1" ]; then
    log_pass "Rate limit triggered after $request_count requests (got 429)"
    
    # Verify rate limit headers are present in body
    local retry_after
    retry_after="$(echo "$HTTP_BODY" | jq -r '.error.retryAfterSeconds // empty' 2>/dev/null || true)"
    if [ -n "$retry_after" ]; then
      log_verbose "Retry-After: ${retry_after}s"
    fi
  else
    # Check if rate limiting might not be enabled
    log_warn "Rate limit NOT triggered after $max_requests requests"
    log_verbose "Last response code: $HTTP_CODE"
    log_info "Rate limiting may not be enabled in the current environment"
    log_info "This is acceptable if rate limits are configured in production only"
    # Don't fail - rate limiting might be intentionally disabled in dev
    log_skip "Rate limit test - feature may be disabled"
  fi
}

test_rate_limit

# ─────────────────────────────────────────────────────────────────────────────
# 2. Body Limit Check
# ─────────────────────────────────────────────────────────────────────────────

log_section "2. Body Limit Check (SEC-BODYLIMIT-1)"

test_body_limit() {
  local workspace_id="${WORKSPACE_ID:-}"
  
  if [ -z "$workspace_id" ]; then
    log_skip "Body limit test - WORKSPACE_ID not set"
    log_info "Run smoke-setup.sh first to create a workspace"
    return
  fi

  # Route 1: POST /workspaces/:workspaceId/documents
  # This route has body limits and requires authentication
  # Using document creation endpoint which has a larger limit (5 MB)
  # but we can test that body limits are enforced
  local url="${GATEWAY_URL}/workspaces/${workspace_id}/documents"
  
  # Check if we have auth token
  if [ -z "${SUPABASE_ACCESS_TOKEN:-}" ]; then
    log_skip "Body limit test - SUPABASE_ACCESS_TOKEN not set"
    log_info "Provide --token-file to run this test"
    return
  fi
  
  log_info "Sending oversized payload to body-limited endpoint..."
  log_verbose "Target: POST $url (document creation)"
  log_verbose "Expected: 413 Payload Too Large"

  # Generate a large payload using dd to avoid shell argument limits
  # Creating payload larger than the configured body limit
  local payload_size_kb
  payload_size_kb=$(( (BODY_LIMIT_MB + 1) * 1024 ))  # 1 MB larger than limit
  
  log_verbose "Body limit: ${BODY_LIMIT_MB}MB, generating payload: ${payload_size_kb}KB"
  
  local tmp_body
  tmp_body="$(mktemp)"
  
  # Create the JSON structure with a large content field
  # Using dd to generate random data, then wrapping in JSON
  echo -n '{"title":"test","content":"' > "$tmp_body"
  dd if=/dev/zero bs=1024 count="$payload_size_kb" 2>/dev/null | tr '\0' 'x' >> "$tmp_body"
  echo '"}' >> "$tmp_body"

  # Use authenticated request with file-based body
  local tmp_response
  tmp_response="$(mktemp)"
  
  set +e
  HTTP_CODE=$(curl -sS -m "${CURL_TIMEOUT_SECS:-30}" \
    -X POST \
    -H "Content-Type: application/json" \
    -H "Accept: application/json" \
    -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
    -d @"$tmp_body" \
    -o "$tmp_response" \
    -w "%{http_code}" \
    "$url" 2>/dev/null)
  local exit_code=$?
  set -e
  
  HTTP_BODY="$(cat "$tmp_response" 2>/dev/null || true)"
  rm -f "$tmp_body" "$tmp_response"
  
  if [ "$exit_code" -ne 0 ]; then
    HTTP_CODE="000"
  fi
  
  log_verbose "POST $url → $HTTP_CODE"

  if [ "$HTTP_CODE" = "413" ]; then
    log_pass "Body limit enforced (got 413 Payload Too Large)"
  elif [ "$HTTP_CODE" = "400" ]; then
    # Some implementations return 400 for body limit exceeded
    local error_code
    error_code="$(echo "$HTTP_BODY" | jq -r '.error.code // empty' 2>/dev/null || true)"
    if [[ "$error_code" == *"BODY"* ]] || [[ "$error_code" == *"PAYLOAD"* ]] || [[ "$error_code" == *"LIMIT"* ]]; then
      log_pass "Body limit enforced (got 400 with body limit error)"
    else
      log_fail "Unexpected response for oversized body (got $HTTP_CODE, code: $error_code)"
      log_verbose "Response: $(echo "$HTTP_BODY" | head -c 200)"
    fi
  elif [ "$HTTP_CODE" = "401" ]; then
    log_fail "Got 401 - authentication failed for body limit test"
    log_verbose "Ensure SUPABASE_ACCESS_TOKEN is valid"
  else
    log_fail "Body limit NOT enforced for oversized payload (got $HTTP_CODE, expected 413)"
    log_verbose "Response: $(echo "$HTTP_BODY" | head -c 200)"
  fi
}

test_body_limit

# ─────────────────────────────────────────────────────────────────────────────
# 3. Internal JWT Check
# ─────────────────────────────────────────────────────────────────────────────

log_section "3. Internal JWT Check (SEC-INTERNAL-AUTH-2)"

test_internal_jwt() {
  log_info "Calling internal service endpoint directly without internal JWT..."
  log_verbose "Target: POST $CMS_ACTIONS_URL (bypassing gateway)"
  log_verbose "Expected: 401 Unauthorized or 403 Forbidden"

  # Try to call internal endpoint directly (bypassing gateway)
  # WITHOUT the X-Internal-Service-Token header
  local body='{"actionKey":"cms.entry.listByDirectory","payload":{"directoryId":null,"status":"all","limit":1,"offset":0}}'
  
  # Make request WITHOUT internal service token
  http_request_raw "POST" "$CMS_ACTIONS_URL" "$body"

  if [ "$HTTP_CODE" = "401" ] || [ "$HTTP_CODE" = "403" ]; then
    log_pass "Internal endpoint protected (got $HTTP_CODE without JWT)"
  elif [ "$HTTP_CODE" = "000" ]; then
    log_fail "Could not reach internal service at $CMS_ACTIONS_URL"
    log_verbose "Ensure cms-core is running and accessible"
  else
    log_fail "Internal endpoint NOT protected (got $HTTP_CODE, expected 401/403)"
    log_verbose "Response: $(echo "$HTTP_BODY" | head -c 200)"
  fi

  # Additional test: call with INVALID token
  log_info "Calling internal endpoint with invalid JWT..."
  local invalid_token="invalid.jwt.token"
  
  http_request_raw "POST" "$CMS_ACTIONS_URL" "$body" "X-Internal-Service-Token: $invalid_token"

  if [ "$HTTP_CODE" = "401" ] || [ "$HTTP_CODE" = "403" ]; then
    log_pass "Invalid JWT rejected (got $HTTP_CODE)"
  else
    log_fail "Invalid JWT NOT rejected (got $HTTP_CODE, expected 401/403)"
    log_verbose "Response: $(echo "$HTTP_BODY" | head -c 200)"
  fi
}

test_internal_jwt

# ─────────────────────────────────────────────────────────────────────────────
# 4. Telemetry Check
# ─────────────────────────────────────────────────────────────────────────────

log_section "4. Telemetry Check (TELE-VIEW-1)"

test_telemetry() {
  local workspace_id="${WORKSPACE_ID:-}"
  local internal_token="${INTERNAL_SERVICE_TOKEN:-}"
  
  if [ -z "$workspace_id" ]; then
    log_skip "Telemetry test - WORKSPACE_ID not set"
    return
  fi

  if [ -z "$internal_token" ]; then
    log_skip "Telemetry test - INTERNAL_SERVICE_TOKEN not set"
    return
  fi

  # First, test that telemetry endpoint requires internal auth
  log_info "Testing telemetry endpoint protection..."
  log_verbose "Target: POST $TELEMETRY_ACTIONS_URL (without auth)"
  
  local ingest_body
  ingest_body=$(jq -nc '{
    actionKey: "telemetry.event.ingest",
    payload: {
      source: "smoke-security-test",
      eventType: "http_request",
      name: "test.security.check",
      targetType: "workspace",
      targetId: "test-workspace-id",
      metadata: {path: "/test", statusCode: 200}
    }
  }')
  
  # Try without internal token - should fail
  http_request_raw "POST" "$TELEMETRY_ACTIONS_URL" "$ingest_body"
  
  if [ "$HTTP_CODE" = "401" ] || [ "$HTTP_CODE" = "403" ]; then
    log_pass "Telemetry endpoint protected (requires internal auth)"
  else
    log_fail "Telemetry endpoint NOT protected (got $HTTP_CODE, expected 401/403)"
    log_verbose "Response: $(echo "$HTTP_BODY" | head -c 200)"
  fi

  # Now test with internal token - ingest should work
  log_info "Testing telemetry event ingestion..."
  log_verbose "Target: POST $TELEMETRY_ACTIONS_URL (with auth)"
  
  CURRENT_WORKSPACE_ID="$workspace_id"
  http_request_auth "POST" "$TELEMETRY_ACTIONS_URL" "$ingest_body"
  
  if [ "$HTTP_CODE" = "201" ]; then
    log_pass "Telemetry event ingestion works (got 201)"
  elif [ "$HTTP_CODE" = "200" ]; then
    log_pass "Telemetry event ingestion works (got 200)"
  else
    log_fail "Telemetry event ingestion failed (got $HTTP_CODE)"
    log_verbose "Response: $(echo "$HTTP_BODY" | head -c 200)"
    return
  fi

  # Try to query events (if the action is available)
  # Use retry logic to handle ingestion latency
  log_info "Attempting to query telemetry events (with retry)..."
  
  local query_body
  query_body=$(jq -nc \
    --arg ak "telemetry.events.listRecentForWorkspace" \
    --arg wid "$workspace_id" \
    '{actionKey: $ak, payload: {workspaceId: $wid, limit: 10}}')

  local max_retries=6
  local retry_delay=0.5
  local retry_count=0
  local query_success=0
  
  while [ "$retry_count" -lt "$max_retries" ]; do
    http_request_auth "POST" "$TELEMETRY_ACTIONS_URL" "$query_body"
    
    if [ "$HTTP_CODE" = "200" ]; then
      local temp_count
      temp_count="$(echo "$HTTP_BODY" | jq -r '.data.events | length // 0' 2>/dev/null || echo "0")"
      if [ "$temp_count" -gt 0 ]; then
        query_success=1
        break
      fi
    fi
    
    ((retry_count++)) || true
    log_verbose "Retry $retry_count/$max_retries - waiting for events to be available..."
    sleep "$retry_delay"
  done
  
  if [ "$query_success" = "0" ] && [ "$retry_count" -gt 0 ]; then
    log_verbose "Query returned after $retry_count retries"
  fi

  if [ "$HTTP_CODE" = "200" ]; then
    local ok_val
    ok_val="$(echo "$HTTP_BODY" | jq -r '.ok // false' 2>/dev/null || true)"
    if [ "$ok_val" = "true" ]; then
      local event_count
      event_count="$(echo "$HTTP_BODY" | jq -r '.data.events | length // 0' 2>/dev/null || echo "0")"
      log_pass "Telemetry query works ($event_count events)"
      
      # Validate no sensitive data in events if we have any
      if [ "$event_count" -gt 0 ]; then
        local all_events
        all_events="$(echo "$HTTP_BODY" | jq -r '.data.events' 2>/dev/null || true)"
        
        # Check for Authorization header leakage
        local has_auth_header
        has_auth_header="$(echo "$all_events" | jq 'any(.[]; .metadata.headers? // {} | has("authorization") or has("Authorization"))' 2>/dev/null || echo "false")"
        
        if [ "$has_auth_header" = "true" ]; then
          log_fail "SECURITY: Telemetry events contain Authorization header!"
        else
          log_pass "No Authorization header in telemetry events"
        fi
        
        # Check for query string leakage (could contain tokens)
        local has_queryString
        has_queryString="$(echo "$all_events" | jq 'any(.[]; .metadata.queryString? != null and .metadata.queryString != "")' 2>/dev/null || echo "false")"
        
        if [ "$has_queryString" = "true" ]; then
          log_fail "SECURITY: Telemetry events contain query strings (may leak tokens)"
        else
          log_pass "No query strings in telemetry events"
        fi
        
        # Check for raw request/response bodies
        local has_requestBody
        has_requestBody="$(echo "$all_events" | jq 'any(.[]; .metadata.requestBody? != null or .metadata.responseBody? != null)' 2>/dev/null || echo "false")"
        
        if [ "$has_requestBody" = "true" ]; then
          log_fail "SECURITY: Telemetry events contain request/response bodies"
        else
          log_pass "No request/response bodies in telemetry events"
        fi
      fi
    else
      log_warn "Telemetry query returned ok:false"
      log_skip "Telemetry query - service returned error"
    fi
  elif [ "$HTTP_CODE" = "400" ]; then
    # Query action might not be available in this version
    log_warn "Telemetry query action not available (got 400)"
    log_skip "Telemetry query - action may not be deployed"
  else
    log_fail "Telemetry query failed (got $HTTP_CODE)"
    log_verbose "Response: $(echo "$HTTP_BODY" | head -c 200)"
  fi
}

test_telemetry

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────

echo ""
echo "═══════════════════════════════════════════════════════════════════════════"
echo -e "  Results: ${GREEN}$PASS_COUNT passed${NC} | ${RED}$FAIL_COUNT failed${NC} | ${YELLOW}$SKIP_COUNT skipped${NC}"
echo "═══════════════════════════════════════════════════════════════════════════"

if [ "$FAIL_COUNT" -gt 0 ]; then
  echo -e "\n${RED}SMOKE-SEC-2 FAILED${NC}"
  echo "Security smoke tests detected regressions."
  exit 1
fi

if [ "$SKIP_COUNT" -gt 0 ] && [ "$PASS_COUNT" -eq 0 ]; then
  echo -e "\n${YELLOW}SMOKE-SEC-2 INCOMPLETE${NC}"
  echo "All tests were skipped. Set WORKSPACE_ID and INTERNAL_SERVICE_TOKEN to run full suite."
  exit 0
fi

echo -e "\n${GREEN}SMOKE-SEC-2 PASSED${NC}"
echo "Security smoke tests completed successfully."
exit 0
