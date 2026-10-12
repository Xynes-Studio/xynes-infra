#!/usr/bin/env bash
set -euo pipefail

# STORAGE-FU-AB-FIX-1 (2026-06-02) — Static / shape tests for the
# storage-service Dockerfile fail-fast RUN probes that verify the
# FU-A (sharp) + FU-B (ffmpeg-static) in-process processor binaries
# land in the image at build time, plus the deployment-posture.md
# §9.0 operator-rebuild prerequisite block.
#
# Why this exists: FU-A/B added `sharp@^0.34.5` + `ffmpeg-static@^5.3.0`
# to `xynes-storage-service/package.json` on 2026-05-28, but the
# Dockerfile had no build-time guard that the deps actually resolved.
# A regression that drops one of them would only surface as a single
# WARN log line at runtime followed by every `image_optimize` / `video_*`
# job dead-lettering with `PROCESSOR_FAILED`. This validator makes
# that regression impossible to escape review.
#
# Source ownership:
#   - Dockerfile lives in `xynes-storage-service` (the runtime repo).
#   - deployment-posture.md lives in `xynes-storage-service`.
#   - This validator + the FU-AB-FIX-1 plan archive live in `xynes-infra`.
#
# Wired into the file-based test runner via the glob in
# `scripts/test/run.sh`.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# Sibling repo path. Same layout assumption as the FU-G validator.
STORAGE_DOCKERFILE="../xynes-storage-service/Dockerfile"
STORAGE_PACKAGE_JSON="../xynes-storage-service/package.json"
DEPLOYMENT_POSTURE_DOC="../xynes-storage-service/docs/deployment-posture.md"
PLAN_DOC="./docs/plans/archive/2026-06-02-storage-fu-ab-fix-1.md"

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

