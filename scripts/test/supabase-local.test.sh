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
  echo "$haystack" | grep -Fq -- "$needle" || fail "expected output to contain: $needle"
}

OUT="$(bash ./scripts/supabase-local.sh --help)"
assert_contains "$OUT" "--no-bootstrap"
assert_contains "$OUT" "--bootstrap-env-file"
assert_contains "$OUT" "--no-restart-gateway"

SCRIPT_CONTENT="$(cat ./scripts/supabase-local.sh)"
assert_contains "$SCRIPT_CONTENT" "supabase db reset --yes"
assert_contains "$SCRIPT_CONTENT" "bootstrap-local-db.sh"
assert_contains "$SCRIPT_CONTENT" ".env.localhost.local"

echo "PASS: supabase-local.test.sh"
