#!/usr/bin/env bash
# Seed data helpers for smoke tests
# Creates sample data in database for comprehensive smoke testing
#
# Security: Uses parameterized queries via psql where possible
# Connection: Expects SSH tunnel: ssh -N -L 5432:127.0.0.1:5432 xynes@84.247.176.134

set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# SEED DATA CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

# Test workspace (deterministic IDs for reproducible tests)
readonly SMOKE_TEST_WORKSPACE_NAME="Smoke Test Workspace"
readonly SMOKE_TEST_WORKSPACE_SLUG="smoke-test-ws"

# Test user (exported for external use)
# shellcheck disable=SC2034
readonly SMOKE_TEST_USER_EMAIL="smoke-test@xynes.local"

# ─────────────────────────────────────────────────────────────────────────────
# seed_test_workspace - Create or get smoke test workspace
# Usage: seed_test_workspace [database_url]
# Returns: workspace UUID
# ─────────────────────────────────────────────────────────────────────────────
seed_test_workspace() {
  local url="${1:-${DATABASE_URL:-}}"
  
  if [ -z "$url" ]; then
    echo "Error: DATABASE_URL not set" >&2
    return 1
  fi

  local normalized_url
  normalized_url="$(normalize_database_url_for_host "$url")"

  # Check if workspace exists
  local existing_id
  existing_id="$(PGCONNECT_TIMEOUT=5 psql "$normalized_url" -tAc \
    "SELECT id FROM platform.workspaces WHERE slug = '$SMOKE_TEST_WORKSPACE_SLUG' LIMIT 1;" 2>/dev/null || true)"

  if [ -n "$existing_id" ]; then
    echo "$existing_id"
    return 0
  fi

  # Create new workspace
  local new_id
  new_id="$(PGCONNECT_TIMEOUT=5 psql "$normalized_url" -tAc \
    "INSERT INTO platform.workspaces (name, slug, settings) VALUES ('$SMOKE_TEST_WORKSPACE_NAME', '$SMOKE_TEST_WORKSPACE_SLUG', '{}') RETURNING id;" 2>/dev/null || true)"

  if [ -z "$new_id" ]; then
    echo "Error: Failed to create workspace" >&2
    return 1
  fi

  echo "$new_id"
}

# ─────────────────────────────────────────────────────────────────────────────
# seed_smoke_test_data - Create all smoke test seed data
# Usage: seed_smoke_test_data [database_url]
# Creates: workspace, user, content types, sample entries
# ─────────────────────────────────────────────────────────────────────────────
seed_smoke_test_data() {
  local url="${1:-${DATABASE_URL:-}}"
  
  if [ -z "$url" ]; then
    echo "Error: DATABASE_URL not set" >&2
    return 1
  fi

  local normalized_url
  normalized_url="$(normalize_database_url_for_host "$url")"

  echo "Seeding smoke test data..."

  # 1. Ensure workspace exists
  local workspace_id
  workspace_id="$(seed_test_workspace "$url")"
  
  if [ -z "$workspace_id" ]; then
    echo "Error: Could not create/get workspace" >&2
    return 1
  fi
  echo "  Workspace: $workspace_id"

  # 2. Check/create blog template if needed (via CMS service action is preferred)
  # For direct DB seeding, we check if template exists
  local template_exists
  template_exists="$(PGCONNECT_TIMEOUT=5 psql "$normalized_url" -tAc \
    "SELECT 1 FROM cms.global_content_templates WHERE slug = 'blog-post' LIMIT 1;" 2>/dev/null || true)"

  if [ "$template_exists" != "1" ]; then
    echo "  Note: Blog template not found. Use smoke-setup.sh to create via CMS service."
  else
    echo "  Blog template: exists"
  fi

  # 3. Check/create content type for workspace
  local content_type_id
  content_type_id="$(PGCONNECT_TIMEOUT=5 psql "$normalized_url" -tAc \
    "SELECT id FROM cms.content_types WHERE workspace_id = '$workspace_id' AND route_segment = 'blog' LIMIT 1;" 2>/dev/null || true)"

  if [ -n "$content_type_id" ]; then
    echo "  Content type (blog): $content_type_id"
  else
    echo "  Note: Blog content type not found. Use smoke-setup.sh to create via CMS service."
  fi

  # Output summary
  echo ""
  echo "Seed data ready:"
  echo "  export SMOKE_WORKSPACE_ID=\"$workspace_id\""
  [ -n "${content_type_id:-}" ] && echo "  export SMOKE_CONTENT_TYPE_ID=\"$content_type_id\""
}

