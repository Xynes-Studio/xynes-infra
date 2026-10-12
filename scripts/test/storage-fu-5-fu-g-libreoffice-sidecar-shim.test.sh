#!/usr/bin/env bash
set -euo pipefail

# STORAGE-FU-5-FU-G — Static / shape tests for the LibreOffice sidecar
# Bun HTTP shim image (POST /convert + GET /health on TCP 8100).
#
# Source ownership:
#   - Dockerfile + Bun shim + tests live in
#     `xynes-storage-service/sidecars/libreoffice/` (storage-service repo).
#   - This validator + Compose overlay + K8s draft pin updates live in
#     `xynes-infra` (the repo policy is: no runnable code in infra; this
#     file is a pure static-shape check + image-pin assertion).
#
# Wired into the file-based test runner via the glob in
# `scripts/test/run.sh`.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# Sibling repo path. We assume the workspace layout
# `<workspace>/xynes/xynes-infra/` + `<workspace>/xynes/xynes-storage-service/`.
SIDECAR_DIR="../xynes-storage-service/sidecars/libreoffice"
COMPOSE_OVERLAY="./infra/compose/storage-live-processors.yml"
K8S_DEPLOYMENT="./infra/release/deployment-posture/k8s/10-libreoffice-sidecar.deployment.yaml"
PLAN_DOC="./docs/plans/archive/2026-05-27-storage-followups-combined.md"

# Pinned image tag — FU-G ships 0.1.0. Bump this constant when the
# sidecar image rotates.
PINNED_IMAGE="xynes/libreoffice-sidecar:0.1.0"
PLACEHOLDER_IMAGE="lscr.io/linuxserver/libreoffice"

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

assert_file_exists() {
  [ -f "$1" ] || fail "expected file missing: $1"
}

assert_dir_exists() {
  [ -d "$1" ] || fail "expected dir missing: $1"
}

assert_file_contains() {
  local file="$1"
  local needle="$2"
  grep -Fq -- "$needle" "$file" \
    || fail "expected ${file} to contain: ${needle}"
}

assert_file_not_contains() {
  local file="$1"
  local needle="$2"
  if grep -Fq -- "$needle" "$file"; then
    fail "${file} must NOT contain: ${needle}"
  fi
}

# ─────────────────────────────────────────────────────────────────────
# 1) Sidecar source tree exists in xynes-storage-service.
# ─────────────────────────────────────────────────────────────────────
assert_dir_exists "${SIDECAR_DIR}"
assert_file_exists "${SIDECAR_DIR}/Dockerfile"
assert_file_exists "${SIDECAR_DIR}/package.json"
assert_file_exists "${SIDECAR_DIR}/tsconfig.json"
assert_file_exists "${SIDECAR_DIR}/src/shim.ts"
assert_file_exists "${SIDECAR_DIR}/src/convert.ts"
assert_file_exists "${SIDECAR_DIR}/src/health.ts"
assert_file_exists "${SIDECAR_DIR}/src/parse-request.ts"
assert_file_exists "${SIDECAR_DIR}/src/safe-mime.ts"
assert_file_exists "${SIDECAR_DIR}/src/errors.ts"
assert_file_exists "${SIDECAR_DIR}/src/soffice-runner.ts"
assert_file_exists "${SIDECAR_DIR}/tests/shim.test.ts"
assert_file_exists "${SIDECAR_DIR}/tests/convert.test.ts"
assert_file_exists "${SIDECAR_DIR}/tests/health.test.ts"
assert_file_exists "${SIDECAR_DIR}/tests/parse-request.test.ts"
assert_file_exists "${SIDECAR_DIR}/tests/safe-mime.test.ts"
assert_file_exists "${SIDECAR_DIR}/tests/errors.test.ts"
assert_file_exists "${SIDECAR_DIR}/tests/soffice-runner.test.ts"
assert_file_exists "${SIDECAR_DIR}/tests/config.test.ts"
assert_file_exists "${SIDECAR_DIR}/tests/shim-boot.test.ts"

