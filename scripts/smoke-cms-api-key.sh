#!/usr/bin/env bash
# shellcheck disable=SC2155
#
# CMS-API-KEY-ACTOR-1 — Story E: Cross-repo smoke harness
# (see docs/plans/2026-05-10-cms-core-api-key-actor-recognition.md §9)
#
# What it verifies (live, against a running stack on localhost):
#
#   cms_readonly preset (RO):
#     RO.1  GET  /workspaces/:ws/content/:routeSegment        → 200 (public list)
#     RO.2  GET  /workspaces/:ws/content/:routeSegment/:slug  → 200 (public bySlug)
#     RO.3  GET  /workspaces/:ws/blog                         → 200 (public blog list)
#     RO.4  POST /workspaces/:ws/content/entries              → 403 (scope-miss; not in preset)
#
#   cms_authoring preset (AU):
#     AU.1  POST  /workspaces/:ws/content/entries              → 200  + ok envelope
#                  + DB assert: created_by IS NULL, updated_by IS NULL.
#     AU.2  GET   /workspaces/:ws/content/entries              → 200  + ok envelope
#     AU.3  GET   /workspaces/:ws/content/entries/:entryId     → 200  + ok envelope
#     AU.4  PATCH /workspaces/:ws/content/entries/:entryId     → 200  + ok envelope
#                  + DB assert: updated_by IS NULL.
#     AU.5  POST  /workspaces/:ws/content/entries/:entryId/publish → 403
#                  (out of preset — gateway scope-miss).
#     AU.6  DELETE /workspaces/:ws/content/entries/:entryId    → 403 (out of preset).
#
#   cms_publisher preset (PU):
#     PU.1  POST /workspaces/:ws/content/entries/:entryId/publish → 200 + ok envelope
#                  + DB assert: status='published', published_at not null, updated_by IS NULL.
#     PU.2  POST /workspaces/:ws/content/entries/:entryId/status  → 200 + ok envelope
#                  + DB assert: updated_by IS NULL.
#
#   Cross-workspace + auth failure rows:
#     X.1  AU on a DIFFERENT workspace → 403 ("not authorized for this workspace")
#     X.2  Unknown key                 → 401
#     X.3  Revoked key (RO) on public  → 401 (fail-closed)
#
#   Backwards compatibility:
#     BC.1 User-actor (JWT) on POST /entries → 200 + ok envelope
#                  + DB assert: created_by = <user uuid>, updated_by = <user uuid>.
#
#   Redaction:
#     R.1  No raw `xynes_live_*` substring in `cms-core` logs (since the
#          smoke started). Same invariant the gateway-side smoke verifies.
#     R.2  Same for `gateway` logs.
#
# Preconditions:
#   1. Stack is up via `docker-compose.dev.yml`.
#   2. Workspace API key auth is wired (gateway `app.ts` calls
#      `setApiKeyRepository(...)` when DATABASE_URL is set — already on develop).
#   3. cms-core image carries Stories A, B, C of the actor-recognition plan
#      (verify with: docker compose exec cms-core grep "Story C" /app/src/middleware/actor-guards.ts).
#   4. The DB has the workspace_admin_integrations migration applied AND
#      the CMS dashboard routes seed applied (`platform.routes` has
#      `cms.entry.*` rows). Both are part of the standard infra reset.
#   5. JWT_SECRET matches the one the gateway boots with.
#   6. Bun is installed on the host (used to mint the dev JWT).
#   7. psql is installed (used to discover workspace/user and to assert
#      audit-column invariants).
#
# Out of scope:
#   This script does not exercise telemetry-service or doc-service —
#   neither is in any MVP API-key preset.
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

# shellcheck source=./scripts/lib/colors.sh
source "${SCRIPT_DIR}/lib/colors.sh"

# ── Options ─────────────────────────────────────────────────────

