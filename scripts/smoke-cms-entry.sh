#!/usr/bin/env bash
set -euo pipefail

# SMOKE-CMS-ENTRY-1 – CMS directory-first entry action contract
#
# Verifies:
# - cms.entry.create
# - cms.entry.getById
# - cms.entry.update
# - cms.entry.collaborators.set
# - cms.entry.favorite.toggle
# - cms.entry.favorite.list
# - cms.entry.listByDirectory
# - cms.entry.share.generateInternalLink
# - cms.entry.publish
# - cms.entry.status.set
# - cms.entry.delete (soft delete)

MODE="${MODE:-localhost}" # localhost | env
ENV_FILE="${XYNES_INFRA_ENV_FILE:-.env.dev}"
CURL_TIMEOUT_SECS="${CURL_TIMEOUT_SECS:-10}"
TOKEN_FILE="${TOKEN_FILE:-}"
WORKSPACE_ID="${WORKSPACE_ID:-}"
WORKSPACE_SLUG="${WORKSPACE_SLUG:-smoke-workspace}"
NOW_SECS="${NOW_SECS:-}"

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
Usage: ./scripts/smoke-cms-entry.sh [options]

Options:
  --mode <localhost|env>      localhost: http://localhost:<PORT> (default)
                              env: use CMS_CORE_URL as-is
  --env-file <path>           env file to load (default: .env.dev)
  --token-file <path>         read INTERNAL_SERVICE_TOKEN from file
  --timeout <seconds>         curl timeout seconds (default: 10)
  --workspace-id <uuid>       workspace to run against (or set WORKSPACE_ID)
  --workspace-slug <slug>     slug used for share link assertion
  -h, --help                  show this help

Environment:
  WORKSPACE_ID
  WORKSPACE_SLUG (optional; default smoke-workspace)
  INTERNAL_SERVICE_TOKEN
  XS_USER_ID (optional, default generated UUID)
  CMS_CORE_URL (when --mode env)
  CMS_CORE_PORT (when --mode localhost, default 4202)
EOF
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
    --token-file)
      TOKEN_FILE="${2:-}"
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

if [ -n "$TOKEN_FILE" ]; then
  if [ ! -f "$TOKEN_FILE" ]; then
    echo -e "${RED}Error:${NC} token file not found: $TOKEN_FILE"
    exit 1
  fi
  INTERNAL_SERVICE_TOKEN="$(tr -d '\r\n' <"$TOKEN_FILE")"
  if [ -z "$INTERNAL_SERVICE_TOKEN" ]; then
    echo -e "${RED}Error:${NC} token file is empty: $TOKEN_FILE"
    exit 1
  fi
  export INTERNAL_SERVICE_TOKEN
fi

CMS_CORE_PORT="${CMS_CORE_PORT:-4202}"
if [ "$MODE" = "localhost" ]; then
  CMS_URL="http://localhost:${CMS_CORE_PORT}"
else
  CMS_URL="${CMS_CORE_URL:-http://cms-core:${CMS_CORE_PORT}}"
fi
CMS_ACTIONS_URL="${CMS_URL%/}/internal/cms-actions"

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
  echo "Tip: unset XS_USER_ID to let the script generate one, or set a valid UUID."
  exit 1
fi

if [ -z "$NOW_SECS" ]; then
  NOW_SECS="$(date +%s)"
fi

export CURRENT_WORKSPACE_ID="$WORKSPACE_ID"
export CURRENT_USER_ID="$XS_USER_ID"

echo -e "${GREEN}Starting SMOKE-CMS-ENTRY-1 (directory-first entry actions)...${NC}"
echo "workspaceId=$WORKSPACE_ID userId=$XS_USER_ID"

ENTRY_TITLE="Smoke Entry ${NOW_SECS}"
ENTRY_TITLE_UPDATED="Smoke Entry ${NOW_SECS} Updated"
ENTRY_DESCRIPTION="Created by smoke-cms-entry.sh"
ENTRY_DESCRIPTION_UPDATED="Updated by smoke-cms-entry.sh"

assert_action_ok() {
  local label="$1"
  if ! expect_http_ok "$label"; then
    echo "  body: $HTTP_BODY"
    exit 1
  fi
  if ! expect_ok_true "$label"; then
    echo "  body: $HTTP_BODY"
    exit 1
  fi
}

