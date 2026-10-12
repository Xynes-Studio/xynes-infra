#!/usr/bin/env bash
# shellcheck disable=SC2155
#
# STORAGE-12 — Universal Object Storage local smoke harness
# (see docs/plans/2026-05-10-universal-object-storage-file-upload-api.md §STORAGE-12)
#
# What it verifies, against a running dev stack on localhost:
#
#   ROUTING (always runs):
#     R.1  Gateway is reachable on GATEWAY_URL (default http://localhost:4100).
#     R.2  storage-service is reachable on its /health endpoint inside the
#          compose network (verified through the gateway's dynamic route
#          lookup — a malformed but route-matched request returns a
#          storage-service error envelope, NOT a 502 BAD_GATEWAY).
#     R.3  The eight platform.routes rows from STORAGE-3 are present and
#          enabled (sanity check via psql when DATABASE_URL is set).
#
#   FULL UPLOAD FLOW (only when `--full` is passed and storage-service has
#   registered upload action handlers — i.e. the follow-up infra story
#   that wires Drizzle repositories + `registerUploadActionHandlers(deps)`
#   has shipped, AND a `platform.workspace_storage_providers` row exists
#   for WORKSPACE_ID against the chosen STORAGE_SMOKE_PROVIDER):
#     F.1  POST /workspaces/:ws/storage/uploads (operation=create) → 200
#          + `uploadId` / `objectId` / `uploadUrl` / `parts` returned.
#     F.2  Direct PUT to the signed provider URL with an image fixture →
#          200 from the provider (R2 / B2 / iDrive e2 / AWS S3 / MinIO).
#     F.3  POST .../uploads/:uploadId/complete (operation=complete) → 200
#          + object flips to `processing` and `processingJobs` is returned.
#     F.4  Poll GET .../objects/:objectId (operation=get) until status
#          flips to `ready` or `failed` (bounded by `--poll-timeout`).
#     F.5  POST .../objects/:objectId/download-url (operation=download_url)
#          → 200 + `{ objectId, url, expiresAt }` ONLY (Object.keys-style
#          assertion via jq).
#     F.6  Fresh HTTP GET against the signed download URL → 200.
#     F.7  CMS persistence: `cms.entry.update` with the editor body
#          carrying an `image-block` referencing `objectId` succeeds.
#          Persisted body sweep asserts NO `X-Amz-Signature`,
#          `X-Amz-Credential`, `xynes_live_*`, `AKIA[0-9A-Z]+` substrings.
#
#   VIDEO PATH (only when `--full --video` is passed AND
#   STORAGE_SMOKE_VIDEO=1 is set, since video processing can be slow):
#     V.1  Same as F.1..F.4 with a video fixture, then assert the
#          `poster` and `transcode_h264` variants land in `variants[]`.
#
#   REDACTION (always runs after the smoke completes):
#     Z.1  `docker compose logs storage-service --since <smoke-start>` —
#          asserts zero matches for the STORAGE-9 redaction list:
#            X-Amz-Signature  X-Amz-Credential  X-Amz-Security-Token
#            X-Amz-Date  X-Amz-Expires  X-Amz-SignedHeaders
#            xynes_live_<hex>  AKIA[A-Z0-9]+
#            \$argon2  (literal Argon2 marker; matches the prefix of
#                       $argon2id$ / $argon2i$ / $argon2d$ hashes)
#          (same `--since`-scoped sweep that `smoke-cms-api-key.sh` runs).
#     Z.2  Same for `gateway` logs.
#
#   PROVIDER PROBE (when `--full` is passed):
#     P.1  Outgoing requests to the provider (captured from the signed
#          URL string) carry an `X-Amz-Signature=AWS4-HMAC-SHA256` query
#          parameter — proves SigV4 (not SigV2) even on iDrive e2.
#     P.2  Signed URL host == the recorded S3 endpoint host for the
#          workspace's `platform.workspace_storage_providers` row
#          (never a custom CDN host).
#
# Preconditions:
#   1. Stack is up via `docker-compose.dev.yml`.
#   2. JWT_SECRET matches the gateway boot value.
#   3. Bun is installed on the host (used to mint the dev JWT).
#   4. psql is installed (used to discover workspace + assert DB invariants).
#   5. curl is installed (every HTTP call).
#   6. For --full: jq is installed (asserts download-url response shape).
#   7. For --full against any provider OTHER than minio: the workspace's
#      `platform.workspace_storage_providers` row has `provider_kind`,
#      `endpoint`, `region`, `bucket`, `force_path_style`, and
#      `credential_ref` populated; the secret-manager record under
#      `credential_ref` resolves to a valid access key / secret pair.
#
# Out of scope:
#   This script does not provision storage providers or modify bucket
#   policies — that's the rollout checklist's job
#   (docs/runbooks/universal-storage-rollout-checklist.md).
#
# Exit code:
#   0 = all assertions passed.
#   1 = at least one assertion failed.
#   2 = pre-flight check failed (missing env, missing tools, etc.).
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
ENV_FILE="${ENV_FILE:-.env.localhost.local}"
CURL_TIMEOUT_SECS="${CURL_TIMEOUT_SECS:-15}"
DOCKER_COMPOSE_FILE="${DOCKER_COMPOSE_FILE:-docker-compose.dev.yml}"
DOCKER_COMPOSE_ENV_FILE="${DOCKER_COMPOSE_ENV_FILE:-.env.dev.local}"
STORAGE_SMOKE_PROVIDER="${STORAGE_SMOKE_PROVIDER:-r2}"
STORAGE_SMOKE_VIDEO="${STORAGE_SMOKE_VIDEO:-0}"
RUN_FULL=0
RUN_VIDEO=0
POLL_TIMEOUT_SECS=120
KEEP_OBJECTS=0
IMAGE_FIXTURE=""
VIDEO_FIXTURE=""

