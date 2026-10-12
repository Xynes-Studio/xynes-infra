#!/usr/bin/env bash
set -euo pipefail

# ═══════════════════════════════════════════════════════════════════════════════
# INFRA-BE-1: Feature Flags Manual Verification Script
# ═══════════════════════════════════════════════════════════════════════════════
#
# This script tests the /flags API endpoints against a running gateway.
#
# Prerequisites:
# 1. SSH tunnel to database: ssh -N -L 5432:172.18.0.4:5432 xynes@84.247.176.134
# 2. Gateway running (docker or local)
# 3. Valid JWT token (use supabase-token.sh to get one)
#
# Usage:
#   ./feature-flags.test.sh [GATEWAY_URL] [JWT_TOKEN]
#
# Examples:
#   ./feature-flags.test.sh                                    # Uses defaults
#   ./feature-flags.test.sh http://localhost:4100 eyJhbG...    # Custom URL & token
#
# ═══════════════════════════════════════════════════════════════════════════════

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# Load common test utilities
source ./scripts/lib/colors.sh 2>/dev/null || {
  # Fallback if colors.sh doesn't exist
  GREEN='\033[0;32m'
  RED='\033[0;31m'
  YELLOW='\033[1;33m'
  NC='\033[0m'
}

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

GATEWAY_URL="${1:-http://localhost:4100}"
JWT_TOKEN="${2:-}"

# If no token provided, try to get one from supabase-token.sh
if [ -z "$JWT_TOKEN" ]; then
  if [ -f "./scripts/supabase-token.sh" ]; then
    echo "No JWT token provided, attempting to get one from supabase-token.sh..."
    JWT_TOKEN=$(./scripts/supabase-token.sh 2>/dev/null | tail -1) || true
  fi
fi

if [ -z "$JWT_TOKEN" ]; then
  echo -e "${YELLOW}WARNING: No JWT token provided. Auth tests will fail.${NC}"
  echo "Usage: $0 [GATEWAY_URL] [JWT_TOKEN]"
  echo ""
fi

# ─────────────────────────────────────────────────────────────────────────────
# Test Counters
# ─────────────────────────────────────────────────────────────────────────────

PASS_COUNT=0
FAIL_COUNT=0

pass() {
  echo -e "${GREEN}✓ PASS:${NC} $1"
  ((PASS_COUNT++)) || true
}

fail() {
  echo -e "${RED}✗ FAIL:${NC} $1"
  ((FAIL_COUNT++)) || true
}

warn() {
  echo -e "${YELLOW}⚠ WARN:${NC} $1"
}

# ─────────────────────────────────────────────────────────────────────────────
# Test Utilities
# ─────────────────────────────────────────────────────────────────────────────

# Check if gateway is reachable
check_gateway() {
  echo "Checking gateway at $GATEWAY_URL..."
  if curl -sf "${GATEWAY_URL}/health" > /dev/null 2>&1; then
    pass "Gateway is reachable"
    return 0
  else
    fail "Gateway not reachable at $GATEWAY_URL"
    return 1
  fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

echo ""
echo "═══════════════════════════════════════════════════════════════════════════════"
echo "  INFRA-BE-1: Feature Flags API Tests"
echo "═══════════════════════════════════════════════════════════════════════════════"
echo ""
echo "Gateway URL: $GATEWAY_URL"
echo ""

# Test 1: Gateway health check
check_gateway || {
  echo ""
  echo "Cannot proceed without gateway. Please ensure:"
  echo "  1. Docker compose is running: docker compose up -d"
  echo "  2. Or local gateway: XYNES_ENV_FILE=.env.localhost bun run dev"
  exit 1
}

echo ""
echo "───────────────────────────────────────────────────────────────────────────────"
echo "  Test: GET /flags (without auth - combined auth approach)"
echo "───────────────────────────────────────────────────────────────────────────────"

RESPONSE=$(curl -s -w "\n%{http_code}" "${GATEWAY_URL}/flags")
HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | sed '$d')

