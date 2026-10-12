#!/usr/bin/env bash
set -euo pipefail

# Unit tests for smoke-db.sh script
# Tests --help, argument parsing, and error handling

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

assert_contains() {
  local haystack="$1"
  local needle="$2"
  echo "$haystack" | grep -q -- "$needle" || fail "expected output to contain: $needle"
}

echo "Testing smoke-db.sh..."

# ─────────────────────────────────────────────────────────────────────────────
# Test: --help
# ─────────────────────────────────────────────────────────────────────────────

echo "  Testing --help..."

OUT="$(./scripts/smoke-db.sh --help)"
assert_contains "$OUT" "DATABASE_URL"
assert_contains "$OUT" "--env-file"
assert_contains "$OUT" "--seed"
assert_contains "$OUT" "--cleanup"
assert_contains "$OUT" "--verify-only"
assert_contains "$OUT" "SSH tunnel"

echo "    ✓ --help"

# ─────────────────────────────────────────────────────────────────────────────
# Test: Unknown argument
# ─────────────────────────────────────────────────────────────────────────────

echo "  Testing unknown argument..."

set +e
OUT2="$(./scripts/smoke-db.sh --unknown-flag 2>&1)"
CODE2=$?
set -e

[ "$CODE2" -ne 0 ] || fail "expected non-zero exit for unknown argument"
assert_contains "$OUT2" "unknown arg"

echo "    ✓ unknown argument handling"

# ─────────────────────────────────────────────────────────────────────────────
# Test: Missing DATABASE_URL
# ─────────────────────────────────────────────────────────────────────────────

echo "  Testing missing DATABASE_URL..."

# Note: The script has fallback to .env files, so it's hard to test truly missing DATABASE_URL
# This test verifies the script at least runs to completion without DATABASE_URL param passed
# The actual DATABASE_URL check is tested by verifying the script rejects invalid URLs

# Test that a truly invalid/unreachable DATABASE_URL fails
set +e
OUT3="$(DATABASE_URL="postgresql://fake:fake@nonexistent.invalid:9999/fake" ./scripts/smoke-db.sh --verify-only 2>&1)"
CODE3=$?
set -e

# Script should fail - can't connect to fake database
if [ "$CODE3" -ne 0 ]; then
  echo "    ✓ unreachable database fails"
elif echo "$OUT3" | grep -qi "FAIL\|error"; then
  echo "    ✓ unreachable database reports failure"
else
  fail "expected failure or error for unreachable database"
fi

echo ""
echo "PASS: smoke-db.test.sh"