# 1) create draft entry
CREATE_PAYLOAD="$(
  jq -nc \
    --arg title "$ENTRY_TITLE" \
    --arg description "$ENTRY_DESCRIPTION" \
    '{
      title: $title,
      description: $description,
      tags: ["smoke","entry"],
      ownerName: "Smoke User",
      publishNow: false
    }'
)"
internal_action "$CMS_ACTIONS_URL" "cms.entry.create" "$CREATE_PAYLOAD"
assert_action_ok "cms.entry.create"
ENTRY_ID="$(extract_json '.data.entry.id')"
ENTRY_STATUS="$(extract_json '.data.entry.status')"
if [ -z "$ENTRY_ID" ] || ! is_uuid "$ENTRY_ID"; then
  echo -e "${RED}FAIL${NC} cms.entry.create returned invalid entry ID"
  echo "  body: $HTTP_BODY"
  exit 1
fi
if [ "$ENTRY_STATUS" != "draft" ]; then
  echo -e "${RED}FAIL${NC} cms.entry.create expected status=draft got '$ENTRY_STATUS'"
  echo "  body: $HTTP_BODY"
  exit 1
fi
echo -e "${GREEN}PASS${NC} cms.entry.create: entryId=$ENTRY_ID"

# 2) get by id
internal_action "$CMS_ACTIONS_URL" "cms.entry.getById" "{\"entryId\":\"$ENTRY_ID\"}"
assert_action_ok "cms.entry.getById"
if ! echo "$HTTP_BODY" | jq -e --arg title "$ENTRY_TITLE" '
  (.data.entry.title == $title) and (.data.entry.status == "draft")
' >/dev/null; then
  echo -e "${RED}FAIL${NC} cms.entry.getById returned unexpected values"
  echo "  body: $HTTP_BODY"
  exit 1
fi
echo -e "${GREEN}PASS${NC} cms.entry.getById"

# 3) update
UPDATE_PAYLOAD="$(
  jq -nc \
    --arg entryId "$ENTRY_ID" \
    --arg title "$ENTRY_TITLE_UPDATED" \
    --arg description "$ENTRY_DESCRIPTION_UPDATED" \
    '{
      entryId: $entryId,
      title: $title,
      description: $description,
      tags: ["smoke","entry","updated"]
    }'
)"
internal_action "$CMS_ACTIONS_URL" "cms.entry.update" "$UPDATE_PAYLOAD"
assert_action_ok "cms.entry.update"
if ! echo "$HTTP_BODY" | jq -e --arg title "$ENTRY_TITLE_UPDATED" '
  .data.entry.title == $title
' >/dev/null; then
  echo -e "${RED}FAIL${NC} cms.entry.update title mismatch"
  echo "  body: $HTTP_BODY"
  exit 1
fi
echo -e "${GREEN}PASS${NC} cms.entry.update"

# 4) collaborators set
COLLAB_1="$(gen_uuid)"
COLLAB_2="$(gen_uuid)"
if [ "$COLLAB_1" = "$COLLAB_2" ]; then
  COLLAB_2="00000000-0000-0000-0000-000000000002"
fi
COLLAB_PAYLOAD="$(
  jq -nc \
    --arg entryId "$ENTRY_ID" \
    --arg u1 "$COLLAB_1" \
    --arg u2 "$COLLAB_2" \
    '{
      entryId: $entryId,
      collaborators: [
        { userId: $u1, displayName: "Collab One" },
        { userId: $u2, displayName: "Collab Two" }
      ]
    }'
)"
internal_action "$CMS_ACTIONS_URL" "cms.entry.collaborators.set" "$COLLAB_PAYLOAD"
assert_action_ok "cms.entry.collaborators.set"
if ! echo "$HTTP_BODY" | jq -e '.data.collaborators | length == 2' >/dev/null; then
  echo -e "${RED}FAIL${NC} cms.entry.collaborators.set expected 2 collaborators"
  echo "  body: $HTTP_BODY"
  exit 1
fi
echo -e "${GREEN}PASS${NC} cms.entry.collaborators.set"

# 5) favorite toggle on
internal_action "$CMS_ACTIONS_URL" "cms.entry.favorite.toggle" "{\"entryId\":\"$ENTRY_ID\"}"
assert_action_ok "cms.entry.favorite.toggle (on)"
if ! echo "$HTTP_BODY" | jq -e '.data.isFavorite == true' >/dev/null; then
  echo -e "${RED}FAIL${NC} cms.entry.favorite.toggle expected isFavorite=true"
  echo "  body: $HTTP_BODY"
  exit 1
fi
echo -e "${GREEN}PASS${NC} cms.entry.favorite.toggle (on)"

