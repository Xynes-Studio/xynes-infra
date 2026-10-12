#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

# shellcheck source=./scripts/lib/env.sh
source ./scripts/lib/env.sh

TMP_ENV="$(mktemp)"
cat >"$TMP_ENV" <<'EOF'
# comment
FOO="bar baz"
export BAZ='qux'
NUM=123
EOF

# Does not override
export FOO="already"
load_env_file_no_override "$TMP_ENV"

[ "$FOO" = "already" ] || fail "expected FOO not overridden"
[ "$BAZ" = "qux" ] || fail "expected BAZ to be loaded"
[ "$NUM" = "123" ] || fail "expected NUM to be loaded"

rm -f "$TMP_ENV" || true

echo "PASS: env-lib.test.sh"
