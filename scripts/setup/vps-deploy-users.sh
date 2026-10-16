#!/usr/bin/env bash
# vps-deploy-users.sh — Story E-6 (MVP release plan, Phase 2 core).
#
# Idempotently creates the three per-environment deploy users on a hardened
# Ubuntu LTS host, per:
#   - infra/release/VPS-SETUP.md §2.12
#   - infra/release/SECRETS.md §4 (SSH deploy key recipe)
#
# For each env in {dev, qa, prod}:
#   1. Create the system user `xynes_<env>_deploy` with --disabled-password
#      --gecos "" (SSH-key login only; no shell password ever set).
#   2. Add the user to the `docker` group (so the env's deploy workflow can
#      `docker compose up -d --pull always` over SSH without sudo).
#   3. Drop the matching deploy SSH public key into
#      /home/xynes_<env>_deploy/.ssh/authorized_keys (mode 0600, owner
#      <user>:<user>). Append-if-missing — never overwrite operator-added
#      keys; never echo the key bytes to stdout/stderr.
#   4. chown -R xynes_<env>_deploy:xynes /srv/xynes/<env>/ so the deploy
#      user owns the env's compose / env / supabase / data subtree.
#
# Deliberately out of scope (do NOT add here):
#   - Base OS hardening (E-2, vps-bootstrap.sh §2.2–2.8).
#   - Docker engine install + log driver (E-3, vps-bootstrap.sh §2.9).
#   - Docker networks (E-5, vps-bootstrap.sh §2.11).
#   - Filesystem layout under /srv/xynes (E-4, vps-filesystem.sh).
#   - Restricted shell (rbash / curated allowlist) — VPS-SETUP §2.12 defers
#     to Phase 5 explicitly. Not blocking.
#   - GitHub Actions secret upload — that step lives in the operator's
#     password manager and the SECRETS.md §2 register; this script never
#     touches private keys.
#
# Behavior contract:
#   - Conservative idempotency: detect current state first (user existence,
#     group membership, authorized_keys contents, /srv/xynes/<env>/ owner).
#     Mutate ONLY on divergence. Re-running on a converged host is a no-op.
#   - Fail-closed on missing prerequisites in apply mode:
#       * Apex boundary marker exists or is a symlink → legacy apply refused.
#         Dry-run and help remain available; the marker is never read.
#       * `xynes` group missing → exit 1 with cross-link to E-2 §2.2.
#       * /srv/xynes/<env>/ missing → exit 1 with cross-link to E-4.
#     Dry-run downgrades both to a WARN so the operator can preview on a
#     dev laptop without the full prerequisite chain.
#   - Public key validation rejects:
#       * private-key material (any line starting with BEGIN OPENSSH PRIVATE
#         KEY / BEGIN RSA PRIVATE KEY / BEGIN EC PRIVATE KEY etc.).
#       * multi-line input (a single pubkey is one line).
#       * unsupported prefix (must be one of: ssh-ed25519, ssh-rsa,
#         ecdsa-sha2-nistp{256,384,521}, sk-* hardware variants).
#   - Logs NEVER echo pubkey bytes. Audit lines use the SHA256 fingerprint
#     (`ssh-keygen -lf - <<< $key | awk '{print $2}'`) when available, or
#     a `sha256(<algo>:<base64 prefix>...8 chars)` fallback otherwise. The
#     full key is required to disk via authorized_keys; the fingerprint is
#     used only for operator-visible "added key X to user Y" log messages.
#   - Every modified file backed up to <path>.bak.YYYYmmddHHMMSS.
#   - --dry-run prints "WOULD: <cmd>" for every intended mutation and
#     performs zero side effects. No root needed for --dry-run.
#   - --only <env> restricts the run to one env (useful for partial
#     rollouts and re-runs after a rotation).
#
# Usage:
#   bash scripts/setup/vps-deploy-users.sh --help
#   bash scripts/setup/vps-deploy-users.sh --dry-run \
#       --dev-pubkey-file ./xynes_dev_deploy.pub \
#       --qa-pubkey-file ./xynes_qa_deploy.pub \
#       --prod-pubkey-file ./xynes_prod_deploy.pub
#   sudo bash scripts/setup/vps-deploy-users.sh \
#       --keys-file /root/deploy-keys.env
#   sudo bash scripts/setup/vps-deploy-users.sh \
#       --only dev --dev-pubkey-file /root/xynes_dev_deploy.pub
#
# --keys-file format (one KEY=VALUE per line; KEY is one of DEV/QA/PROD;
# blank lines and `#` comments allowed):
#
#   DEV=ssh-ed25519 AAAAC3... deploy:dev:20260605
#   QA=ssh-ed25519 AAAAC3... deploy:qa:20260605
#   PROD=ssh-ed25519 AAAAC3... deploy:prod:20260605
#
# Exit codes:
#   0   Apply or dry-run completed; ledger summary printed.
#   1   Validation error (bad key, missing prerequisite, runtime failure).
#   64  Unknown flag.

set -euo pipefail

# ------------------------------------------------------------------------------
# Defaults
# ------------------------------------------------------------------------------

SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"

MODE="apply"                 # apply | dry-run
LAYOUT_ROOT="/srv/xynes"     # E-4 root; overridable via --srv-root for offline tests
ONLY_ENV=""                  # one of: dev | qa | prod (empty = all three)