# 6) favorite list contains entry
internal_action "$CMS_ACTIONS_URL" "cms.entry.favorite.list" '{"limit":20,"offset":0}'
assert_action_ok "cms.entry.favorite.list"
if ! echo "$HTTP_BODY" | jq -e --arg entryId "$ENTRY_ID" '
  (.data.items | map(select(.id == $entryId)) | length) >= 1
' >/dev/null; then
  echo -e "${RED}FAIL${NC} cms.entry.favorite.list missing entry"
  echo "  body: $HTTP_BODY"
  exit 1
fi
echo -e "${GREEN}PASS${NC} cms.entry.favorite.list"

# 7) favorite toggle off
internal_action "$CMS_ACTIONS_URL" "cms.entry.favorite.toggle" "{\"entryId\":\"$ENTRY_ID\"}"
assert_action_ok "cms.entry.favorite.toggle (off)"
if ! echo "$HTTP_BODY" | jq -e '.data.isFavorite == false' >/dev/null; then
  echo -e "${RED}FAIL${NC} cms.entry.favorite.toggle expected isFavorite=false"
  echo "  body: $HTTP_BODY"
  exit 1
fi
echo -e "${GREEN}PASS${NC} cms.entry.favorite.toggle (off)"

# 8) listByDirectory
LIST_PAYLOAD="$(
  jq -nc \
    --arg search "$ENTRY_TITLE_UPDATED" \
    '{
      directoryId: null,
      search: $search,
      sortBy: "date",
      sortDirection: "desc",
      status: "all",
      limit: 20,
      offset: 0
    }'
)"
internal_action "$CMS_ACTIONS_URL" "cms.entry.listByDirectory" "$LIST_PAYLOAD"
assert_action_ok "cms.entry.listByDirectory"
if ! echo "$HTTP_BODY" | jq -e --arg entryId "$ENTRY_ID" '
  (.data.items | map(select(.id == $entryId)) | length) >= 1
' >/dev/null; then
  echo -e "${RED}FAIL${NC} cms.entry.listByDirectory missing entry"
  echo "  body: $HTTP_BODY"
  exit 1
fi
echo -e "${GREEN}PASS${NC} cms.entry.listByDirectory"

# 9) share internal link
SHARE_PAYLOAD="$(
  jq -nc \
    --arg entryId "$ENTRY_ID" \
    --arg workspaceSlug "$WORKSPACE_SLUG" \
    '{entryId: $entryId, workspaceSlug: $workspaceSlug}'
)"
internal_action "$CMS_ACTIONS_URL" "cms.entry.share.generateInternalLink" "$SHARE_PAYLOAD"
assert_action_ok "cms.entry.share.generateInternalLink"
EXPECTED_URL="/dashboard/${WORKSPACE_SLUG}/content/entry/${ENTRY_ID}/edit"
if [ "$(extract_json '.data.url')" != "$EXPECTED_URL" ]; then
  echo -e "${RED}FAIL${NC} cms.entry.share.generateInternalLink URL mismatch"
  echo "  expected: $EXPECTED_URL"
  echo "  body: $HTTP_BODY"
  exit 1
fi
echo -e "${GREEN}PASS${NC} cms.entry.share.generateInternalLink"

# 10) publish
internal_action "$CMS_ACTIONS_URL" "cms.entry.publish" "{\"entryId\":\"$ENTRY_ID\"}"
assert_action_ok "cms.entry.publish"
if ! echo "$HTTP_BODY" | jq -e '
  (.data.entry.status == "published") and (.data.entry.publishedAt != null)
' >/dev/null; then
  echo -e "${RED}FAIL${NC} cms.entry.publish status/publishedAt mismatch"
  echo "  body: $HTTP_BODY"
  exit 1
fi
echo -e "${GREEN}PASS${NC} cms.entry.publish"

# 11) unpublish back to draft via status.set
STATUS_DRAFT_PAYLOAD="$(
  jq -nc \
    --arg entryId "$ENTRY_ID" \
    '{entryId: $entryId, status: "draft"}'
)"
internal_action "$CMS_ACTIONS_URL" "cms.entry.status.set" "$STATUS_DRAFT_PAYLOAD"
assert_action_ok "cms.entry.status.set (draft)"
if ! echo "$HTTP_BODY" | jq -e '
  (.data.entry.status == "draft") and (.data.entry.publishedAt == null)
' >/dev/null; then
  echo -e "${RED}FAIL${NC} cms.entry.status.set draft mismatch"
  echo "  body: $HTTP_BODY"
  exit 1
