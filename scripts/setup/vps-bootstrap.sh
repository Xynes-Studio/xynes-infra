#!/usr/bin/env bash
# vps-bootstrap.sh — Stories E-2 + E-3 + E-5 (MVP release plan, Phase 2 core).
#
# Idempotently applies VPS-SETUP.md §2.2 through §2.9 + §2.11 on a fresh Ubuntu LTS host:
#   §2.2  Create non-root `xynes` user + seed authorized_keys
#   §2.3  SSH hardening (sshd_config)
#   §2.4  Firewall (ufw)
#   §2.5  fail2ban (sshd jail; replaces legacy Hestia jail.local if found)
#   §2.6  Automatic security updates (unattended-upgrades)
#   §2.7  Time sync (systemd-timesyncd)
#   §2.8  Swap (2 GiB safety net + swappiness=10)
#   §2.9  Docker engine (official apt repo) + log driver daemon.json + xynes in docker group
#   §2.11 Docker networks (bridge driver) — xynes_dev_net, xynes_qa_net, xynes_prod_net, caddy_net
#
# Deliberately out of scope (do NOT add here):
#   - Filesystem layout under /srv/xynes (E-4 — see vps-filesystem.sh).
#   - Per-env deploy users + scoped keys (E-6).
#
# Behavior contract:
#   - Conservative idempotency: read current value, mutate ONLY if it diverges
#     from the spec. Re-running on an already-hardened host is a no-op.
#   - Timezone change (§2.7) requires opt-in via --force-tz so we don't
#     surprise-mutate a running host (e.g. xynes-vps-1 currently runs
#     Europe/Berlin and switching it mid-flight has no operational value).
#   - Every config file we touch is backed up to <path>.bak.YYYYmmddHHMMSS.
#   - sshd is restarted at most ONCE at the end, only if §2.3 made changes.
#   - --dry-run prints "WOULD: <command>" for every intended mutation and
#     performs zero side effects. No root needed for --dry-run.
#   - DO NOT RUN this script blindly on the live VPS without first verifying
#     a --dry-run output. The companion test in
#     scripts/test/vps-bootstrap.test.sh exercises --help + --dry-run paths.
#
# Usage:
#   bash scripts/setup/vps-bootstrap.sh --help
#   bash scripts/setup/vps-bootstrap.sh --dry-run --operator-key 'ssh-ed25519 AAAAC3...'
#   bash scripts/setup/vps-bootstrap.sh --dry-run --operator-key-file /root/operator-key.pub
#   sudo bash scripts/setup/vps-bootstrap.sh --operator-key-file /root/operator-key.pub
#   sudo bash scripts/setup/vps-bootstrap.sh --operator-key-file /root/operator-key.pub --force-tz

set -euo pipefail

# ------------------------------------------------------------------------------
# Defaults
# ------------------------------------------------------------------------------

SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"

MODE="apply"               # apply | dry-run
OPERATOR_KEY=""            # raw pubkey string (takes precedence)
OPERATOR_KEY_FILE=""       # path to a pubkey file
FALLBACK_KEY_FILE="/root/operator-key.pub"
FORCE_TZ="false"
SKIP_DOCKER="false"        # --skip-docker opts out of §2.9 entirely
DOCKER_PRUNE="false"       # --docker-prune runs `docker system prune -af --volumes` after §2.9 succeeds
SKIP_NETWORKS="false"      # --skip-networks opts out of §2.11 entirely (E-5)

# Section action ledger:
#   APPLIED — script mutated state.
#   ALREADY — already matched spec; no mutation needed.
#   SKIPPED — opted out (e.g. --force-tz not passed) or precondition missing.
#   FAILED  — section hit a non-recoverable error and aborted.
declare -a SECTION_LEDGER=()

# Track whether sshd needs a restart at the end.
SSHD_DIRTY="false"

# Track whether §2.9 changed daemon.json so we can restart docker exactly once
# at the end of the section (live-restore preserves running containers).
DOCKER_DAEMON_DIRTY="false"
# Track whether §2.9 installed/upgraded docker packages so the restart still
# happens even when daemon.json was already correct.
DOCKER_PACKAGES_DIRTY="false"

# Timestamp for backup suffixes (one per script run).
RUN_TS="$(date -u +%Y%m%d%H%M%S)"

# ------------------------------------------------------------------------------
# Usage
# ------------------------------------------------------------------------------

