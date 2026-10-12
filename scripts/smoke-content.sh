#!/usr/bin/env bash
set -euo pipefail

# Directory-first CMS smoke test (SMOKE-CONTENT-1)
#
# Verifies end-to-end flow (actual curl calls + assertions):
# - docs.document.create (doc-service)
# - cms.entry.create (cms-core)
# - cms.entry.getById (cms-core)
# - cms.entry.listByDirectory (cms-core)
# - cms.entry.share.generateInternalLink (cms-core)
# - cms.entry.delete (cms-core, soft delete)

MODE="${MODE:-localhost}" # localhost | env
ENV_FILE="${XYNES_INFRA_ENV_FILE:-.env.dev}"
CURL_TIMEOUT_SECS="${CURL_TIMEOUT_SECS:-10}"
NOW_SECS="${NOW_SECS:-}"
WORKSPACE_SLUG="${WORKSPACE_SLUG:-smoke-workspace}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=./scripts/lib/colors.sh
source "${SCRIPT_DIR}/lib/colors.sh"
# shellcheck source=./scripts/lib/env.sh
source "${SCRIPT_DIR}/lib/env.sh"
# shellcheck source=./scripts/lib/http.sh
source "${SCRIPT_DIR}/lib/http.sh"

usage() {
  cat <<'USAGE'
Usage: ./scripts/smoke-content.sh [options]

Options:
  --mode <localhost|env>   localhost: http://localhost:<PORT> (default)
                           env: use *_URL variables as-is
  --env-file <path>        env file to load (default: .env.dev)
  --timeout <seconds>      curl timeout seconds (default: 10)
  --workspace-id <uuid>    workspace to run against (or set WORKSPACE_ID)
  --workspace-slug <slug>  workspace slug for share-link assertion
  -h, --help               show this help

Environment:
  WORKSPACE_ID
  WORKSPACE_SLUG (optional, default smoke-workspace)
  INTERNAL_SERVICE_TOKEN
  XS_USER_ID (optional, default generated UUID)
  DOC_SERVICE_URL / CMS_CORE_URL (when --mode env)
  DOC_SERVICE_PORT / CMS_CORE_PORT (when --mode localhost)
USAGE
}

cd "$ROOT_DIR"

WORKSPACE_ID="${WORKSPACE_ID:-}"

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
    --workspace-id)
      WORKSPACE_ID="${2:-}"
      shift 2
      ;;
    --workspace-slug)
      WORKSPACE_SLUG="${2:-}"
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

require_bin curl
require_bin jq

load_env_file_no_override "$ENV_FILE"
load_env_file_no_override ".env"
load_env_file_no_override ".env.localhost"
load_env_file_no_override ".env.local"

DOC_SERVICE_PORT="${DOC_SERVICE_PORT:-4201}"
CMS_CORE_PORT="${CMS_CORE_PORT:-4202}"

if [ "$MODE" = "localhost" ]; then
  DOC_SERVICE_URL="http://localhost:${DOC_SERVICE_PORT}"
  CMS_URL="http://localhost:${CMS_CORE_PORT}"
else
  DOC_SERVICE_URL="${DOC_SERVICE_URL:-http://doc-service:${DOC_SERVICE_PORT}}"
  CMS_URL="${CMS_CORE_URL:-http://cms-core:${CMS_CORE_PORT}}"
fi

if [ -z "$WORKSPACE_ID" ]; then
  echo -e "${RED}Error:${NC} WORKSPACE_ID is required."
  echo "Tip: run ./scripts/smoke-setup.sh to create/populate one, then export WORKSPACE_ID."
  exit 1
fi

if [ -z "${INTERNAL_SERVICE_TOKEN:-}" ]; then
  echo -e "${RED}Error:${NC} INTERNAL_SERVICE_TOKEN is required for /internal/* calls."
  echo "Tip: set it in $ENV_FILE (or .env.localhost) and restart services if needed."
  exit 1
fi

is_uuid() {
  local v="${1:-}"
  [[ "$v" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]
}

gen_uuid() {
  if command -v uuidgen >/dev/null 2>&1; then
    uuidgen | tr '[:upper:]' '[:lower:]'
    return 0
  fi
  echo "00000000-0000-0000-0000-000000000001"
}

if [ -z "${XS_USER_ID:-}" ]; then
  XS_USER_ID="$(gen_uuid)"
fi

if ! is_uuid "$WORKSPACE_ID"; then
  echo -e "${RED}Error:${NC} WORKSPACE_ID must be a UUID (got '$WORKSPACE_ID')."
  exit 1
fi

if ! is_uuid "$XS_USER_ID"; then
  echo -e "${RED}Error:${NC} XS_USER_ID must be a UUID (got '$XS_USER_ID')."
  exit 1
fi

if [ -z "$NOW_SECS" ]; then
  NOW_SECS="$(date +%s)"
fi

http_request() {
  local method="$1"
  local url="$2"
  local data="${3:-}"

  HTTP_CODE="000"
  HTTP_BODY=""

  local tmp
  tmp="$(mktemp)"

  if [ "$method" = "GET" ]; then
    HTTP_CODE="$(
      curl -sS -m "$CURL_TIMEOUT_SECS" \
        -H "Accept: application/json" \
        -o "$tmp" \
        -w "%{http_code}" \
        "$url"
    )"
  else
    HTTP_CODE="$(
      curl -sS -m "$CURL_TIMEOUT_SECS" \
        -H "Accept: application/json" \
        -H "Content-Type: application/json" \
        -H "X-Internal-Service-Token: ${INTERNAL_SERVICE_TOKEN}" \
        -H "X-Workspace-Id: ${WORKSPACE_ID}" \
        -H "X-XS-User-Id: ${XS_USER_ID}" \
        -o "$tmp" \
        -w "%{http_code}" \
        -X "$method" \
        -d "$data" \
        "$url"
    )"
  fi

  HTTP_BODY="$(cat "$tmp" 2>/dev/null || true)"
  rm -f "$tmp"
}

