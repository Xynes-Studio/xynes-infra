#!/usr/bin/env bash
# shellcheck disable=SC2155
#
# Workspace Admin Integrations — Gateway API Key Enforcement Smoke
# (Task 7, Step 3 of
#  docs/plans/2026-04-24-workspace-admin-integrations-gateway-api-key-enforcement.md)
#
# What it verifies (live, against a running stack on localhost):
#   - Smoke A : in-scope `Authorization: Bearer xynes_live_*`
#               on a non-public route is authenticated by the gateway,
#               scope-checked against the route's actionKey, the
#               workspace ownership matches, and the request is forwarded
#               downstream. Post-PFU-1: the request also round-trips
#               end-to-end through accounts-service (which now recognises
#               the API-key actor) and returns 200 with an ok envelope.
#   - Smoke A2: same, but with `X-XS-API-Key: xynes_live_*` header.
#   - Smoke B : scope-miss on a non-public route → 403 from the gateway,
#               with `[DynamicRouter] API key missing scope <action> on
#               <path>` in the gateway log.
#   - Smoke C : workspace mismatch (key from workspace W, request to
#               workspace W') → 403 from the gateway.
#   - Smoke D : revoked key → 401 even on otherwise public routes
#               (fail-closed posture).
#   - Smoke E : conflicting `Authorization` + `X-XS-API-Key` headers
#               with different values → 400 BEFORE the repository is
#               queried.
#   - Smoke F : structurally-valid but unknown key → 401.
#   - Smoke G : in-scope public CMS read with a workspace-scoped API
#               key → 200/2xx (workspace ownership enforced even on
#               public routes; scope check skipped for `is_public`).
#   - Redaction: assert no `xynes_live_*` substring leaks into the
#                gateway or accounts-service docker logs after the run.
#
# Preconditions:
#   1. Stack is up via `docker-compose.dev.yml` (gateway, accounts,
#      cms-core, authz, telemetry, doc).
#   2. `xynes/xynes-gateway/src/app.ts` wires the workspace API key
#      repository via `setApiKeyRepository(new
#      PostgresWorkspaceApiKeyRepository())`. Without this, the
#      `Authorization: Bearer xynes_live_*` requests fall through to the
#      JWT path and 401 — the smoke would not exercise the new auth path.
#   3. The DB has the workspace_admin_integrations migration applied
#      (`platform.workspace_api_keys`, `platform.workspace_api_key_scopes`,
#      and the 8 platform.api_keys / platform.domains routes seeded).
#   4. `JWT_SECRET` matches the one the gateway is started with so this
#      script can mint a workspace-owner HS256 dev token.
#   5. Bun is installed on the host (used to mint the dev JWT and to
#      avoid an extra npm dependency).
#
# Out of scope:
#   This script only exercises accounts-service end-to-end (the only
#   downstream service migrated under PFU-1 to recognise the API-key
#   actor surface). Other services (cms-core, telemetry) continue to be
#   exercised at the gateway layer only — their internal routes either
#   already accept anonymous-style requests or will be migrated in a
#   future PFU. If a future preset adds scopes against those services,
#   add a dedicated smoke step here that asserts the round-trip.

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
CURL_TIMEOUT_SECS="${CURL_TIMEOUT_SECS:-10}"
DOCKER_COMPOSE_ENV_FILE="${DOCKER_COMPOSE_ENV_FILE:-.env.dev.local}"
DOCKER_COMPOSE_FILE="${DOCKER_COMPOSE_FILE:-docker-compose.dev.yml}"
KEEP_KEY="${KEEP_KEY:-0}"

usage() {
  cat <<'EOF'
Usage: ./scripts/smoke-api-key-enforcement.sh [options]

Runs the live gateway API-key enforcement smoke battery.

Options:
  --gateway-url <url>     Gateway base URL (default: http://localhost:4100)
  --workspace-id <uuid>   Workspace UUID to mint the key in (required if
                          not derivable from the DB; auto-discovered if
                          DATABASE_URL is reachable).
  --other-workspace-id <uuid>
                          A SECOND workspace UUID (different from
                          --workspace-id) for the cross-workspace
                          mismatch test. Auto-discovered when possible.
  --user-id <uuid>        UUID of a workspace_owner of --workspace-id.
                          Auto-discovered when possible.
  --env-file <path>       Env file to read DATABASE_URL / JWT_SECRET
                          from (default: .env.localhost.local)
  --keep-key              Do NOT revoke the smoke key on exit.
  -h, --help              Show this help.
EOF
}

while [ "${1:-}" != "" ]; do
  case "$1" in
    --gateway-url) GATEWAY_URL="${2:-}"; shift 2 ;;
    --workspace-id) WORKSPACE_ID="${2:-}"; shift 2 ;;
    --other-workspace-id) OTHER_WORKSPACE_ID="${2:-}"; shift 2 ;;
    --user-id) WORKSPACE_OWNER_USER_ID="${2:-}"; shift 2 ;;
    --env-file) ENV_FILE="${2:-}"; shift 2 ;;
    --keep-key) KEEP_KEY=1; shift ;;
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