# Per-env public-key sources. Precedence (highest first):
#   1. --<env>-pubkey '<key>'
#   2. --<env>-pubkey-file <path>
#   3. --keys-file <path> entry
DEV_PUBKEY=""
QA_PUBKEY=""
PROD_PUBKEY=""
# *_PUBKEY_FILE are read via indirect expansion (${!file_var_name}) inside
# resolve_env_pubkey. The inline SC2034 disables at the `=*` assignment forms
# below cover shellcheck's flow analysis; we leave the bare declarations
# here so the parse-time defaults are obvious.
DEV_PUBKEY_FILE=""
QA_PUBKEY_FILE=""
PROD_PUBKEY_FILE=""
KEYS_FILE=""

# The supported environments. Single source of truth — adding a future env is
# a one-line spec change + the matching test guard.
ENVS=(dev qa prod)

# Section action ledger:
#   APPLIED — script mutated state.
#   ALREADY — already matched spec; no mutation needed.
#   SKIPPED — opted out (e.g. --only restricted away from this env).
#   FAILED  — section hit a non-recoverable error.
declare -a USER_LEDGER=()

# Timestamp for backup suffixes (one per script run).
RUN_TS="$(date -u +%Y%m%d%H%M%S)"

# ------------------------------------------------------------------------------
# Usage
# ------------------------------------------------------------------------------

print_usage() {
  cat <<EOF
$SCRIPT_NAME — Create per-env deploy users + scoped SSH keys per VPS-SETUP.md
                §2.12 (Story E-6).

USAGE:
  $SCRIPT_NAME [--dry-run] [--only <env>] [--srv-root <path>] \\
      [--dev-pubkey '<key>' | --dev-pubkey-file <path>] \\
      [--qa-pubkey '<key>'  | --qa-pubkey-file <path>] \\
      [--prod-pubkey '<key>' | --prod-pubkey-file <path>] \\
      [--keys-file <path>]
  $SCRIPT_NAME --help

OPTIONS:
  --dry-run                  Print "WOULD: <cmd>" for every intended mutation.
                             No side effects. Does not require root.
  --only <env>               Restrict the run to one env (dev | qa | prod).
                             Useful for partial rollouts and key rotation.
  --srv-root <path>          Override /srv/xynes root (offline-test escape
                             hatch). Validated against ^/[A-Za-z0-9._/-]+\$
                             with explicit '..' rejection.
  --<env>-pubkey '<key>'     SSH public key for the env, as a single string.
  --<env>-pubkey-file <path> Path to a file containing the SSH public key
                             (first line is used).
  --keys-file <path>         Single file with KEY=VALUE lines: DEV=, QA=,
                             PROD=. Blank lines and '#' comments allowed.
                             Per-env --pubkey / --pubkey-file flags override
                             the corresponding --keys-file entry.
  --help, -h                 Print this usage and exit 0.

WHAT THIS SCRIPT DOES FOR EACH ENV:
  1. adduser xynes_<env>_deploy --disabled-password --gecos ""  (idempotent).
  2. usermod -aG docker xynes_<env>_deploy                       (idempotent).
  2b. usermod -aG xynes xynes_<env>_deploy                       (idempotent).
     (Required so the deploy user can read /srv/xynes/<env>/env/.env.<env>
     via group-read at mode 0640 - the F-2-FU-1 carve-out keeps the file
     owned by root:xynes.)
  3. Append the matching public key to
     /home/xynes_<env>_deploy/.ssh/authorized_keys (mode 0600,
     owner xynes_<env>_deploy:xynes_<env>_deploy). Append-if-missing.
  4. chown -R xynes_<env>_deploy:xynes /srv/xynes/<env>/.

CONTRACT:
  - Conservative idempotency: zero mutations on a converged host.
  - Apply refuses once /etc/xynes/deploy-boundary.json exists or is a symlink.
    Use the Apex deployment boundary; --dry-run and --help remain available.
  - Fail-closed in apply mode if 'xynes' group missing (E-2 §2.2) or
    /srv/xynes/<env>/ missing (E-4). Dry-run downgrades both to a WARN.
  - Public keys are NEVER echoed to stdout/stderr. Audit lines use the
    SHA256 fingerprint via 'ssh-keygen -lf -' when available.
  - Private-key material is rejected at parse time (multi-line input,
    BEGIN ... PRIVATE KEY headers, missing supported prefix).
  - Every modified file backed up to <path>.bak.$RUN_TS.

EXIT CODES:
  0   Apply or dry-run completed; ledger summary printed.
  1   Validation error or runtime failure.
  64  Unknown flag.

EXAMPLES:
  # Preview the run on a dev laptop (no root, no real VPS).
  bash $SCRIPT_NAME --dry-run \\
      --dev-pubkey-file ./xynes_dev_deploy.pub \\
      --qa-pubkey-file ./xynes_qa_deploy.pub \\
      --prod-pubkey-file ./xynes_prod_deploy.pub

  # Apply on the VPS (run as root).
  sudo bash $SCRIPT_NAME --keys-file /root/deploy-keys.env

  # Rotate the dev deploy key only.
  sudo bash $SCRIPT_NAME --only dev --dev-pubkey-file /root/xynes_dev_deploy.pub
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
    --only)
      [[ $# -ge 2 ]] || { echo "ERROR: --only requires a value" >&2; exit 1; }
      ONLY_ENV="$2"
      shift 2
      ;;
    --only=*)
      ONLY_ENV="${1#*=}"
      shift
      ;;
    --srv-root)
      [[ $# -ge 2 ]] || { echo "ERROR: --srv-root requires a value" >&2; exit 1; }
      LAYOUT_ROOT="$2"
      shift 2
      ;;
    --srv-root=*)
      LAYOUT_ROOT="${1#*=}"
      shift
      ;;
    --dev-pubkey)
      [[ $# -ge 2 ]] || { echo "ERROR: --dev-pubkey requires a value" >&2; exit 1; }
      DEV_PUBKEY="$2"
      shift 2
      ;;
    --dev-pubkey=*)
      DEV_PUBKEY="${1#*=}"
      shift
      ;;
    --qa-pubkey)
      [[ $# -ge 2 ]] || { echo "ERROR: --qa-pubkey requires a value" >&2; exit 1; }
      QA_PUBKEY="$2"
      shift 2
      ;;
    --qa-pubkey=*)
      QA_PUBKEY="${1#*=}"
      shift
      ;;
    --prod-pubkey)
      [[ $# -ge 2 ]] || { echo "ERROR: --prod-pubkey requires a value" >&2; exit 1; }
      PROD_PUBKEY="$2"
      shift 2
      ;;
    --prod-pubkey=*)
      PROD_PUBKEY="${1#*=}"
      shift
      ;;
    --dev-pubkey-file)
      [[ $# -ge 2 ]] || { echo "ERROR: --dev-pubkey-file requires a value" >&2; exit 1; }
      DEV_PUBKEY_FILE="$2"
      shift 2
      ;;
    --dev-pubkey-file=*)
      # shellcheck disable=SC2034  # read via indirect expansion in resolve_env_pubkey
      DEV_PUBKEY_FILE="${1#*=}"
      shift
      ;;
    --qa-pubkey-file)
      [[ $# -ge 2 ]] || { echo "ERROR: --qa-pubkey-file requires a value" >&2; exit 1; }
      QA_PUBKEY_FILE="$2"
      shift 2
      ;;
    --qa-pubkey-file=*)
      # shellcheck disable=SC2034  # read via indirect expansion in resolve_env_pubkey
      QA_PUBKEY_FILE="${1#*=}"
      shift
      ;;
    --prod-pubkey-file)
      [[ $# -ge 2 ]] || { echo "ERROR: --prod-pubkey-file requires a value" >&2; exit 1; }
      PROD_PUBKEY_FILE="$2"
      shift 2
      ;;
    --prod-pubkey-file=*)
      # shellcheck disable=SC2034  # read via indirect expansion in resolve_env_pubkey
      PROD_PUBKEY_FILE="${1#*=}"
      shift
      ;;
    --keys-file)
      [[ $# -ge 2 ]] || { echo "ERROR: --keys-file requires a value" >&2; exit 1; }
      KEYS_FILE="$2"
      shift 2
      ;;
    --keys-file=*)
      KEYS_FILE="${1#*=}"
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
  # ledger_add <env> <verdict> <note>
  USER_LEDGER+=("$1|$2|$3")
}

