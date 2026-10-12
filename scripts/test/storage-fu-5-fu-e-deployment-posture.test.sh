#!/usr/bin/env bash
set -euo pipefail

# STORAGE-FU-5-FU-E — Static / shape tests for the deployment-posture
# decision (Compose overlay + K8s draft manifests + DEVELOPER.md /
# plan references). Mirrors `scripts/test/smoke-universal-storage.test.sh`:
# pure static checks; no Docker / kubectl / network required.
#
# Why this exists: STORAGE-FU-5-FU-E ships compose + K8s drafts but
# does NOT auto-apply them. The drafts must stay in sync with the
# DEVELOPER.md + plan references, the env contract documented in
# deployment-posture.md §4, and the security invariants documented
# in §8. A future agent editing one but not the others trips this
# guard before the change escapes review.
#
# Wired into the file-based test runner via the glob in
# `scripts/test/run.sh`.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

COMPOSE_OVERLAY="./infra/compose/storage-live-processors.yml"
K8S_DIR="./infra/release/deployment-posture/k8s"
DEPLOYMENT_POSTURE_DOC="../xynes-storage-service/docs/deployment-posture.md"
# Plan was archived alongside the combined STORAGE follow-ups epic
# closure on 2026-05-29. STORAGE-FU-E-FIX-1 (2026-06-02) repointed
# this validator at the archive path so the suite is green again.
PLAN_DOC="./docs/plans/archive/2026-05-27-storage-followups-combined.md"

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
assert_file_exists "${COMPOSE_OVERLAY}"
assert_file_exists "${DEPLOYMENT_POSTURE_DOC}"
assert_file_exists "${PLAN_DOC}"

for required_k8s in \
  README.md \
  00-namespace.yaml \
  10-libreoffice-sidecar.deployment.yaml \
  11-libreoffice-sidecar.service.yaml \
  20-clamav-clamd.deployment.yaml \
  21-clamav-clamd.service.yaml \
  22-clamav-freshclam.deployment.yaml \
  30-clamav-defs.persistentvolumeclaim.yaml \
  40-networkpolicy.yaml; do
  assert_file_exists "${K8S_DIR}/${required_k8s}"
done

# ─────────────────────────────────────────────────────────────────────
# 2) Compose overlay declares the three sidecars + extends storage-service.
# ─────────────────────────────────────────────────────────────────────
for service in libreoffice-sidecar clamav-clamd clamav-freshclam; do
  assert_file_contains "${COMPOSE_OVERLAY}" "${service}:"
done
# storage-service block extended with the env contract + depends_on.
assert_file_contains "${COMPOSE_OVERLAY}" "  storage-service:"
assert_file_contains "${COMPOSE_OVERLAY}" "LIBREOFFICE_SERVICE_URL=http://libreoffice-sidecar:8100"
assert_file_contains "${COMPOSE_OVERLAY}" "CLAMD_HOST=clamav-clamd"
assert_file_contains "${COMPOSE_OVERLAY}" "CLAMD_PORT=3310"
# Security posture — every sidecar drops capabilities + no-new-privileges.
# We assert the literals are present at least once each per security gate.
assert_file_contains "${COMPOSE_OVERLAY}" "no-new-privileges:true"
assert_file_contains "${COMPOSE_OVERLAY}" "cap_drop:"
assert_file_contains "${COMPOSE_OVERLAY}" "read_only: true"
# Pinned image tags — no `:latest` anywhere.
assert_file_not_contains "${COMPOSE_OVERLAY}" ":latest"
# Macros disabled on the LibreOffice sidecar (STORAGE-9 §3.6 invariant).
assert_file_contains "${COMPOSE_OVERLAY}" "SAL_DISABLE_MACROS=1"
# STORAGE-FU-E-FIX-1 (2026-06-02, live-run discovery): both clamav
# containers mount the shared definitions volume :rw because the
# upstream entrypoint chown returns exit 1 on a :ro mount under
# cap_drop ALL. Defense is via process hardening (cap_drop ALL +
# narrow cap_add + no-new-privileges), not mount mode.
assert_file_contains "${COMPOSE_OVERLAY}" "clamav-defs:/var/lib/clamav:rw"
assert_file_not_contains "${COMPOSE_OVERLAY}" "clamav-defs:/var/lib/clamav:ro"

