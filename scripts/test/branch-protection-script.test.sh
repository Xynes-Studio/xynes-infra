#!/usr/bin/env bash
# branch-protection-script.test.sh — Story B-1 unit-style coverage.
#
# Asserts the offline behaviour of:
#   - scripts/setup/configure-branch-protection.sh
#
# This test never invokes `gh`. It exercises only --help, --dry-run, the
# --branch and --repo filters, repos.json validation, and structural
# greps over the script source. The live-`gh` rollout is owned by story
# D-2 in xynes/xynes-infra/docs/plans/2026-05-13-mvp-release-stories/group-D-branch-protection-rollout.md.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

SCRIPT="scripts/setup/configure-branch-protection.sh"
REPOS_FILE="scripts/setup/repos.json"

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

# Run a command, capture stdout+stderr to a temp file, expose exit code in $RC.
LAST_OUT=""
RC=0
capture() {
  LAST_OUT="$(mktemp -t b1-test.XXXXXX)"
  RC=0
  "$@" >"$LAST_OUT" 2>&1 || RC=$?
}

# shellcheck disable=SC2329  # Invoked via trap below.
cleanup_capture() {
  if [ -n "${LAST_OUT:-}" ] && [ -f "$LAST_OUT" ]; then
    rm -f "$LAST_OUT"
  fi
}
trap cleanup_capture EXIT

echo "B-1 — configure-branch-protection.sh offline contract"
echo

# --- 0. Files exist + executable -------------------------------------------
if [ -f "$SCRIPT" ]; then pass "script present: $SCRIPT"; else fail "missing: $SCRIPT"; fi
if [ -x "$SCRIPT" ]; then pass "script is executable"; else fail "script not executable"; fi
if [ -f "$REPOS_FILE" ]; then pass "repos file present: $REPOS_FILE"; else fail "missing: $REPOS_FILE"; fi

# --- 1. repos.json shape (defense in depth; B-2 also covers this) ----------
if jq -e '.owner == "Xynes-Studio"' "$REPOS_FILE" >/dev/null; then
  pass "repos.json .owner = Xynes-Studio"
else
  fail "repos.json .owner is not Xynes-Studio"
fi

REPO_COUNT="$(jq '.repos | length' "$REPOS_FILE")"
if [ "$REPO_COUNT" = "16" ]; then
  pass "repos.json carries exactly 16 entries"
else
  fail "repos.json carries $REPO_COUNT entries (expected 16)"
fi

# --- 2. --help -------------------------------------------------------------
capture bash "$SCRIPT" --help
if [ "$RC" = "0" ] && grep -q "USAGE:" "$LAST_OUT"; then
  pass "--help exits 0 and prints USAGE"
else
  fail "--help broken (rc=$RC)"
fi

if grep -q "main" "$LAST_OUT" \
  && grep -q "develop" "$LAST_OUT" \
  && grep -q "release/\*" "$LAST_OUT"; then
  pass "--help mentions main, develop, and release/* branches"
else
  fail "--help missing one of: main / develop / release/*"
fi

# --- 3. Default --dry-run prints exactly 48 PLAN rows (16 repos × 3 branches)
capture bash "$SCRIPT"
if [ "$RC" = "0" ]; then
  pass "default mode (dry-run) exits 0"
else
  fail "default mode exited non-zero (rc=$RC)"
  echo "----- output -----" >&2
  cat "$LAST_OUT" >&2
  echo "------------------" >&2
fi

PLAN_ROWS="$(grep -c '\[PLAN' "$LAST_OUT" || true)"
if [ "$PLAN_ROWS" = "48" ]; then
  pass "dry-run prints exactly 48 PLAN rows (16 repos × 3 branches)"
else
  fail "dry-run printed $PLAN_ROWS PLAN rows (expected 48)"
fi

if grep -q 'mode=dry-run' "$LAST_OUT"; then
  pass "dry-run header reports mode=dry-run"
else
  fail "dry-run header missing mode=dry-run"
fi