assert_file_exists() {
  [ -f "$1" ] || fail "expected file missing: $1"
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
# 1) All required artefacts exist.
# ─────────────────────────────────────────────────────────────────────
assert_file_exists "${STORAGE_DOCKERFILE}"
assert_file_exists "${STORAGE_PACKAGE_JSON}"
assert_file_exists "${DEPLOYMENT_POSTURE_DOC}"
assert_file_exists "${PLAN_DOC}"

# ─────────────────────────────────────────────────────────────────────
# 2) package.json still declares the FU-A + FU-B deps. If a future
#    refactor moves them out (or drops them entirely), the Dockerfile
#    probe would still pass when the probe RUN step is also removed —
#    so we assert the deps explicitly here at the source-of-truth.
# ─────────────────────────────────────────────────────────────────────
assert_file_contains "${STORAGE_PACKAGE_JSON}" '"sharp":'
assert_file_contains "${STORAGE_PACKAGE_JSON}" '"ffmpeg-static":'

# ─────────────────────────────────────────────────────────────────────
# 3) Dockerfile carries the FU-AB-FIX-1 header marker so a future
#    agent does not silently regress the probes.
# ─────────────────────────────────────────────────────────────────────
assert_file_contains "${STORAGE_DOCKERFILE}" "STORAGE-FU-AB-FIX-1"

# ─────────────────────────────────────────────────────────────────────
# 4) Dockerfile multi-stage targets are both probed.
#
#    A new agent who adds a third target (e.g. `AS test`) without a
#    probe still passes this validator — that's deliberate. The two
#    targets we ship (dev + prod) are the only ones operator-rollouts
#    use, so they're the operative guard surface.
#
#    We assert structurally:
#    - The `AS dev` stage must contain a `bun -e '...require("sharp")...'` RUN.
#    - The `AS prod` stage must contain the same.
#    - The probe must include `fs.accessSync(..., X_OK)` on the
#      ffmpeg-static binary path (catches the "module loads but binary
#      missing" edge — sharp's libvips binding is bundled INSIDE the
#      npm package but ffmpeg-static ships a separate binary that must
#      also exist + be executable).
# ─────────────────────────────────────────────────────────────────────
dockerfile_stage_has_probe() {
  # $1 = file, $2 = stage name (e.g. "dev", "prod").
  # Walks the file line-by-line, tracking which AS stage we're in,
  # and asserts at least one RUN line inside the stage contains the
  # probe substring. Stage ends at the next `FROM ... AS` line.
  awk -v stage="$2" '
    /^FROM[[:space:]]+[^[:space:]]+[[:space:]]+AS[[:space:]]+/ {
      if ($NF == stage) { in_stage = 1 } else { in_stage = 0 }
      next
    }
    in_stage && /require\("sharp"\)/ {
      sharp_seen = 1
    }
    in_stage && /require\("ffmpeg-static"\)/ {
      ffmpeg_seen = 1
    }
    in_stage && /accessSync/ && /X_OK/ {
      bin_check_seen = 1
    }
    END {
      if (sharp_seen && ffmpeg_seen && bin_check_seen) exit 0
      exit 1
    }
  ' "$1"
}

for stage in dev prod; do
  if ! dockerfile_stage_has_probe "${STORAGE_DOCKERFILE}" "${stage}"; then
    fail "${STORAGE_DOCKERFILE}: \`AS ${stage}\` stage MUST contain a fail-fast probe that calls BOTH \`require(\"sharp\")\` AND \`require(\"ffmpeg-static\")\` AND \`fs.accessSync(..., X_OK)\` against the ffmpeg-static binary path. See STORAGE-FU-AB-FIX-1 §2.3 + the live repro on 2026-06-02."
  fi
done

# ─────────────────────────────────────────────────────────────────────
# 5) Negative case: the bare `node -e` variant (without `bun`) MUST
#    NOT appear, because the storage-service runtime is Bun and
#    `node` is not installed in the `oven/bun:1` base image. A copy-paste
#    from the plan doc that used `node -e` would silently fail with
#    "node: not found" — which is technically still a build failure,
#    but the operator wouldn't realise the probe was the line that
#    tripped them.
# ─────────────────────────────────────────────────────────────────────
assert_file_not_contains "${STORAGE_DOCKERFILE}" 'node -e '

# ─────────────────────────────────────────────────────────────────────
# 6) deployment-posture.md has the §9.0 operator-rebuild prerequisite
#    block. The block MUST explain:
#    - The named-volume shadow trap (`storage-node_modules`).
#    - The `docker volume rm xynes-infra_storage-node_modules` step.
#    - The `--no-cache` rebuild step.
#    - The `--force-recreate` step.
#    - The cross-link to this plan archive.
# ─────────────────────────────────────────────────────────────────────
assert_file_contains "${DEPLOYMENT_POSTURE_DOC}" "STORAGE-FU-AB-FIX-1"
assert_file_contains "${DEPLOYMENT_POSTURE_DOC}" "storage-node_modules"
assert_file_contains "${DEPLOYMENT_POSTURE_DOC}" "docker volume rm xynes-infra_storage-node_modules"
assert_file_contains "${DEPLOYMENT_POSTURE_DOC}" "--no-cache"
assert_file_contains "${DEPLOYMENT_POSTURE_DOC}" "--force-recreate"

# Cross-link to the plan archive (so future operators can find the
# full bug analysis).
assert_file_contains \
  "${DEPLOYMENT_POSTURE_DOC}" \
  "2026-06-02-storage-fu-ab-fix-1.md"

# ─────────────────────────────────────────────────────────────────────
# 7) Plan archive exists at the expected path + carries the landed
#    status marker so a future agent does not re-open it by mistake.
# ─────────────────────────────────────────────────────────────────────
assert_file_contains "${PLAN_DOC}" "STORAGE-FU-AB-FIX-1"
assert_file_contains "${PLAN_DOC}" "Landed 2026-06-02"

# ─────────────────────────────────────────────────────────────────────
# 8) No raw credentials in any FU-AB-FIX-1 artefact.
# ─────────────────────────────────────────────────────────────────────
for f in \
  "${STORAGE_DOCKERFILE}" \
  "${DEPLOYMENT_POSTURE_DOC}" \
  "${PLAN_DOC}"; do
  for pattern in \
    "AKIA[A-Z0-9]" \
    "xynes_live_" \
    "X-Amz-Signature" \
    "secretAccessKey" \
    "accessKeyId"; do
    if grep -qE -- "${pattern}" "${f}" 2>/dev/null; then
      fail "${f} must NOT contain raw credential pattern: ${pattern}"
    fi
  done
done

echo "All STORAGE-FU-AB-FIX-1 static checks passed."