# ─────────────────────────────────────────────────────────────────────
# 2) Dockerfile carries the documented security posture.
# ─────────────────────────────────────────────────────────────────────
DOCKERFILE="${SIDECAR_DIR}/Dockerfile"
# Non-root user (uid=10001 per plan §12.5).
assert_file_contains "${DOCKERFILE}" "uid 10001"
assert_file_contains "${DOCKERFILE}" "USER xynes"
assert_file_contains "${DOCKERFILE}" "/usr/sbin/nologin"
# Macros disabled at image level (defense in depth on top of FU-E env).
assert_file_contains "${DOCKERFILE}" "SAL_DISABLE_MACROS=1"
# Bun runtime + tini PID-1 init (mitigates soffice zombie accumulation
# per plan §12.5 spawn-per-request strategy).
assert_file_contains "${DOCKERFILE}" "oven/bun"
assert_file_contains "${DOCKERFILE}" "tini"
# Listens on 8100 per FU-C wire contract.
assert_file_contains "${DOCKERFILE}" "EXPOSE 8100"
# LibreOffice subpackages only (NOT the GUI meta-package which adds 1+ GB).
assert_file_contains "${DOCKERFILE}" "libreoffice-core"
assert_file_contains "${DOCKERFILE}" "libreoffice-writer"
assert_file_contains "${DOCKERFILE}" "libreoffice-calc"
assert_file_contains "${DOCKERFILE}" "libreoffice-impress"
# We deliberately do NOT pull `libreoffice` (the GUI meta-package).
# A bare `libreoffice` apt package name is a whole-token match; the
# subpackages all carry a `-` suffix so they never match this regex.
if grep -qE '(^| )libreoffice( |\\|$)' "${DOCKERFILE}"; then
  fail "${DOCKERFILE} must NOT install the libreoffice GUI meta-package"
fi
# No :latest tags anywhere.
assert_file_not_contains "${DOCKERFILE}" ":latest"

# ─────────────────────────────────────────────────────────────────────
# 3) Shim implements the FU-C wire contract.
# ─────────────────────────────────────────────────────────────────────
SHIM="${SIDECAR_DIR}/src/shim.ts"
SAFE_MIME="${SIDECAR_DIR}/src/safe-mime.ts"
# Routes.
assert_file_contains "${SHIM}" "/convert"
assert_file_contains "${SHIM}" "/health"
# Closed-set safe-MIME allowlist mirrors FU-C's
# SAFE_DOCUMENT_PREVIEW_MIMES byte-for-byte.
for mime in \
  "application/pdf" \
  "application/msword" \
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document" \
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" \
  "application/vnd.openxmlformats-officedocument.presentationml.presentation" \
  "application/vnd.oasis.opendocument.text" \
  "application/vnd.oasis.opendocument.spreadsheet" \
  "application/vnd.oasis.opendocument.presentation" \
  "application/rtf"; do
  assert_file_contains "${SAFE_MIME}" "${mime}"
done

# ─────────────────────────────────────────────────────────────────────
# 4) Redaction invariants — no raw bytes / library text in error
#    messages. Closed-set codes only.
# ─────────────────────────────────────────────────────────────────────
ERRORS="${SIDECAR_DIR}/src/errors.ts"
for code in \
  "INVALID_JSON" \
  "MISSING_FIELD" \
  "UNSUPPORTED_FORMAT" \
  "OVER_MAX_BYTES" \
  "INVALID_BASE64" \
  "CONVERT_FAILED" \
  "TIMEOUT" \
  "INTERNAL_ERROR"; do
  assert_file_contains "${ERRORS}" "${code}"
done

# Error MESSAGES (the string values) must NOT carry library hints,
# path leakage, or credential patterns. We extract only the
# SHIM_ERROR_MESSAGE block (the actual wire-shape values) so that
# JSDoc comments mentioning `soffice` etc. don't trip the check.
ERROR_MESSAGES_BLOCK="$(awk '
  /^export const SHIM_ERROR_MESSAGE/ { capture = 1 }
  capture { print }
  capture && /^\};/ { exit }
' "${ERRORS}")"
if [ -z "${ERROR_MESSAGES_BLOCK}" ]; then
  fail "${ERRORS} is missing the SHIM_ERROR_MESSAGE block — validator can't enforce redaction"
