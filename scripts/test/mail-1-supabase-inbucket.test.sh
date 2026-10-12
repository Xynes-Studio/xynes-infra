#!/usr/bin/env bash
set -euo pipefail

# MAIL-1 contract test — guards the Inbucket local-dev inbox config.
#
# Asserts that supabase/config.toml's [inbucket] block stays in the MAIL-1
# shape so app-level mailers (MAIL-2 StubMailerClient, MAIL-5 workspace-invite)
# can deliver to the local inbox without per-developer re-configuration.
#
# What it covers:
#   1. [inbucket] block exists.
#   2. enabled = true (not the upstream `enabled = false` default).
#   3. port  = 54324 (web UI).
#   4. smtp_port = 54325 (uncommented, exposed for SMTP relay from apps).
#   5. pop3_port = 54326 (uncommented, exposed for programmatic inspection).
#   6. ENV_GUIDE.md documents the inbox (web UI + SMTP + POP3).
#   7. DEVELOPER.md links to ENV_GUIDE.md for the inbox callout.
#
# Plan ref: xynes-infra/docs/plans/2026-05-30-invite-mail-delivery.md §5 (MAIL-1).

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

CONFIG_PATH="supabase/config.toml"
ENV_GUIDE_PATH="ENV_GUIDE.md"
DEVELOPER_PATH="DEVELOPER.md"

PASS=0
FAIL=0

pass() {
  echo "  PASS: $1"
  PASS=$((PASS + 1))
}

fail() {
  echo "  FAIL: $1" >&2
  FAIL=$((FAIL + 1))
}

assert_file_exists() {
  local path="$1"
  if [ -f "$path" ]; then
    pass "file exists: $path"
  else
    fail "missing file: $path"
  fi
}