is_dry_run() { [[ "$MODE" == "dry-run" ]]; }

require_legacy_boundary_absent() {
  is_dry_run && return 0
  # Fixed host policy: --srv-root and caller environment cannot redirect it.
  # -L also refuses dangling symlinks; no marker/target contents are read.
  local boundary="/etc/xynes/deploy-boundary.json"
  if [[ -e "$boundary" || -L "$boundary" ]]; then
    log_error "Apex deployment boundary is installed; legacy deploy-user apply refused."
    return 1
  fi
}

# Run a mutating command unless we're in dry-run mode.
# Usage: run_cmd <command> [args...]
run_cmd() {
  if is_dry_run; then
    printf "WOULD: %s\n" "$*"
    return 0
  fi
  "$@"
}

require_root_for_apply() {
  is_dry_run && return 0
  # When applying under a custom --srv-root (e.g. an offline test under a
  # tempdir the operator owns), root is not required — useradd would still
  # be needed but is unreachable; the test patches argv to --dry-run only.
  if [[ "$LAYOUT_ROOT" == "/srv/xynes" ]] && [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    log_error "Apply mode against /srv/xynes requires root. Re-run with sudo, or use --dry-run."
    exit 1
  fi
}

backup_file() {
  # Copy <path> to <path>.bak.$RUN_TS if the file exists. No-op in dry-run.
  local path="$1"
  if [[ ! -e "$path" ]]; then
    return 0
  fi
  if is_dry_run; then
    printf "WOULD: cp -a %s %s.bak.%s\n" "$path" "$path" "$RUN_TS"
    return 0
  fi
  cp -a "$path" "$path.bak.$RUN_TS"
}

# ------------------------------------------------------------------------------
# Path / arg validation
# ------------------------------------------------------------------------------

validate_root() {
  # --srv-root must be absolute and contain only safe characters. Reject `..`
  # and `~` at parse time so a hostile caller cannot escape the intended root
  # via path traversal or shell expansion.
  if [[ ! "$LAYOUT_ROOT" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
    log_error "Invalid --srv-root path: $LAYOUT_ROOT"
    log_error "Must be absolute and match ^/[A-Za-z0-9._/-]+\$ (no .., no ~, no spaces)."
    exit 1
  fi
  if [[ "$LAYOUT_ROOT" == *".."* ]]; then
    log_error "Invalid --srv-root path: $LAYOUT_ROOT (contains '..')"
    exit 1
  fi
}

validate_only_env() {
  if [[ -z "$ONLY_ENV" ]]; then
    return 0
  fi
  case "$ONLY_ENV" in
    dev|qa|prod) return 0 ;;
    *)
      log_error "Invalid --only value: $ONLY_ENV (must be one of: dev | qa | prod)"
      exit 1
      ;;
  esac
}

# ------------------------------------------------------------------------------
# Public-key validation
# ------------------------------------------------------------------------------

# Validate an SSH public key string. Single line; one of the supported algo
# prefixes; explicitly rejects PRIVATE KEY material.
#
# Returns 0 on success; non-zero (with a sanitised error to stderr) otherwise.
#
# IMPORTANT: this function MUST NOT echo any portion of the key on the error
# path. Operators may paste a private key by mistake; we must surface "looks
# like a private key" without ever printing the key bytes.
validate_pubkey() {
  local key="$1"
  local env="${2:-?}"

  if [[ -z "$key" ]]; then
    log_error "($env) public key is empty"
    return 1
  fi

  # Reject multi-line input. A single pubkey is one line. Private keys are
  # always multi-line (they have BEGIN/END headers).
  if [[ "$(printf '%s' "$key" | wc -l | tr -d ' ')" -ne 0 ]]; then
    log_error "($env) public key spans multiple lines — refusing (likely private key material)"
    return 1
  fi

  # Belt-and-suspenders: reject anything that looks like a private-key
  # header even on a single line (e.g. an operator pasted just the first
  # line of a private key).
  case "$key" in
    *"BEGIN OPENSSH PRIVATE KEY"*|*"BEGIN RSA PRIVATE KEY"*|*"BEGIN EC PRIVATE KEY"*|*"BEGIN DSA PRIVATE KEY"*|*"BEGIN PRIVATE KEY"*|*"BEGIN ENCRYPTED PRIVATE KEY"*)
      log_error "($env) public key contains private-key header — refusing"
      return 1
      ;;
  esac

  # Trim leading/trailing whitespace for the prefix check.
  local trimmed="${key#"${key%%[![:space:]]*}"}"
  trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
  case "$trimmed" in
    "ssh-ed25519 "*|"ssh-rsa "*|"ecdsa-sha2-nistp256 "*|"ecdsa-sha2-nistp384 "*|"ecdsa-sha2-nistp521 "*|"sk-ssh-ed25519@openssh.com "*|"sk-ecdsa-sha2-nistp256@openssh.com "*)
      return 0
      ;;
  esac
  log_error "($env) public key failed validation (unsupported algorithm prefix)"
  return 1
}

