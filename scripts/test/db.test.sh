#!/usr/bin/env bash
set -euo pipefail

# Unit tests for db.sh library functions

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

# shellcheck source=./scripts/lib/db.sh
source ./scripts/lib/db.sh

echo "Testing db.sh..."

# ─────────────────────────────────────────────────────────────────────────────
# Test: extract_db_host
# ─────────────────────────────────────────────────────────────────────────────

echo "  Testing extract_db_host..."

HOST1="$(extract_db_host "postgresql://user:pass@myhost.example.com:5432/db")"
assert_eq "myhost.example.com" "$HOST1" "extract complex hostname"

HOST2="$(extract_db_host "postgres://admin:secret123@10.0.0.1:5432/mydb")"
assert_eq "10.0.0.1" "$HOST2" "extract IP address"

echo "    ✓ extract_db_host"

# ─────────────────────────────────────────────────────────────────────────────
# Test: extract_db_port
# ─────────────────────────────────────────────────────────────────────────────

echo "  Testing extract_db_port..."

PORT1="$(extract_db_port "postgresql://user:pass@host:5432/db")"
assert_eq "5432" "$PORT1" "extract standard port"

PORT2="$(extract_db_port "postgresql://user:pass@host:15432/db")"
assert_eq "15432" "$PORT2" "extract custom port"

echo "    ✓ extract_db_port"

# ─────────────────────────────────────────────────────────────────────────────
# Test: check_ssh_tunnel_binding_hint
# ─────────────────────────────────────────────────────────────────────────────

echo "  Testing check_ssh_tunnel_binding_hint..."

# This function prints hints, so we just check it runs without error
set +e
MODE="env" DATABASE_URL="postgresql://user:pass@localhost:5432/db" check_ssh_tunnel_binding_hint >/dev/null 2>&1
CODE=$?
set -e

[ "$CODE" -eq 0 ] || fail "check_ssh_tunnel_binding_hint should not error"

echo "    ✓ check_ssh_tunnel_binding_hint"

# ─────────────────────────────────────────────────────────────────────────────
# Test: wait_for_db timeout behavior
# ─────────────────────────────────────────────────────────────────────────────

echo "  Testing wait_for_db timeout (quick check)..."

# Try to connect to unlikely port with 1 second timeout
set +e
wait_for_db "127.0.0.1" "59999" "1" >/dev/null 2>&1
CODE2=$?
set -e

[ "$CODE2" -ne 0 ] || fail "wait_for_db should timeout on unreachable port"

echo "    ✓ wait_for_db timeout"

echo ""
echo "PASS: db.test.sh"