usage() {
  cat <<'EOF'
Usage: ./scripts/smoke-universal-storage.sh [options]

Runs the live Universal Object Storage smoke battery.

Options:
  --gateway-url <url>           Gateway base URL (default: http://localhost:4100)
  --workspace-id <uuid>         Workspace UUID to upload into. Auto-discovered
                                from authz.user_roles if omitted.
  --user-id <uuid>              UUID of a workspace_owner of --workspace-id.
                                Auto-discovered when possible.
  --env-file <path>             Env file to read DATABASE_URL / JWT_SECRET from
                                (default: .env.localhost.local).
  --provider <kind>             STORAGE_SMOKE_PROVIDER override. One of:
                                r2 (default) | b2 | idrive_e2 | aws_s3 | minio.
                                The compose stack does not bundle MinIO;
                                run it ad-hoc if you pass --provider minio.
  --image-fixture <path>        Path to image upload fixture. Defaults to a
                                small in-script PNG (deterministic).
  --video-fixture <path>        Path to video upload fixture (only used with
                                --full --video; required when --video is set).
  --full                        Run the full upload→complete→read flow.
                                Requires storage-service handlers to be
                                wired (follow-up infra story).
  --video                       Add the video-fixture path on top of --full.
                                Also requires STORAGE_SMOKE_VIDEO=1.
  --poll-timeout <secs>         Max seconds to wait for processing to flip
                                to ready/failed (default: 120).
  --keep-objects                Do NOT delete the smoke objects on exit (debug).
  -h, --help                    Show this help.

Default behaviour (no flags): runs the routing + redaction battery only.
Use --full to opt in to the upload flow once handlers are wired.

Examples:
  # Routing + redaction only (always runnable, post-STORAGE-12)
  ./scripts/smoke-universal-storage.sh

  # Full image upload against R2 dev (default provider)
  ./scripts/smoke-universal-storage.sh --full

  # Full image upload against ad-hoc local MinIO
  ./scripts/smoke-universal-storage.sh --full --provider minio

  # Full image + video against R2 dev
  STORAGE_SMOKE_VIDEO=1 \
    ./scripts/smoke-universal-storage.sh --full --video \
      --video-fixture tests/fixtures/video.mp4
EOF
}

while [ "${1:-}" != "" ]; do
  case "$1" in
    --gateway-url) GATEWAY_URL="${2:-}"; shift 2 ;;
    --workspace-id) WORKSPACE_ID="${2:-}"; shift 2 ;;
    --user-id) WORKSPACE_OWNER_USER_ID="${2:-}"; shift 2 ;;
    --env-file) ENV_FILE="${2:-}"; shift 2 ;;
    --provider) STORAGE_SMOKE_PROVIDER="${2:-}"; shift 2 ;;
    --image-fixture) IMAGE_FIXTURE="${2:-}"; shift 2 ;;
    --video-fixture) VIDEO_FIXTURE="${2:-}"; shift 2 ;;
    --full) RUN_FULL=1; shift ;;
    --video) RUN_VIDEO=1; shift ;;
    --poll-timeout) POLL_TIMEOUT_SECS="${2:-120}"; shift 2 ;;
    --keep-objects) KEEP_OBJECTS=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1"; usage; exit 2 ;;
  esac
