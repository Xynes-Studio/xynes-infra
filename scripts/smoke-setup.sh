#!/usr/bin/env bash
set -euo pipefail

MODE="${MODE:-localhost}" # localhost | env
ENV_FILE="${XYNES_INFRA_ENV_FILE:-}"
CURL_TIMEOUT_SECS="${CURL_TIMEOUT_SECS:-10}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=./scripts/lib/colors.sh
source "${SCRIPT_DIR}/lib/colors.sh"
# shellcheck source=./scripts/lib/env.sh
source "${SCRIPT_DIR}/lib/env.sh"
# shellcheck source=./scripts/lib/http.sh
source "${SCRIPT_DIR}/lib/http.sh"

usage() {
  cat <<'EOF'
Usage: ./scripts/smoke-setup.sh [options]

Creates/derives IDs needed for the smoke test flow:
- Workspace ID (from DB or generated)
- CMS backing content type "blog_post" (required by cms.entry.create)
- Doc Service document (docs.document.create)
- Doc Service verification: update + listByWorkspace (manual verification aid)
- CMS directory-first entry linked to document

Options:
  --mode <localhost|env>   localhost: http://localhost:<PORT> (default)
                           env: use *_SERVICE_URL variables as-is
  --env-file <path>        env file to load (default: .env.localhost for localhost, .env.dev for env)
  --timeout <seconds>      curl timeout seconds (default: 10)
  -h, --help               show this help

Environment overrides:
  MODE, XYNES_INFRA_ENV_FILE, CURL_TIMEOUT_SECS
  INTERNAL_SERVICE_TOKEN
  XS_USER_ID
  DATABASE_URL
  DOC_SERVICE_URL, CMS_CORE_URL
  DOC_SERVICE_PORT, CMS_CORE_PORT
EOF
}


trim_ws() {
  local s="${1:-}"
  echo "$s" | tr -d '[:space:]'
}

cd "$ROOT_DIR"

while [ "${1:-}" != "" ]; do
  case "$1" in
    --mode)
      MODE="${2:-}"
      shift 2
      ;;
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    --timeout)
      CURL_TIMEOUT_SECS="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo -e "${RED}Error:${NC} unknown arg: $1"
      usage
      exit 2
      ;;
  esac
done

echo -e "${GREEN}Starting Smoke Test Setup...${NC}"

require_bin curl
require_bin jq

if [ -z "$ENV_FILE" ]; then
  if [ "$MODE" = "localhost" ]; then
    ENV_FILE=".env.localhost"
  else
    ENV_FILE=".env.dev"
  fi
fi

load_env_file_no_override "$ENV_FILE"
load_env_file_no_override ".env"
load_env_file_no_override ".env.localhost"
load_env_file_no_override ".env.local"

DOCKER_DB_LOOPBACK_ONLY=0
if command -v lsof >/dev/null 2>&1; then
  if echo "${DATABASE_URL:-}" | grep -qE '@(db\.local|host\.docker\.internal):'; then
    listeners="$(lsof -nP -iTCP:5432 -sTCP:LISTEN 2>/dev/null | tail -n +2 || true)"
    if [ -n "${listeners:-}" ]; then
      if ! echo "$listeners" | awk '{print $9}' | grep -vqE '(^127\.0\.0\.1:5432$|^\[::1\]:5432$)'; then
        DOCKER_DB_LOOPBACK_ONLY=1
      fi
    fi
  fi
fi

if [ -z "${INTERNAL_SERVICE_TOKEN:-}" ]; then
  echo -e "${RED}Error:${NC} INTERNAL_SERVICE_TOKEN is required for /internal/* calls."
  echo "Tip: set it in $ENV_FILE (or .env / .env.local) and ensure services use the same value."
  exit 1
fi
if [ "${INTERNAL_SERVICE_TOKEN:-}" = "change-me-to-a-long-random-secret" ]; then
  echo -e "${YELLOW}Warning:${NC} INTERNAL_SERVICE_TOKEN is still set to the default placeholder."
  echo "If services are not using the same value, internal calls will return UNAUTHORIZED."
fi

