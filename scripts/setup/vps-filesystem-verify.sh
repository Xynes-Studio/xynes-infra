#!/usr/bin/env bash
# vps-filesystem-verify.sh — Story E-4 verifier.
#
# Strictly read-only auditor for the /srv/xynes/ tree created by
# `vps-filesystem.sh`. Asserts every directory exists with the (mode, owner)
# pair specified in:
#   - infra/release/ENVIRONMENTS.md §5
#   - infra/release/VPS-SETUP.md §2.10
#
# Defense-in-depth posture:
#   - No mkdir, no chmod, no chown, no rm. Reads only `stat`.
#   - Safe to run on a foreign host: it cannot mutate state.
#   - Same --root override as vps-filesystem.sh for offline tests.
#   - Same path-traversal rejection as vps-filesystem.sh.
#
# Scope (mirrors the creator's spec byte-for-byte):
#   /srv/xynes/{dev,qa,prod}/{compose,env,supabase,supabase/db}
#   /srv/xynes/caddy/{data,log}
#   /srv/xynes/backups/{dev,qa,prod}
#   /srv/xynes/uptime-kuma/data
#
# Advisory check (does NOT fail the run):
#   /srv/xynes/<env>/env/.env.<env>  expected 0640 root:xynes (§2.15).
#   This file is created later by §2.15, not by E-4. The verifier reports its
#   state when present, never penalises its absence.
#
# Per-row verdicts:
#   OK            — directory exists with the correct mode + owner.
#   MISSING       — directory does not exist.
#   MODE_DRIFT    — directory exists but its mode differs from the spec.
#   OWNER_DRIFT   — directory exists but its owner differs from the spec.
#   BOTH_DRIFT    — both mode AND owner diverge.
#
# Exit codes:
#   0   Every row OK (advisory rows may still report mode/owner; they don't gate).
#   1   At least one row reports MODE_DRIFT, OWNER_DRIFT, or BOTH_DRIFT.
#   2   At least one row reports MISSING.
#   64  Unknown flag.
#
# Notes:
#   - When BOTH MISSING and DRIFT rows are present, exit 2 wins (MISSING is the
#     stronger signal: nothing to drift if it doesn't exist).
#   - E-6 ("chown -R xynes_<env>_deploy:xynes /srv/xynes/<env>/") will flip the
#     per-env subtree ownership AWAY from root:xynes. After E-6 lands, this
#     verifier WILL report OWNER_DRIFT on those rows. That is expected and
#     will be addressed by E-6's own follow-up (extending the spec or layering
#     a separate post-E-6 verifier). E-4 owns the directory creation only.
#
# Usage:
#   bash scripts/setup/vps-filesystem-verify.sh --help
#   bash scripts/setup/vps-filesystem-verify.sh
#   bash scripts/setup/vps-filesystem-verify.sh --root /tmp/fake

set -euo pipefail

# ------------------------------------------------------------------------------
# Defaults
# ------------------------------------------------------------------------------

SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"

LAYOUT_ROOT="/srv/xynes"
DRY_RUN="false"  # Accepted for symmetry with vps-filesystem.sh; verifier is
                 # always read-only so the flag is a no-op.

# Verdict ledger:
#   <relative-path>|<verdict>|<expected>|<actual>
declare -a ROW_LEDGER=()

# Tallies
N_OK=0
N_DRIFT=0
N_MISSING=0

# ------------------------------------------------------------------------------
# Spec table — keep byte-for-byte in sync with vps-filesystem.sh's $SPEC.
# The offline contract test asserts both files declare the same paths.
# ------------------------------------------------------------------------------

SPEC=(
  ".|0755|root:root"
  "dev|0755|root:xynes"
  "qa|0755|root:xynes"
  "prod|0755|root:xynes"
  "caddy|0755|root:root"
  "backups|0700|root:root"
  "uptime-kuma|0755|root:root"

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

  "caddy/data|0700|root:root"
  "caddy/log|0755|root:root"

  "backups/dev|0700|root:root"
  "backups/qa|0700|root:root"
  "backups/prod|0700|root:root"

  "uptime-kuma/data|0755|root:root"
)

