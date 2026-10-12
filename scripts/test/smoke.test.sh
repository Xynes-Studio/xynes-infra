#!/usr/bin/env bash
set -euo pipefail

# Unit tests for smoke.sh master runner
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

echo "═══════════════════════════════════════════════════════════════════════════"
echo "  smoke.test.sh - Unit Tests for Master Runner"
echo "═══════════════════════════════════════════════════════════════════════════"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# 1. Help flag tests
# ─────────────────────────────────────────────────────────────────────────────

echo "1. Help flag tests"

# --help should succeed
OUT="$(./scripts/smoke.sh --help 2>&1)"
CODE=$?
assert_exit_zero "$CODE" "--help returns exit 0"
assert_contains "$OUT" "health" "--help mentions health suite"
assert_contains "$OUT" "security" "--help mentions security suite"
assert_contains "$OUT" "gateway-audit" "--help mentions gateway-audit suite"
assert_contains "$OUT" "--all" "--help mentions --all"
assert_contains "$OUT" "--quick" "--help mentions --quick"
assert_contains "$OUT" "--full" "--help mentions --full"
assert_contains "$OUT" "--env-file" "--help mentions --env-file"
assert_contains "$OUT" "--token-file" "--help mentions --token-file"

# -h should also work
./scripts/smoke.sh -h >/dev/null 2>&1
CODE2=$?
assert_exit_zero "$CODE2" "-h returns exit 0"

# ─────────────────────────────────────────────────────────────────────────────
# 2. Invalid argument handling
# ─────────────────────────────────────────────────────────────────────────────

echo ""
echo "2. Invalid argument handling"

# Note: smoke.sh doesn't reject unknown args - it passes them through to sub-scripts
# Instead, test that invalid suite names are handled gracefully
set +e
OUT3="$(./scripts/smoke.sh invalid-suite-name 2>&1)"
# shellcheck disable=SC2034
_CODE3=$?
set -e

# Script shows help when no valid suites found
if echo "$OUT3" | grep -qi "Usage\|help\|suite"; then
  pass "Invalid suite shows usage"
else
  fail "Invalid suite handling not found"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 3. Script structure validation
# ─────────────────────────────────────────────────────────────────────────────

echo ""
echo "3. Script structure validation"

SCRIPT_CONTENT="$(cat ./scripts/smoke.sh)"
assert_contains "$SCRIPT_CONTENT" "set -euo pipefail" "Uses strict mode"
assert_contains "$SCRIPT_CONTENT" 'smoke-${suite}.sh' "Uses dynamic suite loading pattern"
assert_contains "$SCRIPT_CONTENT" "run_suite" "Defines run_suite function"

# ─────────────────────────────────────────────────────────────────────────────
# 4. Suite validation
# ─────────────────────────────────────────────────────────────────────────────

echo ""
echo "4. Suite validation"

# Check that valid suites are recognized
for suite in health setup all security gateway-audit auth content db; do
  if echo "$SCRIPT_CONTENT" | grep -q "$suite"; then
    pass "Suite '$suite' recognized"
  else
    fail "Suite '$suite' not found in script"
  fi
done

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────

echo ""
echo "═══════════════════════════════════════════════════════════════════════════"
echo "  Results: $PASS_COUNT passed | $FAIL_COUNT failed"
echo "═══════════════════════════════════════════════════════════════════════════"

if [ "$FAIL_COUNT" -gt 0 ]; then
  echo "FAIL: smoke.test.sh"
  exit 1
fi

echo "PASS: smoke.test.sh"
exit 0
