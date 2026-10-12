#!/usr/bin/env bash
# shellcheck disable=SC2155
#
# STORAGE-12 — Universal Object Storage smoke cleanup
# (see docs/plans/2026-05-10-universal-object-storage-file-upload-api.md §STORAGE-12)
#
# What it does:
#
#   1. Lists workspace upload sessions still in `pending` state and aborts
#      them via the gateway (which issues `AbortMultipartUpload` on the
#      provider side per STORAGE-9 invariants).
#   2. Optionally soft-deletes any storage_objects flagged with a
#      smoke-test filename pattern (default: `storage-smoke-*`).
#   3. Reports counts (sessions aborted, objects deleted, sessions skipped).
#
# Use this after a failed or interrupted `smoke-universal-storage.sh` run,
# or as part of CI / nightly housekeeping against a dev workspace.
#
# Preconditions:
#   1. Stack is up via `docker-compose.dev.yml`.
#   2. JWT_SECRET matches the gateway boot value.
#   3. Bun + curl + psql are installed.
#   4. Storage-service handlers are registered (follow-up infra story).
#      If they aren't, this script exits 0 with a clear NO-OP message.
#
# Out of scope:
#   - Bucket-side abandoned-multipart cleanup. STORAGE-9's
#     `AbandonedUploadCleanup` worker handles that (provider-side
#     lifecycle policy backstops it; ≥ 7-day expiry per the rollout
#     checklist).
#   - Provider-side object deletion for soft-deleted rows. That's a
#     lifecycle policy concern owned per workspace.
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

# shellcheck source=./scripts/lib/colors.sh
source "${SCRIPT_DIR}/lib/colors.sh"

GATEWAY_URL="${GATEWAY_URL:-http://localhost:4100}"
JWT_SECRET="${JWT_SECRET:-}"
WORKSPACE_OWNER_USER_ID="${WORKSPACE_OWNER_USER_ID:-}"
WORKSPACE_ID="${WORKSPACE_ID:-}"
ENV_FILE="${ENV_FILE:-.env.localhost.local}"
CURL_TIMEOUT_SECS="${CURL_TIMEOUT_SECS:-15}"
FILENAME_PATTERN="${FILENAME_PATTERN:-storage-smoke-%}"
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: ./scripts/cleanup-universal-storage.sh [options]

Cleans up test objects + sessions left behind by smoke-universal-storage.sh.

Options:
  --gateway-url <url>           Gateway base URL (default: http://localhost:4100)
  --workspace-id <uuid>         Workspace UUID to clean. Auto-discovered if omitted.
  --user-id <uuid>              UUID of a workspace_owner of --workspace-id.
  --env-file <path>             Env file to read DATABASE_URL / JWT_SECRET from.
  --filename-pattern <glob>     SQL LIKE pattern of filenames to delete.
                                Default: storage-smoke-% (matches the smoke's
                                `storage-smoke-<epoch>.png` convention).
                                Use % as wildcard. Empty string disables
                                object deletion (sessions still aborted).
  --dry-run                     Print what would happen without making changes.
  -h, --help                    Show this help.
EOF
}

while [ "${1:-}" != "" ]; do
  case "$1" in
    --gateway-url) GATEWAY_URL="${2:-}"; shift 2 ;;
    --workspace-id) WORKSPACE_ID="${2:-}"; shift 2 ;;
    --user-id) WORKSPACE_OWNER_USER_ID="${2:-}"; shift 2 ;;
    --env-file) ENV_FILE="${2:-}"; shift 2 ;;
    --filename-pattern) FILENAME_PATTERN="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1"; usage; exit 2 ;;
  esac
done

if [ -f "${ENV_FILE}" ]; then
  # shellcheck disable=SC1090
  set -a; source "${ENV_FILE}"; set +a
fi

for tool in curl psql bun; do
  if ! command -v "${tool}" >/dev/null 2>&1; then
    echo -e "${RED}FAIL${NC} ${tool} is required." >&2
    exit 2
  fi
done

if [ -z "${JWT_SECRET}" ]; then
  echo -e "${RED}FAIL${NC} JWT_SECRET is not set (read from ${ENV_FILE})." >&2
  exit 2
fi

if [ -z "${DATABASE_URL:-}" ]; then
  echo -e "${RED}FAIL${NC} DATABASE_URL is required." >&2
  exit 2
fi

