#!/usr/bin/env bash
set -euo pipefail

# Contract test for the Workspace Admin integrations platform schema migration.
# Validates the SQL file contains required DDL, constraints, indexes, grants,
# and security invariants without needing a running database.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

MIGRATION="./supabase/migrations/20260424090000_workspace_admin_integrations.sql"

PASS_COUNT=0
FAIL_COUNT=0

fail() {
  echo "FAIL: $1" >&2
  FAIL_COUNT=$((FAIL_COUNT + 1))
}

pass() {
  PASS_COUNT=$((PASS_COUNT + 1))
}

assert_file_exists() {
  local path="$1"
  if [ -f "$path" ]; then
    pass
  else
    fail "expected file to exist: $path"
  fi
}

assert_contains() {
  local path="$1"
  local needle="$2"
  if grep -Fq -- "$needle" "$path"; then
    pass
  else
    fail "expected $path to contain: $needle"
  fi
}

assert_not_contains() {
  local path="$1"
  local needle="$2"
  if grep -Fq -- "$needle" "$path"; then
    fail "expected $path not to contain: $needle"
  else
    pass
  fi
}

echo "Testing Workspace Admin integrations migration..."
echo ""

# ---------------------------------------------------------------------------
# File existence
# ---------------------------------------------------------------------------
assert_file_exists "$MIGRATION"

# ---------------------------------------------------------------------------
# Extension
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "CREATE EXTENSION IF NOT EXISTS pgcrypto;"

# ---------------------------------------------------------------------------
# platform.workspace_domains — table + constraints
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "CREATE TABLE IF NOT EXISTS platform.workspace_domains"
assert_contains "$MIGRATION" "hostname text NOT NULL"
assert_contains "$MIGRATION" "verification_value_hash text NOT NULL"
assert_contains "$MIGRATION" "CHECK (status IN ('pending', 'verified', 'failed', 'disabled'))"
assert_contains "$MIGRATION" "CHECK (verification_method IN ('dns_txt'))"
assert_contains "$MIGRATION" "CONSTRAINT workspace_domains_hostname_lower CHECK (hostname = lower(hostname))"
assert_contains "$MIGRATION" "CONSTRAINT workspace_domains_hostname_shape CHECK"
assert_contains "$MIGRATION" "CONSTRAINT workspace_domains_hostname_not_blank CHECK"
assert_contains "$MIGRATION" "CONSTRAINT workspace_domains_verification_name_not_blank CHECK"
assert_contains "$MIGRATION" "CONSTRAINT workspace_domains_verification_value_hash_not_blank CHECK"
assert_contains "$MIGRATION" "created_by uuid NOT NULL REFERENCES identity.users(id)"

# Indexes
assert_contains "$MIGRATION" "workspace_domains_active_hostname_uidx"
assert_contains "$MIGRATION" "workspace_domains_workspace_hostname_uidx"
assert_contains "$MIGRATION" "workspace_domains_workspace_status_idx"
assert_contains "$MIGRATION" "WHERE status <> 'disabled'"

# ---------------------------------------------------------------------------
# platform.workspace_domain_bindings — table + constraints
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "CREATE TABLE IF NOT EXISTS platform.workspace_domain_bindings"
assert_contains "$MIGRATION" "domain_id uuid NOT NULL REFERENCES platform.workspace_domains(id) ON DELETE CASCADE"
assert_contains "$MIGRATION" "workspace_domain_bindings_workspace_idx"
assert_contains "$MIGRATION" "workspace_domain_bindings_domain_idx"
assert_contains "$MIGRATION" "CONSTRAINT workspace_domain_bindings_app_key_not_blank CHECK"
assert_contains "$MIGRATION" "CONSTRAINT workspace_domain_bindings_type_not_blank CHECK"

# ---------------------------------------------------------------------------
# platform.workspace_api_keys — table + constraints
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "CREATE TABLE IF NOT EXISTS platform.workspace_api_keys"
assert_contains "$MIGRATION" "key_prefix text NOT NULL"
assert_contains "$MIGRATION" "key_hash text NOT NULL"
assert_contains "$MIGRATION" "CHECK (status IN ('active', 'revoked', 'expired'))"
assert_contains "$MIGRATION" "CONSTRAINT workspace_api_keys_name_not_blank CHECK"
assert_contains "$MIGRATION" "CONSTRAINT workspace_api_keys_prefix_not_blank CHECK"
assert_contains "$MIGRATION" "CONSTRAINT workspace_api_keys_hash_not_blank CHECK"
assert_contains "$MIGRATION" "CONSTRAINT workspace_api_keys_revoked_consistency CHECK"

# Indexes
assert_contains "$MIGRATION" "workspace_api_keys_prefix_uidx"
assert_contains "$MIGRATION" "workspace_api_keys_hash_uidx"
assert_contains "$MIGRATION" "workspace_api_keys_workspace_idx"

# ---------------------------------------------------------------------------
# platform.workspace_api_key_scopes — table + constraints
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "CREATE TABLE IF NOT EXISTS platform.workspace_api_key_scopes"
assert_contains "$MIGRATION" "action_key text NOT NULL"
assert_contains "$MIGRATION" "PRIMARY KEY (api_key_id, action_key)"
assert_contains "$MIGRATION" "CONSTRAINT workspace_api_key_scopes_action_key_not_blank CHECK"
assert_contains "$MIGRATION" "workspace_api_key_scopes_action_key_idx"

# ---------------------------------------------------------------------------
# Security invariant: no raw key storage
# ---------------------------------------------------------------------------
assert_not_contains "$MIGRATION" "raw_key"

# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "GRANT SELECT, INSERT, UPDATE, DELETE ON platform.workspace_domains TO postgres;"
assert_contains "$MIGRATION" "GRANT SELECT, INSERT, UPDATE, DELETE ON platform.workspace_domain_bindings TO postgres;"
assert_contains "$MIGRATION" "GRANT SELECT, INSERT, UPDATE, DELETE ON platform.workspace_api_keys TO postgres;"
assert_contains "$MIGRATION" "GRANT SELECT, INSERT, UPDATE, DELETE ON platform.workspace_api_key_scopes TO postgres;"

# ---------------------------------------------------------------------------
# Documentation comments
# ---------------------------------------------------------------------------
assert_contains "$MIGRATION" "COMMENT ON TABLE platform.workspace_domains"
assert_contains "$MIGRATION" "COMMENT ON TABLE platform.workspace_domain_bindings"
assert_contains "$MIGRATION" "COMMENT ON TABLE platform.workspace_api_keys"
assert_contains "$MIGRATION" "COMMENT ON TABLE platform.workspace_api_key_scopes"
assert_contains "$MIGRATION" "COMMENT ON COLUMN platform.workspace_api_keys.key_prefix"
assert_contains "$MIGRATION" "COMMENT ON COLUMN platform.workspace_api_keys.key_hash"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Results: $PASS_COUNT passed, $FAIL_COUNT failed"
echo ""
if [ "$FAIL_COUNT" -gt 0 ]; then
  echo "FAIL: workspace-admin-integrations-migration.test.sh"
  exit 1
fi
echo "PASS: workspace-admin-integrations-migration.test.sh"