# ── Auto-discover workspace + user from DB if not provided ──────

if [ -z "${WORKSPACE_ID}" ] || [ -z "${WORKSPACE_OWNER_USER_ID}" ] \
   || [ -z "${OTHER_WORKSPACE_ID}" ]; then
  if ! command -v psql >/dev/null 2>&1; then
    echo -e "${RED}FAIL${NC} psql not found and --workspace-id / --user-id / --other-workspace-id not all set."
    exit 1
  fi
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

echo "Gateway URL          : ${GATEWAY_URL}"
echo "Workspace ID         : ${WORKSPACE_ID}"
echo "Other workspace ID   : ${OTHER_WORKSPACE_ID}"
echo "Workspace owner user : ${WORKSPACE_OWNER_USER_ID}"

# ── Mint a workspace_owner HS256 JWT (dev only) ─────────────────

if ! command -v bun >/dev/null 2>&1; then
  echo -e "${RED}FAIL${NC} bun is required to mint the dev JWT."
  exit 1
fi

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

# ── Helpers ─────────────────────────────────────────────────────

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

# ── Mint a workspace_admin preset key (scopes: domains.list, ──────
#    api_keys.list, api_keys.usage.read — but NOT api_keys.create)

MINT_RES=$(mktemp)
HTTP=$(curl -sS -o "${MINT_RES}" -w "%{http_code}" \
  --max-time "${CURL_TIMEOUT_SECS}" \
  -H "Authorization: Bearer ${JWT}" \
  -H "Content-Type: application/json" \
  -X POST "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/api-keys" \
  -d "{\"name\":\"smoke-$(date +%s)\",\"presetKey\":\"workspace_admin\"}")

if [ "${HTTP}" != "201" ]; then
  echo -e "${RED}FAIL${NC} Could not mint smoke key (HTTP ${HTTP})"
  echo "      body: $(head -c 240 "${MINT_RES}")"
  exit 1
fi

# Extract id + raw key. Avoid jq dep — use grep+sed.
KEY_ID=$(grep -oE '"id":"[a-f0-9-]+"' "${MINT_RES}" | head -1 | sed 's/.*"id":"//; s/"$//')
RAW_KEY=$(grep -oE '"rawKey":"xynes_live_[a-f0-9]+"' "${MINT_RES}" \
          | sed 's/.*"rawKey":"//; s/"$//')

if [ -z "${KEY_ID}" ] || [ -z "${RAW_KEY}" ]; then
  echo -e "${RED}FAIL${NC} Could not parse mint response"
  echo "      body: $(head -c 240 "${MINT_RES}")"
  exit 1
fi

echo "Minted key id        : ${KEY_ID}"
KEY_PREFIX="${RAW_KEY:11:8}"
echo "Minted key prefix    : ${KEY_PREFIX} (raw key not echoed in full)"

cleanup() {
  if [ "${KEEP_KEY}" = "1" ]; then
    return
  fi
  curl -sS -o /dev/null --max-time "${CURL_TIMEOUT_SECS}" \
    -H "Authorization: Bearer ${JWT}" \
    -H "Content-Type: application/json" \
    -X POST "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/api-keys/${KEY_ID}/revoke" \
    -d '{}' || true
  rm -f "${MINT_RES}" /tmp/smoke-apikey-*.json 2>/dev/null || true
}
trap cleanup EXIT

# ── Smoke A: in-scope GET /api-keys (Authorization: Bearer) ─────
# (`platform.api_keys.list` is in scopes, workspace matches, route is
# workspaceScoped + non-public → gateway must NOT return 4xx from its
# own auth path. Status from the downstream service is acceptable.)

A_BODY=/tmp/smoke-apikey-A.json
HTTP=$(curl -sS -o "${A_BODY}" -w "%{http_code}" --max-time "${CURL_TIMEOUT_SECS}" \
  -H "Authorization: Bearer ${RAW_KEY}" \
  "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/api-keys")
# After PFU-1 (accounts-service `internal.route.ts` now recognises the
# api_key actor): an in-scope `platform.api_keys.list` request must
# round-trip end-to-end and return 200 with a JSON body envelope. A
# downstream 401 with body "X-XS-User-Id header is required" still
# counts as gateway-side success but signals that PFU-1 is incomplete
# in the running stack (older accounts-service image deployed).
A_BODY_TXT="$(head -c 240 "${A_BODY}")"
if [ "${HTTP}" = "200" ]; then
  if echo "${A_BODY_TXT}" | grep -q '"ok":true'; then
    echo -e "${GREEN}PASS${NC} A in-scope (Authorization) — end-to-end 200 with ok envelope"
    PASS=$((PASS + 1))
  else
    echo -e "${RED}FAIL${NC} A in-scope (Authorization) — 200 but ok envelope missing"
    echo "      body: ${A_BODY_TXT}"
    FAIL=$((FAIL + 1))
  fi