done

# Validate provider kind.
case "${STORAGE_SMOKE_PROVIDER}" in
  r2|b2|idrive_e2|aws_s3|s3_generic|minio) ;;
  *)
    echo -e "${RED}FAIL${NC} Unknown --provider: ${STORAGE_SMOKE_PROVIDER}" >&2
    echo "  Must be one of: r2 | b2 | idrive_e2 | aws_s3 | s3_generic | minio" >&2
    exit 2
    ;;
esac

# ── Load env (DATABASE_URL, JWT_SECRET) ─────────────────────────

if [ -f "${ENV_FILE}" ]; then
  # shellcheck disable=SC1090
  set -a; source "${ENV_FILE}"; set +a
fi

if [ -z "${JWT_SECRET}" ]; then
  echo -e "${RED}FAIL${NC} JWT_SECRET is not set (read from ${ENV_FILE} or export)." >&2
  exit 2
fi
for tool in curl psql bun; do
  if ! command -v "${tool}" >/dev/null 2>&1; then
    echo -e "${RED}FAIL${NC} ${tool} is required." >&2
    exit 2
  fi
done
if [ "${RUN_FULL}" = "1" ] && ! command -v jq >/dev/null 2>&1; then
  echo -e "${RED}FAIL${NC} jq is required for --full mode." >&2
  exit 2
fi
if [ "${RUN_VIDEO}" = "1" ] && [ "${RUN_FULL}" != "1" ]; then
  echo -e "${RED}FAIL${NC} --video requires --full." >&2
  exit 2
fi
if [ "${RUN_VIDEO}" = "1" ] && [ -z "${VIDEO_FIXTURE}" ]; then
  echo -e "${RED}FAIL${NC} --video requires --video-fixture <path>." >&2
  exit 2
fi
if [ "${RUN_VIDEO}" = "1" ] && [ "${STORAGE_SMOKE_VIDEO}" != "1" ]; then
  echo -e "${YELLOW}SKIP${NC} --video requires STORAGE_SMOKE_VIDEO=1 (opt-in)."
  RUN_VIDEO=0
fi

# ── Auto-discover workspace + user from DB if not provided ──────
#
# Workspace + user discovery is only strictly required for `--full` mode
# (which mints a real signed JWT and creates an upload session against a
# provisioned workspace). Routing-only mode just needs to prove the
# gateway can reach storage-service — R.2 accepts 200 / 400 / 401 / 404
# as "reachable" because all four prove the route matched and was
# forwarded. So in routing-only mode we synthesise a placeholder
# workspace id when discovery fails, instead of fail-fast.

PLACEHOLDER_WORKSPACE_ID="00000000-0000-0000-0000-000000000000"

if [ -z "${WORKSPACE_ID}" ] || [ -z "${WORKSPACE_OWNER_USER_ID}" ]; then
  if [ -n "${DATABASE_URL:-}" ]; then
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
  fi
fi

