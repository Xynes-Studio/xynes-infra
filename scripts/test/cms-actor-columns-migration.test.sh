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
  echo "$haystack" | grep -Fq -- "$needle" || fail "expected migration to contain: $needle"
}

MIGRATION_CONTENT="$(cat ./supabase/migrations/20260424103000_cms_content_entries_actor_columns.sql)"
assert_contains "$MIGRATION_CONTENT" "to_regclass('cms.content_entries') IS NULL"
assert_contains "$MIGRATION_CONTENT" "Skipping CMS actor columns migration"
assert_contains "$MIGRATION_CONTENT" "EXECUTE"

echo "PASS: cms-actor-columns-migration.test.sh"
