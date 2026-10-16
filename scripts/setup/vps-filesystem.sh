#!/usr/bin/env bash
# vps-filesystem.sh — Story E-4 (MVP release plan, Phase 2 core).
#
# Idempotently creates the /srv/xynes/ directory tree on a fresh Ubuntu LTS
# host with the ownership + permissions specified in:
#   - infra/release/ENVIRONMENTS.md §5
#   - infra/release/VPS-SETUP.md §2.10
#
# Scope (this script only):
#   /srv/xynes/{dev,qa,prod}/{compose,env,supabase,supabase/db}
#   /srv/xynes/caddy/{data,log}
#   /srv/xynes/backups/{dev,qa,prod}
#   /srv/xynes/uptime-kuma/data
#
# Deliberately out of scope (do NOT add here):
#   - Base OS hardening (E-2, vps-bootstrap.sh §2.2–2.8).
#   - Docker engine install + log driver (E-3, vps-bootstrap.sh §2.9).
#   - Docker networks (E-5, vps-bootstrap.sh `create_networks()`).
#   - Per-env deploy users + chown of /srv/xynes/<env>/ (E-6).
#   - Per-env .env.<env> file placement (§2.15).
#
# Behavior contract:
#   - Conservative idempotency: detect current (mode, owner) first; mutate ONLY
#     on divergence. Re-running on a green host is a no-op.
#   - Numeric chown 999:999 for supabase/db (the Supabase Postgres container's
#     runtime UID/GID); the host needs no `postgres` user. The verifier compares
#     NUMERIC owner for spec entries written as "<uid>:<gid>" so Ubuntu 22.04+'s
#     pre-installed systemd-coredump (UID 999) does NOT trip a spurious OWNER_DRIFT
#     row.
#   - Fail-closed on missing `xynes` group: this script will not assign
#     `root:xynes` ownership if E-2 was never applied. Exit 1 with a cross-link.
#   - --dry-run prints "WOULD: <command>" for every intended mutation and
#     performs zero side effects. No root needed for --dry-run.
#   - --root <path> overrides the layout root (useful for offline tests under
#     a $(mktemp -d)). Default: /srv/xynes.
#   - The verifier `vps-filesystem-verify.sh` consumes the same spec table.
#
# Usage:
#   bash scripts/setup/vps-filesystem.sh --help
#   bash scripts/setup/vps-filesystem.sh --dry-run
#   bash scripts/setup/vps-filesystem.sh --dry-run --root /tmp/fake
#   sudo bash scripts/setup/vps-filesystem.sh
#
# Exit codes:
#   0   Apply or dry-run completed; ledger summary printed.
#   1   Validation error (bad --root, missing xynes group, runtime failure).
#   64  Unknown flag.

set -euo pipefail

# ------------------------------------------------------------------------------
# Defaults
# ------------------------------------------------------------------------------

SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"

MODE="apply"            # apply | dry-run
LAYOUT_ROOT="/srv/xynes"

# Section action ledger (mirrors vps-bootstrap.sh shape):
#   APPLIED — script mutated state.
#   ALREADY — already matched spec; no mutation needed.
#   FAILED  — section hit a non-recoverable error.
declare -a DIR_LEDGER=()

# ------------------------------------------------------------------------------
# Spec table
# ------------------------------------------------------------------------------
#
# Format: "<relative-path>|<mode>|<owner>"
#   <relative-path> is appended to $LAYOUT_ROOT.
#   <mode> is a 3-digit octal mode (chmod argument).
#   <owner> is `user:group` (chown argument).
#
# Order is significant: parents MUST come before children so `mkdir -p` chains
# converge cleanly even when --root is a fresh $(mktemp -d).
#
# Tightness rationale:
#   - 0755 root:root on the layout root + caddy/uptime-kuma trees (parent
#     traversal; non-secret content).
#   - 0755 root:xynes on per-env roots so E-6's xynes_<env>_deploy users
#     (members of the `xynes` group) can `cd` into their stack.
#   - 0750 on compose/ and env/ — group-traversable, NOT world-readable
#     (compose files reference image tags; env/ holds .env.<env>).
#   - 0700 on supabase/db (Postgres data dir; owner-only) and caddy/data
#     (ACME private keys; owner-only).
#   - 0700 root:root on backups/ and per-env backup dirs (matches §5).