# Compute a public-key fingerprint for audit logging. NEVER returns the raw
# key bytes. Prefers `ssh-keygen -lf -` (SHA256 fingerprint). Falls back to a
# deterministic short hash of the algorithm + first 16 base64 chars when
# ssh-keygen is unavailable (e.g. minimal CI images).
pubkey_fingerprint() {
  local key="$1"
  local fp=""
  if command -v ssh-keygen >/dev/null 2>&1; then
    # ssh-keygen -lf - reads from stdin. Format: "<bits> SHA256:<hash> <comment> (<algo>)".
    fp="$(printf '%s\n' "$key" | ssh-keygen -lf - 2>/dev/null | awk '{print $2}')" || fp=""
  fi
  if [[ -z "$fp" ]]; then
    # Fallback: extract <algo> and the first 16 base64 chars; never the rest.
    local algo prefix
    algo="$(printf '%s' "$key" | awk '{print $1}')"
    prefix="$(printf '%s' "$key" | awk '{print $2}' | cut -c1-16)"
    fp="${algo}:${prefix}..."
  fi
  printf "%s" "$fp"
}

# ------------------------------------------------------------------------------
# Key resolution
# ------------------------------------------------------------------------------

# Read a single public key from a file. Returns the first non-empty,
# non-comment line. Never echoes the key to stdout (it's the return channel
# only — callers route it directly into validators / authorized_keys).
read_pubkey_file() {
  local path="$1"
  local env="$2"
  if [[ ! -r "$path" ]]; then
    log_error "($env) pubkey file not readable: $path"
    return 1
  fi
  awk 'NF && $0 !~ /^[[:space:]]*#/ { print; exit }' "$path"
}