# ─────────────────────────────────────────────────────────────────────
# 3) Every K8s manifest declares the documented security context.
#
# STORAGE-FU-E-FIX-1 (2026-06-02, live-run discovery): the clamav
# manifests intentionally relax `runAsNonRoot: true` and
# `readOnlyRootFilesystem: true` because the upstream entrypoint
# requires root + writable /etc/clamav (see §9 below). Those two
# invariants are now ONLY asserted on the LibreOffice manifest;
# the clamav manifests carry the substitute posture
# (`allowPrivilegeEscalation: false` + narrow capability allowlist),
# asserted in §9 below.
# ─────────────────────────────────────────────────────────────────────
assert_file_contains \
  "${K8S_DIR}/10-libreoffice-sidecar.deployment.yaml" \
  "runAsNonRoot: true"
assert_file_contains \
  "${K8S_DIR}/10-libreoffice-sidecar.deployment.yaml" \
  "readOnlyRootFilesystem: true"
for f in \
  "${K8S_DIR}/10-libreoffice-sidecar.deployment.yaml" \
  "${K8S_DIR}/20-clamav-clamd.deployment.yaml" \
  "${K8S_DIR}/22-clamav-freshclam.deployment.yaml"; do
  assert_file_contains "$f" "allowPrivilegeEscalation: false"
  assert_file_contains "$f" "drop: "
  assert_file_contains "$f" '"ALL"'
  assert_file_contains "$f" "seccompProfile:"
  assert_file_contains "$f" "RuntimeDefault"
  # Pinned image tags only.
  assert_file_not_contains "$f" ":latest"
done

# LibreOffice sidecar disables macros at the env level.
assert_file_contains \
  "${K8S_DIR}/10-libreoffice-sidecar.deployment.yaml" \
  "SAL_DISABLE_MACROS"

# STORAGE-FU-E-FIX-1 (2026-06-02): the pre-FIX-1 assertion required
# clamav-clamd to mount `/var/lib/clamav` as `readOnly: true`. Live-run
# discovery showed that under `runAsUser: 1000` + `drop: ["ALL"]`,
# the upstream entrypoint's `chown -R clamav:clamav /var/lib/clamav`
# step returns exit 1 on a readOnly mount (not 0, despite chown's
# noisy "Read-only file system" messages). That crashes the
# entrypoint under `set -eu`. Both clamav containers now mount the
# volume read-write; clamd never writes to it but the mount-mode is
# not the defensive boundary — process hardening is.
# Asserted instead: NEITHER manifest carries `readOnly: true` on
# the clamav-defs mount (Compose has the same posture).
assert_file_not_contains \
  "${K8S_DIR}/20-clamav-clamd.deployment.yaml" \
  "readOnly: true"
assert_file_not_contains \
  "${K8S_DIR}/22-clamav-freshclam.deployment.yaml" \
  "readOnly: true"

# NetworkPolicy has the documented default-deny + every allow rule.
NETPOL="${K8S_DIR}/40-networkpolicy.yaml"
assert_file_contains "${NETPOL}" "storage-sidecars-default-deny"
assert_file_contains "${NETPOL}" "allow-storage-to-libreoffice"
assert_file_contains "${NETPOL}" "allow-storage-to-clamd"
assert_file_contains "${NETPOL}" "storage-service-egress-allowlist"
assert_file_contains "${NETPOL}" "allow-freshclam-external-egress"