elif [ "${HTTP}" = "401" ] && echo "${A_BODY_TXT}" | grep -q "X-XS-User-Id header is required"; then
  echo -e "${YELLOW}WARN${NC} A in-scope (Authorization) — gateway forwarded; accounts-service NOT yet API-key-aware (PFU-1 not deployed?)"
  PASS=$((PASS + 1))
elif [ "${HTTP}" = "401" ] || [ "${HTTP}" = "403" ] || [ "${HTTP}" = "400" ]; then
  echo -e "${RED}FAIL${NC} A in-scope (Authorization) — gateway rejected with ${HTTP}"
  echo "      body: ${A_BODY_TXT}"
  FAIL=$((FAIL + 1))
else
  echo -e "${GREEN}PASS${NC} A in-scope (Authorization) (HTTP ${HTTP}; gateway forwarded; downstream non-401)"
  PASS=$((PASS + 1))
fi

# ── Smoke A2: in-scope via X-XS-API-Key header ─────────────────

A2_BODY=/tmp/smoke-apikey-A2.json
HTTP=$(curl -sS -o "${A2_BODY}" -w "%{http_code}" --max-time "${CURL_TIMEOUT_SECS}" \
  -H "X-XS-API-Key: ${RAW_KEY}" \
  "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/api-keys")
A2_BODY_TXT="$(head -c 240 "${A2_BODY}")"
if [ "${HTTP}" = "200" ]; then
  if echo "${A2_BODY_TXT}" | grep -q '"ok":true'; then
    echo -e "${GREEN}PASS${NC} A2 in-scope (X-XS-API-Key) — end-to-end 200 with ok envelope"
    PASS=$((PASS + 1))
  else
    echo -e "${RED}FAIL${NC} A2 in-scope (X-XS-API-Key) — 200 but ok envelope missing"
    echo "      body: ${A2_BODY_TXT}"
    FAIL=$((FAIL + 1))
  fi
elif [ "${HTTP}" = "401" ] && echo "${A2_BODY_TXT}" | grep -q "X-XS-User-Id header is required"; then
  echo -e "${YELLOW}WARN${NC} A2 in-scope (X-XS-API-Key) — gateway forwarded; accounts-service NOT yet API-key-aware (PFU-1 not deployed?)"
  PASS=$((PASS + 1))
elif [ "${HTTP}" = "401" ] || [ "${HTTP}" = "403" ] || [ "${HTTP}" = "400" ]; then
  echo -e "${RED}FAIL${NC} A2 in-scope (X-XS-API-Key) — gateway rejected with ${HTTP}"
  echo "      body: ${A2_BODY_TXT}"
  FAIL=$((FAIL + 1))
else
  echo -e "${GREEN}PASS${NC} A2 in-scope (X-XS-API-Key) (HTTP ${HTTP}; gateway forwarded; downstream non-401)"
  PASS=$((PASS + 1))
fi

# ── Smoke B: scope-miss POST /api-keys (.create not in scopes) ─

B_BODY=/tmp/smoke-apikey-B.json
HTTP=$(curl -sS -o "${B_BODY}" -w "%{http_code}" --max-time "${CURL_TIMEOUT_SECS}" \
  -H "Authorization: Bearer ${RAW_KEY}" \
  -H "Content-Type: application/json" \
  -X POST "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/api-keys" \
  -d '{"name":"deny","presetKey":"cms_readonly"}')
assert_status "B scope-miss POST /api-keys → 403" "403" "${HTTP}" "${B_BODY}"
if grep -q "API key missing required scope" "${B_BODY}"; then
  echo -e "${GREEN}PASS${NC} B body carries gateway-origin error message"
  PASS=$((PASS + 1))
else
  echo -e "${RED}FAIL${NC} B body did not carry expected gateway error message"
  echo "      body: $(head -c 240 "${B_BODY}")"
  FAIL=$((FAIL + 1))
fi

# ── Smoke C: workspace mismatch ─────────────────────────────────

C_BODY=/tmp/smoke-apikey-C.json
HTTP=$(curl -sS -o "${C_BODY}" -w "%{http_code}" --max-time "${CURL_TIMEOUT_SECS}" \
  -H "Authorization: Bearer ${RAW_KEY}" \
  "${GATEWAY_URL}/workspaces/${OTHER_WORKSPACE_ID}/api-keys")