# Parse a --keys-file into the three per-env globals (only when those globals
# are still empty — explicit per-env flags take precedence).
#
# Format: KEY=VALUE per line, where KEY is one of DEV|QA|PROD (case-insensitive)
# and VALUE is a single SSH public key string. Blank lines and '#' comments
# allowed.
parse_keys_file() {
  local path="$1"
  if [[ ! -r "$path" ]]; then
    log_error "--keys-file not readable: $path"
    exit 1
  fi
  local line key val
  while IFS= read -r line || [[ -n "$line" ]]; do
    # Skip blank + comment lines.
    if [[ -z "${line//[[:space:]]/}" ]] || [[ "${line#"${line%%[![:space:]]*}"}" == "#"* ]]; then
      continue
    fi
    if [[ "$line" != *=* ]]; then
      log_error "--keys-file malformed line (missing '='): (line redacted)"
      exit 1
    fi
    key="${line%%=*}"
    val="${line#*=}"
    # Strip whitespace around the key only; preserve value verbatim so the
    # base64 body survives untouched.
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    case "$(printf '%s' "$key" | tr '[:upper:]' '[:lower:]')" in
      dev)  [[ -z "$DEV_PUBKEY"  ]] && DEV_PUBKEY="$val"  ;;
      qa)   [[ -z "$QA_PUBKEY"   ]] && QA_PUBKEY="$val"   ;;
      prod) [[ -z "$PROD_PUBKEY" ]] && PROD_PUBKEY="$val" ;;
      *)
        log_error "--keys-file unknown env key: $key (expected: DEV | QA | PROD)"
        exit 1
        ;;
    esac
  done < "$path"
}

# Resolve the public key for one env, honouring the precedence:
#   1. --<env>-pubkey  (already in the *_PUBKEY var)
#   2. --<env>-pubkey-file  (read into the *_PUBKEY var)
#   3. --keys-file entry  (already merged into *_PUBKEY var by parse_keys_file)
resolve_env_pubkey() {
  local env="$1"
  local var_name file_var_name
  case "$env" in
    dev)  var_name="DEV_PUBKEY";  file_var_name="DEV_PUBKEY_FILE"  ;;
    qa)   var_name="QA_PUBKEY";   file_var_name="QA_PUBKEY_FILE"   ;;
    prod) var_name="PROD_PUBKEY"; file_var_name="PROD_PUBKEY_FILE" ;;
    *)
      log_error "resolve_env_pubkey: unknown env $env"
      return 1
      ;;
  esac
  local current="${!var_name}"
  local file_path="${!file_var_name}"
  if [[ -n "$current" ]]; then
    # Direct --<env>-pubkey wins; nothing to do.
    return 0
  fi
  if [[ -n "$file_path" ]]; then
    local key
    if ! key="$(read_pubkey_file "$file_path" "$env")"; then
      return 1
    fi
    printf -v "$var_name" '%s' "$key"
  fi
}

# ------------------------------------------------------------------------------
# Prerequisite checks (E-2 / E-4)
# ------------------------------------------------------------------------------

preflight_xynes_group_exists() {
  # The `chown -R xynes_<env>_deploy:xynes /srv/xynes/<env>/` step requires
  # the `xynes` group. If E-2 (§2.2) was never applied, this script would
  # fail late at chown time. Detect early and surface a clear cross-link.
  local has_group="false"
  if command -v getent >/dev/null 2>&1; then
    if getent group xynes >/dev/null 2>&1; then
      has_group="true"
    fi
  elif [[ -r /etc/group ]] && grep -qE '^xynes:' /etc/group; then
    has_group="true"
  fi

  if [[ "$has_group" == "true" ]]; then
    return 0
  fi

  if is_dry_run; then
    log_warn "'xynes' group not found on this host."
    log_warn "Apply mode would FAIL chown of /srv/xynes/<env>/ subtree."
    log_warn "On the VPS, run vps-bootstrap.sh first (E-2, §2.2)."
    return 0
  fi

  if [[ "$LAYOUT_ROOT" != "/srv/xynes" ]]; then
    log_warn "'xynes' group not found on this host."
    log_warn "Continuing because --srv-root is overridden (offline test mode);"
    log_warn "chown calls may FAIL and surface as FAILED ledger rows."
    return 0
  fi

  log_error "'xynes' group missing — refusing to chown /srv/xynes/<env>/ subtree."
  log_error "Run vps-bootstrap.sh first (E-2, VPS-SETUP.md §2.2)."
  exit 1
}

preflight_srv_env_exists() {
  # E-4's /srv/xynes/<env>/ subtree must exist before E-6 can chown into it.
  # Dry-run downgrades to a WARN so the operator can preview before E-4.
  local env="$1"
  local subtree="$LAYOUT_ROOT/$env"
  if [[ -d "$subtree" ]]; then
    return 0
  fi
  if is_dry_run; then
    log_warn "($env) $subtree does not exist on this host."
    log_warn "Apply mode would FAIL the chown step. Run vps-filesystem.sh first (E-4, §2.10)."
    return 0
  fi
  if [[ "$LAYOUT_ROOT" != "/srv/xynes" ]]; then
    log_warn "($env) $subtree does not exist under custom --srv-root; chown step will fail."
    return 1
  fi
  log_error "($env) $subtree missing — refusing chown step."
  log_error "Run vps-filesystem.sh first (E-4, VPS-SETUP.md §2.10)."
  return 1
}

# ------------------------------------------------------------------------------
# Chown carve-outs (F-2-FU-1 + E-6-FU-1, 2026-06-12)
# ------------------------------------------------------------------------------

# CARVEOUT_PATHS — sub-paths under /srv/xynes/<env>/ that MUST retain a
# non-deploy owner. Listed relative to the env subtree. Each entry is
# tested at apply time via `find ... -path <subtree>/<entry> -prune` so a
# missing path is silently skipped (e.g. supabase/db won't exist on the
# first apply against a freshly-laid-out E-4 tree).
#
#   env/         : F-2-FU-1 — canonical owner root:xynes (hosted secrets).
#   supabase/db  : E-6-FU-1 — canonical owner 999:999 (Supabase Postgres).
#
# Adding a future carve-out: append to this array AND add a positive
# offline-test assertion in scripts/test/vps-deploy-users.test.sh under
# the "F-2-FU-1 + E-6-FU-1 carve-outs" describe block. Bash array order
# is stable; the test asserts each entry independently.
CARVEOUT_PATHS=(
  "env"
  "supabase/db"
)

