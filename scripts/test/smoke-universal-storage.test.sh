#!/usr/bin/env bash
set -euo pipefail

# STORAGE-12 — Static / shape tests for the Universal Object Storage smoke
# harness. Mirrors the pattern used by `scripts/test/smoke-cms-api-key.test.sh`:
# exercises the script's `--help` path, fast-fail behaviour when required
# env is missing, and asserts the script covers the action keys / route
# patterns / redaction patterns that the STORAGE-12 plan requires.
#
# NOTE: This test never reaches the network — it only validates the
# script's static surface. The live battery is exercised by running the
# script itself against a real local stack (see plan §STORAGE-12).

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

SMOKE_SCRIPT="./scripts/smoke-universal-storage.sh"
CLEANUP_SCRIPT="./scripts/cleanup-universal-storage.sh"
COMPOSE_FILE="./docker-compose.dev.yml"
ENV_DEV="./.env.dev"
ENV_DEV_LOCAL="./.env.dev.local"
ROLLOUT_DOC="./docs/runbooks/universal-storage-rollout-checklist.md"

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

assert_contains() {
  local haystack="$1"
  local needle="$2"
  # Use `grep -- <pattern>` so needles that start with `-` (e.g.
  # `--workspace-id`) aren't parsed as grep flags by macOS grep.
  echo "$haystack" | grep -q -- "$needle" \
    || fail "expected output to contain: ${needle}"
}

assert_file_contains() {
  local file="$1"
  local needle="$2"
  grep -Fq -- "$needle" "$file" \
    || fail "expected ${file} to contain: ${needle}"
}

# ─────────────────────────────────────────────────────────────────────
# 1) Both scripts exist and are executable.
# ─────────────────────────────────────────────────────────────────────
[ -x "${SMOKE_SCRIPT}" ] || fail "${SMOKE_SCRIPT} is not executable"
[ -x "${CLEANUP_SCRIPT}" ] || fail "${CLEANUP_SCRIPT} is not executable"

# ─────────────────────────────────────────────────────────────────────
# 2) --help should succeed and mention the documented flags + presets.
# ─────────────────────────────────────────────────────────────────────
SMOKE_HELP="$(${SMOKE_SCRIPT} --help)"
assert_contains "$SMOKE_HELP" "smoke-universal-storage.sh"
assert_contains "$SMOKE_HELP" "--workspace-id"
assert_contains "$SMOKE_HELP" "--user-id"
assert_contains "$SMOKE_HELP" "--provider"
assert_contains "$SMOKE_HELP" "--full"
assert_contains "$SMOKE_HELP" "--video"
assert_contains "$SMOKE_HELP" "--image-fixture"
assert_contains "$SMOKE_HELP" "--video-fixture"
assert_contains "$SMOKE_HELP" "--poll-timeout"
assert_contains "$SMOKE_HELP" "--keep-objects"
assert_contains "$SMOKE_HELP" "STORAGE_SMOKE_PROVIDER"
# Provider list per plan §3 "MVP-ready alternate providers".
for p in r2 b2 idrive_e2 minio; do
  assert_contains "$SMOKE_HELP" "$p"
done

CLEANUP_HELP="$(${CLEANUP_SCRIPT} --help)"
assert_contains "$CLEANUP_HELP" "cleanup-universal-storage.sh"
assert_contains "$CLEANUP_HELP" "--workspace-id"
assert_contains "$CLEANUP_HELP" "--filename-pattern"
assert_contains "$CLEANUP_HELP" "--dry-run"

# ─────────────────────────────────────────────────────────────────────
# 3) Missing JWT_SECRET should fail fast with a clear error BEFORE any
#    network or DB activity.
# ─────────────────────────────────────────────────────────────────────
set +e
SMOKE_OUT="$(JWT_SECRET="" ENV_FILE="/tmp/does-not-exist-$(date +%s).env" \
       "${SMOKE_SCRIPT}" 2>&1)"
SMOKE_CODE=$?
set -e
[ "$SMOKE_CODE" -ne 0 ] || fail "expected missing JWT_SECRET to return non-zero"
assert_contains "$SMOKE_OUT" "JWT_SECRET is not set"

set +e
CLEANUP_OUT="$(JWT_SECRET="" ENV_FILE="/tmp/does-not-exist-$(date +%s).env" \
       "${CLEANUP_SCRIPT}" 2>&1)"
CLEANUP_CODE=$?
set -e
[ "$CLEANUP_CODE" -ne 0 ] || fail "expected missing JWT_SECRET to return non-zero (cleanup)"
assert_contains "$CLEANUP_OUT" "JWT_SECRET is not set"

# ─────────────────────────────────────────────────────────────────────
# 4) Smoke + cleanup scripts together must reference every action key +
#    path fragment the plan requires. The smoke script drives the
#    create/complete/get/download_url path; the cleanup script drives
#    the abort/delete path.
# ─────────────────────────────────────────────────────────────────────
COMBINED_CONTENT="$(cat "${SMOKE_SCRIPT}" "${CLEANUP_SCRIPT}")"
for fragment in \
  "/workspaces/" \
  "/storage/uploads" \
  "/storage/uploads/" \
  "/complete" \
  "/abort" \
  "/storage/objects" \
  "/download-url" \
  ; do
  if ! echo "$COMBINED_CONTENT" | grep -Fq -- "$fragment"; then
    fail "smoke + cleanup scripts do not reference: ${fragment}"
  fi
done