# ─────────────────────────────────────────────────────────────────────
# 4) deployment-posture.md has every documented section + env contract.
# ─────────────────────────────────────────────────────────────────────
for section in \
  "## 1. Decision summary" \
  "## 2. Tier-1: in-process processors" \
  "## 3. Tier-2: sidecars" \
  "## 4. Env contract (consumed by FU-A..D)" \
  "## 5. Compose service definitions (dev / QA)" \
  "## 6. Kubernetes draft manifests (future)" \
  "## 7. Cold-start + memory budget" \
  "## 8. Security posture" \
  "## 9. Operator rollout sequence" \
  "## 10. Out of scope"; do
  assert_file_contains "${DEPLOYMENT_POSTURE_DOC}" "${section}"
done

# Env contract names match what FU-A..D will read.
for env in \
  "STORAGE_PROCESSOR_MODE" \
  "STORAGE_FFMPEG_TIMEOUT_MS" \
  "STORAGE_SOFFICE_TIMEOUT_MS" \
  "LIBREOFFICE_SERVICE_URL" \
  "CLAMD_HOST" \
  "CLAMD_PORT" \
  "CLAMD_SOCKET"; do
  assert_file_contains "${DEPLOYMENT_POSTURE_DOC}" "${env}"
done

# Posture table mentions each of the four processors.
for proc in sharp ffmpeg LibreOffice clamav; do
  assert_file_contains "${DEPLOYMENT_POSTURE_DOC}" "${proc}"
done

# Operator rollout sequence references the FU-A live-mode env flip.
assert_file_contains "${DEPLOYMENT_POSTURE_DOC}" "STORAGE_PROCESSOR_MODE=live"
assert_file_contains "${DEPLOYMENT_POSTURE_DOC}" "infra/compose/storage-live-processors.yml"

# ─────────────────────────────────────────────────────────────────────
# 5) Plan §6 row reflects "Landed" status.
# ─────────────────────────────────────────────────────────────────────
assert_file_contains "${PLAN_DOC}" "STORAGE-FU-5-FU-E"
assert_file_contains "${PLAN_DOC}" "Landed 2026-05-28"

# ─────────────────────────────────────────────────────────────────────
# 6) No raw credentials in any FU-E artefact.
# ─────────────────────────────────────────────────────────────────────
for f in \
  "${COMPOSE_OVERLAY}" \
  "${K8S_DIR}/10-libreoffice-sidecar.deployment.yaml" \
  "${K8S_DIR}/20-clamav-clamd.deployment.yaml" \
  "${K8S_DIR}/22-clamav-freshclam.deployment.yaml" \
  "${DEPLOYMENT_POSTURE_DOC}"; do
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

# ─────────────────────────────────────────────────────────────────────
# STORAGE-FU-E-FIX-1 (2026-06-02) — Overlay must actually boot.
#
# Three independent bugs surfaced during the 2026-06-02 operator-side
# validation of the EICAR malware-block smoke. Each one independently
# blocks `docker compose up`; all three must stay fixed forever:
#
#   1. ClamAV image tag `1.3` was retagged off Docker Hub. The overlay
#      and K8s drafts must pin a still-published tag (`1.5.2-debian`
#      as of 2026-06-02). `1.5`/`stable`/`latest`-flavoured tags allow
#      silent drift and are forbidden.
#   2. The ClamAV image's default `clamd.conf` enables `TCPSocket 3310`
#      but leaves `TCPAddr` commented (binds to localhost only). The
#      upstream entrypoint processes `CLAMD_CONF_<key>=<value>` envs
#      to rewrite `/etc/clamav/clamd.conf` at startup, so the overlay
#      and K8s drafts MUST set `CLAMD_CONF_TCPAddr=0.0.0.0` (alongside
#      the documented `CLAMD_CONF_TCPSocket=3310`) so cross-container
#      callers can reach clamd. Without this, storage-service probes
#      get ECONNREFUSED.
#   3. The clamav-freshclam entrypoint chowns `/var/lib/clamav` at
#      startup. Combined with `read_only: true` rootfs + a fresh named
#      volume, the chown fails and definitions never download. The
#      overlay MUST relax freshclam's rootfs to writable AND mount the
#      clamav-defs volume `:rw` (live-run discovery 2026-06-02 showed
#      that clamd's rootfs ALSO has to be writable — the entrypoint's
#      `sed -i` on `/etc/clamav/clamd.conf` runs there too). The K8s
#      drafts converge on the same root + cap_add posture as Compose
#      rather than an initContainer chown step; an earlier draft of
#      the plan called for an initContainer but live-run validation
#      proved the cap_add approach works uniformly on both runtimes.
#   4. The FU-G LibreOffice shim image (`xynes/libreoffice-sidecar:0.1.0`)
#      is not yet published to any pull-able registry. Compose v2 tries
#      to pull every service in every `-f` file before starting ANY,
#      so the dangling image blocks the entire stack. The overlay MUST
#      gate the libreoffice-sidecar block behind a Compose `profiles:`
#      so it is only resolved when the operator explicitly opts in
#      with `--profile libreoffice`.
# ─────────────────────────────────────────────────────────────────────