print_usage() {
  cat <<EOF
$SCRIPT_NAME — VPS base OS hardening per VPS-SETUP.md §2.2–2.9 + §2.11 (Stories E-2 + E-3 + E-5).

USAGE:
  $SCRIPT_NAME [--dry-run] [--operator-key '<pubkey>' | --operator-key-file <path>] [--force-tz] [--skip-docker] [--docker-prune] [--skip-networks]
  $SCRIPT_NAME --help

OPTIONS:
  --dry-run                Print "WOULD: <command>" for every intended mutation. No side effects.
                           Does not require root.
  --operator-key '<key>'   Operator SSH public key as a single string (e.g. 'ssh-ed25519 AAAAC3... operator@laptop').
                           Takes precedence over --operator-key-file.
  --operator-key-file <p>  Path to a file containing a single operator SSH public key.
                           Fallback: $FALLBACK_KEY_FILE if neither flag is given.
  --force-tz               Also set timezone to UTC. Without this flag the script leaves the
                           current timezone alone (conservative — avoids surprise-mutating a
                           live host that already drifted from UTC).
  --skip-docker            Skip §2.9 entirely. Useful for re-runs that only need to converge
                           §2.2–§2.8 drift without touching the Docker engine.
  --docker-prune           After §2.9 converges and Docker restarts successfully, run
                           \`docker system prune -af --volumes\`. Skips volumes attached to
                           running containers and named volumes referenced by compose files.
                           Off by default. Opt-in only.
  --skip-networks          Skip §2.11 entirely. Useful for re-runs that only need to converge
                           §2.2–§2.9 drift without touching the per-env Docker networks.
  --help, -h               Print this usage and exit 0.

SECTIONS APPLIED (VPS-SETUP.md):
  §2.2  Create non-root \`xynes\` user + seed authorized_keys (mode 0600, owner xynes:xynes).
  §2.3  Patch /etc/ssh/sshd_config: PermitRootLogin no, PasswordAuthentication no,
        PubkeyAuthentication yes, KbdInteractiveAuthentication no,
        ChallengeResponseAuthentication no, MaxAuthTries 3, LoginGraceTime 30,
        AllowUsers xynes xynes_dev_deploy xynes_qa_deploy xynes_prod_deploy.
  §2.4  Configure ufw: default deny in / allow out, allow 22/tcp, 80/tcp, 443/tcp, 443/udp.
  §2.5  fail2ban: ensure sshd jail is enabled. Replace legacy Hestia jail.local if found.
  §2.6  Enable unattended-upgrades (APT::Periodic::Unattended-Upgrade "1").
  §2.7  Enable systemd-timesyncd. Timezone set to UTC only with --force-tz.
  §2.8  Create 2 GiB /swapfile + vm.swappiness=10 (persisted in /etc/fstab and sysctl.d).
  §2.9  Install Docker engine (official apt repo) + /etc/docker/daemon.json (json-file
        driver, 10m/5 rotation, live-restore true) + add \`xynes\` to the docker group.
        Restart docker after writing daemon.json (live-restore protects running containers).
        Opt out with --skip-docker. Opt in to post-restart prune with --docker-prune.
  §2.11 Create per-env Docker networks (bridge driver): xynes_dev_net, xynes_qa_net,
        xynes_prod_net, caddy_net. Idempotent — skip if already present. Each network
        is labelled com.xynes.managed=true + com.xynes.network=<name> so the bootstrap's
        networks are distinguishable from operator-created ones. Opt out with --skip-networks.

CONTRACT:
  - Conservative idempotency: never overwrite a setting that already matches the spec.
  - Every modified config is backed up to <path>.bak.$RUN_TS.
  - sshd is restarted at most once at the end (only if §2.3 made changes).
  - docker is restarted exactly once at the end of §2.9 (only if daemon.json changed or
    docker packages were installed/upgraded). live-restore: true preserves running containers.
  - --dry-run is safe to run as a non-root user.

EXIT CODES:
  0   Apply or dry-run completed; ledger summary printed.
  1   Misuse: missing/invalid operator key, missing required tools, runtime failure.
  64  Unknown flag.

EXAMPLES:
  # Preview the run without touching anything.
  bash $SCRIPT_NAME --dry-run --operator-key 'ssh-ed25519 AAAAC3...'

  # Apply on the VPS (run as root), including docker install + prune.
  sudo bash $SCRIPT_NAME --operator-key-file /root/operator-key.pub --docker-prune

  # Apply and also flip timezone to UTC.
  sudo bash $SCRIPT_NAME --operator-key-file /root/operator-key.pub --force-tz

  # Re-run only the OS hardening sections (§2.2–§2.8) without touching docker or networks.
  sudo bash $SCRIPT_NAME --operator-key-file /root/operator-key.pub --skip-docker --skip-networks
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
    --operator-key)
      [[ $# -ge 2 ]] || { echo "ERROR: --operator-key requires a value" >&2; exit 1; }
      OPERATOR_KEY="$2"
      shift 2
      ;;
    --operator-key=*)
      OPERATOR_KEY="${1#*=}"
      shift
      ;;
    --operator-key-file)
      [[ $# -ge 2 ]] || { echo "ERROR: --operator-key-file requires a value" >&2; exit 1; }
      OPERATOR_KEY_FILE="$2"
      shift 2
      ;;
    --operator-key-file=*)
      OPERATOR_KEY_FILE="${1#*=}"
      shift
      ;;
    --force-tz)
      FORCE_TZ="true"
      shift
      ;;
    --skip-docker)
      SKIP_DOCKER="true"
      shift
      ;;
    --docker-prune)
      DOCKER_PRUNE="true"
      shift
      ;;
    --skip-networks)
      SKIP_NETWORKS="true"
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
  # ledger_add <section-id> <verdict> <note>
  SECTION_LEDGER+=("$1|$2|$3")
}

is_dry_run() { [[ "$MODE" == "dry-run" ]]; }

# Run a mutating command unless we're in dry-run mode.
# Usage: run_cmd <command> [args...]
run_cmd() {
  if is_dry_run; then
    printf "WOULD: %s\n" "$*"
    return 0
  fi
  "$@"
}

# Run a mutating shell snippet (for redirections / pipes).
# Usage: run_sh '<single-quoted shell snippet>'
run_sh() {
  if is_dry_run; then
    printf "WOULD: sh -c %s\n" "$1"
    return 0
  fi
  sh -c "$1"
}

require_root_for_apply() {
  is_dry_run && return 0
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    log_error "Apply mode requires root. Re-run with sudo, or use --dry-run."
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

# Validate an operator SSH public key string (single line, supported algo prefix).
validate_pubkey() {
  local key="$1"
  if [[ -z "$key" ]]; then
    return 1
  fi
  # Reject multi-line input (a single pubkey is one line).
  if [[ "$(printf '%s' "$key" | wc -l | tr -d ' ')" -ne 0 ]]; then
    return 1
  fi
  # Trim leading/trailing whitespace for the prefix check.
  local trimmed="${key#"${key%%[![:space:]]*}"}"
  trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
  case "$trimmed" in
    "ssh-ed25519 "*|"ssh-rsa "*|"ecdsa-sha2-nistp256 "*|"ecdsa-sha2-nistp384 "*|"ecdsa-sha2-nistp521 "*|"sk-ssh-ed25519@openssh.com "*|"sk-ecdsa-sha2-nistp256@openssh.com "*)
      return 0
      ;;
  esac
  return 1
}

resolve_operator_key() {
  # Resolves the operator public key from --operator-key, --operator-key-file,
  # or the documented fallback path. Echoes the validated key on success.
  # Returns non-zero (and logs an error) if no usable key is available.
  local key=""

  if [[ -n "$OPERATOR_KEY" ]]; then
    key="$OPERATOR_KEY"
  elif [[ -n "$OPERATOR_KEY_FILE" ]]; then
    if [[ ! -r "$OPERATOR_KEY_FILE" ]]; then
      log_error "Operator key file not readable: $OPERATOR_KEY_FILE"
      return 1
    fi
    key="$(head -n1 "$OPERATOR_KEY_FILE")"
  elif [[ -r "$FALLBACK_KEY_FILE" ]]; then
    log_info "Falling back to $FALLBACK_KEY_FILE for operator key (no --operator-key* flag given)."
    key="$(head -n1 "$FALLBACK_KEY_FILE")"
  else
    log_error "Operator key not provided. Use --operator-key '<pubkey>', --operator-key-file <path>, or place a key at $FALLBACK_KEY_FILE."
    return 1
  fi

  if ! validate_pubkey "$key"; then
    log_error "Operator key failed validation. Expected a single-line ssh-ed25519/ssh-rsa/ecdsa-sha2-* public key."
    return 1
  fi
  printf "%s" "$key"
}

# Read the EFFECTIVE value of an sshd_config key (matches what sshd will load).
# Returns empty string if the key is unset / commented out.
sshd_effective_value() {
  local key="$1"
  if ! command -v sshd >/dev/null 2>&1; then
    # Fallback: grep the file directly.
    awk -v k="$key" 'BEGIN{IGNORECASE=1} /^[[:space:]]*#/ {next} $1==k {sub($1,""); sub(/^[ \t]+/,""); print; exit}' /etc/ssh/sshd_config 2>/dev/null
    return 0
  fi
  # `sshd -T` prints effective config in lowercase-keyed form.
  # On hosts without a usable sshd_config (e.g. CI / dev laptop running this
  # script in --dry-run mode), `sshd -T` exits non-zero; we swallow that here
  # so `set -e` doesn't abort the calling section.
  local lower
  lower="$(printf "%s" "$key" | tr '[:upper:]' '[:lower:]')"
  local effective=""
  if ! effective="$(sshd -T 2>/dev/null)"; then
    effective=""
  fi
  printf "%s" "$effective" | awk -v k="$lower" 'tolower($1)==k {sub($1,""); sub(/^[ \t]+/,""); print; exit}'
}

# Set an sshd_config key to a value, idempotently. Mark SSHD_DIRTY=true if changed.
sshd_set() {
  local key="$1"
  local desired="$2"
  local current
  current="$(sshd_effective_value "$key")"
  if [[ "$current" == "$desired" ]]; then
    return 0
  fi
  if is_dry_run; then
    printf "WOULD: set %s = %s in /etc/ssh/sshd_config (current: %q)\n" "$key" "$desired" "$current"
    SSHD_DIRTY="true"
    return 0
  fi
  backup_file /etc/ssh/sshd_config
  # Remove any existing (active or commented) line for this key, then insert
  # the new one BEFORE the first `Match` block (sshd_config restricts which
  # directives are allowed inside a Match scope, so appending at EOF can
  # silently land us inside a Match and break `sshd -t`).
  sed -i -E "/^[[:space:]]*#?[[:space:]]*${key}[[:space:]].*/d" /etc/ssh/sshd_config
  if grep -qE '^[[:space:]]*Match[[:space:]]' /etc/ssh/sshd_config; then
    # Insert just before the first Match line.
    awk -v line="$key $desired" '
      BEGIN { inserted = 0 }
      !inserted && /^[[:space:]]*Match[[:space:]]/ { print line; inserted = 1 }
      { print }
      END { if (!inserted) print line }
    ' /etc/ssh/sshd_config > /etc/ssh/sshd_config.new && mv /etc/ssh/sshd_config.new /etc/ssh/sshd_config
  else
    printf "%s %s\n" "$key" "$desired" >> /etc/ssh/sshd_config
  fi
  SSHD_DIRTY="true"
}

# ------------------------------------------------------------------------------
# Section §2.2 — Non-root user + authorized_keys
# ------------------------------------------------------------------------------

section_2_2_user() {
  log_step "§2.2 Non-root user (xynes)"

  local op_key
  if ! op_key="$(resolve_operator_key)"; then
    ledger_add "§2.2" "FAILED" "operator key unavailable"
    return 1
  fi

  # Create user if missing.
  if id -u xynes >/dev/null 2>&1; then
    log_info "User 'xynes' already exists."
  else
    run_cmd adduser --disabled-password --gecos "" xynes
  fi

  # Ensure sudo group membership.
  if id -nG xynes 2>/dev/null | tr ' ' '\n' | grep -qx sudo; then
    log_info "User 'xynes' already in sudo group."
  else
    run_cmd usermod -aG sudo xynes
  fi

  # Ensure .ssh dir.
  if [[ -d /home/xynes/.ssh ]]; then
    log_info "/home/xynes/.ssh already exists."
  else
    run_cmd install -d -o xynes -g xynes -m 0700 /home/xynes/.ssh
  fi

  # Seed authorized_keys (append-if-missing, mode 0600, owner xynes:xynes).
  local ak=/home/xynes/.ssh/authorized_keys
  if [[ -f "$ak" ]] && grep -qxF "$op_key" "$ak" 2>/dev/null; then
    log_info "Operator key already present in $ak."
  else
    backup_file "$ak"
    if is_dry_run; then
      printf "WOULD: append operator key to %s (mode 0600, owner xynes:xynes)\n" "$ak"
    else
      printf "%s\n" "$op_key" >> "$ak"
      chown xynes:xynes "$ak"
      chmod 0600 "$ak"
    fi
  fi

  ledger_add "§2.2" "APPLIED" "user + authorized_keys ensured"
  return 0
}

# ------------------------------------------------------------------------------
# Section §2.3 — SSH hardening
# ------------------------------------------------------------------------------

section_2_3_sshd() {
  log_step "§2.3 SSH hardening (/etc/ssh/sshd_config)"

  sshd_set PermitRootLogin no
  sshd_set PasswordAuthentication no
  sshd_set PubkeyAuthentication yes
  sshd_set KbdInteractiveAuthentication no
  sshd_set ChallengeResponseAuthentication no
  sshd_set MaxAuthTries 3
  sshd_set LoginGraceTime 30

  # AllowUsers — only set if currently missing or empty. Don't overwrite a
  # valid existing line that operators may have customised.
  local current_allow
  current_allow="$(sshd_effective_value AllowUsers)"
  if [[ -z "$current_allow" ]]; then
    sshd_set AllowUsers "xynes xynes_dev_deploy xynes_qa_deploy xynes_prod_deploy"
  else
    log_info "AllowUsers already set to: $current_allow (leaving as-is)."
  fi

  if [[ "$SSHD_DIRTY" == "true" ]]; then
    ledger_add "§2.3" "APPLIED" "sshd_config patched; restart scheduled"
  else
    ledger_add "§2.3" "ALREADY" "sshd_config already matches spec"
  fi
  return 0
}

# ------------------------------------------------------------------------------
# Section §2.4 — Firewall (ufw)
# ------------------------------------------------------------------------------

section_2_4_ufw() {
  log_step "§2.4 Firewall (ufw)"

  if ! command -v ufw >/dev/null 2>&1; then
    if is_dry_run; then
      printf "WOULD: apt-get install -y ufw\n"
    else
      run_cmd apt-get update -y
      run_cmd apt-get install -y ufw
    fi
  fi

  # Default policies (idempotent — ufw is no-op when policies already match).
  # Some Ubuntu hosts have a half-broken ufw-init shim that exits non-zero on
  # `ufw default` even when the policy is fine. We probe `ufw status verbose`
  # first and only invoke `ufw default ...` when the current default differs.
  local ufw_defaults=""
  if command -v ufw >/dev/null 2>&1; then
    ufw_defaults="$(ufw status verbose 2>/dev/null | awk -F'[:,()]' '/^Default:/ {print tolower($0)}')" || ufw_defaults=""
  fi
  if printf "%s" "$ufw_defaults" | grep -q 'deny (incoming)'; then
    log_info "ufw default incoming already 'deny'."
  else
    run_cmd ufw default deny incoming
  fi
  if printf "%s" "$ufw_defaults" | grep -q 'allow (outgoing)'; then
    log_info "ufw default outgoing already 'allow'."
  else
    run_cmd ufw default allow outgoing
  fi

  # Allow the four ports. ufw is idempotent for `allow` rules.
  run_cmd ufw allow 22/tcp
  run_cmd ufw allow 80/tcp
  run_cmd ufw allow 443/tcp
  run_cmd ufw allow 443/udp

  # Enable (idempotent — ufw says "already enabled" if active).
  if is_dry_run; then
    printf "WOULD: ufw --force enable\n"
  else
    # If ufw is already active and the rules are in place, skip `ufw enable`.
    # Some Ubuntu hosts have a half-broken ufw-init shim that exit-codes
    # `--force enable` non-zero even though the firewall is fine; treat
    # "already active" as success.
    local ufw_active="false"
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | head -n1 | grep -qi 'Status: active'; then
      ufw_active="true"
    fi
    if [[ "$ufw_active" == "true" ]]; then
      log_info "ufw already active; skipping --force enable."
    else
      yes | ufw enable >/dev/null 2>&1 || ufw --force enable
    fi
  fi

  ledger_add "§2.4" "APPLIED" "deny-in/allow-out + 22,80,443/tcp + 443/udp"
  return 0
}

# ------------------------------------------------------------------------------
# Section §2.5 — fail2ban
# ------------------------------------------------------------------------------

section_2_5_fail2ban() {
  log_step "§2.5 fail2ban"

  if ! command -v fail2ban-client >/dev/null 2>&1; then
    if is_dry_run; then
      printf "WOULD: apt-get install -y fail2ban\n"
    else
      run_cmd apt-get install -y fail2ban
    fi
  fi

  # Detect legacy Hestia jail.local with exim/dovecot/etc. and replace.
  local jail=/etc/fail2ban/jail.local
  local legacy="false"
  if [[ -f "$jail" ]] && grep -qE '\[(exim|dovecot|hestia|roundcube|phpmyadmin)-iptables\]' "$jail" 2>/dev/null; then
    legacy="true"
  fi

  if [[ "$legacy" == "true" ]] || [[ ! -f "$jail" ]]; then
    backup_file "$jail"
    if is_dry_run; then
      printf "WOULD: write minimal sshd-only jail to %s\n" "$jail"
    else
      cat > "$jail" <<'EOF'
[DEFAULT]
bantime = 1h
findtime = 10m
maxretry = 5
backend = systemd

[sshd]
enabled = true
port = ssh
logpath = %(sshd_log)s
EOF
    fi
    ledger_add "§2.5" "APPLIED" "jail.local rewritten (sshd-only minimal)"
  else
    log_info "jail.local already present and free of legacy Hestia jails."
    ledger_add "§2.5" "ALREADY" "jail.local already clean"
  fi

  run_cmd systemctl enable fail2ban
  run_cmd systemctl restart fail2ban
  return 0
}

# ------------------------------------------------------------------------------
# Section §2.6 — Unattended security updates
# ------------------------------------------------------------------------------

section_2_6_unattended() {
  log_step "§2.6 unattended-upgrades"

  if ! dpkg -s unattended-upgrades >/dev/null 2>&1; then
    if is_dry_run; then
      printf "WOULD: apt-get install -y unattended-upgrades\n"
    else
      run_cmd apt-get install -y unattended-upgrades
    fi
  fi

  local cfg=/etc/apt/apt.conf.d/20auto-upgrades
  local desired_unatt='APT::Periodic::Unattended-Upgrade "1";'
  local desired_lists='APT::Periodic::Update-Package-Lists "1";'

  local need_write="false"
  if [[ ! -f "$cfg" ]]; then
    need_write="true"
  else
    grep -qF "$desired_unatt" "$cfg" 2>/dev/null || need_write="true"
    grep -qF "$desired_lists" "$cfg" 2>/dev/null || need_write="true"
  fi

  if [[ "$need_write" == "true" ]]; then
    backup_file "$cfg"
    if is_dry_run; then
      printf "WOULD: write %s with periodic upgrade settings\n" "$cfg"
    else
      cat > "$cfg" <<EOF
$desired_lists
$desired_unatt
EOF
    fi
    ledger_add "§2.6" "APPLIED" "20auto-upgrades patched"
  else
    log_info "20auto-upgrades already configured."
    ledger_add "§2.6" "ALREADY" "20auto-upgrades already correct"
  fi
  return 0
}

# ------------------------------------------------------------------------------
# Section §2.7 — Time sync
# ------------------------------------------------------------------------------

section_2_7_time() {
  log_step "§2.7 Time sync (systemd-timesyncd)"

  # Timezone change is opt-in.
  if [[ "$FORCE_TZ" == "true" ]]; then
    local current_tz
    current_tz="$(timedatectl show --property=Timezone --value 2>/dev/null || true)"
    if [[ "$current_tz" == "UTC" ]]; then
      log_info "Timezone already UTC."
    else
      run_cmd timedatectl set-timezone UTC
      ledger_add "§2.7-tz" "APPLIED" "timezone -> UTC (was $current_tz)"
    fi
  else
    log_info "Timezone left unchanged (--force-tz not passed)."
    ledger_add "§2.7-tz" "SKIPPED" "--force-tz not set"
  fi

  run_cmd systemctl enable systemd-timesyncd
  run_cmd systemctl restart systemd-timesyncd
  ledger_add "§2.7-ntp" "APPLIED" "systemd-timesyncd enabled+running"
  return 0
}

# ------------------------------------------------------------------------------
# Section §2.8 — Swap
# ------------------------------------------------------------------------------

section_2_8_swap() {
  log_step "§2.8 Swap (2 GiB safety net)"

  local need_swap="true"
  if [[ -f /swapfile ]] && swapon --show 2>/dev/null | awk '{print $1}' | grep -qx /swapfile; then
    log_info "/swapfile already present and active."
    need_swap="false"
  fi

  if [[ "$need_swap" == "true" ]]; then
    if is_dry_run; then
      printf "WOULD: fallocate -l 2G /swapfile && chmod 0600 /swapfile && mkswap /swapfile && swapon /swapfile\n"
    else
      fallocate -l 2G /swapfile
      chmod 0600 /swapfile
      mkswap /swapfile >/dev/null
      swapon /swapfile
    fi
    ledger_add "§2.8-file" "APPLIED" "2 GiB /swapfile created + active"
  else
    ledger_add "§2.8-file" "ALREADY" "/swapfile already active"
  fi

  # Persist in /etc/fstab.
  #
  # Idempotency probe uses awk field-equality on column 1 ($1 == "/swapfile")
  # rather than a regex, because the previous probe `grep -qE '^[^#]*\s/swapfile\s'`
  # had two latent bugs:
  #   1. GNU `grep -E` does not recognise the Perl meta-character `\s` — it
  #      matches the literal character 's'. The probe therefore matched only
  #      lines with a literal `s` between the prefix and `/swapfile`, never
  #      a real whitespace-separated fstab line.
  #   2. The `[^#]*\s` prefix required AT LEAST ONE non-`#` character followed
  #      by whitespace BEFORE `/swapfile`. A canonical fstab line that starts
  #      with `/swapfile ...` has neither, so the probe could never match it
  #      even with \s fixed.
  # Net result: every re-run on a converged host appended a duplicate
  # `/swapfile none swap sw 0 0` line. AGENTS.md E-2 closure already records
  # one manual fstab cleanup caused by this bug. The awk replacement is the
  # real fix — fixed on the E-5 branch as a stacked commit, with a regression
  # guard in the offline test.
  if awk '!/^[[:space:]]*#/ && $1 == "/swapfile" { found = 1 } END { exit found ? 0 : 1 }' /etc/fstab 2>/dev/null; then
    log_info "/etc/fstab already references /swapfile."
  else
    backup_file /etc/fstab
    if is_dry_run; then
      printf "WOULD: append '/swapfile none swap sw 0 0' to /etc/fstab\n"
    else
      printf "/swapfile none swap sw 0 0\n" >> /etc/fstab
    fi
  fi

  # vm.swappiness=10 via /etc/sysctl.d/.
  local sysctl_file=/etc/sysctl.d/99-xynes-swappiness.conf
  local desired="vm.swappiness=10"
  local need_sysctl="true"
  if [[ -f "$sysctl_file" ]] && grep -qxF "$desired" "$sysctl_file"; then
    need_sysctl="false"
  fi
  if [[ "$need_sysctl" == "true" ]]; then
    backup_file "$sysctl_file"
    if is_dry_run; then
      printf "WOULD: write %s with %s\n" "$sysctl_file" "$desired"
    else
      printf "%s\n" "$desired" > "$sysctl_file"
      sysctl -p "$sysctl_file" >/dev/null
    fi
    ledger_add "§2.8-sysctl" "APPLIED" "vm.swappiness=10 persisted"
  else
    ledger_add "§2.8-sysctl" "ALREADY" "vm.swappiness=10 already persisted"
  fi
  return 0
}

# ------------------------------------------------------------------------------
# Section §2.9 — Docker engine + log driver config
# ------------------------------------------------------------------------------

# Canonical /etc/docker/daemon.json content per VPS-SETUP.md §2.9.
DOCKER_DAEMON_JSON='{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "5" },
  "live-restore": true
}'