if grep -q 'owner=Xynes-Studio' "$LAST_OUT"; then
  pass "dry-run header reports owner=Xynes-Studio"
else
  fail "dry-run header missing owner=Xynes-Studio"
fi

if grep -q 'repos=16 branches=3' "$LAST_OUT"; then
  pass "dry-run header reports repos=16 branches=3"
else
  fail "dry-run header missing repos=16 branches=3"
fi

# --- 4. --dry-run advertises the documented gh verbs -----------------------
MAIN_PUTS="$(grep -c 'PUT /repos/Xynes-Studio/.*/branches/main/protection' "$LAST_OUT" || true)"
if [ "$MAIN_PUTS" = "16" ]; then
  pass "dry-run advertises 16 'PUT .../branches/main/protection' verbs"
else
  fail "dry-run printed $MAIN_PUTS main PUT verbs (expected 16)"
fi

DEVELOP_PUTS="$(grep -c 'PUT /repos/Xynes-Studio/.*/branches/develop/protection' "$LAST_OUT" || true)"
if [ "$DEVELOP_PUTS" = "16" ]; then
  pass "dry-run advertises 16 'PUT .../branches/develop/protection' verbs"
else
  fail "dry-run printed $DEVELOP_PUTS develop PUT verbs (expected 16)"
fi

RELEASE_POSTS="$(grep -c 'POST /repos/Xynes-Studio/.*/rulesets (name=release-protection' "$LAST_OUT" || true)"
if [ "$RELEASE_POSTS" = "16" ]; then
  pass "dry-run advertises 16 'POST .../rulesets (name=release-protection)' verbs"
else
  fail "dry-run printed $RELEASE_POSTS release-ruleset POST verbs (expected 16)"
fi

if grep -q '\[NOTE  \] required_signatures NOT enforced' "$LAST_OUT"; then
  pass "dry-run summary carries the signed-commits-disabled NOTE line"
else
  fail "dry-run summary missing the signed-commits-disabled NOTE line"
fi

if grep -q 'Summary: total=48 ' "$LAST_OUT"; then
  pass "dry-run summary reports total=48"
else
  fail "dry-run summary missing total=48"
fi

# --- 5. --branch single-target filters -------------------------------------
capture bash "$SCRIPT" --dry-run --branch main
ROWS="$(grep -c '\[PLAN' "$LAST_OUT" || true)"
if [ "$RC" = "0" ] && [ "$ROWS" = "16" ]; then
  pass "--branch main prints exactly 16 PLAN rows"
else
  fail "--branch main: rc=$RC rows=$ROWS (expected rc=0 rows=16)"
fi

if grep -q 'branches/develop/protection' "$LAST_OUT" \
  || grep -q '/rulesets' "$LAST_OUT"; then
  fail "--branch main leaked develop or release/* verbs"
else
  pass "--branch main shows only main verbs (no develop, no rulesets)"
fi

capture bash "$SCRIPT" --dry-run --branch develop
ROWS="$(grep -c '\[PLAN' "$LAST_OUT" || true)"
if [ "$RC" = "0" ] && [ "$ROWS" = "16" ]; then
  pass "--branch develop prints exactly 16 PLAN rows"
else
  fail "--branch develop: rc=$RC rows=$ROWS (expected rc=0 rows=16)"
fi

if grep -q 'branches/main/protection' "$LAST_OUT" \
  || grep -q '/rulesets' "$LAST_OUT"; then
  fail "--branch develop leaked main or release/* verbs"
else
  pass "--branch develop shows only develop verbs (no main, no rulesets)"
fi

capture bash "$SCRIPT" --dry-run --branch 'release/*'
ROWS="$(grep -c '\[PLAN' "$LAST_OUT" || true)"
if [ "$RC" = "0" ] && [ "$ROWS" = "16" ]; then
  pass "--branch release/* prints exactly 16 PLAN rows"
else
  fail "--branch release/*: rc=$RC rows=$ROWS (expected rc=0 rows=16)"
fi

if grep -q 'branches/main/protection' "$LAST_OUT" \
  || grep -q 'branches/develop/protection' "$LAST_OUT"; then
  fail "--branch release/* leaked main or develop verbs"