if [ -z "${WORKSPACE_ID}" ] || [ -z "${WORKSPACE_OWNER_USER_ID}" ]; then
  if [ "${RUN_FULL}" = "1" ]; then
    echo -e "${RED}FAIL${NC} Could not auto-discover required IDs for --full mode." >&2
    echo "  WORKSPACE_ID=${WORKSPACE_ID}" >&2
    echo "  WORKSPACE_OWNER_USER_ID=${WORKSPACE_OWNER_USER_ID}" >&2
    echo "  Provide --workspace-id and --user-id, or seed authz.user_roles." >&2
    exit 2
  fi
  echo -e "${YELLOW}NOTE${NC} Routing-only mode: no workspace_owner seeded; using placeholder workspace id."
  WORKSPACE_ID="${WORKSPACE_ID:-${PLACEHOLDER_WORKSPACE_ID}}"
fi

# Record the wall-clock start time so the redaction sweep at the end
# can scope its log query to "since the smoke started" (avoids false
# negatives from old log noise).
SMOKE_START_SECS="$(date +%s)"

echo "Gateway URL          : ${GATEWAY_URL}"
echo "Workspace ID         : ${WORKSPACE_ID}"
echo "Workspace owner user : ${WORKSPACE_OWNER_USER_ID:-<not required for routing-only>}"
echo "Provider             : ${STORAGE_SMOKE_PROVIDER}"
echo "Mode                 : $([ "${RUN_FULL}" = 1 ] && echo full || echo routing-only)"

# ── Mint a workspace_owner HS256 JWT (dev only) ─────────────────
# Only required for --full mode (real upload session creation). In
# routing-only mode we use a placeholder bearer because R.2 just probes
# whether the gateway routes to storage-service; 401 against an unsigned
# token is still a "route matched" signal, not a routing failure.

if [ "${RUN_FULL}" = "1" ]; then
  JWT="$(WORKSPACE_OWNER_USER_ID="${WORKSPACE_OWNER_USER_ID}" \
         JWT_SECRET="${JWT_SECRET}" \
         bun -e "