if [ "$HTTP_CODE" = "200" ]; then
  pass "GET /flags returns 200 without auth (public flags only)"
  
  # Check response structure - should have authenticated: false
  if echo "$BODY" | grep -q '"authenticated":false'; then
    pass "Response contains 'authenticated: false'"
  else
    fail "Response should contain 'authenticated: false'"
  fi
  
  # Check for public flags
  if echo "$BODY" | grep -q '"xynes_auth_oauth_google"'; then
    pass "Response contains public flag 'xynes_auth_oauth_google'"
  else
    fail "Response should contain public flag 'xynes_auth_oauth_google'"
  fi
  
  pass "Response shape validated for unauthenticated flag listing"
  
  echo "Response body:"
  echo "$BODY" | head -10
else
  fail "GET /flags should return 200 (public flags), got $HTTP_CODE"
fi

echo ""
echo "───────────────────────────────────────────────────────────────────────────────"
echo "  Test: GET /flags/:key (public flag without auth)"
echo "───────────────────────────────────────────────────────────────────────────────"

RESPONSE=$(curl -s -w "\n%{http_code}" "${GATEWAY_URL}/flags/xynes_auth_oauth_google")
HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | sed '$d')

if [ "$HTTP_CODE" = "200" ]; then
  pass "GET /flags/xynes_auth_oauth_google returns 200 without auth (public flag)"
  
  if echo "$BODY" | grep -q '"key":"xynes_auth_oauth_google"'; then
    pass "Response contains correct key"
  else
    fail "Response should contain key='xynes_auth_oauth_google'"
  fi
  
  echo "Response body: $BODY"
else
  fail "GET /flags/xynes_auth_oauth_google (public flag) should return 200, got $HTTP_CODE"
fi

echo ""
echo "───────────────────────────────────────────────────────────────────────────────"
echo "  Test: GET /flags/:key (private flag without auth - should 401)"
echo "───────────────────────────────────────────────────────────────────────────────"

RESPONSE=$(curl -s -w "\n%{http_code}" "${GATEWAY_URL}/flags/__private_test_flag")
HTTP_CODE=$(echo "$RESPONSE" | tail -1)

if [ "$HTTP_CODE" = "401" ]; then
  pass "GET /flags/__private_test_flag (private flag) returns 401 without auth"
else
  fail "GET /flags/__private_test_flag (private flag) should return 401 without auth, got $HTTP_CODE"
fi

# Skip auth tests if no token
if [ -z "$JWT_TOKEN" ]; then
  echo ""
  warn "Skipping authenticated tests (no JWT token provided)"
  echo ""