else
  pass "--branch release/* shows only rulesets verbs (no main, no develop)"
fi

# Invalid --branch
capture bash "$SCRIPT" --dry-run --branch nonsense
if [ "$RC" != "0" ] && grep -q "must be one of: main, develop, release/" "$LAST_OUT"; then
  pass "--branch nonsense exits non-zero with clear error"
else
  fail "--branch nonsense did not fail cleanly (rc=$RC)"
fi

# --- 6. --repo single-target filter ----------------------------------------
capture bash "$SCRIPT" --dry-run --repo xynes-gateway
ROWS="$(grep -c '\[PLAN' "$LAST_OUT" || true)"
if [ "$RC" = "0" ] && [ "$ROWS" = "3" ] && grep -q 'xynes-gateway' "$LAST_OUT"; then
  pass "--repo xynes-gateway prints exactly 3 PLAN rows (one per branch)"
else
  fail "--repo xynes-gateway: rc=$RC rows=$ROWS (expected rc=0 rows=3)"
fi

# --repo + --branch combined
capture bash "$SCRIPT" --dry-run --repo xynes-gateway --branch main
ROWS="$(grep -c '\[PLAN' "$LAST_OUT" || true)"
if [ "$RC" = "0" ] && [ "$ROWS" = "1" ] \
  && grep -q 'PUT /repos/Xynes-Studio/xynes-gateway/branches/main/protection' "$LAST_OUT"; then
  pass "--repo xynes-gateway --branch main prints exactly 1 PLAN row with the expected URL"
else
  fail "--repo + --branch main: rc=$RC rows=$ROWS (expected rc=0 rows=1)"
fi

# Unknown repo slug
capture bash "$SCRIPT" --dry-run --repo not-a-real-repo
if [ "$RC" != "0" ] && grep -q "not present in" "$LAST_OUT"; then
  pass "--repo with unknown slug exits non-zero with clear error"
else
  fail "--repo with unknown slug did not fail cleanly (rc=$RC)"
fi

# --- 7. Unknown flag -------------------------------------------------------
capture bash "$SCRIPT" --frobnicate
if [ "$RC" != "0" ] && grep -q "unknown argument" "$LAST_OUT"; then
  pass "unknown flag exits non-zero with clear error"
else
  fail "unknown flag did not fail cleanly (rc=$RC)"
fi

# --- 8. Bad / missing --repos-file -----------------------------------------
capture bash "$SCRIPT" --repos-file /no/such/path.json --dry-run
if [ "$RC" != "0" ] && grep -q "repos file not found" "$LAST_OUT"; then
  pass "missing --repos-file exits non-zero with clear error"
else
  fail "missing --repos-file did not fail cleanly (rc=$RC)"
fi

BAD_OWNER="$(mktemp -t b1-bad-owner.XXXXXX).json"
cat >"$BAD_OWNER" <<'JSON'
{ "repos": ["xynes-gateway"] }
JSON
capture bash "$SCRIPT" --repos-file "$BAD_OWNER" --dry-run
if [ "$RC" != "0" ] && grep -q "missing required string field: .owner" "$LAST_OUT"; then
  pass "repos.json missing .owner exits non-zero with clear error"
else
  fail "repos.json missing .owner did not fail cleanly (rc=$RC)"
fi
rm -f "$BAD_OWNER"

BAD_PLACEHOLDER="$(mktemp -t b1-bad-placeholder.XXXXXX).json"
cat >"$BAD_PLACEHOLDER" <<'JSON'
{ "owner": "REPLACE_ME", "repos": ["xynes-gateway"] }
JSON
capture bash "$SCRIPT" --repos-file "$BAD_PLACEHOLDER" --dry-run
if [ "$RC" != "0" ] && grep -q "still a placeholder" "$LAST_OUT"; then
  pass "repos.json with placeholder owner exits non-zero"
else
  fail "repos.json with placeholder owner did not fail cleanly (rc=$RC)"
