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
  echo "$haystack" | grep -q "$needle" || fail "expected output to contain: $needle"
}

# 1) --help should succeed and mention workspace requirement
OUT="$(./scripts/smoke-cms-entry.sh --help)"
assert_contains "$OUT" "WORKSPACE_ID"

# 2) Missing workspace should fail before network calls
set +e
OUT2="$(INTERNAL_SERVICE_TOKEN="smoke-token" ./scripts/smoke-cms-entry.sh --env-file .env.localhost 2>&1)"
CODE2=$?
set -e

[ "$CODE2" -ne 0 ] || fail "expected missing WORKSPACE_ID to return non-zero"
assert_contains "$OUT2" "WORKSPACE_ID is required"

# 3) Invalid workspace UUID should fail fast
set +e
OUT3="$(WORKSPACE_ID="not-a-uuid" INTERNAL_SERVICE_TOKEN="smoke-token" ./scripts/smoke-cms-entry.sh --env-file .env.localhost 2>&1)"
CODE3=$?
set -e

[ "$CODE3" -ne 0 ] || fail "expected invalid WORKSPACE_ID to return non-zero"
assert_contains "$OUT3" "WORKSPACE_ID must be a UUID"

# 4) --token-file missing should fail fast
set +e
OUT4="$(WORKSPACE_ID="00000000-0000-0000-0000-000000000001" INTERNAL_SERVICE_TOKEN="" ./scripts/smoke-cms-entry.sh --token-file .tmp/does-not-exist --env-file .env.localhost 2>&1)"
CODE4=$?
set -e

[ "$CODE4" -ne 0 ] || fail "expected missing token-file to return non-zero"
assert_contains "$OUT4" "token file not found"

# 5) --token-file empty should fail fast
TMP_EMPTY="$(mktemp)"
set +e
OUT5="$(WORKSPACE_ID="00000000-0000-0000-0000-000000000001" INTERNAL_SERVICE_TOKEN="" ./scripts/smoke-cms-entry.sh --token-file "$TMP_EMPTY" --env-file .env.localhost 2>&1)"
CODE5=$?
set -e
rm -f "$TMP_EMPTY" || true

[ "$CODE5" -ne 0 ] || fail "expected empty token-file to return non-zero"
assert_contains "$OUT5" "token file is empty"

# 6) Script should cover lifecycle status mutations
SCRIPT_CONTENT="$(cat ./scripts/smoke-cms-entry.sh)"
assert_contains "$SCRIPT_CONTENT" "cms.entry.status.set"
assert_contains "$SCRIPT_CONTENT" 'status: "scheduled"'
assert_contains "$SCRIPT_CONTENT" 'status: "archived"'

echo "PASS: smoke-cms-entry.test.sh"