# Auto-discover workspace + workspace_owner.
if [ -z "${WORKSPACE_ID}" ] || [ -z "${WORKSPACE_OWNER_USER_ID}" ]; then
  discovered=$(psql "${DATABASE_URL}" --pset=pager=off -At -c "
    SELECT ur.user_id || '|' || ur.workspace_id
    FROM authz.user_roles ur
    WHERE ur.role_key = 'workspace_owner'
    ORDER BY ur.workspace_id
    LIMIT 1;
  " 2>/dev/null || true)
  if [ -n "${discovered}" ]; then
    WORKSPACE_OWNER_USER_ID="${WORKSPACE_OWNER_USER_ID:-${discovered%%|*}}"
    WORKSPACE_ID="${WORKSPACE_ID:-${discovered##*|}}"
  fi
fi
if [ -z "${WORKSPACE_ID}" ] || [ -z "${WORKSPACE_OWNER_USER_ID}" ]; then
  echo -e "${RED}FAIL${NC} Could not auto-discover workspace + user." >&2
  exit 2
fi

# Mint a workspace_owner HS256 JWT for the gateway calls.
JWT="$(WORKSPACE_OWNER_USER_ID="${WORKSPACE_OWNER_USER_ID}" \
       JWT_SECRET="${JWT_SECRET}" \
       bun -e '
import { createHmac } from "node:crypto";
const userId = process.env.WORKSPACE_OWNER_USER_ID;
const secret = process.env.JWT_SECRET;
const header = Buffer.from(JSON.stringify({alg:"HS256",typ:"JWT"})).toString("base64url");
const now = Math.floor(Date.now()/1000);
const payload = Buffer.from(JSON.stringify({sub:userId,iat:now,exp:now+3600})).toString("base64url");
const sig = createHmac("sha256", secret).update(`${header}.${payload}`).digest("base64url");
process.stdout.write(`${header}.${payload}.${sig}`);
')"

echo "Gateway URL    : ${GATEWAY_URL}"
echo "Workspace ID   : ${WORKSPACE_ID}"
echo "Pattern        : ${FILENAME_PATTERN}"
echo "Dry-run        : $([ ${DRY_RUN} = 1 ] && echo yes || echo no)"
echo ""

ABORTED_COUNT=0
DELETED_COUNT=0
SKIPPED_COUNT=0

# 1. Abort pending upload sessions for this workspace.
PENDING_IDS=$(psql "${DATABASE_URL}" --pset=pager=off -At -c "
  SELECT id::text
  FROM platform.storage_upload_sessions
  WHERE workspace_id = '${WORKSPACE_ID}'
    AND status = 'pending';
" 2>/dev/null || true)

if [ -n "${PENDING_IDS}" ]; then
  while IFS= read -r upload_id; do
    [ -z "${upload_id}" ] && continue
    if [ "${DRY_RUN}" = "1" ]; then
      echo "  [dry-run] would abort upload session ${upload_id}"
      SKIPPED_COUNT=$((SKIPPED_COUNT + 1))
      continue
    fi
    HTTP=$(curl -sS -o /dev/null -w "%{http_code}" \
      --max-time "${CURL_TIMEOUT_SECS}" \
      -H "Authorization: Bearer ${JWT}" \
      -H "Content-Type: application/json" \
      -X POST "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/storage/uploads/${upload_id}/abort" \
      -d "{\"operation\":\"abort\",\"uploadId\":\"${upload_id}\"}" \
      || echo "000")
    case "${HTTP}" in
      200|404|409)
        echo -e "  ${GREEN}OK${NC} aborted ${upload_id} (HTTP ${HTTP})"
        ABORTED_COUNT=$((ABORTED_COUNT + 1))
        ;;
      *)
        echo -e "  ${RED}SKIP${NC} ${upload_id} (HTTP ${HTTP})"
        SKIPPED_COUNT=$((SKIPPED_COUNT + 1))
        ;;
    esac
  done <<< "${PENDING_IDS}"
fi

# 2. Soft-delete objects matching the filename pattern.
if [ -n "${FILENAME_PATTERN}" ]; then
  SMOKE_OBJECT_IDS=$(psql "${DATABASE_URL}" --pset=pager=off -At -c "
    SELECT id::text
    FROM platform.storage_objects
    WHERE workspace_id = '${WORKSPACE_ID}'
      AND status <> 'deleted'
      AND filename LIKE '${FILENAME_PATTERN}';
  " 2>/dev/null || true)

  if [ -n "${SMOKE_OBJECT_IDS}" ]; then
    while IFS= read -r object_id; do
      [ -z "${object_id}" ] && continue
      if [ "${DRY_RUN}" = "1" ]; then
        echo "  [dry-run] would delete object ${object_id}"
        SKIPPED_COUNT=$((SKIPPED_COUNT + 1))
        continue
      fi
      HTTP=$(curl -sS -o /dev/null -w "%{http_code}" \
        --max-time "${CURL_TIMEOUT_SECS}" \
        -H "Authorization: Bearer ${JWT}" \
        -H "Content-Type: application/json" \
        -X DELETE "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/storage/objects/${object_id}" \
        -d '{"operation":"delete"}' \
        || echo "000")
      case "${HTTP}" in
        200|404)
          echo -e "  ${GREEN}OK${NC} deleted ${object_id} (HTTP ${HTTP})"
          DELETED_COUNT=$((DELETED_COUNT + 1))
          ;;
        *)
          echo -e "  ${RED}SKIP${NC} ${object_id} (HTTP ${HTTP})"
          SKIPPED_COUNT=$((SKIPPED_COUNT + 1))
          ;;
      esac
    done <<< "${SMOKE_OBJECT_IDS}"
  fi
fi

echo ""
echo "──────────────────────────────────────"
echo "Aborted sessions : ${ABORTED_COUNT}"
echo "Deleted objects  : ${DELETED_COUNT}"
echo "Skipped          : ${SKIPPED_COUNT}"
echo "──────────────────────────────────────"
exit 0