# chown_subtree_with_carveouts <env> <user> <subtree>
#   Recursively chown <subtree> to <user>:xynes, EXCEPT for the
#   protected sub-paths listed in CARVEOUT_PATHS. Honours dry-run via
#   a synthetic WOULD: line that names the carve-outs explicitly so the
#   operator audit trail stays readable. Apply mode runs a single
#   `find ... -prune` invocation per protected path plus one terminal
#   `-exec chown ... +` so the chown happens in one syscall batch.
#
#   Returns 0 on success / dry-run, non-zero if the find/chown fails.
chown_subtree_with_carveouts() {
  local env="$1"
  local user="$2"
  local subtree="$3"

  # Build the find argv. Each carve-out becomes a `-path <subtree>/<p> -prune -o`
  # pair so find skips that subtree entirely. The trailing `-exec chown ... +`
  # batches the remaining paths into one chown call.
  local find_args=( "$subtree" )
  local p
  for p in "${CARVEOUT_PATHS[@]}"; do
    find_args+=( "-path" "$subtree/$p" "-prune" "-o" )
  done
  find_args+=( "-exec" "chown" "$user:xynes" "{}" "+" )

  if is_dry_run; then
    # Human-readable WOULD: line — names the carve-outs explicitly so the
    # operator can sanity-check the intent without reading the find argv.
    local carveout_list
    carveout_list="$(printf '%s, ' "${CARVEOUT_PATHS[@]}")"
    carveout_list="${carveout_list%, }"
    printf "WOULD: chown -R %s:xynes %s (excluding carve-outs: %s)\n" \
      "$user" "$subtree" "$carveout_list"
    return 0
  fi

  if ! find "${find_args[@]}"; then
    return 1
  fi
  return 0
}

# ------------------------------------------------------------------------------
# Per-env convergence
# ------------------------------------------------------------------------------