is_uuid() {
  local v="${1:-}"
  [[ "$v" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]
}

if [ -n "${XS_USER_ID:-}" ] && ! is_uuid "$XS_USER_ID"; then
  echo -e "${RED}Error:${NC} XS_USER_ID must be a UUID (got '$XS_USER_ID')."
  echo "Tip: unset XS_USER_ID to let the script generate one, or set a valid UUID."
  exit 1
fi

DOC_SERVICE_PORT="${DOC_SERVICE_PORT:-4201}"
CMS_CORE_PORT="${CMS_CORE_PORT:-4202}"

if [ "$MODE" = "localhost" ]; then
  DOC_SERVICE_URL="http://localhost:${DOC_SERVICE_PORT}"
  CMS_URL="http://localhost:${CMS_CORE_PORT}"
else
  DOC_SERVICE_URL="${DOC_SERVICE_URL:-http://doc-service:${DOC_SERVICE_PORT}}"
  CMS_URL="${CMS_CORE_URL:-http://cms-core:${CMS_CORE_PORT}}"
fi

# Fast readiness checks (helps debug DB/tunnel failures early).
check_ready() {
  local label="$1"
  local base_url="$2"
  local url="${base_url%/}/ready"

  if [ "${DOCKER_DB_LOOPBACK_ONLY:-0}" -eq 1 ]; then
    echo -e "${YELLOW}SKIP${NC} $label /ready - DB tunnel is loopback-only; Docker cannot reach it"
    echo "  url: $url"
    echo "  hint: DATABASE_URL uses db.local/host.docker.internal but local 5432 listens only on 127.0.0.1"
    return 0
  fi

  local code body status
  local tmp
  tmp="$(mktemp)"
  code="$(curl -sS -m "$CURL_TIMEOUT_SECS" -o "$tmp" -w "%{http_code}" "$url" 2>/dev/null || echo "000")"
  body="$(cat "$tmp" 2>/dev/null || true)"
  rm -f "$tmp" 2>/dev/null || true

  if [ "$code" != "200" ]; then
    echo -e "${RED}FAIL${NC} $label /ready - expected http=200 got http=$code"
    echo "  url: $url"
    echo "  body: $body"
    return 1
  fi

  status="$(echo "$body" | jq -r '.status // empty' 2>/dev/null || true)"
  if [ "$status" != "ready" ]; then
    echo -e "${RED}FAIL${NC} $label /ready - expected status=ready got status=$status"
    echo "  url: $url"
    echo "  body: $body"
    echo
    echo -e "${YELLOW}Hint:${NC} if you're using Docker + .env.dev, your SSH tunnel must be reachable from containers."
    echo "  - Your tunnel command binds to 127.0.0.1 only and may break Docker DB access."
    echo "  - Either run services on host with .env.localhost, or bind the tunnel for Docker (e.g. -g / 0.0.0.0)."
    return 1
  fi

  return 0
}

check_ready "doc-service" "$DOC_SERVICE_URL"
check_ready "cms-core" "$CMS_URL"

# Fallback DATABASE_URL if still empty (assume local default)
if [ -z "${DATABASE_URL:-}" ]; then
  echo -e "${YELLOW}DATABASE_URL not set, assuming local default:${NC} postgres://postgres:postgres@localhost:5432/xynes"
  DATABASE_URL="postgres://postgres:postgres@localhost:5432/xynes"
fi


# Helper function to run SQL query
run_sql() {
    local query="$1"
  local host_db_url
  host_db_url="$(normalize_database_url_for_host "${DATABASE_URL:-}")"
    
    # Method 1: psql (if available on host)
    if command -v psql &> /dev/null; then
      psql "$host_db_url" -t -c "$query" 2>/dev/null
        return $?
    fi

    # Method 2: Docker + Bun (using cms-core container)
    if command -v docker &> /dev/null; then
        # We use bun -e to run a script inside the container using the 'postgres' lib which is a dependency.
        # Filter out docker compose warnings by only capturing stdout.
        local result
        result=$(docker compose --env-file .env.dev -f docker-compose.dev.yml exec -T -e SQL_QUERY="$query" cms-core bun -e "
          import postgres from 'postgres';
          const sql = postgres(process.env.DATABASE_URL);
          try {
            const rows = await sql.unsafe(process.env.SQL_QUERY);
            if (rows.length > 0) {
              console.log(Object.values(rows[0])[0] || '');
            }
            await sql.end();
            process.exit(0);
          } catch (e) {
            process.exit(1);
          }
        " 2>/dev/null | grep -v 'WARN\|obsolete\|version')
        echo "$result"
        return 0
    fi

    return 1
}

if [ -z "${XS_USER_ID:-}" ]; then
    XS_USER_ID="$(trim_ws "$(run_sql "SELECT user_id FROM platform.workspace_members WHERE status = 'active' LIMIT 1;" 2>/dev/null || true)")"
fi

if [ -z "${XS_USER_ID:-}" ]; then
  if command -v uuidgen >/dev/null 2>&1; then
    XS_USER_ID="$(uuidgen | tr '[:upper:]' '[:lower:]')"
  else
    XS_USER_ID="00000000-0000-0000-0000-000000000001"
  fi
fi

if ! is_uuid "$XS_USER_ID"; then
  echo -e "${RED}Error:${NC} XS_USER_ID must be a UUID (got '$XS_USER_ID')."
  exit 1
fi

export XS_USER_ID

# 1. Get or Create Workspace
echo "Fetching Workspace..."

# Prefer a workspace where the acting user is an active member.
if [ -n "${XS_USER_ID:-}" ]; then
    WORKSPACE_ID="$(trim_ws "$(run_sql "SELECT workspace_id FROM platform.workspace_members WHERE user_id = '$XS_USER_ID' AND status = 'active' LIMIT 1;" 2>/dev/null || true)")"
fi

if [ -z "$WORKSPACE_ID" ]; then
    WORKSPACE_ID="$(trim_ws "$(run_sql "SELECT id FROM workspaces LIMIT 1;" 2>/dev/null || true)")"
fi

# If failed (e.g. table doesn't exist), try fetching from existing content IDs
if [ -z "$WORKSPACE_ID" ]; then
    WORKSPACE_ID="$(trim_ws "$(run_sql "SELECT DISTINCT workspace_id FROM cms.content_types LIMIT 1;" 2>/dev/null || true)")"
fi

# If still empty (no existing data or tables missing), generate a new UUID
if [ -z "$WORKSPACE_ID" ]; then
    echo "No existing workspace found or could not access DB. Generating new Workspace ID."
    if command -v uuidgen &> /dev/null; then
        WORKSPACE_ID=$(uuidgen | tr '[:upper:]' '[:lower:]')
    else
        # Fallback simple UUID gen for bash if uuidgen missing
        WORKSPACE_ID="00000000-0000-0000-0000-$(date +%s)"
    fi
fi

echo "Using Workspace ID: $WORKSPACE_ID"

# 2. Get or Create Backing Content Type (required by cms.entry.create)
echo "Fetching backing Content Type 'blog_post'..."
CONTENT_TYPE_ID="$(trim_ws "$(run_sql "SELECT id FROM cms.content_types WHERE template_key = 'blog_post' AND workspace_id = '$WORKSPACE_ID' LIMIT 1;" 2>/dev/null || true)")"

if [ -z "$CONTENT_TYPE_ID" ]; then
     # Try to create it via SQL
    # First ensure global template exists
    echo "Ensuring 'blog_post' template exists..."
    run_sql "INSERT INTO cms.global_content_templates (key, fields_schema, description) VALUES ('blog_post', '{\"title\":{\"type\":\"string\",\"required\":true},\"slug\":{\"type\":\"string\",\"required\":true}}', 'Blog Post Template') ON CONFLICT DO NOTHING;" >/dev/null || true

    echo "Creating 'blog_post' content type..."
    run_sql "INSERT INTO cms.content_types (workspace_id, template_key, name, slug, route_segment, config) VALUES ('$WORKSPACE_ID', 'blog_post', 'Blog Post', 'blog-post', 'blog', '{}') ON CONFLICT DO NOTHING;" >/dev/null || true
    CONTENT_TYPE_ID="$(trim_ws "$(run_sql "SELECT id FROM cms.content_types WHERE template_key = 'blog_post' AND workspace_id = '$WORKSPACE_ID' LIMIT 1;" 2>/dev/null || true)")"
fi

if [ -z "$CONTENT_TYPE_ID" ]; then
  echo -e "${RED}Failed to resolve backing content type blog_post.${NC}"
  exit 1
fi


# 3. Create Document
echo "Creating Document..."
DOC_RESPONSE=$(curl -sS -m "$CURL_TIMEOUT_SECS" -X POST "$DOC_SERVICE_URL/internal/doc-actions" \
  -H "Content-Type: application/json" \
  -H "X-Internal-Service-Token: $INTERNAL_SERVICE_TOKEN" \
  -H "X-Workspace-Id: $WORKSPACE_ID" \
  -H "X-XS-User-Id: $XS_USER_ID" \
  -d '{
    "actionKey": "docs.document.create",
    "payload": {
      "title": "Smoke Setup Doc",
      "type": "doc",
      "content": { "text": "Generated by smoke-setup.sh" },
      "status": "draft"
    }
  }')

DOC_ID=$(echo "$DOC_RESPONSE" | jq -r '.data.id // empty')

if [ -z "$DOC_ID" ]; then
    echo -e "${RED}Failed to create document.${NC}"
    echo "Response: $DOC_RESPONSE"
    exit 1
fi
echo "Created Document: $DOC_ID"

# 3a. Manual verification: Update Document (docs.document.update)
echo "Verifying Document Update (docs.document.update)..."
DOC_UPDATED_TITLE="Smoke Setup Doc (updated)"
DOC_UPDATE_RESPONSE=$(curl -sS -m "$CURL_TIMEOUT_SECS" -X POST "$DOC_SERVICE_URL/internal/doc-actions" \
  -H "Content-Type: application/json" \
  -H "X-Internal-Service-Token: $INTERNAL_SERVICE_TOKEN" \
  -H "X-Workspace-Id: $WORKSPACE_ID" \
  -H "X-XS-User-Id: $XS_USER_ID" \
  -d '{
    "actionKey": "docs.document.update",
    "payload": {
      "id": "'"$DOC_ID"'",
      "title": "'"$DOC_UPDATED_TITLE"'",
      "status": "published"
    }
  }')

DOC_UPDATED_ID=$(echo "$DOC_UPDATE_RESPONSE" | jq -r '.data.id // empty')
DOC_UPDATED_STATUS=$(echo "$DOC_UPDATE_RESPONSE" | jq -r '.data.status // empty')
DOC_UPDATED_RETURNED_TITLE=$(echo "$DOC_UPDATE_RESPONSE" | jq -r '.data.title // empty')

if [ "$DOC_UPDATED_ID" != "$DOC_ID" ] || [ "$DOC_UPDATED_STATUS" != "published" ] || [ "$DOC_UPDATED_RETURNED_TITLE" != "$DOC_UPDATED_TITLE" ]; then
  echo -e "${RED}Failed to update document.${NC}"
  echo "Response: $DOC_UPDATE_RESPONSE"
  exit 1
fi
echo -e "${GREEN}Updated Document OK${NC}: id=$DOC_UPDATED_ID status=$DOC_UPDATED_STATUS"

# 3b. Manual verification: List Documents (docs.document.listByWorkspace)
echo "Verifying Document List (docs.document.listByWorkspace)..."
DOC_LIST_RESPONSE=$(curl -sS -m "$CURL_TIMEOUT_SECS" -X POST "$DOC_SERVICE_URL/internal/doc-actions" \
  -H "Content-Type: application/json" \
  -H "X-Internal-Service-Token: $INTERNAL_SERVICE_TOKEN" \
  -H "X-Workspace-Id: $WORKSPACE_ID" \
  -H "X-XS-User-Id: $XS_USER_ID" \
  -d '{
    "actionKey": "docs.document.listByWorkspace",
    "payload": { "limit": 5, "offset": 0 }
  }')

FOUND_IN_LIST=$(echo "$DOC_LIST_RESPONSE" | jq -r --arg id "$DOC_ID" '[.data[]? | select(.id == $id)] | length')
HAS_CONTENT_KEY=$(echo "$DOC_LIST_RESPONSE" | jq -r --arg id "$DOC_ID" '([.data[]? | select(.id == $id) | has("content")] | any)')

if [ "$FOUND_IN_LIST" != "1" ] || [ "$HAS_CONTENT_KEY" != "false" ]; then
  echo -e "${RED}Failed to list documents for workspace.${NC}"
  echo "Response: $DOC_LIST_RESPONSE"
  exit 1
fi
echo -e "${GREEN}List Documents OK${NC}"

# 4. Create directory-first entry
echo "Creating Directory-First Entry..."
ENTRY_RESPONSE=$(curl -sS -m "$CURL_TIMEOUT_SECS" -X POST "$CMS_URL/internal/cms-actions" \
  -H "Content-Type: application/json" \
  -H "X-Internal-Service-Token: $INTERNAL_SERVICE_TOKEN" \
  -H "X-Workspace-Id: $WORKSPACE_ID" \
  -H "X-XS-User-Id: $XS_USER_ID" \
  -d '{
    "actionKey": "cms.entry.create",
    "payload": {
      "directoryId": null,
      "publishNow": true,
      "title": "Smoke Setup Entry",
      "description": "Generated by script",
      "tags": ["smoke", "auto-generated"],
      "ownerName": "Smoke Setup"
    }
  }')

ENTRY_ID=$(echo "$ENTRY_RESPONSE" | jq -r '.data.entry.id // .entry.id // empty')
ENTRY_STATUS=$(echo "$ENTRY_RESPONSE" | jq -r '.data.entry.status // .entry.status // empty')

if [ -z "$ENTRY_ID" ]; then
    echo -e "${RED}Failed to create directory-first entry.${NC}"
    echo "Response: $ENTRY_RESPONSE"
    exit 1
fi
if [ "$ENTRY_STATUS" != "published" ]; then
    echo -e "${RED}Failed to publish entry during setup.${NC}"
    echo "Response: $ENTRY_RESPONSE"
    exit 1
fi
echo "Created Entry: $ENTRY_ID (status=$ENTRY_STATUS)"

echo -e "\n${GREEN}Setup Complete!${NC}"
echo "---------------------------------------------------"
echo "Run the following commands to set up your environment:"
echo "---------------------------------------------------"
echo "export DOC_SERVICE_URL=\"$DOC_SERVICE_URL\""
echo "export CMS_URL=\"$CMS_URL\""
echo "export WORKSPACE_ID=\"$WORKSPACE_ID\""
echo "export DOC_ID=\"$DOC_ID\""
echo "export ENTRY_ID=\"$ENTRY_ID\""
echo "---------------------------------------------------"

echo
echo "Manual verification (Doc Service):"
echo "---------------------------------------------------"
echo "curl -sS -X POST \"$DOC_SERVICE_URL/internal/doc-actions\" \\"
echo "  -H \"Content-Type: application/json\" \\"
echo "  -H \"X-Internal-Service-Token: \\$INTERNAL_SERVICE_TOKEN\" \\"
echo "  -H \"X-Workspace-Id: $WORKSPACE_ID\" \\"
echo "  -H \"X-XS-User-Id: $XS_USER_ID\" \\"
echo "  -d '{\"actionKey\":\"docs.document.update\",\"payload\":{\"id\":\"$DOC_ID\",\"title\":\"$DOC_UPDATED_TITLE\",\"status\":\"published\"}}' | jq"
echo
echo "curl -sS -X POST \"$DOC_SERVICE_URL/internal/doc-actions\" \\"
echo "  -H \"Content-Type: application/json\" \\"
echo "  -H \"X-Internal-Service-Token: \\$INTERNAL_SERVICE_TOKEN\" \\"
echo "  -H \"X-Workspace-Id: $WORKSPACE_ID\" \\"
echo "  -H \"X-XS-User-Id: $XS_USER_ID\" \\"
echo "  -d '{\"actionKey\":\"docs.document.listByWorkspace\",\"payload\":{\"limit\":5,\"offset\":0}}' | jq"
echo "---------------------------------------------------"
