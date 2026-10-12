#!/usr/bin/env bash
set -euo pipefail

# Unit tests for seed.sh library functions
# Tests function signatures and error handling without requiring database

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

assert_eq() {
  local expected="$1"
  local actual="$2"
  local msg="${3:-assertion failed}"
  if [ "$expected" != "$actual" ]; then
    fail "$msg: expected '$expected', got '$actual'"
  fi
}

assert_contains() {
  local haystack="$1"
  local needle="$2"
  echo "$haystack" | grep -q -- "$needle" || fail "expected output to contain: $needle"
}

# shellcheck source=./scripts/lib/env.sh
source ./scripts/lib/env.sh
# shellcheck source=./scripts/lib/db.sh
source ./scripts/lib/db.sh
# shellcheck source=./scripts/lib/db-actions.sh
source ./scripts/lib/db-actions.sh
# shellcheck source=./scripts/lib/seed.sh
source ./scripts/lib/seed.sh

echo "Testing seed.sh..."

# ─────────────────────────────────────────────────────────────────────────────
# Test: Constants are defined
# ─────────────────────────────────────────────────────────────────────────────

echo "  Testing constants..."

[ -n "$SMOKE_TEST_WORKSPACE_NAME" ] || fail "SMOKE_TEST_WORKSPACE_NAME not defined"
[ -n "$SMOKE_TEST_WORKSPACE_SLUG" ] || fail "SMOKE_TEST_WORKSPACE_SLUG not defined"
[ -n "$SMOKE_TEST_USER_EMAIL" ] || fail "SMOKE_TEST_USER_EMAIL not defined"

echo "    ✓ constants defined"

# ─────────────────────────────────────────────────────────────────────────────
# Test: seed_test_workspace (error handling)
# ─────────────────────────────────────────────────────────────────────────────

echo "  Testing seed_test_workspace error handling..."

set +e
OUT="$(DATABASE_URL="" seed_test_workspace 2>&1)"
CODE=$?
set -e

[ "$CODE" -ne 0 ] || fail "expected non-zero exit for empty DATABASE_URL"
assert_contains "$OUT" "DATABASE_URL not set"

echo "    ✓ seed_test_workspace error handling"

# ─────────────────────────────────────────────────────────────────────────────
# Test: seed_smoke_test_data (error handling)
# ─────────────────────────────────────────────────────────────────────────────

echo "  Testing seed_smoke_test_data error handling..."

set +e
OUT2="$(DATABASE_URL="" seed_smoke_test_data 2>&1)"
CODE2=$?
set -e

[ "$CODE2" -ne 0 ] || fail "expected non-zero exit for empty DATABASE_URL"
assert_contains "$OUT2" "DATABASE_URL not set"

echo "    ✓ seed_smoke_test_data error handling"

# ─────────────────────────────────────────────────────────────────────────────
# Test: cleanup_smoke_test_data (error handling)
# ─────────────────────────────────────────────────────────────────────────────

echo "  Testing cleanup_smoke_test_data error handling..."

set +e
OUT3="$(DATABASE_URL="" cleanup_smoke_test_data 2>&1)"
CODE3=$?
set -e

[ "$CODE3" -ne 0 ] || fail "expected non-zero exit for empty DATABASE_URL"
assert_contains "$OUT3" "DATABASE_URL not set"

echo "    ✓ cleanup_smoke_test_data error handling"

# ─────────────────────────────────────────────────────────────────────────────
# Test: get_smoke_test_ids (error handling)
# ─────────────────────────────────────────────────────────────────────────────

echo "  Testing get_smoke_test_ids error handling..."

set +e
OUT4="$(DATABASE_URL="" get_smoke_test_ids 2>&1)"
CODE4=$?
set -e

[ "$CODE4" -ne 0 ] || fail "expected non-zero exit for empty DATABASE_URL"
assert_contains "$OUT4" "DATABASE_URL not set"

echo "    ✓ get_smoke_test_ids error handling"

# ─────────────────────────────────────────────────────────────────────────────
# Test: verify_smoke_test_prerequisites (error handling)
# ─────────────────────────────────────────────────────────────────────────────

echo "  Testing verify_smoke_test_prerequisites error handling..."

set +e
OUT5="$(DATABASE_URL="" verify_smoke_test_prerequisites 2>&1)"
CODE5=$?
set -e

[ "$CODE5" -ne 0 ] || fail "expected non-zero exit for empty DATABASE_URL"
assert_contains "$OUT5" "DATABASE_URL not set"

echo "    ✓ verify_smoke_test_prerequisites error handling"

echo ""
echo "PASS: seed.test.sh"
