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

OUT="$(./scripts/supabase-token.sh --help)"
assert_contains "$OUT" "--base-url"
assert_contains "$OUT" "--anon-key"
assert_contains "$OUT" "--email"
assert_contains "$OUT" "--password-file"
assert_contains "$OUT" "--insecure"
assert_contains "$OUT" "--service-role-key"

echo "PASS: supabase-token.test.sh"
