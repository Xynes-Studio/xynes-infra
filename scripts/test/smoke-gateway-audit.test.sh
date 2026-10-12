#!/usr/bin/env bash
set -euo pipefail

# Unit tests for smoke-gateway-audit.sh
# Covers CLI contract and static script structure (no live network dependencies)

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

PASS_COUNT=0
FAIL_COUNT=0

fail() {
  echo "FAIL: $1" >&2
  ((FAIL_COUNT++)) || true
}

pass() {
  echo "PASS: $1"
  ((PASS_COUNT++)) || true
}

assert_exit_zero() {
  local code="$1"
  local label="$2"
  if [ "$code" -eq 0 ]; then
    pass "$label"
  else
    fail "$label (exit code: $code)"
  fi
}

assert_exit_nonzero() {
  local code="$1"
  local label="$2"
  if [ "$code" -ne 0 ]; then
    pass "$label"
  else
    fail "$label (expected non-zero exit)"
  fi
}

assert_contains() {
  local haystack="$1"
  local needle="$2"
  local label="$3"
  if echo "$haystack" | grep -qF -- "$needle"; then
    pass "$label"
  else
    fail "$label - expected output to contain: $needle"
  fi
}

echo "═══════════════════════════════════════════════════════════════════════════"
echo "  smoke-gateway-audit.test.sh - Unit Tests"
echo "═══════════════════════════════════════════════════════════════════════════"
echo ""

echo "1. Help/usage tests"
OUT_HELP="$(./scripts/smoke-gateway-audit.sh --help 2>&1)"
CODE_HELP=$?
assert_exit_zero "$CODE_HELP" "--help returns exit 0"
assert_contains "$OUT_HELP" "telemetry.gateway.logs.ingest" "--help mentions canonical action"
assert_contains "$OUT_HELP" "telemetry.gateway_request_logs" "--help mentions canonical table"
assert_contains "$OUT_HELP" "telemetry.v_gateway_request_logs" "--help mentions canonical view"
assert_contains "$OUT_HELP" "--db-timeout" "--help mentions --db-timeout option"
assert_contains "$OUT_HELP" "--dynamic-path" "--help mentions --dynamic-path option"
assert_contains "$OUT_HELP" "180 days" "--help mentions retention target"

echo ""
echo "2. Invalid argument handling"
set +e
OUT_BAD_ARG="$(./scripts/smoke-gateway-audit.sh --not-a-real-flag 2>&1)"
CODE_BAD_ARG=$?
set -e
assert_exit_nonzero "$CODE_BAD_ARG" "Unknown flag returns non-zero"
assert_contains "$OUT_BAD_ARG" "unknown arg" "Unknown flag prints explicit error"

set +e
OUT_BAD_MODE="$(./scripts/smoke-gateway-audit.sh --mode nope 2>&1)"
CODE_BAD_MODE=$?
set -e
assert_exit_nonzero "$CODE_BAD_MODE" "Invalid --mode returns non-zero"
assert_contains "$OUT_BAD_MODE" "invalid --mode" "Invalid --mode prints explicit error"

set +e
BAD_METHOD="GET';DROP"
OUT_BAD_METHOD="$(./scripts/smoke-gateway-audit.sh --dynamic-method "$BAD_METHOD" 2>&1)"
CODE_BAD_METHOD=$?
set -e
assert_exit_nonzero "$CODE_BAD_METHOD" "Invalid --dynamic-method returns non-zero"
assert_contains "$OUT_BAD_METHOD" "invalid --dynamic-method" "Invalid --dynamic-method prints explicit error"

echo ""
echo "3. Static structure checks"
SCRIPT_CONTENT="$(cat ./scripts/smoke-gateway-audit.sh)"
assert_contains "$SCRIPT_CONTENT" "set -euo pipefail" "Script uses strict mode"
assert_contains "$SCRIPT_CONTENT" "validate_runtime_contract" "Script validates runtime contract"
assert_contains "$SCRIPT_CONTENT" "wait_for_log_json" "Script polls async telemetry persistence"
assert_contains "$SCRIPT_CONTENT" "resolve_dynamic_route" "Script validates dynamic route logging"
assert_contains "$SCRIPT_CONTENT" "NOT_FOUND" "Script validates not-found error code"
assert_contains "$SCRIPT_CONTENT" "telemetry.gateway_request_logs" "Script targets canonical table"

echo ""
echo "═══════════════════════════════════════════════════════════════════════════"
echo "  Results: $PASS_COUNT passed | $FAIL_COUNT failed"
echo "═══════════════════════════════════════════════════════════════════════════"

if [ "$FAIL_COUNT" -gt 0 ]; then
  echo "FAIL: smoke-gateway-audit.test.sh"
  exit 1
fi

echo "PASS: smoke-gateway-audit.test.sh"
exit 0