SPEC=(
  # Layout root + first-level parents
  ".|0755|root:root"
  "dev|0755|root:xynes"
  "qa|0755|root:xynes"
  "prod|0755|root:xynes"
  "caddy|0755|root:root"
  "backups|0700|root:root"
  "uptime-kuma|0755|root:root"

  # Per-env subtrees
  "dev/compose|0750|root:xynes"
  "dev/env|0750|root:xynes"
  "dev/supabase|0755|root:xynes"
  "dev/supabase/db|0700|999:999"
  "qa/compose|0750|root:xynes"
  "qa/env|0750|root:xynes"
  "qa/supabase|0755|root:xynes"
  "qa/supabase/db|0700|999:999"
  "prod/compose|0750|root:xynes"
  "prod/env|0750|root:xynes"
  "prod/supabase|0755|root:xynes"
  "prod/supabase/db|0700|999:999"

  # Caddy
  "caddy/data|0700|root:root"
  "caddy/log|0755|root:root"

  # Backups (per-env)
  "backups/dev|0700|root:root"
  "backups/qa|0700|root:root"
  "backups/prod|0700|root:root"

  # Uptime Kuma
  "uptime-kuma/data|0755|root:root"
)

# Re-export the spec for the verifier via a shared library? Out of scope —
# the verifier carries its own copy and the offline test asserts they stay
# in sync via a structural grep. KISS.

# ------------------------------------------------------------------------------
# Usage
# ------------------------------------------------------------------------------