CLAMAV_IMAGE_PIN="clamav/clamav:1.5.2-debian"
LIBREOFFICE_PROFILE="libreoffice"

# 7) ClamAV image pin is the current semver-specific tag.
assert_file_contains "${COMPOSE_OVERLAY}" "${CLAMAV_IMAGE_PIN}"
assert_file_contains \
  "${K8S_DIR}/20-clamav-clamd.deployment.yaml" \
  "${CLAMAV_IMAGE_PIN}"
assert_file_contains \
  "${K8S_DIR}/22-clamav-freshclam.deployment.yaml" \
  "${CLAMAV_IMAGE_PIN}"

# The retagged `1.3` line must NEVER reappear in any FU-E artefact.
for f in \
  "${COMPOSE_OVERLAY}" \
  "${K8S_DIR}/20-clamav-clamd.deployment.yaml" \
  "${K8S_DIR}/22-clamav-freshclam.deployment.yaml"; do
  assert_file_not_contains "$f" "clamav/clamav:1.3"
done

# Floating tags would silently drift across CVE backports. Pin only.
for f in \
  "${COMPOSE_OVERLAY}" \
  "${K8S_DIR}/20-clamav-clamd.deployment.yaml" \
  "${K8S_DIR}/22-clamav-freshclam.deployment.yaml"; do
  for floating in \
    "clamav/clamav:stable" \
    "clamav/clamav:latest" \
    "clamav/clamav:1.5-debian" \
    "clamav/clamav:1.5_base" \
    "clamav/clamav:stable-debian"; do
    assert_file_not_contains "$f" "${floating}"
  done
done

# 8) ClamAV 1.5.x TCP binding wired explicitly on BOTH the daemon
#    container (so the listener accepts cross-container connections)
#    AND the freshclam container (defense in depth — both use the
#    same image and same entrypoint, even though freshclam itself
#    does not listen).
assert_file_contains "${COMPOSE_OVERLAY}" "CLAMD_CONF_TCPSocket=3310"
assert_file_contains "${COMPOSE_OVERLAY}" "CLAMD_CONF_TCPAddr=0.0.0.0"
assert_file_contains \
  "${K8S_DIR}/20-clamav-clamd.deployment.yaml" \
  "CLAMD_CONF_TCPSocket"
assert_file_contains \
  "${K8S_DIR}/20-clamav-clamd.deployment.yaml" \
  "CLAMD_CONF_TCPAddr"