# converge_env <env> <pubkey>
#   1. ensure xynes_<env>_deploy user exists (adduser --disabled-password
#      --gecos "").
#   2. ensure docker group membership.
#   3. drop pubkey into ~/.ssh/authorized_keys (mode 0600, owner self).
#   4. chown -R xynes_<env>_deploy:xynes /srv/xynes/<env>/.
#
# Returns 0 on success (including no-op). Records one ledger row per env.
converge_env() {
  local env="$1"
  local pubkey="$2"
  local user="xynes_${env}_deploy"
  local home="/home/$user"
  local sshdir="$home/.ssh"
  local ak="$sshdir/authorized_keys"
  local subtree="$LAYOUT_ROOT/$env"

  log_step "Env: $env  (user: $user)"

  # Validate the supplied key once, up-front.
  if ! validate_pubkey "$pubkey" "$env"; then
    ledger_add "$env" "FAILED" "pubkey validation failed"
    return 1
  fi
  local fp
  fp="$(pubkey_fingerprint "$pubkey")"
  log_info "($env) pubkey fingerprint: $fp"

  # 1. User existence.
  if id -u "$user" >/dev/null 2>&1; then
    log_info "($env) user '$user' already exists."
  else
    if ! run_cmd adduser --disabled-password --gecos "" "$user"; then
      ledger_add "$env" "FAILED" "adduser failed"
      return 1
    fi
  fi

  # 2. Docker group membership. Use `id -nG` to list groups; treat absence
  # of the `docker` group on this host as a hard error in apply mode (it
  # means E-3 §2.9 wasn't run; subsequent steps would silently leave the
  # deploy user unable to `docker ps`).
  if ! getent group docker >/dev/null 2>&1; then
    if is_dry_run; then
      log_warn "($env) 'docker' group not found; apply mode would FAIL usermod -aG docker $user."
      log_warn "Run vps-bootstrap.sh first (E-3, §2.9)."
      # Still emit the WOULD line so the operator sees the intent.
      printf "WOULD: usermod -aG docker %s\n" "$user"
    else
      ledger_add "$env" "FAILED" "'docker' group missing (E-3 §2.9 not applied)"
      return 1
    fi
  else
    if id -nG "$user" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
      log_info "($env) '$user' already in 'docker' group."
    else
      if ! run_cmd usermod -aG docker "$user"; then
        ledger_add "$env" "FAILED" "usermod -aG docker failed"
        return 1
      fi
    fi
  fi

  # 2b. xynes group membership (F-2-FU-1 + E-6-FU-1 + Codex P1, 2026-06-12).
  # The chown carve-outs in step 4 preserve root:xynes ownership on
  # /srv/xynes/<env>/env/ (mode 0640 per ENVIRONMENTS.md §"Filesystem
  # layout on the VPS") and 999:999 on supabase/db/. The deploy user
  # MUST be in the xynes group to read .env.<env> via group-read
  # permission (mode 0640 grants the group r--); without it, every
  # docker compose --env-file /srv/xynes/<env>/env/.env.<env> invocation
  # would fail with permission denied.
  #
  # The xynes group is created by vps-bootstrap.sh §2.2 (E-2). The
  # preflight check at the top of main() (preflight_xynes_group_exists)
  # already enforces this — by the time we reach this block the group is
  # guaranteed to exist in apply mode, so we don't re-check.
  #
  # Discovered during Codex P1 review on PR #111 — the original carve-out
  # PR would have silently broken dev + prod deployments the moment the
  # operator ran the post-merge sudo chown root:xynes step.
  if id -nG "$user" 2>/dev/null | tr ' ' '\n' | grep -qx xynes; then
    log_info "($env) '$user' already in 'xynes' group."
  else
    if ! run_cmd usermod -aG xynes "$user"; then
      ledger_add "$env" "FAILED" "usermod -aG xynes failed"
      return 1
    fi
  fi

  # 3. Authorized keys. Detect-then-write so re-runs are no-ops. NEVER echo
  # the key bytes; the audit log uses the fingerprint only.
  if [[ ! -d "$sshdir" ]]; then
    if ! run_cmd install -d -o "$user" -g "$user" -m 0700 "$sshdir"; then
      ledger_add "$env" "FAILED" "install -d $sshdir failed"
      return 1
    fi
  fi
  local need_write="false"
  if [[ -f "$ak" ]] && grep -qxF "$pubkey" "$ak" 2>/dev/null; then
    log_info "($env) deploy key already present in $ak."
  else
    need_write="true"
  fi
  if [[ "$need_write" == "true" ]]; then
    backup_file "$ak"
    if is_dry_run; then
      # NOTE: we deliberately do NOT print the key here. Logs are operator
      # visible; the key must never appear in them.
      printf "WOULD: append deploy key (fingerprint %s) to %s (mode 0600, owner %s:%s)\n" \
        "$fp" "$ak" "$user" "$user"
    else
      printf '%s\n' "$pubkey" >> "$ak"
      chown "$user:$user" "$ak"
      chmod 0600 "$ak"
    fi
  fi

  # 3b. Re-affirm authorized_keys ownership + mode even on the "key already
  # present" no-op path. Without this, a pre-existing file written by a
  # different operator (e.g. with root:root ownership or mode 0644) would be
  # reported as converged but sshd would refuse the key (StrictModes).
  # Detect-then-mutate so a correctly-permissioned file stays a true no-op.
  if [[ -f "$ak" ]]; then
    local ak_mode ak_owner
    if ak_mode="$(stat -c '%a' "$ak" 2>/dev/null)" \
        || ak_mode="$(stat -f '%Lp' "$ak" 2>/dev/null)"; then
      if [[ "$ak_mode" != "600" ]]; then
        if is_dry_run; then
          printf "WOULD: chmod 0600 %s (current mode: %s)\n" "$ak" "$ak_mode"
        else
          chmod 0600 "$ak"
          log_info "($env) repaired $ak mode: $ak_mode -> 600."
        fi
      fi
    fi
    if ak_owner="$(stat -c '%U:%G' "$ak" 2>/dev/null)" \
        || ak_owner="$(stat -f '%Su:%Sg' "$ak" 2>/dev/null)"; then
      if [[ "$ak_owner" != "$user:$user" ]]; then
        if is_dry_run; then
          printf "WOULD: chown %s:%s %s (current owner: %s)\n" "$user" "$user" "$ak" "$ak_owner"
        else
          chown "$user:$user" "$ak"
          log_info "($env) repaired $ak owner: $ak_owner -> $user:$user."
        fi
      fi
    fi
  fi

  # 4. chown -R the env subtree. Only when current owner diverges (best
  # effort detection: compare the top-level subtree's owner).
  #
  # F-2-FU-1 + E-6-FU-1 carve-outs (2026-06-12):
  # Two sub-paths under /srv/xynes/<env>/ must retain non-deploy ownership:
  #
  #   - env/        : holds .env.<env> with hosted secrets. Canonical owner is
  #                   root:xynes (ENVIRONMENTS.md §"Filesystem layout on the
  #                   VPS" + infra/release/templates/README.md §"Secure secret
  #                   propagation"). The deploy user only needs group-read via
  #                   `xynes` to pass --env-file to docker compose. Owning the
  #                   file directly would let a compromised deploy SSH key
  #                   cat the file outright.
  #
  #   - supabase/db : Postgres data dir mounted into the Supabase container.
  #                   Canonical owner is 999:999 (uid/gid the postgres user
  #                   maps to inside the official Supabase Postgres image
  #                   per VPS-SETUP.md §2.10 / E-4 §2.10). Clobbering this
  #                   to xynes_<env>_deploy:xynes would prevent the
  #                   Postgres container from writing its data dir on
  #                   next start.
  #
  # The previous unconditional `chown -R $user:xynes $subtree` clobbered
  # both. We now use `find ... -prune` so the chown skips those two
  # sub-paths while still recursing into everything else (compose/,
  # caddy/, future subdirs). Top-level dir itself is still chowned to
  # $user:xynes so the "already converged" short-circuit on the next
  # re-run remains a true no-op.
  if [[ ! -d "$subtree" ]]; then
    # preflight_srv_env_exists already logged; record SKIPPED for the chown.
    log_warn "($env) $subtree missing — skipping chown step."
  else
    local cur_owner
    if cur_owner="$(stat -c '%U:%G' "$subtree" 2>/dev/null)" \
        || cur_owner="$(stat -f '%Su:%Sg' "$subtree" 2>/dev/null)"; then
      if [[ "$cur_owner" == "$user:xynes" ]]; then
        log_info "($env) $subtree already owned by $user:xynes."
      else
        if ! chown_subtree_with_carveouts "$env" "$user" "$subtree"; then
          ledger_add "$env" "FAILED" "chown -R failed on $subtree"
          return 1
        fi
      fi
    else
      log_warn "($env) could not stat $subtree; attempting chown anyway."
      if ! chown_subtree_with_carveouts "$env" "$user" "$subtree"; then
        ledger_add "$env" "FAILED" "chown -R failed on $subtree"
        return 1
      fi
    fi
  fi

  ledger_add "$env" "APPLIED" "user + docker group + authorized_keys + chown -R"
  return 0
}

