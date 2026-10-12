#!/usr/bin/env bash
set -euo pipefail

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

echo "Testing reset-dev-db.sh..."

OUT1="$(./scripts/reset-dev-db.sh --help)"
assert_contains "$OUT1" "Usage: ./scripts/reset-dev-db.sh"
assert_contains "$OUT1" "--plan-only"

OUT2="$(./scripts/reset-dev-db.sh --plan-only)"
assert_contains "$OUT2" "RESET-DEV-DB: PLAN"
assert_contains "$OUT2" "Schemas to wipe"
assert_contains "$OUT2" "platform"
assert_contains "$OUT2" "cms"

# Non-interactive guard should require --yes
set +e
OUT3="$(./scripts/reset-dev-db.sh </dev/null 2>&1)"
CODE3=$?
set -e

[ "$CODE3" -ne 0 ] || fail "expected non-zero exit without --yes in non-interactive mode"
assert_contains "$OUT3" "non-interactive"

echo ""
echo "PASS: reset-dev-db.test.sh"