GATEWAY_URL="${GATEWAY_URL:-http://localhost:4100}"
JWT_SECRET="${JWT_SECRET:-}"
WORKSPACE_OWNER_USER_ID="${WORKSPACE_OWNER_USER_ID:-}"
WORKSPACE_ID="${WORKSPACE_ID:-}"
OTHER_WORKSPACE_ID="${OTHER_WORKSPACE_ID:-}"
ENV_FILE="${ENV_FILE:-.env.localhost.local}"
CURL_TIMEOUT_SECS="${CURL_TIMEOUT_SECS:-15}"
DOCKER_COMPOSE_ENV_FILE="${DOCKER_COMPOSE_ENV_FILE:-.env.dev.local}"
DOCKER_COMPOSE_FILE="${DOCKER_COMPOSE_FILE:-docker-compose.dev.yml}"
ROUTE_SEGMENT="${ROUTE_SEGMENT:-blog}"
KEEP_KEYS="${KEEP_KEYS:-0}"

usage() {
  cat <<'EOF'
Usage: ./scripts/smoke-cms-api-key.sh [options]

Runs the live CMS API-key actor-recognition smoke battery.

Options:
  --gateway-url <url>      Gateway base URL (default: http://localhost:4100)
  --workspace-id <uuid>    Workspace UUID to mint keys in (auto-discovered
                           from authz.user_roles if omitted).
  --other-workspace-id <uuid>
                           A SECOND workspace UUID (different from
                           --workspace-id) used for the cross-workspace 403.
                           Auto-discovered when possible.
  --user-id <uuid>         UUID of a workspace_owner of --workspace-id.
                           Auto-discovered when possible.
  --env-file <path>        Env file to read DATABASE_URL / JWT_SECRET from
                           (default: .env.localhost.local).
  --route-segment <name>   Public read route segment for RO.1 / RO.2
                           (default: blog).
  --keep-keys              Do NOT revoke the smoke keys on exit (debug).
  -h, --help               Show this help.
EOF
}

while [ "${1:-}" != "" ]; do
  case "$1" in
    --gateway-url) GATEWAY_URL="${2:-}"; shift 2 ;;
    --workspace-id) WORKSPACE_ID="${2:-}"; shift 2 ;;
    --other-workspace-id) OTHER_WORKSPACE_ID="${2:-}"; shift 2 ;;
    --user-id) WORKSPACE_OWNER_USER_ID="${2:-}"; shift 2 ;;
    --env-file) ENV_FILE="${2:-}"; shift 2 ;;
    --route-segment) ROUTE_SEGMENT="${2:-}"; shift 2 ;;
    --keep-keys) KEEP_KEYS=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1"; usage; exit 1 ;;
  esac
done

# ── Load env (DATABASE_URL, JWT_SECRET) ─────────────────────────

if [ -f "${ENV_FILE}" ]; then
  # shellcheck disable=SC1090
  set -a; source "${ENV_FILE}"; set +a
fi

if [ -z "${JWT_SECRET}" ]; then
  echo -e "${RED}FAIL${NC} JWT_SECRET is not set (read from ${ENV_FILE} or export)."
  exit 1
fi
if ! command -v psql >/dev/null 2>&1; then
  echo -e "${RED}FAIL${NC} psql is required (workspace discovery + DB audit assertions)."
  exit 1
fi
if ! command -v bun >/dev/null 2>&1; then
  echo -e "${RED}FAIL${NC} bun is required (mint dev JWT)."
  exit 1
fi

# ── Auto-discover workspace + user from DB if not provided ──────