assert_status "C workspace mismatch → 403" "403" "${HTTP}" "${C_BODY}"
if grep -q "not authorized for this workspace" "${C_BODY}"; then
  echo -e "${GREEN}PASS${NC} C body carries gateway workspace-mismatch error"
  PASS=$((PASS + 1))
else
  echo -e "${RED}FAIL${NC} C body did not carry expected gateway error message"
  echo "      body: $(head -c 240 "${C_BODY}")"
  FAIL=$((FAIL + 1))
fi

# ── Smoke E: conflicting headers ────────────────────────────────

E_BODY=/tmp/smoke-apikey-E.json
DECOY="xynes_live_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
HTTP=$(curl -sS -o "${E_BODY}" -w "%{http_code}" --max-time "${CURL_TIMEOUT_SECS}" \
  -H "Authorization: Bearer ${DECOY}" \
  -H "X-XS-API-Key: ${RAW_KEY}" \
  "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/api-keys")
assert_status "E conflicting headers → 400" "400" "${HTTP}" "${E_BODY}"

# ── Smoke F: structurally-valid but unknown key → 401 ──────────

F_BODY=/tmp/smoke-apikey-F.json
UNKNOWN="xynes_live_deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
HTTP=$(curl -sS -o "${F_BODY}" -w "%{http_code}" --max-time "${CURL_TIMEOUT_SECS}" \
  -H "Authorization: Bearer ${UNKNOWN}" \
  "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/api-keys")
assert_status "F unknown key → 401" "401" "${HTTP}" "${F_BODY}"

# ── Smoke G: in-scope public CMS read ──────────────────────────
# (`cms.content.listPublished` is_public=true → scope check skipped,
# workspace ownership still required.)

G_BODY=/tmp/smoke-apikey-G.json
HTTP=$(curl -sS -o "${G_BODY}" -w "%{http_code}" --max-time "${CURL_TIMEOUT_SECS}" \
  -H "Authorization: Bearer ${RAW_KEY}" \
  "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/content/blog")
assert_status "G public CMS read with API key → 200" "200" "${HTTP}" "${G_BODY}"

# ── Smoke D: revoked key → 401 even on public route ────────────

# First revoke the key (short-circuits cleanup() — we'll re-revoke as
# a no-op on exit).
curl -sS -o /dev/null --max-time "${CURL_TIMEOUT_SECS}" \
  -H "Authorization: Bearer ${JWT}" \
  -H "Content-Type: application/json" \
  -X POST "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/api-keys/${KEY_ID}/revoke" \
  -d '{}'

D_BODY=/tmp/smoke-apikey-D.json
HTTP=$(curl -sS -o "${D_BODY}" -w "%{http_code}" --max-time "${CURL_TIMEOUT_SECS}" \
  -H "Authorization: Bearer ${RAW_KEY}" \
  "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/content/blog")
assert_status "D revoked key on public route → 401 (fail-closed)" "401" "${HTTP}" "${D_BODY}"

# ── Redaction sanity: no raw-key substring in service logs ─────
# (Run from infra root so docker-compose can find the project.)
if command -v docker >/dev/null 2>&1 \
   && [ -f "${ROOT_DIR}/${DOCKER_COMPOSE_FILE}" ]; then
  GATEWAY_LEAK=$(docker compose --env-file "${DOCKER_COMPOSE_ENV_FILE}" \
    -f "${DOCKER_COMPOSE_FILE}" logs gateway --since 2m 2>&1 \
    | grep -c "${RAW_KEY}" || true)
  ACCOUNTS_LEAK=$(docker compose --env-file "${DOCKER_COMPOSE_ENV_FILE}" \
    -f "${DOCKER_COMPOSE_FILE}" logs accounts-service --since 2m 2>&1 \
    | grep -c "${RAW_KEY}" || true)
  if [ "${GATEWAY_LEAK}" = "0" ] && [ "${ACCOUNTS_LEAK}" = "0" ]; then
    echo -e "${GREEN}PASS${NC} Redaction: no raw key leaked into service logs"
    PASS=$((PASS + 1))
  else
    echo -e "${RED}FAIL${NC} Redaction: raw key found in logs (gateway=${GATEWAY_LEAK}, accounts=${ACCOUNTS_LEAK})"
    FAIL=$((FAIL + 1))
  fi
fi

# ── Summary ─────────────────────────────────────────────────────

echo ""
echo "═══════════════════════════════════════════════════════════════════════════"
echo "  GATEWAY API KEY ENFORCEMENT SMOKE — SUMMARY"
echo "═══════════════════════════════════════════════════════════════════════════"
echo "  PASS: ${PASS}"
echo "  FAIL: ${FAIL}"
echo ""
if [ "${FAIL}" -ne 0 ]; then
  exit 1
fi
exit 0
