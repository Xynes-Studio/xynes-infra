#!/usr/bin/env bash
set -euo pipefail

# CMS-API-KEY-ACTOR-1 Story E — static / shape tests for the
# `scripts/smoke-cms-api-key.sh` harness. Mirrors the pattern used by
# `scripts/test/smoke-cms-entry.test.sh`: exercises the script's `--help`
# path, fast-fail behaviour when required env is missing, and asserts
# the script covers the action keys that the plan requires.
#
# NOTE: this test never reaches the network — it only validates the
# script's static surface. The live battery is exercised by running the
# script itself against a real local stack (see plan §11).

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

SCRIPT="./scripts/smoke-cms-api-key.sh"

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

assert_contains() {
  local haystack="$1"
  local needle="$2"
  # Use `grep -- <pattern>` so needles that start with `-` (e.g.
  # `--workspace-id`) aren't parsed as grep flags by macOS grep.
  echo "$haystack" | grep -q -- "$needle" \
    || fail "expected output to contain: ${needle}"
}

# 1) Script must exist and be executable.
[ -x "${SCRIPT}" ] || fail "${SCRIPT} is not executable"

# 2) --help should succeed and mention the preset surface + key flags.
OUT="$(${SCRIPT} --help)"
assert_contains "$OUT" "smoke-cms-api-key.sh"
assert_contains "$OUT" "--workspace-id"
assert_contains "$OUT" "--other-workspace-id"
assert_contains "$OUT" "--keep-keys"
assert_contains "$OUT" "route-segment"

# 3) Missing JWT_SECRET should fail fast with a clear error BEFORE any
#    network or DB activity.
set +e
OUT2="$(JWT_SECRET="" ENV_FILE="/tmp/does-not-exist-$(date +%s).env" \
       "${SCRIPT}" 2>&1)"
CODE2=$?
set -e
[ "$CODE2" -ne 0 ] || fail "expected missing JWT_SECRET to return non-zero"
assert_contains "$OUT2" "JWT_SECRET is not set"

# 4) Script must reference every action key the plan requires it to
#    cover (RO public reads, AU writes, PU publish/status, plus the
#    out-of-preset action keys that drive the 403 rows).
SCRIPT_CONTENT="$(cat "${SCRIPT}")"
for action_path in \
  "/content/entries" \
  "/content/entries/\${ENTRY_ID}/publish" \
  "/content/entries/\${ENTRY_ID}/status" \
  "/content/\${ROUTE_SEGMENT}" \
  "/blog"; do
  assert_contains "$SCRIPT_CONTENT" "${action_path}" \
    || fail "smoke script missing expected path fragment: ${action_path}"
done

# 5) Script must mint each of the three CMS preset keys so the matrix
#    has one key per role.
for preset in cms_readonly cms_authoring cms_publisher; do
  assert_contains "$SCRIPT_CONTENT" "mint_key ${preset}" \
    || fail "smoke script does not mint ${preset} key"
done

# 6) Script must assert DB audit invariants (created_by / updated_by
#    NULL for api_key actors; populated for user actor).
assert_contains "$SCRIPT_CONTENT" "AU.1.db created_by IS NULL"
assert_contains "$SCRIPT_CONTENT" "BC.1.db created_by = workspace_owner userId"

# 7) Script must redact-check raw keys against BOTH cms-core and
#    gateway logs (Story C invariant + Story E acceptance criteria).
assert_contains "$SCRIPT_CONTENT" "logs cms-core"
assert_contains "$SCRIPT_CONTENT" "logs gateway"

# 8) Cleanup trap must be wired so smoke keys are revoked on exit.
assert_contains "$SCRIPT_CONTENT" "trap cleanup EXIT"

echo "PASS: smoke-cms-api-key.test.sh"