# ------------------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------------------

print_summary() {
  log_step "Summary"
  printf "%-6s  %-8s  %s\n" "ENV" "VERDICT" "NOTE"
  printf "%-6s  %-8s  %s\n" "---" "-------" "----"
  local row applied=0 already=0 skipped=0 failed=0
  for row in "${USER_LEDGER[@]}"; do
    local env verdict note
    env="${row%%|*}"
    verdict="${row#*|}"; verdict="${verdict%%|*}"
    note="${row##*|}"
    printf "%-6s  %-8s  %s\n" "$env" "$verdict" "$note"
    case "$verdict" in
      APPLIED) applied=$((applied + 1)) ;;
      ALREADY) already=$((already + 1)) ;;
      SKIPPED) skipped=$((skipped + 1)) ;;
      FAILED)  failed=$((failed + 1))   ;;
    esac
  done
  printf "\nMode: %s | srv-root: %s | only: %s | applied=%d already=%d skipped=%d failed=%d\n" \
    "$MODE" "$LAYOUT_ROOT" "${ONLY_ENV:-(all)}" "$applied" "$already" "$skipped" "$failed"
}

# ------------------------------------------------------------------------------
# main()
# ------------------------------------------------------------------------------

main() {
  log_info "$SCRIPT_NAME starting (mode=$MODE, srv-root=$LAYOUT_ROOT, only=${ONLY_ENV:-(all)})"
  validate_root
  validate_only_env
  require_legacy_boundary_absent || exit 1

  # Merge --keys-file entries into the per-env globals (per-env flags win).
  if [[ -n "$KEYS_FILE" ]]; then
    parse_keys_file "$KEYS_FILE"
  fi

  # Resolve any per-env files into the per-env globals.
  local env
  for env in "${ENVS[@]}"; do
    if ! resolve_env_pubkey "$env"; then
      log_error "Failed to resolve pubkey for env: $env"
      exit 1
    fi
  done

  # At this point, every env we plan to process must have a pubkey set.
  # Determine the run list (honour --only).
  local run_envs=()
  for env in "${ENVS[@]}"; do
    if [[ -n "$ONLY_ENV" ]] && [[ "$ONLY_ENV" != "$env" ]]; then
      ledger_add "$env" "SKIPPED" "--only $ONLY_ENV restricted run"
      continue
    fi
    run_envs+=("$env")
  done

  if [[ ${#run_envs[@]} -eq 0 ]]; then
    log_error "No environments selected for processing (check --only flag)."
    exit 1
  fi

  # Verify every selected env has a key.
  for env in "${run_envs[@]}"; do
    local var_name
    case "$env" in
      dev)  var_name="DEV_PUBKEY"  ;;
      qa)   var_name="QA_PUBKEY"   ;;
      prod) var_name="PROD_PUBKEY" ;;
    esac
    if [[ -z "${!var_name}" ]]; then
      log_error "($env) no public key supplied. Use --${env}-pubkey, --${env}-pubkey-file, or --keys-file."
      exit 1
    fi
  done

  require_root_for_apply
  preflight_xynes_group_exists

  log_step "§2.12 Per-env deploy users"

  local failed_any=0
  for env in "${run_envs[@]}"; do
    # Preflight /srv/xynes/<env>/. Dry-run + --srv-root override paths
    # downgrade the missing-subtree case to WARN (see preflight_srv_env_exists);
    # the canonical /srv/xynes apply path returns 1 and we MUST honour it —
    # otherwise the script would create the deploy user, drop the key, then
    # silently skip the chown and exit 0, leaving the env half-converged.
    # Fix for PR #102 review (Codex P1).
    if ! preflight_srv_env_exists "$env"; then
      ledger_add "$env" "FAILED" "preflight: $LAYOUT_ROOT/$env missing — run vps-filesystem.sh (E-4) first"
      failed_any=1
      continue
    fi

    local key_var
    case "$env" in
      dev)  key_var="DEV_PUBKEY"  ;;
      qa)   key_var="QA_PUBKEY"   ;;
      prod) key_var="PROD_PUBKEY" ;;
    esac
    if ! converge_env "$env" "${!key_var}"; then
      failed_any=1
    fi
  done

  print_summary

  if [[ "$failed_any" -ne 0 ]]; then
    log_error "One or more envs failed to converge. Inspect the ledger above."
    exit 1
  fi
}

# Only auto-run main when this file is executed directly (not when sourced by
# the offline test suite, which sources helpers into its own shell to test
# them in isolation).
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main
fi