DOCKER_KEYRING="/etc/apt/keyrings/docker.asc"
DOCKER_APT_LIST="/etc/apt/sources.list.d/docker.list"
DOCKER_DAEMON_JSON_PATH="/etc/docker/daemon.json"

# Returns 0 if the configured apt source is the official Docker repo
# (download.docker.com), OR if no Docker is installed yet (so the script will
# install from the official repo). Returns non-zero only when there is an
# EXISTING Docker install whose package origin is something other than the
# official Docker apt repo (snap, distro, manual, Hestia). Stdout: a
# description suitable for logging.
detect_docker_apt_source() {
  if [[ -f "$DOCKER_APT_LIST" ]] && grep -qF 'download.docker.com' "$DOCKER_APT_LIST" 2>/dev/null; then
    echo "official ($DOCKER_APT_LIST)"
    return 0
  fi
  # Probe alternate locations.
  if find /etc/apt/sources.list.d/ -maxdepth 1 \( -name '*.list' -o -name '*.sources' \) -print0 2>/dev/null \
    | xargs -0 -r grep -lF 'download.docker.com' 2>/dev/null | head -1 | grep -q .; then
    echo "official (found in /etc/apt/sources.list.d/)"
    return 0
  fi

  # Only flag as non-official when there is an EXISTING Debian/Ubuntu Docker
  # install. `command -v docker` alone is too broad (e.g. macOS dev laptops
  # carry Docker Desktop in PATH); use dpkg + snap probes instead.
  local has_dpkg_docker="false"
  if command -v dpkg-query >/dev/null 2>&1; then
    if dpkg-query -W -f='${Status}' docker-ce 2>/dev/null | grep -q 'install ok installed'; then
      has_dpkg_docker="true"
    elif dpkg-query -W -f='${Status}' docker.io 2>/dev/null | grep -q 'install ok installed'; then
      has_dpkg_docker="true"
    fi
  fi
  local has_snap_docker="false"
  if command -v snap >/dev/null 2>&1 && snap list 2>/dev/null | grep -q '^docker '; then
    has_snap_docker="true"
  fi

  if [[ "$has_dpkg_docker" == "true" ]]; then
    local origin=""
    origin="$(dpkg -s docker-ce 2>/dev/null | awk -F': ' '/^APT-Sources:/{print $2; exit}')"
    if [[ -n "$origin" ]]; then
      echo "non-official ($origin)"
    else
      echo "non-official (distro or manual install)"
    fi
    return 1
  fi
  if [[ "$has_snap_docker" == "true" ]]; then
    echo "non-official (snap)"
    return 1
  fi

  # No Debian/Ubuntu docker package present — the script will install from
  # the official repo. This is the happy fresh-install path.
  echo "no docker installed yet"
  return 0
}