# 9) clamav containers run AS ROOT with a narrow `cap_add` allowlist.
#    Live-run discovery (2026-06-02) showed that running as
#    `user: "1000:1000"` does NOT work — the upstream entrypoint's
#    `sed -i` on `/etc/clamav/clamd.conf` cannot create temp files
#    in the root-owned `/etc/clamav/` directory under a non-root
#    user. The pragmatic posture is root + cap_drop ALL + narrow
#    cap_add: the entrypoint needs CHOWN + FOWNER, clamd's
#    privilege-drop to uid 1000 needs SETUID + SETGID, and clamd's
#    log file writes need DAC_OVERRIDE.
#
#    Compose: both `clamav-clamd:` and `clamav-freshclam:` blocks
#    must carry the 5-cap allowlist. We assert each cap is present
#    inside each block to avoid silent drift.
clamav_block_has_cap() {
  awk -v svc="$2" -v cap="$3" '
    $0 ~ "^  " svc ":"            { in_block = 1; next }
    /^  [a-zA-Z0-9_-]+:[[:space:]]*$/ { if (in_block) in_block = 0 }
    /^[a-zA-Z]/                   { if (in_block) in_block = 0 }
    in_block && $0 ~ "^[[:space:]]+-[[:space:]]+" cap "[[:space:]]*$" { found = 1 }
    END { exit found ? 0 : 1 }
  ' "$1"
}
for service in clamav-clamd clamav-freshclam; do
  for cap in CHOWN FOWNER DAC_OVERRIDE SETUID SETGID; do
    if ! clamav_block_has_cap "${COMPOSE_OVERLAY}" "$service" "$cap"; then
      fail "${COMPOSE_OVERLAY}: $service MUST carry cap_add: $cap — required for the upstream entrypoint + clamd daemon to run under cap_drop: [ALL]. See STORAGE-FU-E-FIX-1 §1.2 bug 3 + live-run discovery."
    fi
  done
done
# And both blocks MUST also drop ALL other caps for defense in depth.
clamav_block_has_cap_drop_all() {
  awk -v svc="$2" '
    $0 ~ "^  " svc ":"            { in_block = 1; in_cap_drop = 0; next }
    /^  [a-zA-Z0-9_-]+:[[:space:]]*$/ { if (in_block) in_block = 0 }
    /^[a-zA-Z]/                   { if (in_block) in_block = 0 }
    in_block && /^[[:space:]]+cap_drop:/  { in_cap_drop = 1; next }
    in_cap_drop && /^[[:space:]]+- ALL[[:space:]]*$/ { found = 1; in_cap_drop = 0 }
    in_cap_drop && /^[[:space:]]+[a-z_]+:/ { in_cap_drop = 0 }
    END { exit found ? 0 : 1 }
  ' "$1"
}
for service in clamav-clamd clamav-freshclam; do
  if ! clamav_block_has_cap_drop_all "${COMPOSE_OVERLAY}" "$service"; then
    fail "${COMPOSE_OVERLAY}: $service MUST carry cap_drop: [ALL] for defense in depth — the narrow cap_add allowlist is meaningless without the drop. See STORAGE-FU-E-FIX-1 §1.2 bug 3 + live-run discovery."
  fi
done

#    K8s: the drafts converge on the same Compose-validated posture
#    — root + drop ALL + the same 5-cap allowlist + writable rootfs
#    + `:rw` clamav-defs mount. The 5 caps are asserted above. The
#    K8s drafts are documentation-grade only (no live `kubectl
#    apply` against a real cluster on this host) but every structural
#    rule that the Compose overlay was live-validated to require is
#    mirrored here so the future K3s migration story is mechanical.
for f in \
  "${K8S_DIR}/20-clamav-clamd.deployment.yaml" \
  "${K8S_DIR}/22-clamav-freshclam.deployment.yaml"; do
  for cap in CHOWN FOWNER DAC_OVERRIDE SETUID SETGID; do
    if ! grep -qE -- "\"${cap}\"" "$f"; then
      fail "${f}: MUST carry capabilities.add: \"${cap}\" — required for the upstream entrypoint + clamd daemon to run under drop ALL. See STORAGE-FU-E-FIX-1 §1.2 bug 3 + live-run discovery."
    fi
  done
done

#    K8s: documentation-grade only. The K8s drafts use a different
#    strategy (runAsUser: 1000 + initContainer) that has NOT been
#    live-validated against a real cluster, so we don't lock in a
#    specific posture here. The drafts must still pin the 1.5.2-debian
#    image (asserted above) and carry the CLAMD_CONF_TCP* envs
#    (asserted in §8). Operational K8s rollout will likely need to
#    converge on the same root + cap_add posture as Compose; that
#    convergence is deferred to the future K3s migration story.