print_usage() {
  cat <<EOF
$SCRIPT_NAME — Create the /srv/xynes/ filesystem layout per VPS-SETUP.md §2.10
                + ENVIRONMENTS.md §5 (Story E-4).

USAGE:
  $SCRIPT_NAME [--dry-run] [--root <path>]
  $SCRIPT_NAME --help

OPTIONS:
  --dry-run        Print "WOULD: <command>" for every intended mutation. No
                   side effects. Does not require root.
  --root <path>    Override the layout root. Default: /srv/xynes. The path
                   must be absolute and contain only [A-Za-z0-9._/-]. Useful
                   for offline tests under \$(mktemp -d).
  --help, -h       Print this usage and exit 0.

DIRECTORIES CREATED:
  /srv/xynes/                              0755 root:root
  /srv/xynes/{dev,qa,prod}                 0755 root:xynes
  /srv/xynes/<env>/compose                 0750 root:xynes
  /srv/xynes/<env>/env                     0750 root:xynes
  /srv/xynes/<env>/supabase                0755 root:xynes
  /srv/xynes/<env>/supabase/db             0700 999:999     (Postgres container)
  /srv/xynes/caddy                         0755 root:root
  /srv/xynes/caddy/data                    0700 root:root   (ACME private keys)
  /srv/xynes/caddy/log                     0755 root:root
  /srv/xynes/backups                       0700 root:root
  /srv/xynes/backups/{dev,qa,prod}         0700 root:root
  /srv/xynes/uptime-kuma                   0755 root:root
  /srv/xynes/uptime-kuma/data              0755 root:root

CONTRACT:
  - Conservative idempotency: never mutate a directory whose (mode, owner)
    already matches the spec. Re-running on a green host is a no-op.
  - Fail-closed on missing 'xynes' group: this script will not assign
    root:xynes ownership if E-2 (vps-bootstrap.sh §2.2) was never applied.
  - Numeric chown 999:999 for supabase/db (Postgres container UID/GID); the
    host needs no \`postgres\` user. The verifier compares NUMERIC owner for
    spec entries written as "<uid>:<gid>" so Ubuntu 22.04+'s pre-installed
    systemd-coredump (UID 999) does NOT trip a spurious OWNER_DRIFT row.
  - --dry-run is safe to run as a non-root user.

EXIT CODES:
  0   Apply or dry-run completed; ledger summary printed.
  1   Validation error or runtime failure.
  64  Unknown flag.

EXAMPLES:
  # Preview the run without touching anything.
  bash $SCRIPT_NAME --dry-run

  # Apply on the VPS (run as root).
  sudo bash $SCRIPT_NAME

  # Offline smoke test under a temp root.
  bash $SCRIPT_NAME --root "\$(mktemp -d)"

VERIFICATION:
  After applying, run scripts/setup/vps-filesystem-verify.sh to assert the
  whole tree converged. The verifier is strictly read-only.
EOF
}

# ------------------------------------------------------------------------------
# Argv parsing
# ------------------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      MODE="dry-run"
      shift
      ;;
    --root)
      [[ $# -ge 2 ]] || { echo "ERROR: --root requires a value" >&2; exit 1; }
      LAYOUT_ROOT="$2"
      shift 2
      ;;
    --root=*)
      LAYOUT_ROOT="${1#*=}"
      shift
      ;;
    --help|-h)
      print_usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown flag: $1" >&2
      echo "Run '$SCRIPT_NAME --help' for usage." >&2
      exit 64
      ;;
  esac
done

# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------

log_info()  { printf "[INFO ] %s\n" "$*"; }
log_warn()  { printf "[WARN ] %s\n" "$*" >&2; }
log_error() { printf "[ERROR] %s\n" "$*" >&2; }
log_step()  { printf "\n=== %s ===\n" "$*"; }

ledger_add() {
  # ledger_add <path> <verdict> <note>
  DIR_LEDGER+=("$1|$2|$3")
}

is_dry_run() { [[ "$MODE" == "dry-run" ]]; }

# Run a mutating command unless we're in dry-run mode.
run_cmd() {
  if is_dry_run; then
    printf "WOULD: %s\n" "$*"
    return 0
  fi
  "$@"
}

# ------------------------------------------------------------------------------
# Validation
# ------------------------------------------------------------------------------

validate_root() {
  # --root must be absolute and contain only safe characters. Reject `..` and
  # `~` at parse time so a hostile caller cannot escape the intended root via
  # path traversal or shell expansion. The same regex is used by the verifier.
  if [[ ! "$LAYOUT_ROOT" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
    log_error "Invalid --root path: $LAYOUT_ROOT"
    log_error "Must be absolute and match ^/[A-Za-z0-9._/-]+\$ (no .., no ~, no spaces)."
    exit 1
  fi
  if [[ "$LAYOUT_ROOT" == *".."* ]]; then
    log_error "Invalid --root path: $LAYOUT_ROOT (contains '..')"
    exit 1
  fi
}

# Verify the `xynes` group exists; otherwise refuse to assign root:xynes
# ownership. Cross-link to E-2 (vps-bootstrap.sh §2.2) so the operator knows
# the prerequisite ordering.
preflight_xynes_group_exists() {
  # The spec assigns `root:xynes` to several directories. If the group is
  # missing we'd silently fall back to numeric or fail at chown time. Detect
  # early and surface a clear E-2 cross-link.
  if command -v getent >/dev/null 2>&1; then
    if getent group xynes >/dev/null 2>&1; then
      return 0
    fi
  else
    # macOS / non-glibc: fall back to /etc/group grep. Best-effort.
    if [[ -r /etc/group ]] && grep -qE '^xynes:' /etc/group; then
      return 0
    fi
  fi

  # In dry-run mode we don't actually chown anything, so a missing group on
  # the operator's laptop should NOT abort the preview. Surface a warning so
  # the operator knows they cannot apply on this host without first running
  # vps-bootstrap.sh §2.2.
  if is_dry_run; then
    log_warn "'xynes' group not found on this host."
    log_warn "Apply mode would FAIL chown of root:xynes directories."
    log_warn "On the VPS, run vps-bootstrap.sh first (E-2, §2.2)."
    return 0
  fi

  # Same relaxation when --root has been overridden away from /srv/xynes
  # (the canonical production path). The override is the documented offline-
  # test escape hatch: the operator owns the tempdir, chown will likely fail,
  # but mkdir + chmod still converge enough for the test harness to assert
  # the directory tree shape. require_root_for_apply applies the same logic.
  if [[ "$LAYOUT_ROOT" != "/srv/xynes" ]]; then
    log_warn "'xynes' group not found on this host."
    log_warn "Continuing because --root is overridden (offline test mode);"
    log_warn "chown calls may FAIL and surface as FAILED ledger rows."
    return 0
  fi

  log_error "'xynes' group missing — refusing to assign root:xynes ownership."
  log_error "Run vps-bootstrap.sh first (E-2, VPS-SETUP.md §2.2)."
  exit 1
}

require_root_for_apply() {
  is_dry_run && return 0
  # When applying under a custom --root (e.g. an offline test under a tempdir
  # the operator owns), root is not required — chmod and (most) chown calls
  # against owned files succeed without it. Detect the override and relax.
  if [[ "$LAYOUT_ROOT" == "/srv/xynes" ]] && [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    log_error "Apply mode against /srv/xynes requires root. Re-run with sudo, or use --dry-run."
    exit 1
  fi
}

# ------------------------------------------------------------------------------
# Convergence helper
# ------------------------------------------------------------------------------

# Resolve the absolute path for a spec row. Treats `.` as the layout root.
resolve_path() {
  local rel="$1"
  if [[ "$rel" == "." ]]; then
    printf "%s" "$LAYOUT_ROOT"
  else
    printf "%s/%s" "$LAYOUT_ROOT" "$rel"
  fi
}

# Read the current mode of a path. Portable across Linux (`stat -c`) and
# macOS BSD `stat -f`. Used so the offline test can run on macOS without
# coreutils.
current_mode() {
  local path="$1"
  if stat -c '%a' "$path" >/dev/null 2>&1; then
    stat -c '%a' "$path"
  else
    # macOS BSD stat
    stat -f '%Mp%Lp' "$path" 2>/dev/null | sed -E 's/^0*([0-9]+)$/\1/'
  fi
}

# Read the current owner ("user:group") of a path. Same portability dance.
current_owner() {
  local path="$1"
  if stat -c '%U:%G' "$path" >/dev/null 2>&1; then
    stat -c '%U:%G' "$path"
  else
    stat -f '%Su:%Sg' "$path" 2>/dev/null
  fi
}

# Numeric (UID:GID) variant. Used to resolve name-collision edge cases where
# the spec specifies a numeric owner (e.g. "999:999" for the Supabase Postgres
# container UID/GID) but the host's name lookup returns a different name for
# the same UID. On Ubuntu 22.04+ for example UID 999 is `systemd-coredump`,
# not `postgres` — but the Supabase container still maps its internal
# `postgres` user to UID 999 at mount time, so the dir IS functionally owned
# by the right entity. We compare numerically when the spec is numeric to
# avoid spurious chown calls (and FAILED ledger rows) in that case.
current_owner_numeric() {
  local path="$1"
  if stat -c '%u:%g' "$path" >/dev/null 2>&1; then
    stat -c '%u:%g' "$path"
  else
    stat -f '%u:%g' "$path" 2>/dev/null
  fi
}

# True iff the owner spec is two numeric tokens (e.g. "999:999").
spec_owner_is_numeric() {
  [[ "$1" =~ ^[0-9]+:[0-9]+$ ]]
}

# owners_match <spec-owner> <path>
#
# - Uses NUMERIC comparison when the spec is "<uid>:<gid>" (e.g. "999:999").
# - Uses NAMED comparison ("user:group") for every other spec entry.
owners_match() {
  local spec_owner="$1"
  local path="$2"
  if spec_owner_is_numeric "$spec_owner"; then
    local cur_num
    cur_num="$(current_owner_numeric "$path" || true)"
    [[ "$cur_num" == "$spec_owner" ]]
  else
    local cur_named
    cur_named="$(current_owner "$path" || true)"
    [[ "$cur_named" == "$spec_owner" ]]
  fi
}

# Normalise an octal mode read from `stat` to its 3-digit form (e.g. "750"
# rather than "0750") so it compares equal to the spec's `0750` after we
# strip the leading zero on both sides.
normalise_mode() {
  local m="$1"
  # Strip leading zeros, but keep at least one digit. `750` stays `750`.
  printf "%s" "$m" | sed -E 's/^0+([0-9])/\1/'
}

# converge_directory <rel-path> <spec-mode> <spec-owner>
#
# - mkdir -p if missing.
# - chmod only if the current mode diverges.
# - chown only if the current owner diverges.
# - Appends a single ledger row per directory:
#     APPLIED|<changes>   — at least one mutation fired.
#     ALREADY|spec match  — every attribute already correct.
#     FAILED|<reason>     — stat/chmod/chown failed.
converge_directory() {
  local rel="$1"
  local spec_mode_raw="$2"
  local spec_owner="$3"
  local abs
  abs="$(resolve_path "$rel")"

  local spec_mode_n
  spec_mode_n="$(normalise_mode "$spec_mode_raw")"

  local actions=()
  local existed="false"

  if [[ -d "$abs" ]]; then
    existed="true"
  else
    if ! run_cmd mkdir -p "$abs"; then
      ledger_add "$rel" "FAILED" "mkdir -p failed for $abs"
      return 1
    fi
    actions+=("mkdir")
  fi

  # In dry-run + first-creation case the directory does not actually exist,
  # so stat would fail. Always emit the chmod/chown WOULD lines for the spec
  # to keep the ledger informative.
  local need_chmod="false"
  local need_chown="false"

  if is_dry_run && [[ "$existed" == "false" ]]; then
    need_chmod="true"
    need_chown="true"
  elif [[ -d "$abs" ]]; then
    local cur_mode cur_mode_n
    cur_mode="$(current_mode "$abs" || true)"
    cur_mode_n="$(normalise_mode "${cur_mode:-}")"

    if [[ "$cur_mode_n" != "$spec_mode_n" ]]; then
      need_chmod="true"
    fi
    if ! owners_match "$spec_owner" "$abs"; then
      need_chown="true"
    fi
  fi

  if [[ "$need_chmod" == "true" ]]; then
    if ! run_cmd chmod "$spec_mode_raw" "$abs"; then
      ledger_add "$rel" "FAILED" "chmod $spec_mode_raw failed for $abs"
      return 1
    fi
    actions+=("chmod $spec_mode_raw")
  fi

  if [[ "$need_chown" == "true" ]]; then
    if ! run_cmd chown "$spec_owner" "$abs"; then
      ledger_add "$rel" "FAILED" "chown $spec_owner failed for $abs"
      return 1
    fi
    actions+=("chown $spec_owner")
  fi

  if [[ ${#actions[@]} -eq 0 ]]; then
    ledger_add "$rel" "ALREADY" "spec match ($spec_mode_raw $spec_owner)"
  else
    local note
    note="$(IFS=, ; printf '%s' "${actions[*]}")"
    ledger_add "$rel" "APPLIED" "$note"
  fi
}

# ------------------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------------------

print_summary() {
  log_step "Summary"
  printf "%-32s  %-8s  %s\n" "DIRECTORY" "VERDICT" "NOTE"
  printf "%-32s  %-8s  %s\n" "---------" "-------" "----"
  local row applied=0 already=0 failed=0
  for row in "${DIR_LEDGER[@]}"; do
    local dir verdict note
    dir="${row%%|*}"
    verdict="${row#*|}"; verdict="${verdict%%|*}"
    note="${row##*|}"
    printf "%-32s  %-8s  %s\n" "$dir" "$verdict" "$note"
    case "$verdict" in
      APPLIED) applied=$((applied + 1)) ;;
      ALREADY) already=$((already + 1)) ;;
      FAILED)  failed=$((failed + 1))   ;;
    esac
  done
  printf "\nMode: %s | root: %s | applied=%d already=%d failed=%d\n" \
    "$MODE" "$LAYOUT_ROOT" "$applied" "$already" "$failed"
}

# ------------------------------------------------------------------------------
# main()
# ------------------------------------------------------------------------------

main() {
  log_info "$SCRIPT_NAME starting (mode=$MODE, root=$LAYOUT_ROOT)"
  validate_root
  preflight_xynes_group_exists
  require_root_for_apply

  log_step "§2.10 Filesystem layout"

  local row failed_any=0
  for row in "${SPEC[@]}"; do
    local rel spec_mode spec_owner
    rel="${row%%|*}"
    spec_mode="${row#*|}"; spec_mode="${spec_mode%%|*}"
    spec_owner="${row##*|}"
    if ! converge_directory "$rel" "$spec_mode" "$spec_owner"; then
      failed_any=1
    fi
  done

  print_summary

  if [[ "$failed_any" -ne 0 ]]; then
    log_error "One or more directories failed to converge. Inspect the ledger above."
    exit 1
  fi
}

main