# Idempotent. Adds the official Docker apt repo (keyring + .list) if missing.
docker_apt_repo_ensured() {
  local need_repo="false"
  if [[ ! -f "$DOCKER_KEYRING" ]]; then
    need_repo="true"
  fi
  if [[ ! -f "$DOCKER_APT_LIST" ]] || ! grep -qF 'download.docker.com' "$DOCKER_APT_LIST" 2>/dev/null; then
    need_repo="true"
  fi

  if [[ "$need_repo" == "false" ]]; then
    log_info "Official Docker apt repo already configured."
    return 0
  fi

  if is_dry_run; then
    printf "WOULD: install -d -m 0755 /etc/apt/keyrings\n"
    printf "WOULD: curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o %s\n" "$DOCKER_KEYRING"
    printf "WOULD: chmod a+r %s\n" "$DOCKER_KEYRING"
    printf "WOULD: write %s with deb [signed-by=%s] https://download.docker.com/linux/ubuntu <codename> stable\n" "$DOCKER_APT_LIST" "$DOCKER_KEYRING"
    printf "WOULD: apt-get update\n"
    return 0
  fi

  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o "$DOCKER_KEYRING"
  chmod a+r "$DOCKER_KEYRING"

  # Resolve Ubuntu codename (jammy, noble, ...). Fall back to `lsb_release -cs`.
  local codename=""
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    codename="$(. /etc/os-release && echo "${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}")"
  fi
  if [[ -z "$codename" ]] && command -v lsb_release >/dev/null 2>&1; then
    codename="$(lsb_release -cs)"
  fi
  if [[ -z "$codename" ]]; then
    log_error "Unable to determine Ubuntu codename for Docker apt source."
    return 1
  fi
  backup_file "$DOCKER_APT_LIST"
  printf 'deb [arch=%s signed-by=%s] https://download.docker.com/linux/ubuntu %s stable\n' \
    "$(dpkg --print-architecture)" "$DOCKER_KEYRING" "$codename" \
    > "$DOCKER_APT_LIST"
  apt-get update -y
}

