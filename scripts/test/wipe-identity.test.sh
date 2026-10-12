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

echo "Testing wipe-identity.sh..."

OUT1="$(./scripts/wipe-identity.sh --help)"
assert_contains "$OUT1" "Usage: ./scripts/wipe-identity.sh"
assert_contains "$OUT1" "--user-id"
assert_contains "$OUT1" "--email"

OUT2="$(./scripts/wipe-identity.sh --plan-only)"
assert_contains "$OUT2" "WIPE-IDENTITY: PLAN"
assert_contains "$OUT2" "identity.users"

# Non-interactive guard should require --yes
set +e
OUT3="$(./scripts/wipe-identity.sh --user-id 00000000-0000-0000-0000-000000000000 </dev/null 2>&1)"
CODE3=$?
set -e

[ "$CODE3" -ne 0 ] || fail "expected non-zero exit without --yes in non-interactive mode"
assert_contains "$OUT3" "non-interactive"

echo ""
echo "PASS: wipe-identity.test.sh"