# Advisory file checks (§2.15). One row per env. The file path is built at
# runtime; the spec value here is the expected (mode, owner) when the file
# exists.
ADVISORY_FILES=(
  "dev/env/.env.dev|0640|root:xynes"
  "qa/env/.env.qa|0640|root:xynes"
  "prod/env/.env.prod|0640|root:xynes"
)

# ------------------------------------------------------------------------------
# Usage
# ------------------------------------------------------------------------------

print_usage() {
  cat <<EOF
$SCRIPT_NAME — Verify the /srv/xynes/ filesystem layout per VPS-SETUP.md §2.10
                + ENVIRONMENTS.md §5 (Story E-4).

USAGE:
  $SCRIPT_NAME [--root <path>] [--dry-run]
  $SCRIPT_NAME --help

OPTIONS:
  --root <path>    Override the layout root. Default: /srv/xynes. The path
                   must be absolute and match ^/[A-Za-z0-9._/-]+\$. Useful
                   for offline tests under \$(mktemp -d).
  --dry-run        Accepted for argv symmetry with vps-filesystem.sh. The
                   verifier is strictly read-only, so this flag is a no-op.
  --help, -h       Print this usage and exit 0.

VERDICTS PER ROW:
  OK            — directory exists with the correct mode + owner.
  MISSING       — directory does not exist.
  MODE_DRIFT    — directory exists but its mode differs from the spec.
  OWNER_DRIFT   — directory exists but its owner differs from the spec.
  BOTH_DRIFT    — both mode AND owner diverge.

NUMERIC OWNER SPECS:
  When a spec entry uses a numeric "<uid>:<gid>" owner (e.g. "999:999" for
  the Supabase Postgres container UID/GID), the verifier compares against
  the host's NUMERIC owner. This means name-collision edge cases (Ubuntu
  22.04+ pre-installs systemd-coredump at UID 999) DO NOT trip a spurious
  OWNER_DRIFT row — the Supabase container maps its internal postgres user
  to UID 999 at mount time and functionally owns the directory.

ADVISORY ROWS (do NOT gate the exit code):
  /srv/xynes/<env>/env/.env.<env>  expected 0640 root:xynes (created by §2.15).

EXIT CODES:
  0   Every spec row OK.
  1   At least one MODE_DRIFT / OWNER_DRIFT / BOTH_DRIFT row (no MISSING).
  2   At least one MISSING row (stronger signal than drift).
  64  Unknown flag.

EXAMPLES:
  # Verify the live VPS tree.
  sudo bash $SCRIPT_NAME

  # Verify an offline test tree.
  bash $SCRIPT_NAME --root "\$(mktemp -d)"
EOF
}

# ------------------------------------------------------------------------------
# Argv parsing
# ------------------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root)
      [[ $# -ge 2 ]] || { echo "ERROR: --root requires a value" >&2; exit 1; }
      LAYOUT_ROOT="$2"
      shift 2
      ;;
    --root=*)
      LAYOUT_ROOT="${1#*=}"
      shift
      ;;
    --dry-run)
      DRY_RUN="true"
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

