#!/usr/bin/env bash
# =============================================================================
# env-templates-no-secrets.test.sh
# =============================================================================
# Static gate that sweeps every COMMITTED env template (`.env.example`, the
# hosted `.env.<env>.template` files) for real-secret patterns. If a real
# Cloudflare R2 key, Resend key, AWS access key, JWT, raw API key, or
# verifiable credential ever appears in any of these files, this test fails.
#
# Wired into scripts/test/run.sh via the existing *.test.sh glob.
#
# Rule: any value resembling a real secret in a committed env template is a
# P0 leak. The committed templates may only carry placeholders.
# =============================================================================

set -euo pipefail

INFRA_DIR="${INFRA_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
cd "$INFRA_DIR"

PASS=0
FAIL=0
FAIL_DETAILS=()

assert_no_match() {
  local label="$1"
  local pattern="$2"
  local file="$3"

  local match_line status
  if match_line=$(python3 - "$pattern" "$file" <<'PY_MATCH'
import os, re, stat, sys
from pathlib import Path
LIMIT = 1024 * 1024

def signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

def regular(info):
    if not stat.S_ISREG(info.st_mode) or info.st_size > LIMIT:
        raise ValueError("unsafe template")

parents = []
missing_is_absence = True
try:
    path = Path(sys.argv[2])
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    directory = os.open('/' if path.is_absolute() else '.', flags | os.O_DIRECTORY)
    parents.append(directory)
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for component in parts[:-1]:
        missing_is_absence = True
        before = os.stat(component, dir_fd=directory, follow_symlinks=False)
        missing_is_absence = False
        if not stat.S_ISDIR(before.st_mode):
            raise ValueError("unsafe template parent")
        child = os.open(component, flags | os.O_DIRECTORY, dir_fd=directory)
        parents.append(child)
        if signature(before) != signature(os.fstat(child)):
            raise ValueError("template parent changed")
        directory = child
    name = parts[-1]
    missing_is_absence = True
    before = os.stat(name, dir_fd=directory, follow_symlinks=False)
    missing_is_absence = False
    regular(before)
    descriptor = os.open(name, flags, dir_fd=directory)
    with os.fdopen(descriptor, 'rb') as file:
        opened = os.fstat(file.fileno())
        regular(opened)
        if signature(before) != signature(opened):
            raise ValueError("template changed")
        data = file.read(LIMIT + 1)
        after = os.fstat(file.fileno())
        entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if len(data) > LIMIT or signature(before) != signature(after) or signature(after) != signature(entry):
            raise ValueError("template changed")
    matcher = re.compile(sys.argv[1])
    for number, line in enumerate(data.decode('utf8').splitlines(), 1):
        if matcher.search(line):
            print(number)  # Only location metadata; never the matching line.
            sys.exit(1)
except FileNotFoundError:
    sys.exit(3 if missing_is_absence else 2)  # An observed entry disappearing is unsafe.
except (OSError, ValueError, UnicodeError, re.error):
    sys.exit(2)
finally:
    for descriptor in reversed(parents):
        os.close(descriptor)
PY_MATCH
  ); then
    PASS=$((PASS + 1))
  else
    status=$?
    if [[ "$status" == 3 ]]; then return 3; fi
    FAIL=$((FAIL + 1))
    if [[ "$status" == 1 ]]; then
      FAIL_DETAILS+=("$label in $file:$match_line (value redacted)")
    else
      FAIL_DETAILS+=("Secret check refused for $file (unreadable template or invalid matcher)")
    fi
  fi
}

# -----------------------------------------------------------------------------
# Targets: every committed env template
# -----------------------------------------------------------------------------
TARGETS=(
  ".env.example"
  "infra/release/templates/.env.dev.template"
  "infra/release/templates/.env.qa.template"
  "infra/release/templates/.env.staging.template"
  "infra/release/templates/.env.prod.template"
)

# Also sweep per-service template stubs at sibling repos when run from the
# workspace root.
SIBLINGS=(
  "../xynes-authz-service/.env.example"
  "../xynes-storage-service/.env.example"
  "../xynes-telemetry-service/.env.example"
  "../xynes-platform-config/.env.example"
  "../../xynes-front-end/infra/.env.example"
  "../../xynes-front-end/xynes-auth-app/.env.example"
  "../../xynes-front-end/xynes-auth-sdk/.env.example"
  "../../xynes-front-end/xynes-cms-console-web/.env.example"
)

ALL_TARGETS=("${TARGETS[@]}" "${SIBLINGS[@]}")

# -----------------------------------------------------------------------------
# Forbidden-pattern matrix
# -----------------------------------------------------------------------------
# Each row is "label : regex" — keep this list closed and audit it when adding.
PATTERNS=(
  "AWS_ACCESS_KEY_ID literal:AKIA[A-Z0-9]{16}"
  "Internal raw API key (xynes_live_*):xynes_live_[a-f0-9]{32,}"
  "Resend API key (re_*):re_[A-Za-z0-9_-]{20,}"
  "SigV4 signed-URL parameters:X-Amz-Signature=[A-Fa-f0-9]+"
  "Supabase JWT (3-segment base64):eyJ[A-Za-z0-9_-]{20,}\\.eyJ[A-Za-z0-9_-]{40,}\\."
  "PostHog project API key:phc_[A-Za-z0-9]{30,}"
  "Stripe live secret key:sk_live_[A-Za-z0-9]{20,}"
  "GitHub PAT (ghp_):ghp_[A-Za-z0-9]{30,}"
  "GitHub fine-grained PAT (github_pat_):github_pat_[A-Za-z0-9_]{40,}"
  "Slack bot token:xox[baprs]-[A-Za-z0-9-]{10,}"
  "SSH private key header:-----BEGIN [A-Z ]*PRIVATE KEY-----"
  'Postgres URL with non-placeholder password:postgres(ql)?://[^:]+:(?!(?:postgres|REPLACE[A-Z0-9_]*|replace-with-[A-Za-z0-9_-]*|change-me(?:-[A-Za-z0-9_-]+)?|\$\{[A-Z][A-Z0-9_]*\})@)[^@]+@'
)

for target in "${ALL_TARGETS[@]}"; do
  for row in "${PATTERNS[@]}"; do
    label="${row%%:*}"
    pattern="${row#*:}"
    if assert_no_match "$label" "$pattern" "$target"; then
      :
    else
      status=$?
      if [[ "$status" == 3 ]]; then break; fi
      FAIL=$((FAIL + 1))
      FAIL_DETAILS+=("Secret check refused for $target (unexpected reader status)")
    fi
  done
done

# -----------------------------------------------------------------------------
# Existence of expected templates (regression guard)
# -----------------------------------------------------------------------------
for required in "${TARGETS[@]}"; do
  if [ ! -f "$required" ]; then
    FAIL=$((FAIL + 1))
    FAIL_DETAILS+=("MISSING expected template: $required")
  else
    PASS=$((PASS + 1))
  fi
done

# -----------------------------------------------------------------------------
# Result
# -----------------------------------------------------------------------------
TOTAL=$((PASS + FAIL))
echo "env-templates-no-secrets: $PASS / $TOTAL assertions passed"
if [ "$FAIL" -gt 0 ]; then
  echo ""
  echo "FAILURES:"
  for detail in "${FAIL_DETAILS[@]}"; do
    echo "  - $detail"
  done
  echo ""
  echo "Real secret material in a committed env template is a P0 leak."
  echo "See xynes-infra/infra/release/SECRETS.md §1.3 'Forbidden locations'."
  exit 1
fi

echo "All env-template no-secrets static checks passed."
exit 0