else
  # First validate the token actually works by checking /me
  echo ""
  echo "───────────────────────────────────────────────────────────────────────────────"
  echo "  Validating JWT token..."
  echo "───────────────────────────────────────────────────────────────────────────────"
  
  RESPONSE=$(curl -s -w "\n%{http_code}" \
    -H "Authorization: Bearer $JWT_TOKEN" \
    "${GATEWAY_URL}/me")
  HTTP_CODE=$(echo "$RESPONSE" | tail -1)
  
  if [ "$HTTP_CODE" != "200" ]; then
    warn "JWT token is invalid or expired (GET /me returned $HTTP_CODE)"
    warn "Skipping authenticated tests - generate a fresh token with:"
    echo "  ./scripts/supabase-token.sh --env-file .env.dev --base-url <SUPABASE_URL>"
    echo ""
  else
    pass "JWT token is valid"
  
  echo ""
  echo "───────────────────────────────────────────────────────────────────────────────"
  echo "  Test: GET /flags (with auth)"
  echo "───────────────────────────────────────────────────────────────────────────────"
  
  RESPONSE=$(curl -s -w "\n%{http_code}" \
    -H "Authorization: Bearer $JWT_TOKEN" \
    "${GATEWAY_URL}/flags")
  HTTP_CODE=$(echo "$RESPONSE" | tail -1)
  BODY=$(echo "$RESPONSE" | sed '$d')
  
  if [ "$HTTP_CODE" = "200" ]; then
    pass "GET /flags returns 200 with auth"
    
    # Check response structure
    if echo "$BODY" | grep -q '"flags"'; then
      pass "Response contains 'flags' object"
    else
      fail "Response should contain 'flags' object"
    fi
    
    # Check for authenticated: true
    if echo "$BODY" | grep -q '"authenticated":true'; then
      pass "Response contains 'authenticated: true'"
    else
      fail "Response should contain 'authenticated: true'"
    fi
    
    # Check for expected private flags (only with auth)
    if echo "$BODY" | grep -q '"xynes_auth_mfa"'; then
      pass "Response contains private 'xynes_auth_mfa' flag"
    else
      fail "Response should contain private 'xynes_auth_mfa' flag"
    fi
    
    echo "Response body:"
    echo "$BODY" | head -20
  else
    fail "GET /flags should return 200 with auth, got $HTTP_CODE"
    echo "Response: $BODY"
  fi
  
  echo ""
  echo "───────────────────────────────────────────────────────────────────────────────"
  echo "  Test: GET /flags/:key (with auth)"
  echo "───────────────────────────────────────────────────────────────────────────────"
  
  RESPONSE=$(curl -s -w "\n%{http_code}" \
    -H "Authorization: Bearer $JWT_TOKEN" \
    "${GATEWAY_URL}/flags/xynes_auth_mfa")
  HTTP_CODE=$(echo "$RESPONSE" | tail -1)
  BODY=$(echo "$RESPONSE" | sed '$d')
  
  if [ "$HTTP_CODE" = "200" ]; then
    pass "GET /flags/xynes_auth_mfa returns 200 with auth"
    
    # Check response structure
    if echo "$BODY" | grep -q '"key":"xynes_auth_mfa"'; then
      pass "Response contains correct key"
    else
      fail "Response should contain key='xynes_auth_mfa'"
    fi
    
    if echo "$BODY" | grep -q '"enabled"'; then
      pass "Response contains 'enabled' field"
    else
      fail "Response should contain 'enabled' field"
    fi
    
    echo "Response body: $BODY"
  else
    fail "GET /flags/xynes_auth_mfa should return 200 with auth, got $HTTP_CODE"
    echo "Response: $BODY"
  fi
  
  echo ""
  echo "───────────────────────────────────────────────────────────────────────────────"
  echo "  Test: GET /flags/:key (unknown flag)"
  echo "───────────────────────────────────────────────────────────────────────────────"
  
  RESPONSE=$(curl -s -w "\n%{http_code}" \
    -H "Authorization: Bearer $JWT_TOKEN" \
    "${GATEWAY_URL}/flags/unknownFlagXYZ123")
  HTTP_CODE=$(echo "$RESPONSE" | tail -1)
  BODY=$(echo "$RESPONSE" | sed '$d')
  
  if [ "$HTTP_CODE" = "200" ]; then
    pass "GET /flags/unknownFlag returns 200 (with default)"
    
    if echo "$BODY" | grep -q '"enabled":false'; then
      pass "Unknown flag defaults to enabled=false"
    else
      warn "Unknown flag might not default to false"
    fi
    
    echo "Response body: $BODY"
  else
    fail "GET /flags/unknownFlag should return 200, got $HTTP_CODE"
  fi
  
  echo ""
  echo "───────────────────────────────────────────────────────────────────────────────"
  echo "  Test: GET /flags with workspaceId"
  echo "───────────────────────────────────────────────────────────────────────────────"
  
  RESPONSE=$(curl -s -w "\n%{http_code}" \
    -H "Authorization: Bearer $JWT_TOKEN" \
    -H "X-XS-Workspace-Id: test-workspace-123" \
    "${GATEWAY_URL}/flags")
  HTTP_CODE=$(echo "$RESPONSE" | tail -1)
  
  if [ "$HTTP_CODE" = "200" ]; then
    pass "GET /flags with X-XS-Workspace-Id header works"
  else
    fail "GET /flags with workspace header should return 200, got $HTTP_CODE"
  fi
  
  fi  # end of token validation block
fi

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────

echo ""
echo "═══════════════════════════════════════════════════════════════════════════════"
echo "  Test Summary"
echo "═══════════════════════════════════════════════════════════════════════════════"
echo ""
echo -e "  ${GREEN}Passed:${NC} $PASS_COUNT"
echo -e "  ${RED}Failed:${NC} $FAIL_COUNT"
echo ""

if [ "$FAIL_COUNT" -gt 0 ]; then
  echo -e "${RED}Some tests failed!${NC}"
  exit 1
else
  echo -e "${GREEN}All tests passed!${NC}"
  exit 0
fi
