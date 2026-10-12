#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

assert_contains() {
  local haystack="$1"
  local needle="$2"
  echo "$haystack" | grep -q "$needle" || fail "expected output to contain: $needle"
}

CONFIG_PATH="supabase/config.toml"
[ -f "$CONFIG_PATH" ] || fail "missing $CONFIG_PATH"

CONTENT_PATH="$(
  awk '
    $0 ~ /^\[auth\.email\.template\.recovery\]/ { inside=1; next }
    inside && $0 ~ /^\[/ { inside=0 }
    inside && $0 ~ /^content_path[[:space:]]*=/ {
      sub(/.*=/,"")
      gsub(/^[[:space:]]+|[[:space:]]+$/,"")
      gsub(/"/,"")
      print
      exit
    }
  ' "$CONFIG_PATH"
)"

[ -n "$CONTENT_PATH" ] || fail "auth.email.template.recovery.content_path is not set in $CONFIG_PATH"

if [[ "$CONTENT_PATH" == ./* ]]; then
  # Supabase resolves content_path relative to the project root (the dir that contains ./supabase),
  # not relative to the supabase/ folder itself.
  TEMPLATE_FILE="$ROOT_DIR/${CONTENT_PATH#./}"
else
  TEMPLATE_FILE="$CONTENT_PATH"
fi

[ -f "$TEMPLATE_FILE" ] || fail "recovery template not found at: $TEMPLATE_FILE (from content_path=$CONTENT_PATH)"

TEMPLATE_CONTENT="$(cat "$TEMPLATE_FILE")"
assert_contains "$TEMPLATE_CONTENT" "{{ .ConfirmationURL }}"

echo "PASS: supabase-email-templates.test.sh"
