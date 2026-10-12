#!/usr/bin/env bash
set -euo pipefail

# Unit tests for smoke-security.sh
# Following TDD patterns from ADR-001

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

assert_not_contains() {
  local haystack="$1"
  local needle="$2"
  local label="$3"
  if ! echo "$haystack" | grep -qF -- "$needle"; then
    pass "$label"
  else
    fail "$label - expected output NOT to contain: $needle"
  fi
}

echo "═══════════════════════════════════════════════════════════════════════════"
echo "  smoke-security.test.sh - Unit Tests"
echo "═══════════════════════════════════════════════════════════════════════════"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# 1. Help flag tests
# ─────────────────────────────────────────────────────────────────────────────

echo "1. Help flag tests"

# --help should succeed
OUT="$(./scripts/smoke-security.sh --help 2>&1)"
CODE=$?
assert_exit_zero "$CODE" "--help returns exit 0"
assert_contains "$OUT" "Rate limit" "--help mentions rate limit"
assert_contains "$OUT" "Body limit" "--help mentions body limit"
assert_contains "$OUT" "Internal JWT" "--help mentions internal JWT"
assert_contains "$OUT" "Telemetry" "--help mentions telemetry"
assert_contains "$OUT" "--token-file" "--help mentions --token-file"
assert_contains "$OUT" "--env-file" "--help mentions --env-file"

# -h should also work
./scripts/smoke-security.sh -h >/dev/null 2>&1
CODE2=$?
assert_exit_zero "$CODE2" "-h returns exit 0"

# ─────────────────────────────────────────────────────────────────────────────
# 2. Invalid argument handling
# ─────────────────────────────────────────────────────────────────────────────

echo ""
echo "2. Invalid argument handling"

set +e
OUT3="$(./scripts/smoke-security.sh --invalid-flag 2>&1)"
CODE3=$?
set -e

assert_exit_nonzero "$CODE3" "Invalid flag returns non-zero"
assert_contains "$OUT3" "unknown" "Invalid flag shows error message"

# Test invalid mode argument
set +e
OUT_MODE="$(./scripts/smoke-security.sh --mode invalid-mode 2>&1)"
CODE_MODE=$?
set -e

assert_exit_nonzero "$CODE_MODE" "Invalid --mode returns non-zero"
assert_contains "$OUT_MODE" "invalid" "Invalid mode shows error message"

# ─────────────────────────────────────────────────────────────────────────────
# 3. Environment file handling
# ─────────────────────────────────────────────────────────────────────────────

echo ""
echo "3. Environment file handling"

# Non-existent env file should not cause script to fail (load_env_file_no_override is permissive)
# But we can test that it mentions the env file
OUT4="$(./scripts/smoke-security.sh --help | head -5)"
assert_contains "$OUT4" "SMOKE-SEC-2" "Script header is present"

# Test --body-limit option is documented
OUT_HELP="$(./scripts/smoke-security.sh --help 2>&1)"
assert_contains "$OUT_HELP" "--body-limit" "--help mentions --body-limit option"

# ─────────────────────────────────────────────────────────────────────────────
# 4. Token file handling
# ─────────────────────────────────────────────────────────────────────────────

echo ""
echo "4. Token file handling"

# Non-existent token file should fail fast
set +e
OUT5="$(./scripts/smoke-security.sh --token-file .tmp/nonexistent-token-file --env-file .env.dev 2>&1)"
CODE5=$?
set -e

assert_exit_nonzero "$CODE5" "Non-existent token file fails"
assert_contains "$OUT5" "not found" "Missing token file shows error"

# Empty token file should fail
TMP_EMPTY="$(mktemp)"
set +e
OUT6="$(./scripts/smoke-security.sh --token-file "$TMP_EMPTY" --env-file .env.dev 2>&1)"
CODE6=$?
set -e
rm -f "$TMP_EMPTY" || true

assert_exit_nonzero "$CODE6" "Empty token file fails"
assert_contains "$OUT6" "empty" "Empty token file shows error"

# ─────────────────────────────────────────────────────────────────────────────
# 5. Script structure validation
# ─────────────────────────────────────────────────────────────────────────────

echo ""
echo "5. Script structure validation"

# Script should source required libraries
SCRIPT_CONTENT="$(cat ./scripts/smoke-security.sh)"
assert_contains "$SCRIPT_CONTENT" "lib/colors.sh" "Sources colors.sh"
assert_contains "$SCRIPT_CONTENT" "lib/env.sh" "Sources env.sh"
assert_contains "$SCRIPT_CONTENT" "lib/http.sh" "Sources http.sh"

# Script should define key test functions
assert_contains "$SCRIPT_CONTENT" "test_rate_limit" "Defines test_rate_limit function"
assert_contains "$SCRIPT_CONTENT" "test_body_limit" "Defines test_body_limit function"
assert_contains "$SCRIPT_CONTENT" "test_internal_jwt" "Defines test_internal_jwt function"
assert_contains "$SCRIPT_CONTENT" "test_telemetry" "Defines test_telemetry function"

# Script should use secure patterns
assert_contains "$SCRIPT_CONTENT" "set -euo pipefail" "Uses strict mode"

# Verify no direct echo of sensitive variables (checking for 'echo $TOKEN' or 'echo "$TOKEN' patterns)
if echo "$SCRIPT_CONTENT" | grep -qE 'echo.*\$.*TOKEN|echo.*\$.*SECRET|echo.*\$.*PASSWORD'; then
  fail "Script may echo sensitive values directly"
else
  pass "Does not echo sensitive variable values"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 6. Security assertions in telemetry test
# ─────────────────────────────────────────────────────────────────────────────

echo ""
echo "6. Security assertions validation"

# Script should check for sensitive data leakage
if echo "$SCRIPT_CONTENT" | grep -qiE 'authorization'; then
  pass "Checks for Authorization header leakage"
else
  fail "Does not check for Authorization header leakage"
fi

if echo "$SCRIPT_CONTENT" | grep -qF 'queryString'; then
  pass "Checks for query string leakage"
else
  fail "Does not check for query string leakage"
fi

if echo "$SCRIPT_CONTENT" | grep -qE 'requestBody|responseBody'; then
  pass "Checks for body leakage"
else
  fail "Does not check for body leakage"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────

echo ""
echo "═══════════════════════════════════════════════════════════════════════════"
echo "  Results: $PASS_COUNT passed | $FAIL_COUNT failed"
echo "═══════════════════════════════════════════════════════════════════════════"

if [ "$FAIL_COUNT" -gt 0 ]; then
  echo "FAIL: smoke-security.test.sh"
  exit 1
fi

echo "PASS: smoke-security.test.sh"
exit 0