# Operation discriminators (STORAGE-10 contract). The smoke script
# alone must carry create/complete/download_url/get; the cleanup
# script alone must carry abort. We match the escaped form
# (\"operation\":\"...\") because that's what's inside the
# heredoc-free -d "..." curl args.
SMOKE_CONTENT="$(cat "${SMOKE_SCRIPT}")"
for op in 'operation\\":\\"create' 'operation\\":\\"complete' 'operation\\":\\"download_url' 'operation=get'; do
  if ! echo "$SMOKE_CONTENT" | grep -q -- "$op"; then
    fail "smoke script does not reference operation: ${op}"
  fi
done
CLEANUP_CONTENT="$(cat "${CLEANUP_SCRIPT}")"
if ! echo "$CLEANUP_CONTENT" | grep -q -- 'operation\\":\\"abort'; then
  fail "cleanup script does not reference operation: abort"
fi

# The remaining static checks scan SCRIPT_CONTENT (smoke only).
SCRIPT_CONTENT="${SMOKE_CONTENT}"

# ─────────────────────────────────────────────────────────────────────
# 5) Redaction sweep must cover all STORAGE-9 patterns. Same set the
#    storage-service redaction module guards against.
# ─────────────────────────────────────────────────────────────────────
for pat in \
  "X-Amz-Signature" \
  "X-Amz-Credential" \
  "X-Amz-Security-Token" \
  "X-Amz-Date" \
  "X-Amz-Expires" \
  "X-Amz-SignedHeaders" \
  "xynes_live_" \
  "AKIA" \
  "argon2"; do
  if ! echo "$SCRIPT_CONTENT" | grep -Fq -- "$pat"; then
    fail "smoke script does not sweep redaction pattern: ${pat}"
  fi
done

# Sweep must target BOTH storage-service AND gateway log streams.
assert_contains "$SCRIPT_CONTENT" "storage-service"
assert_contains "$SCRIPT_CONTENT" "gateway"

# ─────────────────────────────────────────────────────────────────────
# 6) Documented-keys-only sweep on the F.5 download-URL response.
#    Smoke must verify the response carries ONLY objectId, url, expiresAt
#    and reject every banned provider field.
# ─────────────────────────────────────────────────────────────────────
for banned in provider_kind providerKind providerId providerObjectKey \
              provider_object_key region bucket endpoint \
              credential_ref credentialRef accessKeyId secretAccessKey; do
  if ! echo "$SCRIPT_CONTENT" | grep -Fq "$banned"; then
    fail "smoke script does not sweep banned response field: ${banned}"
  fi
done

# ─────────────────────────────────────────────────────────────────────
# 7) SigV4 verification — P.1 must assert the upload URL is AWS4-HMAC-SHA256.
# ─────────────────────────────────────────────────────────────────────
assert_contains "$SCRIPT_CONTENT" "AWS4-HMAC-SHA256"

# ─────────────────────────────────────────────────────────────────────
# 8) docker-compose.dev.yml must wire the storage-service container.
# ─────────────────────────────────────────────────────────────────────
assert_file_contains "${COMPOSE_FILE}" "storage-service:"
assert_file_contains "${COMPOSE_FILE}" "../xynes-storage-service"
assert_file_contains "${COMPOSE_FILE}" "STORAGE_SERVICE_PORT"
assert_file_contains "${COMPOSE_FILE}" "storage-node_modules"

# Gateway must depend on storage-service so it boots after the container exists.
GATEWAY_DEPENDS=$(awk '/^  gateway:/,/^  [a-z][a-z]*-/' "${COMPOSE_FILE}")
if ! echo "$GATEWAY_DEPENDS" | grep -Fq -- "- storage-service"; then
  fail "gateway must depends_on storage-service in ${COMPOSE_FILE}"
fi

# ─────────────────────────────────────────────────────────────────────
# 9) Env files carry STORAGE_SERVICE_URL at the correct port (4204),
#    NOT the stale STORAGE-3 placeholder port (4500).
# ─────────────────────────────────────────────────────────────────────
for env_file in "${ENV_DEV}" "${ENV_DEV_LOCAL}"; do
  if [ -f "${env_file}" ]; then
    assert_file_contains "${env_file}" "STORAGE_SERVICE_URL=http://storage-service:4204"
    if grep -Fq "STORAGE_SERVICE_URL=http://storage-service:4500" "${env_file}"; then
      fail "${env_file} still contains the stale STORAGE-3 placeholder URL at port 4500"
    fi
    assert_file_contains "${env_file}" "STORAGE_SERVICE_PORT=4204"
  fi
done

# ─────────────────────────────────────────────────────────────────────
# 10) Rollout checklist doc exists and covers the required steps.
# ─────────────────────────────────────────────────────────────────────
[ -f "${ROLLOUT_DOC}" ] || fail "${ROLLOUT_DOC} does not exist"

for required_section in \
  "Bucket creation" \
  "Lifecycle policy" \
  "CORS policy" \
  "Credential reference" \
  "Provider health check" \
  "Worker concurrency" \
  "Telemetry redaction" \
  "SigV4-only" \
  "x-amz-tagging" \
  "iDrive e2" \
  "MinIO"; do
  assert_file_contains "${ROLLOUT_DOC}" "$required_section"
done

# ─────────────────────────────────────────────────────────────────────
# 11) Smoke and cleanup scripts both auto-discover workspace + user
#     from authz.user_roles (same posture as smoke-cms-api-key.sh).
# ─────────────────────────────────────────────────────────────────────
for script in "${SMOKE_SCRIPT}" "${CLEANUP_SCRIPT}"; do
  if ! grep -Fq "authz.user_roles" "${script}"; then
    fail "${script} does not auto-discover workspace from authz.user_roles"
  fi
done

echo "All STORAGE-12 static checks passed."