expect_http_200() {
  local label="$1"
  local url="$2"
  if [ "$HTTP_CODE" != "200" ]; then
    echo -e "${RED}FAIL${NC} $label - expected http=200 got http=$HTTP_CODE"
    echo "  url: $url"
    echo "  body: $HTTP_BODY"
    exit 1
  fi
}

expect_http_success() {
  local label="$1"
  local url="$2"
  if [ "$HTTP_CODE" != "200" ] && [ "$HTTP_CODE" != "201" ]; then
    echo -e "${RED}FAIL${NC} $label - expected http=200 or http=201 got http=$HTTP_CODE"
    echo "  url: $url"
    echo "  body: $HTTP_BODY"
    exit 1
  fi
}

expect_ok_true() {
  local label="$1"
  if ! echo "$HTTP_BODY" | jq -e '.ok == true' >/dev/null 2>&1; then
    echo -e "${RED}FAIL${NC} $label - expected { ok: true }"
    echo "  body: $HTTP_BODY"
    exit 1
  fi
}

echo -e "${GREEN}Starting SMOKE-CONTENT-1 (directory-first content API)...${NC}"

# 1) Create Document via doc-service
DOC_PAYLOAD="$(
  jq -nc \
    --arg title "Smoke Content Doc" \
    --arg type "doc" \
    --arg status "draft" \
    --arg text "Generated by smoke-content.sh" \
    '{
      actionKey: "docs.document.create",
      payload: {
        title: $title,
        type: $type,
        content: { text: $text },
        status: $status
      }
    }'
)"

DOC_ACTIONS_URL="${DOC_SERVICE_URL%/}/internal/doc-actions"
http_request "POST" "$DOC_ACTIONS_URL" "$DOC_PAYLOAD"
expect_http_success "docs.document.create" "$DOC_ACTIONS_URL"
expect_ok_true "docs.document.create"
DOC_ID="$(echo "$HTTP_BODY" | jq -r '.data.id // empty')"
if [ -z "$DOC_ID" ]; then
  echo -e "${RED}FAIL${NC} docs.document.create - missing .data.id"
  echo "  body: $HTTP_BODY"
  exit 1
fi
echo -e "${GREEN}PASS${NC} docs.document.create: docId=$DOC_ID"

# 2) Create published entry via cms.entry.create
CMS_ACTIONS_URL="${CMS_URL%/}/internal/cms-actions"
ENTRY_TITLE="Smoke Content Entry ${NOW_SECS}"
ENTRY_DESCRIPTION="Generated by smoke-content.sh"
ENTRY_CREATE_PAYLOAD="$(
  jq -nc \
    --arg title "$ENTRY_TITLE" \
    --arg description "$ENTRY_DESCRIPTION" \
    '{
      actionKey: "cms.entry.create",
      payload: {
        title: $title,
        description: $description,
        tags: ["smoke", "directory-first"],
        ownerName: "Smoke User",
        publishNow: true
      }
    }'
)"

http_request "POST" "$CMS_ACTIONS_URL" "$ENTRY_CREATE_PAYLOAD"
expect_http_200 "cms.entry.create" "$CMS_ACTIONS_URL"
expect_ok_true "cms.entry.create"

ENTRY_ID="$(echo "$HTTP_BODY" | jq -r '.data.entry.id // empty')"
ENTRY_STATUS="$(echo "$HTTP_BODY" | jq -r '.data.entry.status // empty')"
if [ -z "$ENTRY_ID" ]; then
  echo -e "${RED}FAIL${NC} cms.entry.create - missing .data.entry.id"
  echo "  body: $HTTP_BODY"
  exit 1
fi
if [ "$ENTRY_STATUS" != "published" ]; then
  echo -e "${RED}FAIL${NC} cms.entry.create - expected published status, got '$ENTRY_STATUS'"
  echo "  body: $HTTP_BODY"
  exit 1
fi
echo -e "${GREEN}PASS${NC} cms.entry.create: entryId=$ENTRY_ID status=$ENTRY_STATUS"