# K8s: freshclam Deployment's MAIN container must NOT set
# readOnlyRootFilesystem: true — the upstream entrypoint runs
# `sed -i` against /etc/clamav/*.conf to apply CLAMD_CONF_* /
# FRESHCLAM_CONF_* envs. That sed fails on a readonly rootfs.
# Comment lines are skipped so a documentation reference to
# "readOnlyRootFilesystem: true" does not false-trip this check.
freshclam_main_has_readonly() {
  awk '
    /^      containers:[[:space:]]*$/ { in_main = 1; next }
    in_main {
      stripped = $0
      sub(/^[[:space:]]+/, "", stripped)
      if (stripped ~ /^#/) next
      if ($0 ~ /readOnlyRootFilesystem:[[:space:]]*true/) found = 1
    }
    END { exit found ? 0 : 1 }
  ' "$1"
}
if freshclam_main_has_readonly \
    "${K8S_DIR}/22-clamav-freshclam.deployment.yaml"; then
  fail "${K8S_DIR}/22-clamav-freshclam.deployment.yaml: the MAIN freshclam container MUST NOT set readOnlyRootFilesystem: true — the upstream entrypoint runs sed -i against /etc/clamav/*.conf and that fails on a readonly rootfs. See STORAGE-FU-E-FIX-1 §1.2 bug 3 + live-run discovery."
fi
# Same check applies to clamd's MAIN container in its own manifest.
clamd_main_has_readonly() {
  awk '
    /^      containers:[[:space:]]*$/ { in_main = 1; next }
    in_main {
      stripped = $0
      sub(/^[[:space:]]+/, "", stripped)
      if (stripped ~ /^#/) next
      if ($0 ~ /readOnlyRootFilesystem:[[:space:]]*true/) found = 1
    }
    END { exit found ? 0 : 1 }
  ' "$1"
}
if clamd_main_has_readonly \
    "${K8S_DIR}/20-clamav-clamd.deployment.yaml"; then
  fail "${K8S_DIR}/20-clamav-clamd.deployment.yaml: the MAIN clamd container MUST NOT set readOnlyRootFilesystem: true — the upstream entrypoint runs sed -i against /etc/clamav/clamd.conf and that fails on a readonly rootfs. See STORAGE-FU-E-FIX-1 §1.2 bug 3 + live-run discovery."
fi

# 10) LibreOffice sidecar must be opt-in via a Compose profile so the
#     un-published FU-G image does not block the rest of the stack.
#     The storage-service block's depends_on for libreoffice-sidecar
#     must also be gated by the same profile (otherwise Compose still
#     tries to resolve the image when storage-service is requested).
assert_file_contains "${COMPOSE_OVERLAY}" "profiles:"
# A simple grep for the profile name catches both the sidecar's own
# profile and the storage-service's libreoffice-gated depends_on (if
# we wire it that way) — at least one occurrence is required.
assert_file_contains "${COMPOSE_OVERLAY}" "- ${LIBREOFFICE_PROFILE}"

# Overlay header comment must call out the bring-up changes so a future
# operator does not silently regress them.
assert_file_contains "${COMPOSE_OVERLAY}" "STORAGE-FU-E-FIX-1"

# 11) deployment-posture.md operator rollout sequence must reflect the
#     actual working command (profile-gated for libreoffice) AND the
#     new freshclam wait note. The historical "first-run" guidance
#     must be present so operators know to wait for definitions.
assert_file_contains "${DEPLOYMENT_POSTURE_DOC}" "STORAGE-FU-E-FIX-1"
assert_file_contains "${DEPLOYMENT_POSTURE_DOC}" "--profile ${LIBREOFFICE_PROFILE}"
assert_file_contains "${DEPLOYMENT_POSTURE_DOC}" "freshclam"

echo "All STORAGE-FU-5-FU-E + STORAGE-FU-E-FIX-1 static checks passed."