# Extracts the value of a key from a named TOML table without parsing nested
# tables. Returns the raw RHS after the `=`, trimmed and unquoted. Empty when
# the key is absent or commented out.
toml_value_in_table() {
  local file="$1"
  local table="$2"
  local key="$3"
  awk -v table="[$table]" -v key="$key" '
    $0 == table { inside = 1; next }
    inside && /^\[/ { inside = 0 }
    inside && $0 ~ "^[[:space:]]*" key "[[:space:]]*=" {
      sub(/^[^=]*=[[:space:]]*/, "")
      sub(/[[:space:]]*(#.*)?$/, "")
      gsub(/"/, "")
      print
      exit
    }
  ' "$file"
}

echo "MAIL-1 — Supabase Inbucket local-dev inbox config contract"
echo

# --- 1. Files exist -----------------------------------------------------------
assert_file_exists "$CONFIG_PATH"
assert_file_exists "$ENV_GUIDE_PATH"
assert_file_exists "$DEVELOPER_PATH"

# --- 2. [inbucket] block + values --------------------------------------------
if grep -q '^\[inbucket\]' "$CONFIG_PATH"; then
  pass "[inbucket] block present in $CONFIG_PATH"
else
  fail "[inbucket] block missing in $CONFIG_PATH"
fi

ENABLED="$(toml_value_in_table "$CONFIG_PATH" "inbucket" "enabled")"
if [ "$ENABLED" = "true" ]; then
  pass "[inbucket] enabled = true"
else
  fail "[inbucket] enabled must be 'true', got: '${ENABLED:-<unset>}'"
fi

PORT="$(toml_value_in_table "$CONFIG_PATH" "inbucket" "port")"
if [ "$PORT" = "54324" ]; then
  pass "[inbucket] port = 54324"
else
  fail "[inbucket] port must be 54324, got: '${PORT:-<unset>}'"
fi

SMTP_PORT="$(toml_value_in_table "$CONFIG_PATH" "inbucket" "smtp_port")"
if [ "$SMTP_PORT" = "54325" ]; then
  pass "[inbucket] smtp_port = 54325 (uncommented)"
else
  fail "[inbucket] smtp_port must be 54325 (uncommented), got: '${SMTP_PORT:-<unset>}'"
fi

POP3_PORT="$(toml_value_in_table "$CONFIG_PATH" "inbucket" "pop3_port")"
if [ "$POP3_PORT" = "54326" ]; then
  pass "[inbucket] pop3_port = 54326 (uncommented)"
else
  fail "[inbucket] pop3_port must be 54326 (uncommented), got: '${POP3_PORT:-<unset>}'"
fi

# Defense-in-depth: make sure no later block has flipped enabled back off.
# (This catches a future TOML duplicate-key footgun.)
DOUBLE_ENABLED_FALSE="$(awk '
  /^\[inbucket\]/ { inside = 1; next }
  inside && /^\[/ { inside = 0 }
  inside && /^[[:space:]]*enabled[[:space:]]*=[[:space:]]*false/ { count++ }
  END { print count + 0 }
' "$CONFIG_PATH")"
if [ "$DOUBLE_ENABLED_FALSE" = "0" ]; then
  pass "[inbucket] no 'enabled = false' override inside the block"
else
  fail "[inbucket] block contains an 'enabled = false' line (re-enables upstream default)"
fi

# --- 3. ENV_GUIDE.md documents the inbox -------------------------------------
if grep -q '### Local dev inbox (Inbucket — MAIL-1)' "$ENV_GUIDE_PATH"; then
  pass "ENV_GUIDE.md carries the 'Local dev inbox (Inbucket — MAIL-1)' section"
else
  fail "ENV_GUIDE.md missing the 'Local dev inbox (Inbucket — MAIL-1)' section"
fi

for url_check in \
  "http://127.0.0.1:54324" \
  "smtp://127.0.0.1:54325" \
  "pop3://127.0.0.1:54326"
do
  if grep -qF "$url_check" "$ENV_GUIDE_PATH"; then
    pass "ENV_GUIDE.md documents $url_check"
  else
    fail "ENV_GUIDE.md missing reference to $url_check"
  fi
done

# Mailer wiring guidance must reference the SMTP relay URL so MAIL-2/MAIL-5
# implementers configure their clients correctly.
if grep -qF 'SMTP_RELAY_URL=smtp://127.0.0.1:54325' "$ENV_GUIDE_PATH"; then
  pass "ENV_GUIDE.md documents SMTP_RELAY_URL for app-level mailers"
else
  fail "ENV_GUIDE.md missing SMTP_RELAY_URL guidance for app-level mailers"
fi

# Cross-link to the canonical invite-mail plan must be present so future
# readers find the broader architectural context.
if grep -qF '2026-05-30-invite-mail-delivery.md' "$ENV_GUIDE_PATH"; then
  pass "ENV_GUIDE.md cross-links to the invite-mail delivery plan"
else
  fail "ENV_GUIDE.md missing cross-link to 2026-05-30-invite-mail-delivery.md"
fi

# --- 4. DEVELOPER.md Quick Start callout -------------------------------------
if grep -qF 'Local mail inbox (MAIL-1)' "$DEVELOPER_PATH"; then
  pass "DEVELOPER.md Quick Start carries the MAIL-1 inbox callout"
else
  fail "DEVELOPER.md Quick Start missing the MAIL-1 inbox callout"
fi

if grep -qF '127.0.0.1:54324' "$DEVELOPER_PATH"; then
  pass "DEVELOPER.md mentions the Inbucket web UI URL"
else
  fail "DEVELOPER.md missing Inbucket web UI URL"
fi

# --- 5. Secrets sweep --------------------------------------------------------
# MAIL-1 is config-only. Make sure no raw credentials slipped into the touched
# files (mirrors the STORAGE-LIVE-1 / MAIL-4 secret-handling posture).
SECRET_PATTERNS='AKIA[A-Z0-9]+|xynes_live_[a-f0-9]+|X-Amz-Signature|RESEND_API_KEY[[:space:]]*=[[:space:]]*re_|GOTRUE_SMTP_PASS[[:space:]]*=[[:space:]]*[^[:space:]E]'
for f in "$CONFIG_PATH" "$ENV_GUIDE_PATH" "$DEVELOPER_PATH"; do
  if grep -Eq "$SECRET_PATTERNS" "$f"; then
    fail "$f appears to contain a raw credential (matched: $SECRET_PATTERNS)"
  else
    pass "$f has no raw credential substrings"
  fi
done

# --- Summary ----------------------------------------------------------------
echo
echo "MAIL-1 contract test: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
  echo "FAIL: mail-1-supabase-inbucket.test.sh"
  exit 1
fi
echo "PASS: mail-1-supabase-inbucket.test.sh"