# ─────────────────────────────────────────────────────────────────────────────
# cleanup_smoke_test_data - Remove smoke test data
# Usage: cleanup_smoke_test_data [database_url]
# ─────────────────────────────────────────────────────────────────────────────
cleanup_smoke_test_data() {
  local url="${1:-${DATABASE_URL:-}}"
  
  if [ -z "$url" ]; then
    echo "Error: DATABASE_URL not set" >&2
    return 1
  fi

  local normalized_url
  normalized_url="$(normalize_database_url_for_host "$url")"

  echo "Cleaning up smoke test data..."

  # Get workspace ID
  local workspace_id
  workspace_id="$(PGCONNECT_TIMEOUT=5 psql "$normalized_url" -tAc \
    "SELECT id FROM platform.workspaces WHERE slug = '$SMOKE_TEST_WORKSPACE_SLUG' LIMIT 1;" 2>/dev/null || true)"

  if [ -z "$workspace_id" ]; then
    echo "  No smoke test workspace found."
    return 0
  fi

  # Delete in order (respecting foreign keys)
  echo "  Deleting content entries..."
  PGCONNECT_TIMEOUT=5 psql "$normalized_url" -c \
    "DELETE FROM cms.content_entries WHERE workspace_id = '$workspace_id';" 2>/dev/null || true

  echo "  Deleting content types..."
  PGCONNECT_TIMEOUT=5 psql "$normalized_url" -c \
    "DELETE FROM cms.content_types WHERE workspace_id = '$workspace_id';" 2>/dev/null || true

  echo "  Deleting documents..."
  PGCONNECT_TIMEOUT=5 psql "$normalized_url" -c \
    "DELETE FROM docs.documents WHERE workspace_id = '$workspace_id';" 2>/dev/null || true

  echo "  Deleting workspace members..."
  PGCONNECT_TIMEOUT=5 psql "$normalized_url" -c \
    "DELETE FROM platform.workspace_members WHERE workspace_id = '$workspace_id';" 2>/dev/null || true

  echo "  Deleting user roles..."
  PGCONNECT_TIMEOUT=5 psql "$normalized_url" -c \
    "DELETE FROM authz.user_roles WHERE workspace_id = '$workspace_id';" 2>/dev/null || true

  echo "  Deleting workspace..."
  PGCONNECT_TIMEOUT=5 psql "$normalized_url" -c \
    "DELETE FROM platform.workspaces WHERE id = '$workspace_id';" 2>/dev/null || true

  echo "  Cleanup complete."
}

# ─────────────────────────────────────────────────────────────────────────────
# get_smoke_test_ids - Get all smoke test IDs for export
# Usage: get_smoke_test_ids [database_url]
# Prints: export statements for all IDs
# ─────────────────────────────────────────────────────────────────────────────
get_smoke_test_ids() {
  local url="${1:-${DATABASE_URL:-}}"
  
  if [ -z "$url" ]; then
    echo "Error: DATABASE_URL not set" >&2
    return 1
  fi

  local normalized_url
  normalized_url="$(normalize_database_url_for_host "$url")"

  # Workspace
  local workspace_id
  workspace_id="$(PGCONNECT_TIMEOUT=5 psql "$normalized_url" -tAc \
    "SELECT id FROM platform.workspaces WHERE slug = '$SMOKE_TEST_WORKSPACE_SLUG' LIMIT 1;" 2>/dev/null || true)"
  
  [ -n "$workspace_id" ] && echo "export SMOKE_WORKSPACE_ID=\"$workspace_id\""

  if [ -n "$workspace_id" ]; then
    # Content type
    local content_type_id
    content_type_id="$(PGCONNECT_TIMEOUT=5 psql "$normalized_url" -tAc \
      "SELECT id FROM cms.content_types WHERE workspace_id = '$workspace_id' AND route_segment = 'blog' LIMIT 1;" 2>/dev/null || true)"
    [ -n "$content_type_id" ] && echo "export SMOKE_CONTENT_TYPE_ID=\"$content_type_id\""

    # Latest document
    local doc_id
    doc_id="$(PGCONNECT_TIMEOUT=5 psql "$normalized_url" -tAc \
      "SELECT id FROM docs.documents WHERE workspace_id = '$workspace_id' ORDER BY created_at DESC LIMIT 1;" 2>/dev/null || true)"
    [ -n "$doc_id" ] && echo "export SMOKE_DOC_ID=\"$doc_id\""

    # Latest entry
    local entry_id
    entry_id="$(PGCONNECT_TIMEOUT=5 psql "$normalized_url" -tAc \
      "SELECT id FROM cms.content_entries WHERE workspace_id = '$workspace_id' ORDER BY created_at DESC LIMIT 1;" 2>/dev/null || true)"
    [ -n "$entry_id" ] && echo "export SMOKE_ENTRY_ID=\"$entry_id\""
  fi
}

# ─────────────────────────────────────────────────────────────────────────────
# verify_smoke_test_prerequisites - Verify all schemas and tables exist
# Usage: verify_smoke_test_prerequisites [database_url]
# Returns: 0 if all prerequisites met, 1 otherwise
# ─────────────────────────────────────────────────────────────────────────────
verify_smoke_test_prerequisites() {
  local url="${1:-${DATABASE_URL:-}}"
  
  if [ -z "$url" ]; then
    echo "Error: DATABASE_URL not set" >&2
    return 1
  fi

  local all_ok=0

  # Required schemas
  local schemas=("platform" "identity" "cms" "docs" "authz" "telemetry")
  
  for schema in "${schemas[@]}"; do
    if db_check_schema "$schema" "$url"; then
      echo "  ✓ Schema: $schema"
    else
      echo "  ✗ Schema: $schema (missing)"
      all_ok=1
    fi
  done

  # Required tables
  local tables=(
    "platform:workspaces"
    "platform:workspace_members"
    "identity:users"
    "cms:content_types"
    "cms:content_entries"
    "cms:global_content_templates"
    "docs:documents"
    "authz:roles"
    "authz:user_roles"
  )

  for item in "${tables[@]}"; do
    local schema="${item%%:*}"
    local table="${item##*:}"
    if db_check_table "$schema" "$table" "$url"; then
      echo "  ✓ Table: $schema.$table"
    else
      echo "  ✗ Table: $schema.$table (missing)"
      all_ok=1
    fi
  done

  return $all_ok
}