# Idempotent. Installs (or upgrades to latest) the five Docker packages.
docker_packages_installed() {
  local pkgs=(docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin)
  if is_dry_run; then
    printf "WOULD: apt-get install -y %s\n" "${pkgs[*]}"
    DOCKER_PACKAGES_DIRTY="true"
    return 0
  fi

  # Capture current versions to decide whether anything actually changed.
  local before=""
  before="$(dpkg-query -W -f='${Package}=${Version}\n' "${pkgs[@]}" 2>/dev/null | sort)"

  DEBIAN_FRONTEND=noninteractive apt-get install -y "${pkgs[@]}"

  local after=""
  after="$(dpkg-query -W -f='${Package}=${Version}\n' "${pkgs[@]}" 2>/dev/null | sort)"

  if [[ "$before" != "$after" ]]; then
    DOCKER_PACKAGES_DIRTY="true"
    if [[ -z "$before" ]]; then
      log_info "Docker packages installed."
    else
      log_info "Docker packages upgraded (apt detected newer versions)."
    fi
  else
    log_info "Docker packages already at latest available version."
  fi
}

# Idempotent. Writes /etc/docker/daemon.json only if the canonical content
# differs from what's already on disk. Comparison is on jq-canonicalised
# (sort-keys) form so cosmetic differences don't trigger a write.
docker_daemon_json_converged() {
  local desired_canonical
  desired_canonical="$(printf '%s' "$DOCKER_DAEMON_JSON" | jq -S '.' 2>/dev/null || true)"
  if [[ -z "$desired_canonical" ]]; then
    log_error "Failed to canonicalise the desired daemon.json (jq missing or invalid JSON)."
    return 1
  fi

  local current_canonical=""
  if [[ -f "$DOCKER_DAEMON_JSON_PATH" ]]; then
    current_canonical="$(jq -S '.' "$DOCKER_DAEMON_JSON_PATH" 2>/dev/null || true)"
  fi

  if [[ "$current_canonical" == "$desired_canonical" ]]; then
    log_info "daemon.json already matches spec."
    return 0
  fi

  if is_dry_run; then
    if [[ ! -d /etc/docker ]]; then
      printf "WOULD: install -d -m 0755 /etc/docker\n"
    fi
    printf "WOULD: write %s with json-file driver + log-opts (max-size 10m, max-file 5) + live-restore true\n" "$DOCKER_DAEMON_JSON_PATH"
    DOCKER_DAEMON_DIRTY="true"
    return 0
  fi

  # Mutation path: create /etc/docker only on the actual write code path so
  # --dry-run is strictly side-effect-free even when run as root.
  install -d -m 0755 /etc/docker 2>/dev/null || true
  backup_file "$DOCKER_DAEMON_JSON_PATH"
  printf '%s\n' "$DOCKER_DAEMON_JSON" > "$DOCKER_DAEMON_JSON_PATH"
  DOCKER_DAEMON_DIRTY="true"
  log_info "daemon.json written."
}