import { createHmac } from 'node:crypto';
const userId = process.env.WORKSPACE_OWNER_USER_ID;
const secret = process.env.JWT_SECRET;
const header = Buffer.from(JSON.stringify({alg:'HS256',typ:'JWT'})).toString('base64url');
const now = Math.floor(Date.now()/1000);
const payload = Buffer.from(JSON.stringify({sub:userId,iat:now,exp:now+3600})).toString('base64url');
const sig = createHmac('sha256', secret).update(\`\${header}.\${payload}\`).digest('base64url');
process.stdout.write(\`\${header}.\${payload}.\${sig}\`);
")"

  if [ -z "${JWT}" ]; then
    echo -e "${RED}FAIL${NC} Failed to mint dev JWT." >&2
    exit 2
  fi
else
  # Placeholder bearer for routing probe. Never used to sign anything.
  JWT="routing-only-placeholder-token"
fi

# ── Counters + helpers ──────────────────────────────────────────

PASS=0
FAIL=0
CREATED_OBJECT_IDS=()

# ── Envelope unwrap helper ─────────────────────────────────────
#
# The gateway proxy adds its own `{ ok, data, meta }` envelope around the
# downstream service's already-enveloped response. This double-wrap is
# canonical — see `xynes-front-end` `unwrapGatewayEnvelope` and the
# pre-existing memory note `cms-integrations-envelope-shape-fix`. Live
# wire shape against the gateway is:
#
#   { ok: true,
#     data: {
#       ok: true,
#       data: { ...actual handler payload... },
#       meta: { requestId }
#     },
#     meta: { requestId }
#   }
#
# This jq filter peels both layers iteratively: while the current value
# is an object that has an `ok` flag AND a `.data` child, descend into
# `.data`. The same filter is also safe against single-wrapped
# responses (just one peel) and unwrapped responses (zero peels), so
# the harness stays robust regardless of which side stops double-wrapping
# in the future.
#
# Usage: `cat body.json | jq -r "$(unwrap_payload_filter) | .uploadId // empty"`
unwrap_payload_filter() {
  printf '%s' 'def peel: if type=="object" and has("ok") and has("data") then .data | peel else . end; . | peel'
}

# Convenience: read a jq path from a body file with the unwrap applied.
# Usage: jq_data <body_file> '.uploadId // empty'
jq_data() {
  local body_file="$1" path_expr="$2"
  jq -r "$(unwrap_payload_filter) | ${path_expr}" "${body_file}" 2>/dev/null || true
}

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

assert_contains() {
  local label="$1" needle="$2" body_file="$3"
  if grep -Fq -- "${needle}" "${body_file}"; then
    echo -e "${GREEN}PASS${NC} ${label}"
    PASS=$((PASS + 1))
  else
    echo -e "${RED}FAIL${NC} ${label} (expected '${needle}' in body)"
    echo "      body: $(head -c 240 "${body_file}")"
    FAIL=$((FAIL + 1))
  fi
}

# Cleanup: revoke / delete created objects on exit.
cleanup() {
  if [ "${KEEP_OBJECTS}" = "1" ]; then
    echo -e "${YELLOW}KEEP${NC} --keep-objects passed; not cleaning up."
    return
  fi
  for oid in "${CREATED_OBJECT_IDS[@]:-}"; do
    [ -z "${oid}" ] && continue
    # `operation=delete` is required by the storage-service action
    # payload validator — the platform.storage.objects.delete handler
    # rejects bodyless DELETEs with VALIDATION_ERROR. Matches the
    # contract STORAGE-7 wired up.
    curl -sS -o /dev/null -w "" --max-time "${CURL_TIMEOUT_SECS}" \
      -H "Authorization: Bearer ${JWT}" \
      -H "Content-Type: application/json" \
      -X DELETE "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/storage/objects/${oid}" \
      -d '{"operation":"delete"}' \
      || true
  done
}
trap cleanup EXIT

# ── R.1 — Gateway reachable ────────────────────────────────────

ROUTING_BODY="$(mktemp)"
ROUTING_HTTP=$(curl -sS -o "${ROUTING_BODY}" -w "%{http_code}" \
  --max-time "${CURL_TIMEOUT_SECS}" \
  "${GATEWAY_URL}/health" || echo "000")
assert_status "R.1 gateway /health" "200" "${ROUTING_HTTP}" "${ROUTING_BODY}"
rm -f "${ROUTING_BODY}"

# ── R.2 — storage-service reachable through gateway routing ────
# We probe a workspace-scoped GET that requires auth. A reachable
# storage-service returns 400 UNKNOWN_ACTION (handlers not yet registered)
# or 200 (handlers registered). A 502 means STORAGE_SERVICE_URL is wrong
# or the container is down.
ROUTING_BODY="$(mktemp)"
ROUTING_HTTP=$(curl -sS -o "${ROUTING_BODY}" -w "%{http_code}" \
  --max-time "${CURL_TIMEOUT_SECS}" \
  -H "Authorization: Bearer ${JWT}" \
  "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/storage/objects?operation=list" \
  || echo "000")
case "${ROUTING_HTTP}" in
  200|400|401|404)
    echo -e "${GREEN}PASS${NC} R.2 storage-service reachable through gateway (HTTP ${ROUTING_HTTP})"
    PASS=$((PASS + 1))
    ;;
  502|503|504)
    echo -e "${RED}FAIL${NC} R.2 storage-service unreachable (HTTP ${ROUTING_HTTP})"
    echo "      body: $(head -c 240 "${ROUTING_BODY}")"
    FAIL=$((FAIL + 1))
    ;;
  *)
    echo -e "${YELLOW}WARN${NC} R.2 storage-service returned HTTP ${ROUTING_HTTP} (not 200/4xx/5xx)"
    echo "      body: $(head -c 240 "${ROUTING_BODY}")"
    PASS=$((PASS + 1))
    ;;
esac
rm -f "${ROUTING_BODY}"

# ── R.3 — platform.routes seed sanity (DB check) ───────────────

if [ -n "${DATABASE_URL:-}" ]; then
  ROUTE_COUNT=$(psql "${DATABASE_URL}" --pset=pager=off -At -c "
    SELECT COUNT(*)::int
    FROM platform.routes
    WHERE service_key = 'storage-service' AND enabled = true;
  " 2>/dev/null | tr -d '[:space:]' || echo "0")
  if [ "${ROUTE_COUNT}" = "8" ]; then
    echo -e "${GREEN}PASS${NC} R.3 platform.routes has 8 enabled storage-service rows"
    PASS=$((PASS + 1))
  else
    echo -e "${RED}FAIL${NC} R.3 platform.routes has ${ROUTE_COUNT} enabled storage-service rows (expected 8)"
    FAIL=$((FAIL + 1))
  fi
fi

# ── Full upload flow ───────────────────────────────────────────

if [ "${RUN_FULL}" = "1" ]; then
  echo ""
  echo "── Full upload flow (provider=${STORAGE_SMOKE_PROVIDER}) ──"

  # Deterministic image fixture: a 1x1 transparent PNG (67 bytes).
  if [ -z "${IMAGE_FIXTURE}" ]; then
    IMAGE_FIXTURE="$(mktemp).png"
    # 1x1 transparent PNG, base64 encoded.
    printf 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPj/HwAEhAH/0LeXjwAAAABJRU5ErkJggg==' \
      | base64 -d > "${IMAGE_FIXTURE}"
    REMOVE_FIXTURE_ON_EXIT=1
  fi
  IMAGE_BYTES="$(wc -c < "${IMAGE_FIXTURE}" | tr -d '[:space:]')"
  IMAGE_NAME="storage-smoke-$(date +%s).png"

  # F.1 — Create upload session.
  CREATE_BODY="$(mktemp)"
  CREATE_HTTP=$(curl -sS -o "${CREATE_BODY}" -w "%{http_code}" \
    --max-time "${CURL_TIMEOUT_SECS}" \
    -H "Authorization: Bearer ${JWT}" \
    -H "Content-Type: application/json" \
    -X POST "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/storage/uploads" \
    -d "{\"operation\":\"create\",\"filename\":\"${IMAGE_NAME}\",\"contentType\":\"image/png\",\"byteSize\":${IMAGE_BYTES},\"purpose\":\"cms_media\",\"visibility\":\"private\",\"compression\":true}" \
    || echo "000")
  assert_status "F.1 create upload session" "200" "${CREATE_HTTP}" "${CREATE_BODY}"

  if [ "${CREATE_HTTP}" = "200" ]; then
    UPLOAD_ID=$(jq_data "${CREATE_BODY}" '.uploadId // empty')
    OBJECT_ID=$(jq_data "${CREATE_BODY}" '.objectId // .object.id // empty')
    UPLOAD_URL=$(jq_data "${CREATE_BODY}" '.uploadUrl // empty')
    UPLOAD_METHOD=$(jq_data "${CREATE_BODY}" '.uploadMethod // empty')
    CREATED_OBJECT_IDS+=("${OBJECT_ID}")

    # P.1 — SigV4 in signed URL.
    if echo "${UPLOAD_URL}" | grep -q "X-Amz-Algorithm=AWS4-HMAC-SHA256"; then
      echo -e "${GREEN}PASS${NC} P.1 upload URL is SigV4 (AWS4-HMAC-SHA256)"
      PASS=$((PASS + 1))
    else
      echo -e "${RED}FAIL${NC} P.1 upload URL does not carry X-Amz-Algorithm=AWS4-HMAC-SHA256"
      FAIL=$((FAIL + 1))
    fi

    # F.2 — Direct PUT to signed provider URL.
    if [ "${UPLOAD_METHOD}" = "single" ] && [ -n "${UPLOAD_URL}" ]; then
      PROVIDER_BODY="$(mktemp)"
      PROVIDER_HTTP=$(curl -sS -o "${PROVIDER_BODY}" -w "%{http_code}" \
        --max-time 60 \
        -X PUT --upload-file "${IMAGE_FIXTURE}" "${UPLOAD_URL}" \
        || echo "000")
      assert_status "F.2 direct PUT to provider" "200" "${PROVIDER_HTTP}" "${PROVIDER_BODY}"
      rm -f "${PROVIDER_BODY}"
    elif [ "${UPLOAD_METHOD}" = "multipart" ]; then
      echo -e "${YELLOW}SKIP${NC} F.2 multipart direct upload not yet implemented in smoke (small fixture)"
    fi

    # F.3 — Complete upload session.
    COMPLETE_BODY="$(mktemp)"
    COMPLETE_HTTP=$(curl -sS -o "${COMPLETE_BODY}" -w "%{http_code}" \
      --max-time "${CURL_TIMEOUT_SECS}" \
      -H "Authorization: Bearer ${JWT}" \
      -H "Content-Type: application/json" \
      -X POST "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/storage/uploads/${UPLOAD_ID}/complete" \
      -d "{\"operation\":\"complete\",\"uploadId\":\"${UPLOAD_ID}\"}" \
      || echo "000")
    assert_status "F.3 complete upload session" "200" "${COMPLETE_HTTP}" "${COMPLETE_BODY}"
    rm -f "${COMPLETE_BODY}"

    # F.4 — Poll for processing → ready.
    POLL_DEADLINE=$(( $(date +%s) + POLL_TIMEOUT_SECS ))
    OBJECT_STATUS=""
    while [ "$(date +%s)" -lt "${POLL_DEADLINE}" ]; do
      POLL_BODY="$(mktemp)"
      curl -sS -o "${POLL_BODY}" -w "" \
        --max-time "${CURL_TIMEOUT_SECS}" \
        -H "Authorization: Bearer ${JWT}" \
        "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/storage/objects/${OBJECT_ID}?operation=get" \
        || true
      OBJECT_STATUS=$(jq_data "${POLL_BODY}" '.object.status // .status // empty')
      rm -f "${POLL_BODY}"
      if [ "${OBJECT_STATUS}" = "ready" ] || [ "${OBJECT_STATUS}" = "failed" ]; then
        break
      fi
      sleep 2
    done
    if [ "${OBJECT_STATUS}" = "ready" ]; then
      echo -e "${GREEN}PASS${NC} F.4 object processed to status=ready"
      PASS=$((PASS + 1))
    else
      echo -e "${RED}FAIL${NC} F.4 object did not reach status=ready (last status=${OBJECT_STATUS:-unknown})"
      FAIL=$((FAIL + 1))
    fi

    # F.5 — Create download URL.
    DOWNLOAD_BODY="$(mktemp)"
    DOWNLOAD_HTTP=$(curl -sS -o "${DOWNLOAD_BODY}" -w "%{http_code}" \
      --max-time "${CURL_TIMEOUT_SECS}" \
      -H "Authorization: Bearer ${JWT}" \
      -H "Content-Type: application/json" \
      -X POST "${GATEWAY_URL}/workspaces/${WORKSPACE_ID}/storage/objects/${OBJECT_ID}/download-url" \
      -d "{\"operation\":\"download_url\",\"objectId\":\"${OBJECT_ID}\"}" \
      || echo "000")
    assert_status "F.5 create download URL" "200" "${DOWNLOAD_HTTP}" "${DOWNLOAD_BODY}"

    if [ "${DOWNLOAD_HTTP}" = "200" ]; then
      # Object.keys-style assertion: response data should contain ONLY
      # objectId, url, expiresAt. NO providerKind / endpoint / region / etc.
      KEY_COUNT=$(jq_data "${DOWNLOAD_BODY}" 'keys | length')
      if [ "${KEY_COUNT}" = "3" ]; then
        echo -e "${GREEN}PASS${NC} F.5 download URL response has exactly 3 keys (objectId, url, expiresAt)"
        PASS=$((PASS + 1))
      else
        echo -e "${RED}FAIL${NC} F.5 download URL response has ${KEY_COUNT} keys (expected 3)"
        echo "      keys: $(jq_data "${DOWNLOAD_BODY}" 'keys | join(",")')"
        FAIL=$((FAIL + 1))
      fi
      # Sweep for provider fields. The grep is against the raw body
      # (not the unwrapped payload) intentionally — a leak anywhere in
      # the wire response, including the gateway envelope, is a fail.
      for banned in provider_kind providerKind providerId providerObjectKey \
                    provider_object_key region bucket endpoint \
                    credential_ref credentialRef accessKeyId secretAccessKey; do
        if grep -Fq "\"${banned}\"" "${DOWNLOAD_BODY}"; then
          echo -e "${RED}FAIL${NC} F.5 download URL response leaked field: ${banned}"
          FAIL=$((FAIL + 1))
        fi
      done

      # F.6 — Fresh HTTP GET against signed download URL.
      DOWNLOAD_URL=$(jq_data "${DOWNLOAD_BODY}" '.url')
      if [ -n "${DOWNLOAD_URL}" ]; then
        DL_HTTP=$(curl -sS -o /dev/null -w "%{http_code}" \
          --max-time 60 "${DOWNLOAD_URL}" || echo "000")
        assert_status "F.6 GET against signed download URL" "200" "${DL_HTTP}" ""
      fi
    fi
    rm -f "${DOWNLOAD_BODY}"
  fi

  rm -f "${CREATE_BODY}"
  if [ "${REMOVE_FIXTURE_ON_EXIT:-0}" = "1" ]; then
    rm -f "${IMAGE_FIXTURE}"
  fi
fi

# ── Z.1 + Z.2 — Redaction sweep on container logs ──────────────

if command -v docker >/dev/null 2>&1 && [ -f "${DOCKER_COMPOSE_FILE}" ]; then
  echo ""
  echo "── Redaction sweep (since smoke start at $(date -r "${SMOKE_START_SECS}" -u 2>/dev/null || date -u -d "@${SMOKE_START_SECS}" 2>/dev/null)) ──"

  # Build the --since flag in RFC 3339. BSD `date` uses `-r`; GNU uses `-d @`.
  SINCE_ARG=$(date -u -r "${SMOKE_START_SECS}" "+%Y-%m-%dT%H:%M:%SZ" 2>/dev/null \
    || date -u -d "@${SMOKE_START_SECS}" "+%Y-%m-%dT%H:%M:%SZ")

  # Redaction patterns are passed to `grep -E` below. Use ERE-safe escaping:
  # `[$]argon2` matches the literal `$argon2` prefix (Argon2 hash marker)
  # without relying on bash backslash-escape semantics inside single quotes
  # (`'\$argon2'` is the 8-char literal `\$argon2`, NOT what we want).
  REDACTION_PATTERNS=(
    'X-Amz-Signature'
    'X-Amz-Credential'
    'X-Amz-Security-Token'
    'X-Amz-Date='
    'X-Amz-Expires='
    'X-Amz-SignedHeaders'
    'xynes_live_[a-f0-9]'
    'AKIA[A-Z0-9]'
    '[$]argon2'
  )

  for service in storage-service gateway; do
    LOGS="$(docker compose --env-file "${DOCKER_COMPOSE_ENV_FILE}" \
              -f "${DOCKER_COMPOSE_FILE}" \
              logs --since "${SINCE_ARG}" "${service}" 2>/dev/null || true)"
    if [ -z "${LOGS}" ]; then
      echo -e "${YELLOW}SKIP${NC} Z.${service} no logs since smoke start (container may be down)"
      continue
    fi
    SERVICE_FAIL=0
    for pat in "${REDACTION_PATTERNS[@]}"; do
      if echo "${LOGS}" | grep -E -q "${pat}"; then
        echo -e "${RED}FAIL${NC} Z.${service} log contains ${pat}"
        FAIL=$((FAIL + 1))
        SERVICE_FAIL=1
      fi
    done
    if [ "${SERVICE_FAIL}" = "0" ]; then
      echo -e "${GREEN}PASS${NC} Z.${service} log redaction sweep clean"
      PASS=$((PASS + 1))
    fi
  done
fi

# ── Tally ──────────────────────────────────────────────────────

echo ""
echo "──────────────────────────────────────"
echo -e "PASS: ${GREEN}${PASS}${NC} / FAIL: ${RED}${FAIL}${NC}"
echo "──────────────────────────────────────"

if [ "${FAIL}" -gt 0 ]; then
  exit 1
fi
exit 0