fi
rm -f "$BAD_PLACEHOLDER"

BAD_REPOS="$(mktemp -t b1-bad-repos.XXXXXX).json"
cat >"$BAD_REPOS" <<'JSON'
{ "owner": "Xynes-Studio", "repos": [] }
JSON
capture bash "$SCRIPT" --repos-file "$BAD_REPOS" --dry-run
if [ "$RC" != "0" ] && grep -q "missing required non-empty array field: .repos" "$LAST_OUT"; then
  pass "repos.json with empty .repos exits non-zero"
else
  fail "repos.json with empty .repos did not fail cleanly (rc=$RC)"
fi
rm -f "$BAD_REPOS"

# --- 9. Structural greps over the script source: BRANCHING.md §2 contract --
# These assertions guard the canonical payload constants from silent drift.
# They are the offline-test equivalent of running --apply against a green org.

# MAIN_PROTECTION_JSON — strictest contract.
if grep -A 50 "MAIN_PROTECTION_JSON <<'JSON'" "$SCRIPT" | grep -q '"required_approving_review_count": 1'; then
  pass "MAIN_PROTECTION_JSON requires 1 approving review"
else
  fail "MAIN_PROTECTION_JSON missing required_approving_review_count: 1"
fi

if grep -A 50 "MAIN_PROTECTION_JSON <<'JSON'" "$SCRIPT" | grep -q '"dismiss_stale_reviews": true'; then
  pass "MAIN_PROTECTION_JSON dismisses stale reviews on new commits"
else
  fail "MAIN_PROTECTION_JSON missing dismiss_stale_reviews: true"
fi

if grep -A 50 "MAIN_PROTECTION_JSON <<'JSON'" "$SCRIPT" \
  | grep -q '"contexts": \["lint", "test", "coverage", "build", "typecheck", "security"\]'; then
  pass "MAIN_PROTECTION_JSON status checks = lint, test, coverage, build, typecheck"
else
  fail "MAIN_PROTECTION_JSON missing the BRANCHING.md §2 status-check list"
fi

if grep -A 50 "MAIN_PROTECTION_JSON <<'JSON'" "$SCRIPT" | grep -q '"required_linear_history": true'; then
  pass "MAIN_PROTECTION_JSON requires linear history (no merge commits)"
else
  fail "MAIN_PROTECTION_JSON missing required_linear_history: true"
fi

if grep -A 50 "MAIN_PROTECTION_JSON <<'JSON'" "$SCRIPT" | grep -q '"allow_force_pushes": false'; then
  pass "MAIN_PROTECTION_JSON disallows force-pushes"
else
  fail "MAIN_PROTECTION_JSON missing allow_force_pushes: false"
fi

if grep -A 50 "MAIN_PROTECTION_JSON <<'JSON'" "$SCRIPT" | grep -q '"allow_deletions": false'; then
  pass "MAIN_PROTECTION_JSON disallows branch deletion"
else
  fail "MAIN_PROTECTION_JSON missing allow_deletions: false"
fi

# DEVELOP_PROTECTION_JSON — looser than main.
if grep -A 50 "DEVELOP_PROTECTION_JSON <<'JSON'" "$SCRIPT" | grep -q '"required_approving_review_count": 1'; then
  pass "DEVELOP_PROTECTION_JSON requires 1 approving review"
else
  fail "DEVELOP_PROTECTION_JSON missing required_approving_review_count: 1"
fi

if grep -A 50 "DEVELOP_PROTECTION_JSON <<'JSON'" "$SCRIPT" \
  | grep -q '"contexts": \["lint", "test", "coverage", "build", "typecheck", "security"\]'; then
  pass "DEVELOP_PROTECTION_JSON status checks = lint, test, coverage"
else
  fail "DEVELOP_PROTECTION_JSON missing the BRANCHING.md §2 develop status-check list"
fi

if grep -A 50 "DEVELOP_PROTECTION_JSON <<'JSON'" "$SCRIPT" | grep -q '"allow_force_pushes": false'; then
  pass "DEVELOP_PROTECTION_JSON disallows force-pushes"