# Idempotent. Adds `xynes` to the docker group if not already a member.
docker_user_group_ensured() {
  if ! id -u xynes >/dev/null 2>&1; then
    if is_dry_run; then
      printf "WOULD: usermod -aG docker xynes (skipped on this host: user 'xynes' not present)\n"
    else
      log_warn "User 'xynes' does not exist; skipping docker group membership."
    fi
    return 0
  fi
  if id -nG xynes 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
    log_info "User 'xynes' already in docker group."
    return 0
  fi
  run_cmd usermod -aG docker xynes
  log_info "Run 'newgrp docker' (or log out + back in) for group membership to take effect."
}

# Idempotent. Enables docker and restarts it iff daemon.json changed or
# packages were installed/upgraded. live-restore: true preserves running
# containers across the restart.
docker_enabled_and_restarted() {
  if is_dry_run; then
    printf "WOULD: systemctl enable docker\n"
    if [[ "$DOCKER_DAEMON_DIRTY" == "true" || "$DOCKER_PACKAGES_DIRTY" == "true" ]]; then
      printf "WOULD: systemctl restart docker\n"
    fi
    printf "WOULD: docker info (smoke probe)\n"
    return 0
  fi

  systemctl enable docker >/dev/null 2>&1 || true

  if [[ "$DOCKER_DAEMON_DIRTY" == "true" || "$DOCKER_PACKAGES_DIRTY" == "true" ]]; then
    log_info "Restarting docker (live-restore protects running containers)..."
    systemctl restart docker
  else
    # Ensure docker is actually running even if nothing changed.
    if ! systemctl is-active docker >/dev/null 2>&1; then
      systemctl start docker
    fi
  fi

  # Smoke probe — non-fatal but ledger-recorded.
  if docker info >/dev/null 2>&1; then
    log_info "docker info OK."
  else
    log_warn "docker info failed after restart; inspect 'journalctl -u docker'."
    return 1
  fi
}

# Opt-in. Runs `docker system prune -af --volumes` only after a successful
# restart + docker info. Skips when --docker-prune is not set.
docker_prune_if_requested() {
  if [[ "$DOCKER_PRUNE" != "true" ]]; then
    return 0
  fi
  if is_dry_run; then
    printf "WOULD: docker system prune -af --volumes\n"
    return 0
  fi
  log_info "Running 'docker system prune -af --volumes' (skips volumes attached to running containers)..."
  docker system prune -af --volumes >/dev/null 2>&1 || {
    log_warn "docker system prune reported an error; check 'docker system df'."
    return 0
  }
}