# 3) Get entry by ID
ENTRY_GET_PAYLOAD="$(jq -nc --arg entryId "$ENTRY_ID" '{actionKey:"cms.entry.getById",payload:{entryId:$entryId}}')"
http_request "POST" "$CMS_ACTIONS_URL" "$ENTRY_GET_PAYLOAD"
expect_http_200 "cms.entry.getById" "$CMS_ACTIONS_URL"
expect_ok_true "cms.entry.getById"

if ! echo "$HTTP_BODY" | jq -e --arg entryId "$ENTRY_ID" --arg title "$ENTRY_TITLE" '
  (.data.entry.id == $entryId) and (.data.entry.title == $title)
' >/dev/null; then
  echo -e "${RED}FAIL${NC} cms.entry.getById - returned entry mismatch"
  echo "  body: $HTTP_BODY"
  exit 1
fi
echo -e "${GREEN}PASS${NC} cms.entry.getById"

# 4) List by directory and ensure entry appears
LIST_PAYLOAD="$(
  jq -nc \
    --arg search "$ENTRY_TITLE" \
    '{
      actionKey: "cms.entry.listByDirectory",
      payload: {
        directoryId: null,
        search: $search,
        sortBy: "date",
        sortDirection: "desc",
        status: "all",
        limit: 20,
        offset: 0
      }
    }'
)"

http_request "POST" "$CMS_ACTIONS_URL" "$LIST_PAYLOAD"
expect_http_200 "cms.entry.listByDirectory" "$CMS_ACTIONS_URL"
expect_ok_true "cms.entry.listByDirectory"
if ! echo "$HTTP_BODY" | jq -e --arg entryId "$ENTRY_ID" '
  (.data.items | map(select(.id == $entryId)) | length) >= 1
' >/dev/null; then
  echo -e "${RED}FAIL${NC} cms.entry.listByDirectory - created entry not found"
  echo "  body: $HTTP_BODY"
  exit 1
fi
echo -e "${GREEN}PASS${NC} cms.entry.listByDirectory"

# 5) Share link generation
SHARE_PAYLOAD="$(
  jq -nc \
    --arg entryId "$ENTRY_ID" \
    --arg workspaceSlug "$WORKSPACE_SLUG" \
    '{
      actionKey: "cms.entry.share.generateInternalLink",
      payload: {
        entryId: $entryId,
        workspaceSlug: $workspaceSlug
      }
    }'
)"

http_request "POST" "$CMS_ACTIONS_URL" "$SHARE_PAYLOAD"
expect_http_200 "cms.entry.share.generateInternalLink" "$CMS_ACTIONS_URL"
expect_ok_true "cms.entry.share.generateInternalLink"

EXPECTED_URL="/dashboard/${WORKSPACE_SLUG}/content/entry/${ENTRY_ID}/edit"
ACTUAL_URL="$(echo "$HTTP_BODY" | jq -r '.data.url // empty')"
if [ "$ACTUAL_URL" != "$EXPECTED_URL" ]; then
  echo -e "${RED}FAIL${NC} cms.entry.share.generateInternalLink - URL mismatch"
  echo "  expected: $EXPECTED_URL"
  echo "  actual:   $ACTUAL_URL"
  echo "  body: $HTTP_BODY"
  exit 1
fi
echo -e "${GREEN}PASS${NC} cms.entry.share.generateInternalLink"

# 6) Soft delete and verify hidden from list
DELETE_PAYLOAD="$(jq -nc --arg entryId "$ENTRY_ID" '{actionKey:"cms.entry.delete",payload:{entryId:$entryId}}')"
http_request "POST" "$CMS_ACTIONS_URL" "$DELETE_PAYLOAD"
expect_http_200 "cms.entry.delete" "$CMS_ACTIONS_URL"
expect_ok_true "cms.entry.delete"
echo -e "${GREEN}PASS${NC} cms.entry.delete"

http_request "POST" "$CMS_ACTIONS_URL" "$LIST_PAYLOAD"
expect_http_200 "cms.entry.listByDirectory (after delete)" "$CMS_ACTIONS_URL"
expect_ok_true "cms.entry.listByDirectory (after delete)"
if echo "$HTTP_BODY" | jq -e --arg entryId "$ENTRY_ID" '
  (.data.items | map(select(.id == $entryId)) | length) >= 1
' >/dev/null; then
  echo -e "${RED}FAIL${NC} deleted entry still present in list"
  echo "  body: $HTTP_BODY"
  exit 1
fi
echo -e "${GREEN}PASS${NC} deleted entry excluded from list"

echo
echo -e "${GREEN}SMOKE-CONTENT-1 OK${NC}"
echo "---------------------------------------------------"
echo "export WORKSPACE_ID=\"$WORKSPACE_ID\""
echo "export DOC_ID=\"$DOC_ID\""
echo "export ENTRY_ID=\"$ENTRY_ID\""
echo "---------------------------------------------------"