validate_root() {
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

resolve_path() {
  local rel="$1"
  if [[ "$rel" == "." ]]; then
    printf "%s" "$LAYOUT_ROOT"
  else
    printf "%s/%s" "$LAYOUT_ROOT" "$rel"
  fi
}

current_mode() {
  local path="$1"
  if stat -c '%a' "$path" >/dev/null 2>&1; then
    stat -c '%a' "$path"
  else
    stat -f '%Mp%Lp' "$path" 2>/dev/null | sed -E 's/^0*([0-9]+)$/\1/'
  fi
}

current_owner() {
  local path="$1"
  if stat -c '%U:%G' "$path" >/dev/null 2>&1; then
    stat -c '%U:%G' "$path"
  else
    stat -f '%Su:%Sg' "$path" 2>/dev/null
  fi
}

# Numeric (UID:GID) variant of current_owner. Used to resolve name-collision
# edge cases where the spec specifies a numeric owner (e.g. "999:999" for the
# Supabase Postgres container UID/GID) but the host's name lookup returns a
# different name for the same UID. On Ubuntu 22.04+ for example UID 999 is
# `systemd-coredump`, not `postgres` — but the Supabase container still maps
# its internal `postgres` user to UID 999 at mount time, so the dir IS
# functionally owned by the right entity. We compare numerically when the
# spec is numeric to avoid false-positive OWNER_DRIFT rows in that case.
current_owner_numeric() {
  local path="$1"
  if stat -c '%u:%g' "$path" >/dev/null 2>&1; then
    stat -c '%u:%g' "$path"
  else
    stat -f '%u:%g' "$path" 2>/dev/null
  fi
}

normalise_mode() {
  local m="$1"
  printf "%s" "$m" | sed -E 's/^0+([0-9])/\1/'
}

# True iff the owner spec is two numeric tokens (e.g. "999:999"). When this
# is the case we compare against the numeric UID:GID rather than the named
# UID:GID — see current_owner_numeric for the rationale.
spec_owner_is_numeric() {
  [[ "$1" =~ ^[0-9]+:[0-9]+$ ]]
}

# owners_match <spec-owner> <path>
#
# Smart comparator that:
#   - Uses NUMERIC comparison when the spec is "<uid>:<gid>" (e.g. "999:999").
#     This sidesteps name-collision edge cases (Ubuntu's systemd-coredump = UID
#     999 vs Supabase container's internal postgres = UID 999).
#   - Uses NAMED comparison ("user:group") for every other spec entry.
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

# owner_for_report <spec-owner> <path>
#
# Returns the actual owner string to PRINT in the verifier report. If the
# spec is numeric, we print the numeric UID:GID (matches what we're
# comparing against). Otherwise we print the named owner.
owner_for_report() {
  local spec_owner="$1"
  local path="$2"
  if spec_owner_is_numeric "$spec_owner"; then
    current_owner_numeric "$path" || true
  else
    current_owner "$path" || true
  fi
}

# ------------------------------------------------------------------------------
# Verification
# ------------------------------------------------------------------------------

verify_directory() {
  # verify_directory <rel-path> <spec-mode> <spec-owner>
  local rel="$1"
  local spec_mode="$2"
  local spec_owner="$3"
  local abs
  abs="$(resolve_path "$rel")"

  if [[ ! -e "$abs" ]]; then
    ROW_LEDGER+=("$rel|MISSING|$spec_mode $spec_owner|-")
    N_MISSING=$((N_MISSING + 1))
    return
  fi
  if [[ ! -d "$abs" ]]; then
    # Path exists but is a file or symlink-to-file. Treat as MODE_DRIFT
    # (the strongest non-MISSING signal we can surface without inventing a
    # new verdict).
    ROW_LEDGER+=("$rel|MODE_DRIFT|$spec_mode $spec_owner|not a directory")
    N_DRIFT=$((N_DRIFT + 1))
    return
  fi

  local cur_mode cur_owner_display cur_mode_n spec_mode_n
  cur_mode="$(current_mode "$abs" || true)"
  cur_owner_display="$(owner_for_report "$spec_owner" "$abs")"
  cur_mode_n="$(normalise_mode "${cur_mode:-}")"
  spec_mode_n="$(normalise_mode "$spec_mode")"

  local mode_ok="true" owner_ok="true"
  [[ "$cur_mode_n" != "$spec_mode_n" ]] && mode_ok="false"
  if ! owners_match "$spec_owner" "$abs"; then
    owner_ok="false"
  fi

  if [[ "$mode_ok" == "true" && "$owner_ok" == "true" ]]; then
    ROW_LEDGER+=("$rel|OK|$spec_mode $spec_owner|$cur_mode $cur_owner_display")
    N_OK=$((N_OK + 1))
    return
  fi

  local verdict
  if [[ "$mode_ok" == "false" && "$owner_ok" == "false" ]]; then
    verdict="BOTH_DRIFT"
  elif [[ "$mode_ok" == "false" ]]; then
    verdict="MODE_DRIFT"
  else
    verdict="OWNER_DRIFT"
  fi
  ROW_LEDGER+=("$rel|$verdict|$spec_mode $spec_owner|$cur_mode $cur_owner_display")
  N_DRIFT=$((N_DRIFT + 1))
}

verify_advisory_file() {
  # verify_advisory_file <rel-path> <spec-mode> <spec-owner>
  # Prints an advisory row; never increments OK/DRIFT/MISSING tallies.
  local rel="$1"
  local spec_mode="$2"
  local spec_owner="$3"
  local abs
  abs="$(resolve_path "$rel")"

  if [[ ! -e "$abs" ]]; then
    ROW_LEDGER+=("$rel|ADV_ABSENT|$spec_mode $spec_owner|§2.15 not applied yet")
    return
  fi
  if [[ ! -f "$abs" ]]; then
    ROW_LEDGER+=("$rel|ADV_NOTFILE|$spec_mode $spec_owner|expected regular file")
    return
  fi

  local cur_mode cur_owner_display cur_mode_n spec_mode_n
  cur_mode="$(current_mode "$abs" || true)"
  cur_owner_display="$(owner_for_report "$spec_owner" "$abs")"
  cur_mode_n="$(normalise_mode "${cur_mode:-}")"
  spec_mode_n="$(normalise_mode "$spec_mode")"

  if [[ "$cur_mode_n" == "$spec_mode_n" ]] && owners_match "$spec_owner" "$abs"; then
    ROW_LEDGER+=("$rel|ADV_OK|$spec_mode $spec_owner|$cur_mode $cur_owner_display")
  else
    ROW_LEDGER+=("$rel|ADV_DRIFT|$spec_mode $spec_owner|$cur_mode $cur_owner_display")
  fi
}

# ------------------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------------------

print_summary() {
  log_step "Verifier report"
  printf "%-32s  %-12s  %-18s  %s\n" "DIRECTORY" "VERDICT" "EXPECTED" "ACTUAL"
  printf "%-32s  %-12s  %-18s  %s\n" "---------" "-------" "--------" "------"
  local row
  for row in "${ROW_LEDGER[@]}"; do
    local dir verdict expected actual
    dir="${row%%|*}"
    verdict="${row#*|}"; verdict="${verdict%%|*}"
    expected="${row#*|*|}"; expected="${expected%|*}"
    actual="${row##*|}"
    printf "%-32s  %-12s  %-18s  %s\n" "$dir" "$verdict" "$expected" "$actual"
  done
  printf "\nMode: verify | root: %s | ok=%d drift=%d missing=%d\n" \
    "$LAYOUT_ROOT" "$N_OK" "$N_DRIFT" "$N_MISSING"
}

# ------------------------------------------------------------------------------
# main()
# ------------------------------------------------------------------------------

main() {
  log_info "$SCRIPT_NAME starting (root=$LAYOUT_ROOT, dry-run=$DRY_RUN)"
  validate_root

  log_step "§2.10 Filesystem layout audit"
  local row
  for row in "${SPEC[@]}"; do
    local rel spec_mode spec_owner
    rel="${row%%|*}"
    spec_mode="${row#*|}"; spec_mode="${spec_mode%%|*}"
    spec_owner="${row##*|}"
    verify_directory "$rel" "$spec_mode" "$spec_owner"
  done

  log_step "§2.15 .env.<env> file audit (advisory only)"
  for row in "${ADVISORY_FILES[@]}"; do
    local rel spec_mode spec_owner
    rel="${row%%|*}"
    spec_mode="${row#*|}"; spec_mode="${spec_mode%%|*}"
    spec_owner="${row##*|}"
    verify_advisory_file "$rel" "$spec_mode" "$spec_owner"
  done

  print_summary

  # Exit-code precedence: MISSING (2) wins over DRIFT (1) wins over OK (0).
  if [[ "$N_MISSING" -gt 0 ]]; then
    log_error "$N_MISSING directory(ies) missing under $LAYOUT_ROOT."
    log_error "Run scripts/setup/vps-filesystem.sh to converge."
    exit 2
  fi
  if [[ "$N_DRIFT" -gt 0 ]]; then
    log_error "$N_DRIFT directory(ies) diverge from §2.10 spec under $LAYOUT_ROOT."
    log_error "Run scripts/setup/vps-filesystem.sh to converge."
    exit 1
  fi
  log_info "All $N_OK directories match §2.10 spec. ✅"
}

main
