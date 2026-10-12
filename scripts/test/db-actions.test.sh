#!/usr/bin/env bash
set -euo pipefail

# Unit tests for db-actions.sh library functions
# Tests pure functions without requiring database connection

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

echo "Testing db-actions.sh..."

# ─────────────────────────────────────────────────────────────────────────────
# Test: extract_db_host
# ─────────────────────────────────────────────────────────────────────────────

echo "  Testing extract_db_host..."

HOST1="$(extract_db_host "postgresql://user:pass@localhost:5432/db")"
assert_eq "localhost" "$HOST1" "extract localhost"

HOST2="$(extract_db_host "postgresql://user:pass@127.0.0.1:5432/db")"
assert_eq "127.0.0.1" "$HOST2" "extract 127.0.0.1"

HOST3="$(extract_db_host "postgresql://user:pass@host.docker.internal:5432/db")"
assert_eq "host.docker.internal" "$HOST3" "extract host.docker.internal"

HOST4="$(extract_db_host "postgresql://user:pass@db.local:5432/postgres")"
assert_eq "db.local" "$HOST4" "extract db.local"

echo "    ✓ extract_db_host"

# ─────────────────────────────────────────────────────────────────────────────
# Test: extract_db_port
# ─────────────────────────────────────────────────────────────────────────────

echo "  Testing extract_db_port..."

PORT1="$(extract_db_port "postgresql://user:pass@localhost:5432/db")"
assert_eq "5432" "$PORT1" "extract port 5432"

PORT2="$(extract_db_port "postgresql://user:pass@localhost:5433/db")"
assert_eq "5433" "$PORT2" "extract port 5433"

PORT3="$(extract_db_port "postgresql://user:pass@localhost/db")"
assert_eq "5432" "$PORT3" "default port when missing"

echo "    ✓ extract_db_port"

# ─────────────────────────────────────────────────────────────────────────────
# Test: normalize_database_url_for_host
# ─────────────────────────────────────────────────────────────────────────────

echo "  Testing normalize_database_url_for_host..."

NORM1="$(normalize_database_url_for_host "postgresql://user:pass@host.docker.internal:5432/db")"
assert_eq "postgresql://user:pass@127.0.0.1:5432/db" "$NORM1" "normalize host.docker.internal"

NORM2="$(normalize_database_url_for_host "postgresql://user:pass@db.local:5432/db")"
assert_eq "postgresql://user:pass@127.0.0.1:5432/db" "$NORM2" "normalize db.local"

NORM3="$(normalize_database_url_for_host "postgresql://user:pass@localhost:5432/db")"
assert_eq "postgresql://user:pass@localhost:5432/db" "$NORM3" "keep localhost unchanged"

NORM4="$(normalize_database_url_for_host "")"
assert_eq "" "$NORM4" "empty string returns empty"

echo "    ✓ normalize_database_url_for_host"

# ─────────────────────────────────────────────────────────────────────────────
# Test: db_ssh_tunnel_hint
# ─────────────────────────────────────────────────────────────────────────────

echo "  Testing db_ssh_tunnel_hint..."

HINT="$(db_ssh_tunnel_hint)"
assert_contains "$HINT" "SSH Tunnel Setup"
assert_contains "$HINT" "5432"
assert_contains "$HINT" "xynes@"

echo "    ✓ db_ssh_tunnel_hint"

# ─────────────────────────────────────────────────────────────────────────────
# Test: db_test_connection (error handling)
# ─────────────────────────────────────────────────────────────────────────────

echo "  Testing db_test_connection error handling..."

set +e
OUT="$(DATABASE_URL="" db_test_connection 2>&1)"
CODE=$?
set -e

[ "$CODE" -ne 0 ] || fail "expected non-zero exit for empty DATABASE_URL"
assert_contains "$OUT" "DATABASE_URL not set"

echo "    ✓ db_test_connection error handling"

# ─────────────────────────────────────────────────────────────────────────────
# Test: db_check_schema (error handling)
# ─────────────────────────────────────────────────────────────────────────────

echo "  Testing db_check_schema error handling..."

set +e
OUT2="$(DATABASE_URL="" db_check_schema "public" 2>&1)"
CODE2=$?
set -e

[ "$CODE2" -ne 0 ] || fail "expected non-zero exit for empty DATABASE_URL"
assert_contains "$OUT2" "DATABASE_URL not set"

echo "    ✓ db_check_schema error handling"

# ─────────────────────────────────────────────────────────────────────────────
# Test: db_row_count (error handling)
# ─────────────────────────────────────────────────────────────────────────────

echo "  Testing db_row_count error handling..."

set +e
ROWS="$(DATABASE_URL="" db_row_count "public" "test" 2>&1)"
set -e

assert_eq "-1" "$ROWS" "should return -1 for missing DATABASE_URL"

echo "    ✓ db_row_count error handling"

# ─────────────────────────────────────────────────────────────────────────────
# Test: db_query_json (error handling)
# ─────────────────────────────────────────────────────────────────────────────

echo "  Testing db_query_json error handling..."

set +e
JSON="$(DATABASE_URL="" db_query_json "SELECT 1" 2>&1)"
set -e

assert_eq "[]" "$JSON" "should return empty array for missing DATABASE_URL"

echo "    ✓ db_query_json error handling"

echo ""
echo "PASS: db-actions.test.sh"