if [ -z "${WORKSPACE_ID}" ] || [ -z "${WORKSPACE_OWNER_USER_ID}" ] \
   || [ -z "${OTHER_WORKSPACE_ID}" ]; then
  if [ -z "${DATABASE_URL:-}" ]; then
    echo -e "${RED}FAIL${NC} DATABASE_URL not set; provide --workspace-id, --other-workspace-id, --user-id."
    exit 1
  fi

  if [ -z "${WORKSPACE_OWNER_USER_ID}" ] || [ -z "${WORKSPACE_ID}" ]; then
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
  if [ -z "${OTHER_WORKSPACE_ID}" ]; then
    OTHER_WORKSPACE_ID=$(psql "${DATABASE_URL}" --pset=pager=off -At -c "
      SELECT id::text FROM platform.workspaces
      WHERE id::text <> '${WORKSPACE_ID}'
      ORDER BY created_at LIMIT 1;
    " 2>/dev/null || true)
  fi
fi

if [ -z "${WORKSPACE_ID}" ] || [ -z "${WORKSPACE_OWNER_USER_ID}" ] \
   || [ -z "${OTHER_WORKSPACE_ID}" ]; then
  echo -e "${RED}FAIL${NC} Could not auto-discover required IDs."
  echo "  WORKSPACE_ID=${WORKSPACE_ID}"
  echo "  WORKSPACE_OWNER_USER_ID=${WORKSPACE_OWNER_USER_ID}"
  echo "  OTHER_WORKSPACE_ID=${OTHER_WORKSPACE_ID}"
  exit 1
fi

# Record the wall-clock start time so the redaction sweep at the end
# can scope its log query to "since the smoke started" (avoids false
# negatives from old log noise).
SMOKE_START_SECS="$(date +%s)"

echo "Gateway URL          : ${GATEWAY_URL}"
echo "Workspace ID         : ${WORKSPACE_ID}"
echo "Other workspace ID   : ${OTHER_WORKSPACE_ID}"
echo "Workspace owner user : ${WORKSPACE_OWNER_USER_ID}"
echo "Public route segment : ${ROUTE_SEGMENT}"

# ── Mint a workspace_owner HS256 JWT (dev only) ─────────────────

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

if [ -z "${JWT}" ]; then
  echo -e "${RED}FAIL${NC} Failed to mint dev JWT."
  exit 1
fi

# ── Counters + helpers ──────────────────────────────────────────

PASS=0
FAIL=0

assert_status() {
  local label="$1" expected="$2" actual="$3" body_file="$4"
  if [ "${actual}" = "${expected}" ]; then
    echo -e "${GREEN}PASS${NC} ${label} (HTTP ${actual})"
    PASS=$((PASS + 1))
  else
    echo -e "${RED}FAIL${NC} ${label} (expected ${expected}, got ${actual})"
    if [ -n "${body_file}" ] && [ -f "${body_file}" ]; then
      echo "      body: $(head -c 240 "${body_file}")"
    fi
    FAIL=$((FAIL + 1))
  fi
}

assert_ok_envelope() {
  local label="$1" body_file="$2"
  if grep -q '"ok":true' "${body_file}"; then
    echo -e "${GREEN}PASS${NC} ${label}"
    PASS=$((PASS + 1))
  else
    echo -e "${RED}FAIL${NC} ${label} (ok:true envelope missing)"
    echo "      body: $(head -c 240 "${body_file}")"
    FAIL=$((FAIL + 1))
  fi
}

# psql -At one-line query; trims whitespace.
db_one() {
  psql "${DATABASE_URL}" --pset=pager=off -At -c "$1" 2>/dev/null \
    | tr -d '[:space:]'
}

assert_db() {
  local label="$1" expected="$2" actual="$3"
  if [ "${actual}" = "${expected}" ]; then
    echo -e "${GREEN}PASS${NC} ${label} (db: ${actual})"
    PASS=$((PASS + 1))
  else
    echo -e "${RED}FAIL${NC} ${label} (expected '${expected}', got '${actual}')"
    FAIL=$((FAIL + 1))
  fi
}

# Mint a preset key. Echoes "<keyId>|<rawKey>" on success; exits on error.
mint_key() {
  local preset="$1"
  local body_file="$(mktemp)"
  local http
  http=$(curl -sS -o "${body_file}" -w "%{http_code}" \
    --max-time "${CURL_TIMEOUT_SECS}" \
    -H "Authorization: Bearer ${JWT}" \
    -H "Content-Type: application/json" \
    -X POST "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/api-keys" \
    -d "{\"name\":\"cms-smoke-${preset}-$(date +%s%N | head -c 13)\",\"presetKey\":\"${preset}\"}")
  if [ "${http}" != "201" ]; then
    echo -e "${RED}FAIL${NC} Could not mint ${preset} key (HTTP ${http})" >&2
    echo "      body: $(head -c 240 "${body_file}")" >&2
    rm -f "${body_file}"
    exit 1
  fi
  local kid
  local raw
  kid=$(grep -oE '"id":"[a-f0-9-]+"' "${body_file}" | head -1 | sed 's/.*"id":"//; s/"$//')
  raw=$(grep -oE '"rawKey":"xynes_live_[a-f0-9]+"' "${body_file}" \
        | sed 's/.*"rawKey":"//; s/"$//')
  rm -f "${body_file}"
  if [ -z "${kid}" ] || [ -z "${raw}" ]; then
    echo -e "${RED}FAIL${NC} Could not parse ${preset} mint response" >&2
    exit 1
  fi
  echo "${kid}|${raw}"
}

# ── Mint the three CMS preset keys ──────────────────────────────

RO_PARTS="$(mint_key cms_readonly)"
RO_KEY_ID="${RO_PARTS%%|*}"
RO_RAW="${RO_PARTS##*|}"
echo "Minted RO key id     : ${RO_KEY_ID} (prefix: ${RO_RAW:11:8})"

AU_PARTS="$(mint_key cms_authoring)"
AU_KEY_ID="${AU_PARTS%%|*}"
AU_RAW="${AU_PARTS##*|}"
echo "Minted AU key id     : ${AU_KEY_ID} (prefix: ${AU_RAW:11:8})"

PU_PARTS="$(mint_key cms_publisher)"
PU_KEY_ID="${PU_PARTS%%|*}"
PU_RAW="${PU_PARTS##*|}"
echo "Minted PU key id     : ${PU_KEY_ID} (prefix: ${PU_RAW:11:8})"

# Track created entries for cleanup.
CREATED_ENTRY_IDS=()

cleanup() {
  # Soft-delete any entries we created (DB direct — we cannot use the
  # `cms.entry.delete` action because the workspace_owner JWT also needs
  # `cms.entry.delete` permission, which depends on local seed state).
  # This is additive cleanup that does not break the schema and keeps
  # the test DB tidy across runs.
  if [ "${#CREATED_ENTRY_IDS[@]}" -gt 0 ] && [ -n "${DATABASE_URL:-}" ]; then
    for eid in "${CREATED_ENTRY_IDS[@]}"; do
      if [ -n "${eid}" ]; then
        psql "${DATABASE_URL}" --pset=pager=off -At -c "
          UPDATE cms.content_entries
          SET deleted_at = now()
          WHERE id = '${eid}'::uuid AND deleted_at IS NULL;
        " >/dev/null 2>&1 || true
      fi
    done
  fi
  if [ "${KEEP_KEYS}" = "1" ]; then
    return
  fi
  for kid in "${RO_KEY_ID}" "${AU_KEY_ID}" "${PU_KEY_ID}"; do
    if [ -n "${kid}" ]; then
      curl -sS -o /dev/null --max-time "${CURL_TIMEOUT_SECS}" \
        -H "Authorization: Bearer ${JWT}" \
        -H "Content-Type: application/json" \
        -X POST "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/api-keys/${kid}/revoke" \
        -d '{}' || true
    fi
  done
  rm -f /tmp/smoke-cms-apikey-*.json 2>/dev/null || true
}
trap cleanup EXIT

# ── RO.1 / RO.2 — public CMS reads with cms_readonly key ───────
# `cms.content.listPublished` and `cms.blog_entry.listPublished` are
# `is_public=true` routes — scope check is skipped, workspace ownership
# is still enforced.

RO1_BODY=/tmp/smoke-cms-apikey-RO1.json
HTTP=$(curl -sS -o "${RO1_BODY}" -w "%{http_code}" --max-time "${CURL_TIMEOUT_SECS}" \
  -H "Authorization: Bearer ${RO_RAW}" \
  "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/content/${ROUTE_SEGMENT}")
assert_status "RO.1 GET /content/${ROUTE_SEGMENT} with cms_readonly → 200" "200" "${HTTP}" "${RO1_BODY}"

RO3_BODY=/tmp/smoke-cms-apikey-RO3.json
HTTP=$(curl -sS -o "${RO3_BODY}" -w "%{http_code}" --max-time "${CURL_TIMEOUT_SECS}" \
  -H "Authorization: Bearer ${RO_RAW}" \
  "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/blog")
assert_status "RO.3 GET /blog with cms_readonly → 200" "200" "${HTTP}" "${RO3_BODY}"

# RO.4 — scope-miss: cms_readonly cannot create entries.
RO4_BODY=/tmp/smoke-cms-apikey-RO4.json
HTTP=$(curl -sS -o "${RO4_BODY}" -w "%{http_code}" --max-time "${CURL_TIMEOUT_SECS}" \
  -H "Authorization: Bearer ${RO_RAW}" \
  -H "Content-Type: application/json" \
  -X POST "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/content/entries" \
  -d '{"title":"should-be-denied"}')
assert_status "RO.4 POST /content/entries with cms_readonly → 403 (scope-miss)" "403" "${HTTP}" "${RO4_BODY}"
if grep -q "API key missing required scope" "${RO4_BODY}"; then
  echo -e "${GREEN}PASS${NC} RO.4 body carries gateway scope-miss error"
  PASS=$((PASS + 1))
else
  echo -e "${RED}FAIL${NC} RO.4 body did not carry expected gateway error"
  echo "      body: $(head -c 240 "${RO4_BODY}")"
  FAIL=$((FAIL + 1))
fi

# ── AU.1 — cms_authoring can create entries; audit cols are NULL ─

AU1_BODY=/tmp/smoke-cms-apikey-AU1.json
HTTP=$(curl -sS -o "${AU1_BODY}" -w "%{http_code}" --max-time "${CURL_TIMEOUT_SECS}" \
  -H "Authorization: Bearer ${AU_RAW}" \
  -H "Content-Type: application/json" \
  -X POST "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/content/entries" \
  -d "{\"title\":\"cms-smoke-api-key entry $(date +%s)\"}")
assert_status "AU.1 POST /content/entries with cms_authoring → 200" "200" "${HTTP}" "${AU1_BODY}"
assert_ok_envelope "AU.1 body has ok:true envelope" "${AU1_BODY}"

# Extract entry id from envelope: data.entry.id (handler returns { entry: mapEntry(entry) }).
ENTRY_ID=$(grep -oE '"id":"[a-f0-9-]+"' "${AU1_BODY}" | head -1 \
           | sed 's/.*"id":"//; s/"$//')
if [ -z "${ENTRY_ID}" ]; then
  echo -e "${RED}FAIL${NC} AU.1 — could not parse created entry id"
  echo "      body: $(head -c 240 "${AU1_BODY}")"
  FAIL=$((FAIL + 1))
  ENTRY_ID="00000000-0000-0000-0000-000000000000" # sentinel — later assertions will fail-loud
else
  CREATED_ENTRY_IDS+=("${ENTRY_ID}")
fi
echo "Created entry id     : ${ENTRY_ID}"

# AU.1.db — verify audit columns are NULL for api_key actor.
DB_CREATED_BY=$(db_one "
  SELECT COALESCE(created_by::text, 'NULL')
  FROM cms.content_entries
  WHERE id = '${ENTRY_ID}'::uuid;
")
DB_UPDATED_BY=$(db_one "
  SELECT COALESCE(updated_by::text, 'NULL')
  FROM cms.content_entries
  WHERE id = '${ENTRY_ID}'::uuid;
")
assert_db "AU.1.db created_by IS NULL (api_key actor)" "NULL" "${DB_CREATED_BY}"
assert_db "AU.1.db updated_by IS NULL (api_key actor)" "NULL" "${DB_UPDATED_BY}"

# ── AU.2 — cms_authoring can list entries ────────────────────────

AU2_BODY=/tmp/smoke-cms-apikey-AU2.json
HTTP=$(curl -sS -o "${AU2_BODY}" -w "%{http_code}" --max-time "${CURL_TIMEOUT_SECS}" \
  -H "Authorization: Bearer ${AU_RAW}" \
  "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/content/entries")
assert_status "AU.2 GET /content/entries with cms_authoring → 200" "200" "${HTTP}" "${AU2_BODY}"
assert_ok_envelope "AU.2 body has ok:true envelope" "${AU2_BODY}"

# ── AU.3 — cms_authoring can fetch entry by id ───────────────────

AU3_BODY=/tmp/smoke-cms-apikey-AU3.json
HTTP=$(curl -sS -o "${AU3_BODY}" -w "%{http_code}" --max-time "${CURL_TIMEOUT_SECS}" \
  -H "Authorization: Bearer ${AU_RAW}" \
  "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/content/entries/${ENTRY_ID}")
assert_status "AU.3 GET /content/entries/:entryId with cms_authoring → 200" "200" "${HTTP}" "${AU3_BODY}"
assert_ok_envelope "AU.3 body has ok:true envelope" "${AU3_BODY}"

# ── AU.4 — cms_authoring can update entry; updated_by IS NULL ───

AU4_BODY=/tmp/smoke-cms-apikey-AU4.json
HTTP=$(curl -sS -o "${AU4_BODY}" -w "%{http_code}" --max-time "${CURL_TIMEOUT_SECS}" \
  -H "Authorization: Bearer ${AU_RAW}" \
  -H "Content-Type: application/json" \
  -X PATCH "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/content/entries/${ENTRY_ID}" \
  -d "{\"entryId\":\"${ENTRY_ID}\",\"title\":\"cms-smoke-api-key entry (updated) $(date +%s)\"}")
assert_status "AU.4 PATCH /content/entries/:entryId with cms_authoring → 200" "200" "${HTTP}" "${AU4_BODY}"
assert_ok_envelope "AU.4 body has ok:true envelope" "${AU4_BODY}"

DB_UPDATED_BY_AFTER_PATCH=$(db_one "
  SELECT COALESCE(updated_by::text, 'NULL')
  FROM cms.content_entries
  WHERE id = '${ENTRY_ID}'::uuid;
")
assert_db "AU.4.db updated_by IS NULL after api_key PATCH" "NULL" "${DB_UPDATED_BY_AFTER_PATCH}"

# ── AU.5 — cms_authoring cannot publish (out of preset) ─────────

AU5_BODY=/tmp/smoke-cms-apikey-AU5.json
HTTP=$(curl -sS -o "${AU5_BODY}" -w "%{http_code}" --max-time "${CURL_TIMEOUT_SECS}" \
  -H "Authorization: Bearer ${AU_RAW}" \
  -H "Content-Type: application/json" \
  -X POST "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/content/entries/${ENTRY_ID}/publish" \
  -d '{}')
assert_status "AU.5 POST /publish with cms_authoring → 403 (out of preset)" "403" "${HTTP}" "${AU5_BODY}"
if grep -q "API key missing required scope" "${AU5_BODY}"; then
  echo -e "${GREEN}PASS${NC} AU.5 body carries gateway scope-miss error"
  PASS=$((PASS + 1))
else
  echo -e "${RED}FAIL${NC} AU.5 body did not carry expected gateway error"
  echo "      body: $(head -c 240 "${AU5_BODY}")"
  FAIL=$((FAIL + 1))
fi

# ── AU.6 — cms_authoring cannot delete (out of preset) ──────────

AU6_BODY=/tmp/smoke-cms-apikey-AU6.json
HTTP=$(curl -sS -o "${AU6_BODY}" -w "%{http_code}" --max-time "${CURL_TIMEOUT_SECS}" \
  -H "Authorization: Bearer ${AU_RAW}" \
  -X DELETE "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/content/entries/${ENTRY_ID}")
assert_status "AU.6 DELETE /content/entries/:entryId with cms_authoring → 403" "403" "${HTTP}" "${AU6_BODY}"

# ── PU.1 — cms_publisher can publish; updated_by IS NULL ────────

PU1_BODY=/tmp/smoke-cms-apikey-PU1.json
HTTP=$(curl -sS -o "${PU1_BODY}" -w "%{http_code}" --max-time "${CURL_TIMEOUT_SECS}" \
  -H "Authorization: Bearer ${PU_RAW}" \
  -H "Content-Type: application/json" \
  -X POST "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/content/entries/${ENTRY_ID}/publish" \
  -d '{}')
assert_status "PU.1 POST /publish with cms_publisher → 200" "200" "${HTTP}" "${PU1_BODY}"
assert_ok_envelope "PU.1 body has ok:true envelope" "${PU1_BODY}"

DB_PUBLISH_STATE=$(db_one "
  SELECT status || '|' || COALESCE(published_at::text, 'NULL') || '|' || COALESCE(updated_by::text, 'NULL')
  FROM cms.content_entries
  WHERE id = '${ENTRY_ID}'::uuid;
")
# Expect: published|<non-NULL timestamp>|NULL
PUB_STATUS="${DB_PUBLISH_STATE%%|*}"
PUB_REST="${DB_PUBLISH_STATE#*|}"
PUB_AT="${PUB_REST%%|*}"
PUB_UPDATED_BY="${PUB_REST##*|}"
assert_db "PU.1.db status='published' after api_key publish" "published" "${PUB_STATUS}"
if [ "${PUB_AT}" != "NULL" ] && [ -n "${PUB_AT}" ]; then
  echo -e "${GREEN}PASS${NC} PU.1.db published_at is non-NULL (db: ${PUB_AT})"
  PASS=$((PASS + 1))
else
  echo -e "${RED}FAIL${NC} PU.1.db published_at is NULL after publish"
  FAIL=$((FAIL + 1))
fi
assert_db "PU.1.db updated_by IS NULL (api_key actor)" "NULL" "${PUB_UPDATED_BY}"

# ── PU.2 — cms_publisher can set status; updated_by IS NULL ─────

PU2_BODY=/tmp/smoke-cms-apikey-PU2.json
HTTP=$(curl -sS -o "${PU2_BODY}" -w "%{http_code}" --max-time "${CURL_TIMEOUT_SECS}" \
  -H "Authorization: Bearer ${PU_RAW}" \
  -H "Content-Type: application/json" \
  -X POST "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/content/entries/${ENTRY_ID}/status" \
  -d '{"status":"archived"}')
assert_status "PU.2 POST /status with cms_publisher → 200" "200" "${HTTP}" "${PU2_BODY}"
assert_ok_envelope "PU.2 body has ok:true envelope" "${PU2_BODY}"

DB_STATUS_AFTER=$(db_one "
  SELECT status || '|' || COALESCE(updated_by::text, 'NULL')
  FROM cms.content_entries
  WHERE id = '${ENTRY_ID}'::uuid;
")
PU2_STATUS="${DB_STATUS_AFTER%%|*}"
PU2_UPDATED_BY="${DB_STATUS_AFTER##*|}"
assert_db "PU.2.db status='archived' after api_key status.set" "archived" "${PU2_STATUS}"
assert_db "PU.2.db updated_by IS NULL (api_key actor)" "NULL" "${PU2_UPDATED_BY}"

# ── X.1 — cross-workspace 403 (AU key on other workspace) ───────

X1_BODY=/tmp/smoke-cms-apikey-X1.json
HTTP=$(curl -sS -o "${X1_BODY}" -w "%{http_code}" --max-time "${CURL_TIMEOUT_SECS}" \
  -H "Authorization: Bearer ${AU_RAW}" \
  "${GATEWAY_URL}/workspaces/${OTHER_WORKSPACE_ID}/content/entries")
assert_status "X.1 AU key on OTHER workspace → 403" "403" "${HTTP}" "${X1_BODY}"
if grep -q "not authorized for this workspace" "${X1_BODY}"; then
  echo -e "${GREEN}PASS${NC} X.1 body carries gateway workspace-mismatch error"
  PASS=$((PASS + 1))
else
  echo -e "${RED}FAIL${NC} X.1 body did not carry expected gateway error"
  echo "      body: $(head -c 240 "${X1_BODY}")"
  FAIL=$((FAIL + 1))
fi

# ── X.2 — structurally-valid but unknown key → 401 ─────────────

X2_BODY=/tmp/smoke-cms-apikey-X2.json
UNKNOWN="xynes_live_deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
HTTP=$(curl -sS -o "${X2_BODY}" -w "%{http_code}" --max-time "${CURL_TIMEOUT_SECS}" \
  -H "Authorization: Bearer ${UNKNOWN}" \
  "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/content/entries")
assert_status "X.2 unknown key → 401" "401" "${HTTP}" "${X2_BODY}"

# ── BC.1 — user-actor (JWT) regression: created_by = userId ────

BC1_BODY=/tmp/smoke-cms-apikey-BC1.json
HTTP=$(curl -sS -o "${BC1_BODY}" -w "%{http_code}" --max-time "${CURL_TIMEOUT_SECS}" \
  -H "Authorization: Bearer ${JWT}" \
  -H "Content-Type: application/json" \
  -X POST "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/content/entries" \
  -d "{\"title\":\"cms-smoke-user-actor entry $(date +%s)\"}")
assert_status "BC.1 POST /content/entries with user JWT → 200 (regression)" "200" "${HTTP}" "${BC1_BODY}"
assert_ok_envelope "BC.1 body has ok:true envelope" "${BC1_BODY}"

BC1_ENTRY_ID=$(grep -oE '"id":"[a-f0-9-]+"' "${BC1_BODY}" | head -1 \
               | sed 's/.*"id":"//; s/"$//')
if [ -n "${BC1_ENTRY_ID}" ]; then
  CREATED_ENTRY_IDS+=("${BC1_ENTRY_ID}")
  BC1_CREATED_BY=$(db_one "
    SELECT COALESCE(created_by::text, 'NULL')
    FROM cms.content_entries
    WHERE id = '${BC1_ENTRY_ID}'::uuid;
  ")
  BC1_UPDATED_BY=$(db_one "
    SELECT COALESCE(updated_by::text, 'NULL')
    FROM cms.content_entries
    WHERE id = '${BC1_ENTRY_ID}'::uuid;
  ")
  assert_db "BC.1.db created_by = workspace_owner userId (user actor)" "${WORKSPACE_OWNER_USER_ID}" "${BC1_CREATED_BY}"
  assert_db "BC.1.db updated_by = workspace_owner userId (user actor)" "${WORKSPACE_OWNER_USER_ID}" "${BC1_UPDATED_BY}"
else
  echo -e "${RED}FAIL${NC} BC.1 — could not parse created entry id"
  echo "      body: $(head -c 240 "${BC1_BODY}")"
  FAIL=$((FAIL + 1))
fi

# ── X.3 — revoked RO key on public route → 401 (fail-closed) ───
# Revoke the RO key explicitly, then probe. Cleanup will re-revoke as
# a no-op on exit.

curl -sS -o /dev/null --max-time "${CURL_TIMEOUT_SECS}" \
  -H "Authorization: Bearer ${JWT}" \
  -H "Content-Type: application/json" \
  -X POST "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/api-keys/${RO_KEY_ID}/revoke" \
  -d '{}'

X3_BODY=/tmp/smoke-cms-apikey-X3.json
HTTP=$(curl -sS -o "${X3_BODY}" -w "%{http_code}" --max-time "${CURL_TIMEOUT_SECS}" \
  -H "Authorization: Bearer ${RO_RAW}" \
  "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/content/${ROUTE_SEGMENT}")
assert_status "X.3 revoked RO key on public route → 401 (fail-closed)" "401" "${HTTP}" "${X3_BODY}"

# ── Redaction sanity: no raw key substring in service logs ─────

if command -v docker >/dev/null 2>&1 \
   && [ -f "${ROOT_DIR}/${DOCKER_COMPOSE_FILE}" ]; then
  # Scope the docker log search to "since the smoke started" to avoid
  # false negatives from unrelated log noise older than this run.
  LOG_SINCE_ISO="$(date -u -r "${SMOKE_START_SECS}" '+%Y-%m-%dT%H:%M:%S' 2>/dev/null || \
                   date -u -d "@${SMOKE_START_SECS}" '+%Y-%m-%dT%H:%M:%S' 2>/dev/null || \
                   echo '5m')"
  CMS_LEAK=0
  GW_LEAK=0
  for raw in "${RO_RAW}" "${AU_RAW}" "${PU_RAW}"; do
    n=$(docker compose --env-file "${DOCKER_COMPOSE_ENV_FILE}" \
      -f "${DOCKER_COMPOSE_FILE}" logs cms-core --since "${LOG_SINCE_ISO}" 2>&1 \
      | grep -c "${raw}" || true)
    CMS_LEAK=$((CMS_LEAK + n))
    n=$(docker compose --env-file "${DOCKER_COMPOSE_ENV_FILE}" \
      -f "${DOCKER_COMPOSE_FILE}" logs gateway --since "${LOG_SINCE_ISO}" 2>&1 \
      | grep -c "${raw}" || true)
    GW_LEAK=$((GW_LEAK + n))
  done
  if [ "${CMS_LEAK}" = "0" ] && [ "${GW_LEAK}" = "0" ]; then
    echo -e "${GREEN}PASS${NC} R.1 + R.2 redaction: no raw key leaked into cms-core or gateway logs"
    PASS=$((PASS + 1))
  else
    echo -e "${RED}FAIL${NC} Redaction: raw key found in logs (cms-core=${CMS_LEAK}, gateway=${GW_LEAK})"
    FAIL=$((FAIL + 1))
  fi
fi

# ── Summary ─────────────────────────────────────────────────────

echo ""
echo "═══════════════════════════════════════════════════════════════════════════"
echo "  CMS API KEY ACTOR-RECOGNITION SMOKE — SUMMARY"
echo "═══════════════════════════════════════════════════════════════════════════"
echo "  PASS: ${PASS}"
echo "  FAIL: ${FAIL}"
echo ""
if [ "${FAIL}" -ne 0 ]; then
  exit 1
fi
exit 0