fi
echo -e "${GREEN}PASS${NC} cms.entry.status.set (draft)"

# 12) schedule via status.set
SCHEDULED_PUBLISH_AT="2099-01-01T00:00:00.000Z"
STATUS_SCHEDULED_PAYLOAD="$(
  jq -nc \
    --arg entryId "$ENTRY_ID" \
    --arg publishAt "$SCHEDULED_PUBLISH_AT" \
    '{entryId: $entryId, status: "scheduled", publishAt: $publishAt}'
)"
internal_action "$CMS_ACTIONS_URL" "cms.entry.status.set" "$STATUS_SCHEDULED_PAYLOAD"
assert_action_ok "cms.entry.status.set (scheduled)"
if ! echo "$HTTP_BODY" | jq -e --arg publishAt "$SCHEDULED_PUBLISH_AT" '
  (.data.entry.status == "scheduled") and (.data.entry.publishedAt == $publishAt)
' >/dev/null; then
  echo -e "${RED}FAIL${NC} cms.entry.status.set scheduled mismatch"
  echo "  body: $HTTP_BODY"
  exit 1
fi
echo -e "${GREEN}PASS${NC} cms.entry.status.set (scheduled)"

# 13) list scheduled only
SCHEDULED_LIST_PAYLOAD='{"directoryId":null,"status":"scheduled","limit":20,"offset":0}'
internal_action "$CMS_ACTIONS_URL" "cms.entry.listByDirectory" "$SCHEDULED_LIST_PAYLOAD"
assert_action_ok "cms.entry.listByDirectory (scheduled)"
if ! echo "$HTTP_BODY" | jq -e --arg entryId "$ENTRY_ID" '
  (.data.items | map(select(.id == $entryId and .status == "scheduled")) | length) >= 1
' >/dev/null; then
  echo -e "${RED}FAIL${NC} cms.entry.listByDirectory scheduled filter mismatch"
  echo "  body: $HTTP_BODY"
  exit 1
fi
echo -e "${GREEN}PASS${NC} cms.entry.listByDirectory (scheduled)"

# 14) archive via status.set
STATUS_ARCHIVED_PAYLOAD="$(
  jq -nc \
    --arg entryId "$ENTRY_ID" \
    '{entryId: $entryId, status: "archived"}'
)"
internal_action "$CMS_ACTIONS_URL" "cms.entry.status.set" "$STATUS_ARCHIVED_PAYLOAD"
assert_action_ok "cms.entry.status.set (archived)"
if ! echo "$HTTP_BODY" | jq -e '
  (.data.entry.status == "archived") and (.data.entry.publishedAt == null)
' >/dev/null; then
  echo -e "${RED}FAIL${NC} cms.entry.status.set archived mismatch"
  echo "  body: $HTTP_BODY"
  exit 1
fi
echo -e "${GREEN}PASS${NC} cms.entry.status.set (archived)"

# 15) soft delete
internal_action "$CMS_ACTIONS_URL" "cms.entry.delete" "{\"entryId\":\"$ENTRY_ID\"}"
assert_action_ok "cms.entry.delete"
if ! echo "$HTTP_BODY" | jq -e --arg entryId "$ENTRY_ID" '
  (.data.success == true) and (.data.entryId == $entryId)
' >/dev/null; then
  echo -e "${RED}FAIL${NC} cms.entry.delete response mismatch"
  echo "  body: $HTTP_BODY"
  exit 1
fi
echo -e "${GREEN}PASS${NC} cms.entry.delete"

# 16) ensure deleted entry no longer appears in list
internal_action "$CMS_ACTIONS_URL" "cms.entry.listByDirectory" "$LIST_PAYLOAD"
assert_action_ok "cms.entry.listByDirectory (after delete)"
if echo "$HTTP_BODY" | jq -e --arg entryId "$ENTRY_ID" '
  (.data.items | map(select(.id == $entryId)) | length) >= 1
' >/dev/null; then
  echo -e "${RED}FAIL${NC} deleted entry still present in listByDirectory"
  echo "  body: $HTTP_BODY"
  exit 1
fi
echo -e "${GREEN}PASS${NC} deleted entry excluded from listByDirectory"

echo
echo -e "${GREEN}SMOKE-CMS-ENTRY-1 OK${NC}"
echo "---------------------------------------------------"
echo "export WORKSPACE_ID=\"$WORKSPACE_ID\""
echo "export ENTRY_ID=\"$ENTRY_ID\""
echo "---------------------------------------------------"