fi
for forbidden in \
  "soffice" \
  "libreoffice" \
  "/tmp" \
  "/var" \
  "stderr" \
  "AKIA" \
  "xynes_live_" \
  "X-Amz-Signature"; do
  if printf '%s' "${ERROR_MESSAGES_BLOCK}" | grep -F -- "${forbidden}" >/dev/null 2>&1; then
    fail "${ERRORS} SHIM_ERROR_MESSAGE block must NOT contain redaction-leak: ${forbidden}"
  fi
done

# ─────────────────────────────────────────────────────────────────────
# 5) Compose overlay pins the FU-G image + uses /health probe.
# ─────────────────────────────────────────────────────────────────────
assert_file_exists "${COMPOSE_OVERLAY}"
assert_file_contains "${COMPOSE_OVERLAY}" "image: ${PINNED_IMAGE}"
# Placeholder image removed.
assert_file_not_contains "${COMPOSE_OVERLAY}" "${PLACEHOLDER_IMAGE}"
# Healthcheck flipped from TCP probe → /health.
assert_file_contains "${COMPOSE_OVERLAY}" "/health"
# FU-G image is intentionally slim and does not install wget/curl;
# healthcheck must use bun runtime tooling.
assert_file_not_contains "${COMPOSE_OVERLAY}" "wget -qO-"
# Security posture preserved (already enforced by FU-E validator;
# re-check for defense in depth on this edit).
assert_file_contains "${COMPOSE_OVERLAY}" "no-new-privileges:true"
assert_file_contains "${COMPOSE_OVERLAY}" "read_only: true"
assert_file_contains "${COMPOSE_OVERLAY}" "SAL_DISABLE_MACROS=1"

# ─────────────────────────────────────────────────────────────────────
# 6) K8s Deployment pins the FU-G image + uses httpGet /health probes.
# ─────────────────────────────────────────────────────────────────────
assert_file_exists "${K8S_DEPLOYMENT}"
assert_file_contains "${K8S_DEPLOYMENT}" "image: ${PINNED_IMAGE}"
assert_file_not_contains "${K8S_DEPLOYMENT}" "${PLACEHOLDER_IMAGE}"
# Both probes use httpGet /health.
assert_file_contains "${K8S_DEPLOYMENT}" "httpGet:"
assert_file_contains "${K8S_DEPLOYMENT}" "path: /health"
# tcpSocket probes removed (we want HTTP-level health, not just
# port-bind confirmation).
assert_file_not_contains "${K8S_DEPLOYMENT}" "tcpSocket:"

# ─────────────────────────────────────────────────────────────────────
# 7) No raw credentials in any FU-G artefact.
# ─────────────────────────────────────────────────────────────────────
for f in \
  "${DOCKERFILE}" \
  "${SIDECAR_DIR}/src/shim.ts" \
  "${SIDECAR_DIR}/src/convert.ts" \
  "${SIDECAR_DIR}/src/health.ts" \
  "${SIDECAR_DIR}/src/parse-request.ts" \
  "${SIDECAR_DIR}/src/safe-mime.ts" \
  "${SIDECAR_DIR}/src/soffice-runner.ts" \
  "${SIDECAR_DIR}/src/errors.ts" \
  "${COMPOSE_OVERLAY}" \
  "${K8S_DEPLOYMENT}"; do
  for pattern in \
    "AKIA[A-Z0-9]" \
    "xynes_live_" \
    "X-Amz-Signature" \
    "secretAccessKey" \
    "accessKeyId" \
    "secret://"; do
    if grep -qE -- "${pattern}" "${f}" 2>/dev/null; then
      fail "${f} must NOT contain raw credential pattern: ${pattern}"
    fi
  done
done

# ─────────────────────────────────────────────────────────────────────
# 8) Plan §2 + §12.5 reflect FU-G ownership + image pin.
# ─────────────────────────────────────────────────────────────────────
assert_file_contains "${PLAN_DOC}" "STORAGE-FU-5-FU-G"
# Section §12.5 lives in the plan.
assert_file_contains "${PLAN_DOC}" "12.5"
# Ownership row mentions xynes-storage-service.
assert_file_contains "${PLAN_DOC}" "xynes/xynes-storage-service"

echo "All STORAGE-FU-5-FU-G static checks passed."