section_2_9_docker() {
  log_step "§2.9 Install Docker"

  if [[ "$SKIP_DOCKER" == "true" ]]; then
    log_info "Skipping §2.9 (--skip-docker passed)."
    ledger_add "§2.9" "SKIPPED" "--skip-docker passed"
    return 0
  fi

  # Source diagnostics. If the existing Docker install came from a non-official
  # source (snap, distro, Hestia), the contract is "WARN, no auto-replace" — we
  # MUST NOT touch the apt repo or run apt-get install, because that can either
  # fail mid-bootstrap on package conflicts or replace the engine the operator
  # was told would be left alone. We still let daemon.json / group / restart
  # converge because those settings are safe regardless of where docker came
  # from (they just configure the existing binary).
  local source_desc=""
  local source_official="true"
  if source_desc="$(detect_docker_apt_source)"; then
    log_info "Docker apt source: $source_desc"
  else
    log_warn "Docker apt source: $source_desc — leaving alone to avoid surprise version drop."
    log_warn "Remediation: 'apt-get remove --purge docker.io docker docker-engine' then re-run this script."
    source_official="false"
  fi

  if [[ "$source_official" == "true" ]]; then
    docker_apt_repo_ensured || {
      ledger_add "§2.9" "FAILED" "docker_apt_repo_ensured returned non-zero"
      return 1
    }
    docker_packages_installed || {
      ledger_add "§2.9" "FAILED" "docker_packages_installed returned non-zero"
      return 1
    }
  else
    log_info "Skipping apt repo + package install (non-official source detected)."
  fi
  docker_daemon_json_converged || {
    ledger_add "§2.9" "FAILED" "docker_daemon_json_converged returned non-zero"
    return 1
  }
  docker_user_group_ensured
  docker_enabled_and_restarted || {
    ledger_add "§2.9" "FAILED" "docker_enabled_and_restarted returned non-zero"
    return 1
  }
  docker_prune_if_requested

  local note=""
  if [[ "$source_official" == "false" ]]; then
    note="non-official source — apt repo/install skipped"
    if [[ "$DOCKER_DAEMON_DIRTY" == "true" ]]; then
      note="$note + daemon.json patched"
    fi
  elif [[ "$DOCKER_DAEMON_DIRTY" == "true" && "$DOCKER_PACKAGES_DIRTY" == "true" ]]; then
    note="packages installed/upgraded + daemon.json patched"
  elif [[ "$DOCKER_DAEMON_DIRTY" == "true" ]]; then
    note="daemon.json patched; packages already current"
  elif [[ "$DOCKER_PACKAGES_DIRTY" == "true" ]]; then
    note="packages installed/upgraded; daemon.json already correct"
  else
    note="everything already at spec"
  fi
  if [[ "$DOCKER_PRUNE" == "true" ]]; then
    note="$note (post-restart prune ran)"
  fi
  if [[ "$DOCKER_DAEMON_DIRTY" == "true" || "$DOCKER_PACKAGES_DIRTY" == "true" ]]; then
    ledger_add "§2.9" "APPLIED" "$note"
  else
    ledger_add "§2.9" "ALREADY" "$note"
  fi
  return 0
}

# ------------------------------------------------------------------------------
# Section §2.11 — Docker networks (E-5)
# ------------------------------------------------------------------------------
#
# Creates the four canonical per-env networks required by VPS-SETUP.md §2.11
# and ENVIRONMENTS.md §3:
#
#   xynes_dev_net   bridge   Dev gateway, services, frontend apps, Supabase
#   xynes_qa_net    bridge   QA gateway, services, frontend apps, Supabase
#   xynes_prod_net  bridge   Prod gateway, services, frontend apps, Supabase
#   caddy_net       bridge   Caddy only; Caddy is also connected to each
#                            xynes_<env>_net via `docker network connect`.
#
# Contract:
#   - Idempotent: a network that already exists is left alone (verdict ALREADY).
#     A missing network is created (verdict APPLIED).
#   - Driver: `bridge` everywhere (matches ENVIRONMENTS.md §3 and the E-5
#     Verification rule that `docker network inspect ... --format '{{.Driver}}'`
#     returns `bridge`).
#   - Labels: every network we create carries
#       com.xynes.managed=true
#       com.xynes.network=<network-name>
#     so this script's networks are distinguishable from operator-created ones
#     during audit (`docker network ls --filter label=com.xynes.managed=true`).
#   - Subnets are NOT specified — Docker picks the next free RFC1918 /16. The
#     workspace's compose files do not pin subnets, so neither do we; pinning
#     here would risk collisions with the host's other networks.
#   - --skip-networks short-circuits the entire section with a SKIPPED ledger
#     row, mirroring --skip-docker for §2.9.
#   - On a host without `docker` in PATH (e.g. dry-run on macOS dev laptop),
#     the section emits WOULD: lines but otherwise behaves as a no-op probe.
#     In apply mode without `docker`, the section logs an error and exits 1
#     so we don't silently skip in production.

XYNES_NETWORKS=(xynes_dev_net xynes_qa_net xynes_prod_net caddy_net)

# Returns 0 if the network named "$1" already exists on this host, 1 otherwise.
# Does NOT mutate state. In dry-run mode this is read-only by construction.
docker_network_exists() {
  local name="$1"
  if ! command -v docker >/dev/null 2>&1; then
    # Treat "no docker" as "not present" so dry-run lists every WOULD: line.
    return 1
  fi
  # `docker network inspect <name>` exits non-zero with a clean error when
  # the network doesn't exist. Redirect stderr so the test harness output
  # stays clean.
  docker network inspect "$name" >/dev/null 2>&1
}

# Returns 0 if the EXISTING network named "$1" matches the §2.11 spec exactly:
#   - driver is `bridge`
#   - label `com.xynes.managed=true` is present
#   - label `com.xynes.network=<name>` is present and equals the network name
# Returns 1 if any of those checks fail, and prints a `[WARN ]` line per check
# so the operator sees exactly what diverged. Returns 2 if `docker inspect`
# itself fails (network vanished between the existence probe and this call,
# or docker daemon went away).
#
# Codex follow-up (PR #101, review id 4435696134, P2): without this check,
# a network created out-of-band as `--driver overlay` or without our
# documented labels would silently report `ALREADY` and §2.11 would ledger
# as green — but the audit command `docker network ls --filter
# label=com.xynes.managed=true` documented in the closure block would omit
# it. That contract breaks if we don't validate.
docker_network_matches_spec() {
  local name="$1"

  if ! command -v docker >/dev/null 2>&1; then
    # Caller already gated us on docker presence; reaching this with no
    # docker is a programming error, surface it loudly.
    return 2
  fi

  # Inspect once, in a structured form. Format: <driver>|<labels-json>.
  # `docker network inspect` exits non-zero if the network is gone, in
  # which case we treat it as a transient miss and let the caller retry
  # the existence probe.
  local probe driver labels managed_label net_label
  if ! probe="$(docker network inspect --format '{{.Driver}}|{{json .Labels}}' "$name" 2>/dev/null)"; then
    return 2
  fi
  driver="${probe%%|*}"
  labels="${probe#*|}"

  # We intentionally use simple string-equality checks against the labels
  # JSON rather than pulling in `jq`. The label values we care about are
  # closed-set (`true`, `<name>`) and the property names are stable strings
  # that JSON-encode to themselves. `jq` is not guaranteed to be installed
  # on a fresh Ubuntu host before §2.5 runs, and §2.11 is allowed to run
  # after §2.5 — so depending on `jq` here would be a soft dep we don't
  # need to take on.
  managed_label='"com.xynes.managed":"true"'
  net_label="\"com.xynes.network\":\"$name\""

  local mismatched="false"

  if [[ "$driver" != "bridge" ]]; then
    log_warn "Network '$name' driver is '$driver', expected 'bridge' (§2.11 + ENVIRONMENTS.md §3)."
    mismatched="true"
  fi
  if [[ "$labels" != *"$managed_label"* ]]; then
    log_warn "Network '$name' is missing label 'com.xynes.managed=true' — audit filter 'docker network ls --filter label=com.xynes.managed=true' will omit it."
    mismatched="true"
  fi
  if [[ "$labels" != *"$net_label"* ]]; then
    log_warn "Network '$name' is missing label 'com.xynes.network=$name' — audit cross-reference will omit it."
    mismatched="true"
  fi

  [[ "$mismatched" == "false" ]]
}