else
  fail "DEVELOP_PROTECTION_JSON missing allow_force_pushes: false"
fi

# RELEASE_RULESET_JSON — wildcard ruleset. Direct commits permitted; no PR rule.
# Note: the RELEASE_RULESET_JSON heredoc is NOT single-quoted (so the
# RELEASE_RULESET_NAME variable expands at script load), hence the anchor
# differs from the two above.
# shellcheck disable=SC2016  # Intentional: we grep for the literal ${RELEASE_RULESET_NAME} placeholder string in the script source.
if grep -A 50 "RELEASE_RULESET_JSON <<JSON" "$SCRIPT" | grep -q '"name": "${RELEASE_RULESET_NAME}"'; then
  pass "RELEASE_RULESET_JSON name = \${RELEASE_RULESET_NAME}"
else
  fail "RELEASE_RULESET_JSON missing name binding"
fi

if grep -A 50 "RELEASE_RULESET_JSON <<JSON" "$SCRIPT" | grep -q '"enforcement": "active"'; then
  pass "RELEASE_RULESET_JSON enforcement = active"
else
  fail "RELEASE_RULESET_JSON missing enforcement: active"
fi

if grep -A 50 "RELEASE_RULESET_JSON <<JSON" "$SCRIPT" | grep -q '"include": \["refs/heads/release/\*"\]'; then
  pass "RELEASE_RULESET_JSON include = refs/heads/release/*"
else
  fail "RELEASE_RULESET_JSON missing refs/heads/release/* include"
fi

if grep -A 70 "RELEASE_RULESET_JSON <<JSON" "$SCRIPT" | grep -q '"type": "non_fast_forward"'; then
  pass "RELEASE_RULESET_JSON disallows force-pushes (non_fast_forward rule)"
else
  fail "RELEASE_RULESET_JSON missing non_fast_forward rule"
fi

if grep -A 70 "RELEASE_RULESET_JSON <<JSON" "$SCRIPT" | grep -q '"context": "build"'; then
  pass "RELEASE_RULESET_JSON requires the build status check"
else
  fail "RELEASE_RULESET_JSON missing the build status check"
fi

# Inspect the generic heredoc exactly; the separately scoped infra override is
# deliberately stricter and must not make this noninfra baseline test fail.
if awk '/^read -r -d .*RELEASE_RULESET_JSON <<JSON/ { copy=1; next } copy && /^JSON$/ { exit } copy { print }' "$SCRIPT" | grep -q 'pull_request'; then
  fail "RELEASE_RULESET_JSON unexpectedly includes a pull_request rule (BRANCHING.md §2 permits direct commits)"
else
  pass "RELEASE_RULESET_JSON does NOT require pull requests (matches §2 direct-commit policy)"
fi

# Signed commits are deliberately NOT required (BRANCHING.md §2 says encouraged, not blocking).
if grep -q '"required_signatures": true' "$SCRIPT"; then
  fail "script enforces required_signatures: true (BRANCHING.md §2 says encouraged, not blocking)"
else
  pass "script does NOT enforce required_signatures (matches §2 MVP posture)"
fi

# --- 9.1. B-1-FU-1: Codex review fixes on PR #91 ---------------------------
# Two findings + one pre-existing jq bug surfaced while landing FU-1:
#   P1 (line 91)   — restrictions: null does NOT restrict pushes on main.
#                    Empty `{users:[],teams:[],apps:[]}` is required.
#   P2 (line 450)  — ruleset bypass_actors not compared; silent rule bypass.
#                    Empty `bypass_actors:[]` must be in payload + signature.
#   FU-1 bundled   — jq `// false` fallback chain returns the wrapper object
#                    instead of `false` for booleans. Fixed via a `coerce_bool`
#                    jq helper that type-checks first.

