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

# 1) --help should succeed and mention the token env var
OUT="$(./scripts/smoke-auth.sh --help)"
assert_contains "$OUT" "SUPABASE_ACCESS_TOKEN"

# 2) Missing token should fail fast without making network calls
set +e
OUT2="$(SUPABASE_ACCESS_TOKEN="" ./scripts/smoke-auth.sh --env-file .env.localhost 2>&1)"
CODE2=$?
set -e

[ "$CODE2" -ne 0 ] || fail "expected missing token to return non-zero"
assert_contains "$OUT2" "SUPABASE_ACCESS_TOKEN is required"

# 3) --token-file missing should fail fast
set +e
OUT3="$(SUPABASE_ACCESS_TOKEN="" ./scripts/smoke-auth.sh --token-file .tmp/does-not-exist --env-file .env.localhost 2>&1)"
CODE3=$?
set -e

[ "$CODE3" -ne 0 ] || fail "expected missing token-file to return non-zero"
assert_contains "$OUT3" "token file not found"

# 4) --token-file empty should fail fast
TMP_EMPTY="$(mktemp)"
set +e
OUT4="$(SUPABASE_ACCESS_TOKEN="" ./scripts/smoke-auth.sh --token-file "$TMP_EMPTY" --env-file .env.localhost 2>&1)"
CODE4=$?
set -e
rm -f "$TMP_EMPTY" || true

[ "$CODE4" -ne 0 ] || fail "expected empty token-file to return non-zero"
assert_contains "$OUT4" "token file is empty"

echo "PASS: smoke-auth.test.sh"