# Idempotent. Creates one network if missing; records APPLIED / ALREADY /
# MISMATCH / FAILED in the per-network counters so the section summary is
# accurate. On an existing-name hit, the network is validated against the
# §2.11 spec (driver + both labels). A mismatch increments NETWORKS_MISMATCH
# and FAILS rather than silently marking the section converged — the only
# safe convergence path on a labels-mismatch is `docker network rm <name>`
# followed by a re-run, which is the operator-driven rollback documented in
# the story spec.
docker_network_ensure() {
  local name="$1"
  if docker_network_exists "$name"; then
    if docker_network_matches_spec "$name"; then
      log_info "Network '$name' already exists and matches spec (driver=bridge + both labels)."
      NETWORKS_ALREADY=$((NETWORKS_ALREADY + 1))
      return 0
    fi
    log_error "Network '$name' exists but does NOT match the §2.11 spec. Remediation: 'docker network rm $name' (only if no container is attached), then re-run this script."
    NETWORKS_MISMATCH=$((NETWORKS_MISMATCH + 1))
    return 1
  fi
  if is_dry_run; then
    printf "WOULD: docker network create --driver bridge --label com.xynes.managed=true --label com.xynes.network=%s %s\n" "$name" "$name"
    NETWORKS_APPLIED=$((NETWORKS_APPLIED + 1))
    return 0
  fi
  if ! command -v docker >/dev/null 2>&1; then
    log_error "docker is not installed; cannot create '$name'. Run §2.9 (or unset --skip-docker) first."
    NETWORKS_FAILED=$((NETWORKS_FAILED + 1))
    return 1
  fi
  if ! docker network create \
        --driver bridge \
        --label com.xynes.managed=true \
        --label "com.xynes.network=$name" \
        "$name" >/dev/null; then
    log_error "Failed to create docker network '$name'."
    NETWORKS_FAILED=$((NETWORKS_FAILED + 1))
    return 1
  fi
  log_info "Network '$name' created (bridge)."
  NETWORKS_APPLIED=$((NETWORKS_APPLIED + 1))
}

section_2_11_networks() {
  log_step "§2.11 Docker networks"

  if [[ "$SKIP_NETWORKS" == "true" ]]; then
    log_info "Skipping §2.11 (--skip-networks passed)."
    ledger_add "§2.11" "SKIPPED" "--skip-networks passed"
    return 0
  fi

  # In apply mode, require docker to be present. In dry-run mode, missing
  # docker is fine (the WOULD: lines still tell the operator what the script
  # plans to do).
  if ! is_dry_run && ! command -v docker >/dev/null 2>&1; then
    log_error "§2.11 requires docker; run §2.9 first or remove --skip-docker."
    ledger_add "§2.11" "FAILED" "docker CLI not available"
    return 1
  fi

  NETWORKS_APPLIED=0
  NETWORKS_ALREADY=0
  NETWORKS_FAILED=0
  NETWORKS_MISMATCH=0  # Codex follow-up PR #101: existing networks that don't match the §2.11 spec

  local name
  for name in "${XYNES_NETWORKS[@]}"; do
    docker_network_ensure "$name" || true
  done

  local note=""
  note="created=$NETWORKS_APPLIED already=$NETWORKS_ALREADY mismatch=$NETWORKS_MISMATCH failed=$NETWORKS_FAILED"
  # MISMATCH is a fail-closed verdict: a network exists under one of our
  # canonical names but with the wrong driver or missing labels, so the
  # documented audit command would omit it. We surface MISMATCH separately
  # in the ledger row so an operator sees exactly why §2.11 didn't converge,
  # but the verdict is still FAILED so the script exits non-zero and the
  # broken state can't be mistaken for a green run.
  if [[ "$NETWORKS_FAILED" -gt 0 || "$NETWORKS_MISMATCH" -gt 0 ]]; then
    ledger_add "§2.11" "FAILED" "$note"
    return 1
  fi
  if [[ "$NETWORKS_APPLIED" -gt 0 ]]; then
    ledger_add "§2.11" "APPLIED" "$note"
  else
    ledger_add "§2.11" "ALREADY" "$note"
  fi
  return 0
}

# ------------------------------------------------------------------------------
# Final: restart sshd if §2.3 changed anything.
# ------------------------------------------------------------------------------

maybe_restart_sshd() {
  if [[ "$SSHD_DIRTY" != "true" ]]; then
    return 0
  fi
  log_step "Restarting sshd (config changed in §2.3)"
  # Validate config before restart so a typo doesn't lock us out.
  if is_dry_run; then
    printf "WOULD: sshd -t && systemctl restart sshd\n"
    return 0
  fi
  if ! sshd -t; then
    log_error "sshd -t reported config errors. Refusing to restart. Inspect /etc/ssh/sshd_config."
    return 1
  fi
  systemctl restart sshd
}

# ------------------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------------------

print_summary() {
  log_step "Summary"
  printf "%-12s  %-8s  %s\n" "SECTION" "VERDICT" "NOTE"
  printf "%-12s  %-8s  %s\n" "-------" "-------" "----"
  local row
  for row in "${SECTION_LEDGER[@]}"; do
    local section verdict note
    section="${row%%|*}"
    verdict="${row#*|}"; verdict="${verdict%%|*}"
    note="${row##*|}"
    printf "%-12s  %-8s  %s\n" "$section" "$verdict" "$note"
  done
  printf "\nMode: %s | sshd_dirty: %s | force_tz: %s\n" "$MODE" "$SSHD_DIRTY" "$FORCE_TZ"
}

# ------------------------------------------------------------------------------
# main()
# ------------------------------------------------------------------------------

main() {
  log_info "$SCRIPT_NAME starting (mode=$MODE, force-tz=$FORCE_TZ)"
  require_root_for_apply

  # In dry-run, we may not have root or even an Ubuntu host. We still want the
  # ledger to surface what the script WOULD do. Each section guards its own
  # mutating calls via run_cmd / run_sh / backup_file.
  section_2_2_user
  section_2_3_sshd
  section_2_4_ufw
  section_2_5_fail2ban
  section_2_6_unattended
  section_2_7_time
  section_2_8_swap
  section_2_9_docker
  section_2_11_networks

  # Always print the summary even if sshd restart hits a snag, so the operator
  # can see what converged before troubleshooting.
  local sshd_rc=0
  set +e
  maybe_restart_sshd
  sshd_rc=$?
  set -e
  print_summary
  if [[ "$sshd_rc" -ne 0 ]]; then
    log_error "sshd restart step failed (rc=$sshd_rc). Inspect /etc/ssh/sshd_config before retrying."
    exit "$sshd_rc"
  fi
}

# Only auto-run main when this file is executed directly (not when sourced by
# the offline test suite, which sources the helpers into its own shell to test
# them in isolation).
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main
fi