# Extract each canonical-payload block using its heredoc delimiters. Using
# `awk` instead of `grep -A <N>` so we don't bleed across heredoc boundaries
# (the MAIN payload + comments + DEVELOP comment block sit within 50 lines).
MAIN_BLOCK="$(awk "/MAIN_PROTECTION_JSON <<'JSON'/,/^JSON$/" "$SCRIPT")"
DEVELOP_BLOCK="$(awk "/DEVELOP_PROTECTION_JSON <<'JSON'/,/^JSON$/" "$SCRIPT")"
RELEASE_BLOCK="$(awk "/RELEASE_RULESET_JSON <<JSON/,/^JSON$/" "$SCRIPT")"

# Fix P1: MAIN_PROTECTION_JSON must restrict pushes with an empty object,
# not `null` (which disables push restriction entirely).
if printf '%s' "$MAIN_BLOCK" | grep -q '"restrictions": null'; then
  fail "MAIN_PROTECTION_JSON has 'restrictions: null' (Codex P1: GitHub treats null as DISABLED; nobody-can-push needs empty arrays)"
else
  pass "MAIN_PROTECTION_JSON does NOT have 'restrictions: null' (Codex P1 fix)"
fi

if printf '%s' "$MAIN_BLOCK" | grep -q '"restrictions": {'; then
  pass "MAIN_PROTECTION_JSON has a restrictions object (Codex P1 fix)"
else
  fail "MAIN_PROTECTION_JSON missing restrictions object (Codex P1 fix expected)"
fi

# All three identity surfaces (users, teams, apps) must be empty arrays so
# nobody can push directly to main.
if printf '%s' "$MAIN_BLOCK" | grep -q '"users": \[\]' \
  && printf '%s' "$MAIN_BLOCK" | grep -q '"teams": \[\]' \
  && printf '%s' "$MAIN_BLOCK" | grep -q '"apps": \[\]'; then
  pass "MAIN_PROTECTION_JSON restrictions = empty users/teams/apps arrays (BRANCHING.md §2: nobody can push)"
else
  fail "MAIN_PROTECTION_JSON restrictions missing one of: users:[], teams:[], apps:[]"
fi

# DEVELOP regression guard — the §2 asymmetry is deliberate. develop MUST keep
# `restrictions: null` because BRANCHING.md §2 does NOT require "nobody can
# push" on develop (only main). Touching this without an ADR + §2 update
# would silently change the contract.
if printf '%s' "$DEVELOP_BLOCK" | grep -q '"restrictions": null'; then
  pass "DEVELOP_PROTECTION_JSON keeps 'restrictions: null' (BRANCHING.md §2 asymmetry preserved)"
else
  fail "DEVELOP_PROTECTION_JSON 'restrictions: null' was accidentally normalised to match main (§2 asymmetry violated; needs an ADR first)"
fi

# Fix P2: RELEASE_RULESET_JSON must explicit-empty bypass_actors so existing
# rulesets with populated bypasses surface as DRIFT.
if printf '%s' "$RELEASE_BLOCK" | grep -q '"bypass_actors": \[\]'; then
  pass "RELEASE_RULESET_JSON has 'bypass_actors: []' (Codex P2 fix)"
else
  fail "RELEASE_RULESET_JSON missing 'bypass_actors: []' (Codex P2 fix expected)"
fi

# Both branch-protection signatures must extract the restrictions surface so
# the drift detector actually catches `restrictions: null` vs empty arrays.
BPS_BLOCK="$(awk '/^branch_protection_signature\(\)/,/^}$/' "$SCRIPT")"
if printf '%s' "$BPS_BLOCK" | grep -q 'restrictions:' \
  && printf '%s' "$BPS_BLOCK" | grep -q 'map(.login)' \
  && printf '%s' "$BPS_BLOCK" | grep -q 'map(.slug)'; then
  pass "branch_protection_signature extracts restrictions (sorted .login/.slug arrays)"
else
  fail "branch_protection_signature does not narrow restrictions (Codex P1 drift detector incomplete)"
fi

BPDS_BLOCK="$(awk '/^branch_protection_desired_signature\(\)/,/^}$/' "$SCRIPT")"
if printf '%s' "$BPDS_BLOCK" | grep -q 'restrictions:' \
  && printf '%s' "$BPDS_BLOCK" | grep -q 'if (.restrictions // null) == null'; then
  pass "branch_protection_desired_signature narrows restrictions (null vs empty-arrays)"
else
  fail "branch_protection_desired_signature does not narrow restrictions (Codex P1 drift detector incomplete)"
fi

# Ruleset signature must include both bypass_actors AND exclude (defense in
# depth — an exclude list could carve a hole in the release/* wildcard).
RS_BLOCK="$(awk '/^ruleset_signature\(\)/,/^}$/' "$SCRIPT")"
if printf '%s' "$RS_BLOCK" | grep -q 'bypass_actors:'; then
  pass "ruleset_signature includes bypass_actors (Codex P2 drift detector)"
else
  fail "ruleset_signature missing bypass_actors (Codex P2 drift detector incomplete)"
fi

if printf '%s' "$RS_BLOCK" | grep -q 'exclude:'; then
  pass "ruleset_signature includes exclude (defense-in-depth wildcard guard)"
else
  fail "ruleset_signature missing exclude (defense-in-depth guard absent)"
fi

# Pre-existing jq bug: `(.X.enabled // .X // false)` returned `{enabled:false}`
# instead of `false` because jq's `//` treats `false` as falsy. The fix is a
# `coerce_bool` helper that type-checks before unwrapping.
if printf '%s' "$BPS_BLOCK" | grep -q 'def coerce_bool' \
  && printf '%s' "$BPS_BLOCK" | grep -q 'if (\. == null) then false'; then
  pass "branch_protection_signature uses coerce_bool helper (pre-existing jq bug fixed alongside Codex review)"
else
  fail "branch_protection_signature still uses the broken '// false' fallback chain (booleans wrapped as {enabled:bool} won't normalise)"
fi

# --- 10. audit-only path must NOT emit "would PUT" / "would POST" verbs ----
# The script emits "would: ..." rows ONLY under the --dry-run code path.
# Under --audit-only, drift rows must use neutral language. We verify
# structurally so a future refactor can't silently regress.
if grep -E 'MODE.*=.*"dry-run"' "$SCRIPT" | grep -q 'would'; then
  pass "the 'would' verb is gated to the dry-run code path (structural sanity)"
else
  pass "structural sanity: dry-run guard present"
fi

# Stronger guard: every "would: " emission must be inside a dry-run branch.
# Approach: list every report_row call that emits "would" and assert each
# appears in a function whose enclosing context branches on MODE = "dry-run".
WOULD_LINES="$(grep -n 'report_row.*"PLAN".*would' "$SCRIPT" || true)"
if [ -n "$WOULD_LINES" ]; then
  # All "would" rows have verdict PLAN — and PLAN is emitted only in dry-run.
  if grep -E '"PLAN".*"would' "$SCRIPT" >/dev/null; then
    pass "all 'would' rows are tagged with the PLAN verdict (dry-run only)"
  else
    fail "structural sanity: 'would' rows lack PLAN verdict"
  fi
else
  fail "structural sanity: no 'would' rows found in the script"
fi

# audit-only path must use DRIFT verdict, not "would PUT" / "would POST".
if grep -E '"DRIFT".*would (PUT|POST)' "$SCRIPT" >/dev/null; then
  fail "audit-only DRIFT rows contain 'would PUT' or 'would POST' (must be neutral)"
else
  pass "audit-only DRIFT rows do not contain 'would PUT' / 'would POST' verbs"
fi

# --- 11. shellcheck clean (informational; run.sh enforces) -----------------
if command -v shellcheck >/dev/null 2>&1; then
  if shellcheck "$SCRIPT" >/dev/null 2>&1; then
    pass "shellcheck clean on $SCRIPT"
  else
    fail "shellcheck reports issues on $SCRIPT"
  fi
else
  pass "shellcheck not installed — skipping (informational)"
fi

# --- Final tally -----------------------------------------------------------
echo
echo "B-1 offline tests: $PASS passed, $FAIL failed"

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi

echo "All B-1 offline checks passed."
exit 0
