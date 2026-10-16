#!/usr/bin/env bash
# configure-branch-protection.sh — Story B-1 (MVP release plan, Phase 1).
#
# Applies the canonical branch-protection contract from
# xynes/xynes-infra/infra/release/BRANCHING.md §2 to every repo in
# scripts/setup/repos.json. Three branches are handled:
#
#   - main            → legacy PUT /repos/{owner}/{repo}/branches/main/protection
#   - develop         → legacy PUT /repos/{owner}/{repo}/branches/develop/protection
#   - release/*       → Rulesets API POST/PUT /repos/{owner}/{repo}/rulesets
#                       (named "release-protection"; pattern refs/heads/release/*)
#
# The legacy /branches/{branch}/protection endpoint does not accept wildcard
# branch names, so release/* uses the modern Rulesets API. The named ruleset
# also auto-covers future release/* cuts without re-running the script.
#
# Deliberately out of scope (do NOT add here):
#   - feature/* and hotfix/* protection. BRANCHING.md §2 says "no protection".
#   - Tag protection.
#   - Required signed commits. BRANCHING.md §2 says "encouraged, not blocking"
#     for MVP. A [NOTE ] line in the summary documents the choice.
#   - CODEOWNERS files. Group C (C-1..C-15) owns those.
#   - Live `gh` execution against the live org. Story D-2 runs --audit-only
#     and --apply in production; B-1 ships only the offline contract.
#
# Hard guarantees:
#   - Idempotent. --apply is gated by a jq-based structural diff so re-running
#     against a green repo is a no-op (no GitHub mutation event fires).
#   - Fails closed per-repo. One bad repo doesn't abort the run; the overall
#     exit code reflects the worst per-repo outcome.
#   - No force-pushes. No deletes. No tag operations. No branch renames.
#   - --dry-run requires no `gh` auth at all (no live calls of any kind).
#
# Usage:
#   bash scripts/setup/configure-branch-protection.sh --help
#   bash scripts/setup/configure-branch-protection.sh --dry-run
#   bash scripts/setup/configure-branch-protection.sh --audit-only
#   bash scripts/setup/configure-branch-protection.sh --apply
#   bash scripts/setup/configure-branch-protection.sh --apply --repo xynes-gateway
#   bash scripts/setup/configure-branch-protection.sh --dry-run --branch main
#   bash scripts/setup/configure-branch-protection.sh --print-policy --repo xynes-gateway --branch main

set -euo pipefail

# ------------------------------------------------------------------------------
# Defaults
# ------------------------------------------------------------------------------

SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEFAULT_REPOS_FILE="$REPO_ROOT/scripts/setup/repos.json"

REPOS_FILE=""
MODE="dry-run" # one of: dry-run | print-policy | audit-only | apply
TARGET_REPO=""
TARGET_BRANCH="all" # one of: all | main | develop | release/*

# Ruleset name used for the release/* wildcard protection.
RELEASE_RULESET_NAME="release-protection"

# ------------------------------------------------------------------------------
# Canonical protection payloads (BRANCHING.md §2)
# ------------------------------------------------------------------------------
#
# These payloads are kept as top-level constants (not built inline in functions)
# so the offline test script can grep them for the BRANCHING.md §2 invariants
# without invoking gh. Any structural change here MUST be mirrored in
# BRANCHING.md §2 and in scripts/test/branch-protection-script.test.sh.

# main — Prod source of truth. Strictest contract.
#  - PR required, 1 approval, dismiss stale on new commits.
#  - Status checks: lint, test, coverage, build, typecheck, security plus repo gates.
#  - Linear history (squash or rebase only — no merge commits).
#  - No force-pushes. No deletions. No direct pushes (enforced via an EMPTY
#    restrictions object — { users: [], teams: [], apps: [] } — NOT null. The
#    GitHub API treats `restrictions: null` as "no push restriction" and would
#    leave write/admin users free to push directly. Empty arrays mean "nobody",
#    matching BRANCHING.md §2's "Restrict who can push: nobody" rule. Surfaced
#    by Codex review on PR #91 (P1 finding); closed by B-1-FU-1 2026-06-05.
#  - required_signatures intentionally omitted — BRANCHING.md §2 marks signed
#    commits as "encouraged, not blocking" for MVP. Future ADR can flip.
read -r -d '' MAIN_PROTECTION_JSON <<'JSON' || true
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["lint", "test", "coverage", "build", "typecheck", "security"]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "required_approving_review_count": 1,
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false
  },
  "restrictions": {
    "users": [],
    "teams": [],
    "apps": []
  },
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "block_creations": false,
  "required_conversation_resolution": true
}
JSON

# develop — Dev source of truth. Looser than main (no linear-history requirement,
# the same security/quality gates).
#
# Intentional asymmetry vs main: BRANCHING.md §2 only requires "Restrict who can
# push: nobody" on main, NOT on develop. Develop accepts direct PR merges from
# any contributor with write access — only the PR + approvals + status checks
# gate it. So `restrictions: null` here is the correct §2 posture; do NOT
# normalise this to match main without an ADR + §2 update.
read -r -d '' DEVELOP_PROTECTION_JSON <<'JSON' || true
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["lint", "test", "coverage", "build", "typecheck", "security"]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "required_approving_review_count": 1,
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false
  },
  "restrictions": null,
  "required_linear_history": false,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "block_creations": false,
  "required_conversation_resolution": true
}
JSON

# release/* — Wildcard ruleset. Direct commits PERMITTED per BRANCHING.md §2
# (small bug fixes only), so no pull-request rule is included. Status checks
# AND non-fast-forward are enforced. Created (or updated) under the fixed
# name "release-protection" so re-runs are idempotent.
read -r -d '' RELEASE_RULESET_JSON <<JSON || true
{
  "name": "${RELEASE_RULESET_NAME}",
  "target": "branch",
  "enforcement": "active",
  "conditions": {
    "ref_name": {
      "include": ["refs/heads/release/*"],
      "exclude": []
    }
  },
  "rules": [
    {
      "type": "deletion"
    },
    {
      "type": "non_fast_forward"
    },
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": true,
        "required_status_checks": [
          { "context": "lint" },
          { "context": "test" },
          { "context": "coverage" },
          { "context": "build" },
          { "context": "typecheck" },
          { "context": "security" }
        ]
      }
    }
  ],
  "bypass_actors": []
}
JSON

# SEC-008: bind exact emitted check names to GitHub Actions (verified app 15368).
# Preserve the standard names, append repository-owned security/browser checks,
# and omit TypeScript only for the frontend operations-only repository.
BASE_MAIN_PROTECTION_JSON="$MAIN_PROTECTION_JSON"
BASE_DEVELOP_PROTECTION_JSON="$DEVELOP_PROTECTION_JSON"
BASE_RELEASE_RULESET_JSON="$RELEASE_RULESET_JSON"
INFRA_REVIEWS_ENFORCED=false

select_repository_checks() {
  local slug="$1" contexts canonical_repository
  INFRA_REVIEWS_ENFORCED=false
  contexts='["lint","test","coverage","build","typecheck","security"]'
  case "$slug" in
    xynes-frontend-infra) contexts='["lint","test","coverage","build","security"]' ;;
    xynes-storage-service) contexts='["lint","test","coverage","build","typecheck","security","release-image-security","immutable-source-security","integration-processors","archive-safety"]' ;;
    lumia-ds) contexts='["lint","test","coverage","build","typecheck","security","Visual Regression"]' ;;
    xynes-i18n) contexts='["lint","test","coverage","build","typecheck","security","CodeQL analyze"]' ;;
    xynes-cms-console-web) contexts='["lint","test","coverage","build","typecheck","security","Code and browser gates"]' ;;
  esac
  MAIN_PROTECTION_JSON="$(printf '%s' "$BASE_MAIN_PROTECTION_JSON" | jq --argjson contexts "$contexts" '.required_status_checks.contexts=$contexts | .required_status_checks.checks=[$contexts[] | {context:.,app_id:15368}]')"
  DEVELOP_PROTECTION_JSON="$(printf '%s' "$BASE_DEVELOP_PROTECTION_JSON" | jq --argjson contexts "$contexts" '.required_status_checks.contexts=$contexts | .required_status_checks.checks=[$contexts[] | {context:.,app_id:15368}]')"
  RELEASE_RULESET_JSON="$(printf '%s' "$BASE_RELEASE_RULESET_JSON" | jq --argjson contexts "$contexts" '(.rules[] | select(.type=="required_status_checks") | .parameters.required_status_checks)=[$contexts[] | {context:.,integration_id:15368}]')"
  # The public infrastructure controller has an explicitly stronger review
  # contract. Keep every other repository's emitted payload unchanged, including
  # another organisation that happens to use the same repository slug. Reviewer
  # identities belong in CODEOWNERS/access administration, never this policy.
  canonical_repository="$(printf '%s/%s' "$OWNER" "$slug" | LC_ALL=C tr '[:upper:]' '[:lower:]')"
  if [[ "$canonical_repository" == "xynes-studio/xynes-infra" ]]; then
    INFRA_REVIEWS_ENFORCED=true
    local reviews
    reviews='{"require_code_owner_reviews":true,"require_last_push_approval":true,"bypass_pull_request_allowances":{"users":[],"teams":[],"apps":[]}}'
    MAIN_PROTECTION_JSON="$(printf '%s' "$MAIN_PROTECTION_JSON" | jq --argjson reviews "$reviews" '.required_pull_request_reviews += $reviews')"
    DEVELOP_PROTECTION_JSON="$(printf '%s' "$DEVELOP_PROTECTION_JSON" | jq --argjson reviews "$reviews" '.required_pull_request_reviews += $reviews')"
    RELEASE_RULESET_JSON="$(printf '%s' "$RELEASE_RULESET_JSON" | jq '.rules += [{type:"pull_request",parameters:{required_approving_review_count:1,dismiss_stale_reviews_on_push:true,require_code_owner_review:true,require_last_push_approval:true,required_review_thread_resolution:true}}]')"
  fi
}

# ------------------------------------------------------------------------------
# Usage
# ------------------------------------------------------------------------------

print_usage() {
  cat <<EOF
$SCRIPT_NAME — apply BRANCHING.md §2 protection across all release repos.

USAGE:
  $SCRIPT_NAME [--repos-file <path>] [--dry-run | --audit-only | --apply]
                                [--branch <main|develop|release/*>] [--repo <slug>]
  $SCRIPT_NAME --help

OPTIONS:
  --repos-file <path>   JSON file. Default: scripts/setup/repos.json.
                        Required shape: { "owner": "<org>", "repos": [<slug>, ...] }.
  --print-policy        Offline JSON for --repo and --branch; no gh calls.
  --dry-run             (default) Audit + print the gh calls that WOULD run. No mutation.
                        Requires no gh auth at all.
  --audit-only          Audit only (live read). Reports drift; never prints
                        "would PUT" / "would POST" mutation verbs.
  --apply               Execute mutations via gh. Requires gh auth with admin
                        scope on each repo. PUTs are gated by a jq diff against
                        current state — applying to a green repo is a no-op.
  --branch <ref>        Limit to one branch: main | develop | release/*
                        Default: all (applies all three in sequence).
  --repo <slug>         Operate on one repo only. Slug must appear in --repos-file.
  --help                Print this usage and exit 0.

CONTRACT (from BRANCHING.md §2):
  - main:      PR required (1 approval, dismiss stale); status checks
               lint, test, coverage, build, typecheck (strict); linear history;
               no force-pushes; no deletions.
  - develop:   PR required (1 approval); status checks lint, test, coverage
               (strict); no force-pushes; no deletions.
  - release/*: Ruleset "release-protection"; status checks lint, test, coverage,
               build (strict); no force-pushes; no deletions. Direct commits
               permitted (small bug fixes only).
  - Required signed commits are intentionally NOT enforced — BRANCHING.md §2
    marks them "encouraged, not blocking" for MVP.
  - Xynes-Studio/xynes-infra additionally requires codeowner and last-push
    approval on main, develop and release/*, with no review/ruleset bypass.

EXIT CODES:
  0   All targeted (repo, branch) pairs are green (post-audit or post-apply).
  1   --repos-file missing, malformed; --branch invalid; --repo not in list.
  2   Audit detected drift in --audit-only mode.
  3   One or more (repo, branch) pairs failed during --apply.
  64  Internal error (e.g., gh missing, jq missing).

EXAMPLES:
  # Show what would run, no gh auth needed:
  $SCRIPT_NAME --dry-run

  # Live audit (requires gh auth with read scope on each repo):
  $SCRIPT_NAME --audit-only

  # Apply for real (requires gh auth with admin scope on each repo):
  $SCRIPT_NAME --apply

  # Apply only main protection across all repos:
  $SCRIPT_NAME --apply --branch main

  # Surgical fix for a single repo, single branch:
  $SCRIPT_NAME --apply --repo xynes-gateway --branch develop
EOF
}

# ------------------------------------------------------------------------------
# Argument parsing
# ------------------------------------------------------------------------------

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --help|-h)
        print_usage
        exit 0
        ;;
      --repos-file)
        if [ $# -lt 2 ] || [ -z "${2:-}" ]; then
          echo "ERROR: --repos-file requires a path argument." >&2
          exit 1
        fi
        REPOS_FILE="$2"
        shift 2
        ;;
      --print-policy)
        MODE="print-policy"
        shift
        ;;
      --dry-run)
        MODE="dry-run"
        shift
        ;;
      --audit-only)
        MODE="audit-only"
        shift
        ;;
      --apply)
        MODE="apply"
        shift
        ;;
      --branch)
        if [ $# -lt 2 ] || [ -z "${2:-}" ]; then
          echo "ERROR: --branch requires a value." >&2
          exit 1
        fi
        case "$2" in
          main|develop|release/*|all)
            TARGET_BRANCH="$2"
            ;;
          *)
            echo "ERROR: --branch must be one of: main, develop, release/*, all (got: $2)" >&2
            exit 1
            ;;
        esac
        shift 2
        ;;
      --repo)
        if [ $# -lt 2 ] || [ -z "${2:-}" ]; then
          echo "ERROR: --repo requires a slug argument." >&2
          exit 1
        fi
        TARGET_REPO="$2"
        shift 2
        ;;
      *)
        echo "ERROR: unknown argument: $1" >&2
        echo "Run '$SCRIPT_NAME --help' for usage." >&2
        exit 1
        ;;
    esac
  done

  if [ -z "$REPOS_FILE" ]; then
    REPOS_FILE="$DEFAULT_REPOS_FILE"
  fi
}

# ------------------------------------------------------------------------------
# Preflight
# ------------------------------------------------------------------------------

require_tool() {
  local tool="$1"
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "ERROR: required tool not found on PATH: $tool" >&2
    exit 64
  fi
}

require_repos_file() {
  if [ ! -f "$REPOS_FILE" ]; then
    echo "ERROR: repos file not found: $REPOS_FILE" >&2
    exit 1
  fi
  if ! jq -e '.owner and (.owner | type == "string") and (.owner | length > 0)' "$REPOS_FILE" >/dev/null 2>&1; then
    echo "ERROR: repos file missing required string field: .owner ($REPOS_FILE)" >&2
    exit 1
  fi
  if ! jq -e '.repos and (.repos | type == "array") and (.repos | length > 0)' "$REPOS_FILE" >/dev/null 2>&1; then
    echo "ERROR: repos file missing required non-empty array field: .repos ($REPOS_FILE)" >&2
    exit 1
  fi
  # Reject sentinel owners that would map to bogus URLs.
  local owner
  owner="$(jq -r '.owner' "$REPOS_FILE")"
  case "$owner" in
    REPLACE_ME|REPLACE_WITH_*|TODO|TBD)
      echo "ERROR: repos file .owner is still a placeholder: $owner ($REPOS_FILE)" >&2
      exit 1
      ;;
  esac
}

# ------------------------------------------------------------------------------
# gh wrappers — only invoked outside --dry-run mode
# ------------------------------------------------------------------------------

# Read the current legacy-protection JSON for a named branch. Echoes JSON or
# empty string on 404 / missing branch.
gh_get_branch_protection() {
  local slug="$1"
  local branch="$2"
  gh api "/repos/$OWNER/$slug/branches/$branch/protection" 2>/dev/null || true
}

# Apply the canonical protection payload via PUT.
gh_put_branch_protection() {
  local slug="$1"
  local branch="$2"
  local payload="$3"
  printf '%s' "$payload" | gh api -X PUT \
    -H "Accept: application/vnd.github+json" \
    --input - \
    "/repos/$OWNER/$slug/branches/$branch/protection" >/dev/null
}

# Check that a named branch actually exists on a repo. Returns 0 if it does.
gh_branch_exists() {
  local slug="$1"
  local branch="$2"
  gh api "/repos/$OWNER/$slug/branches/$branch" >/dev/null 2>&1
}

# Look up the id of the ruleset named "release-protection" on a repo.
# Echoes the numeric id, or empty if no such ruleset exists.
gh_find_release_ruleset_id() {
  local slug="$1"
  gh api --paginate "/repos/$OWNER/$slug/rulesets" --jq \
    ".[] | select(.name == \"$RELEASE_RULESET_NAME\") | .id" 2>/dev/null || true
}

# Read the full ruleset body by id (for drift detection).
gh_get_ruleset_body() {
  local slug="$1"
  local ruleset_id="$2"
  gh api "/repos/$OWNER/$slug/rulesets/$ruleset_id" 2>/dev/null || true
}

# Create the release-protection ruleset.
gh_create_ruleset() {
  local slug="$1"
  local payload="$2"
  printf '%s' "$payload" | gh api -X POST \
    -H "Accept: application/vnd.github+json" \
    --input - \
    "/repos/$OWNER/$slug/rulesets" >/dev/null
}

# Update an existing release-protection ruleset by id.
gh_update_ruleset() {
  local slug="$1"
  local ruleset_id="$2"
  local payload="$3"
  printf '%s' "$payload" | gh api -X PUT \
    -H "Accept: application/vnd.github+json" \
    --input - \
    "/repos/$OWNER/$slug/rulesets/$ruleset_id" >/dev/null
}

# ------------------------------------------------------------------------------
# Drift detection (structural; jq-based)
# ------------------------------------------------------------------------------

# Compare the BRANCHING.md §2 fields we actually care about between the desired
# payload and the current GitHub state. Echoes a short drift summary on stdout
# (or empty string if in sync). The comparison is deliberately narrow: GitHub
# decorates protection responses with many fields we do not enforce (urls,
# enabled wrappers, _links, etc.). We compare ONLY the contract surface.

# Narrow a legacy-protection response down to the contract surface.
#
# Note on boolean fallback semantics: jq's `//` operator treats `false` as
# falsy, so `(.X.enabled // .X // false)` silently falls through whenever the
# wrapped value is `{ "enabled": false }` and the caller gets the wrapper
# object back instead of the boolean. We therefore type-check first
# (`if type == "object" then .enabled else . end`) so both wire shapes — the
# wrapped `{ "enabled": true|false }` form (GET /branches/<b>/protection) and
# the bare boolean form (some envelope edges) — normalise to the same boolean.
# Pre-existing bug surfaced while landing B-1-FU-1 (Codex review on PR #91);
# closed alongside the Codex P1 fix on 2026-06-05.
branch_protection_signature() {
  # Opt in only when comparing the controller's stronger declared contract.
  # Undeclared last-push/bypass settings elsewhere retain their old comparison.
  jq -c --argjson infra_reviews "${1:-false}" '
    def coerce_bool: if (. == null) then false elif (type == "object") then (.enabled // false) else . end;
    {
    required_status_checks: {
      strict: (.required_status_checks.strict // false),
      contexts: ((.required_status_checks.contexts // []) | sort),
      checks: ([.required_status_checks.checks[]? | {context,app_id:(.app_id // null)}] | sort_by(.context))
    },
    enforce_admins: (.enforce_admins | coerce_bool),
    required_pull_request_reviews: ({
      required_approving_review_count: (.required_pull_request_reviews.required_approving_review_count // 0),
      dismiss_stale_reviews: (.required_pull_request_reviews.dismiss_stale_reviews // false),
      require_code_owner_reviews: (.required_pull_request_reviews.require_code_owner_reviews // false)
    } + if $infra_reviews then {
      require_last_push_approval: (.required_pull_request_reviews.require_last_push_approval // false),
      bypass_pull_request_allowances: {
        users: ((.required_pull_request_reviews.bypass_pull_request_allowances.users // []) | map(.login) | sort),
        teams: ((.required_pull_request_reviews.bypass_pull_request_allowances.teams // []) | map(.slug) | sort),
        apps: ((.required_pull_request_reviews.bypass_pull_request_allowances.apps // []) | map(.slug) | sort)
      }
    } else {} end),
    restrictions: (
      if (.restrictions // null) == null
      then null
      else {
        users: ((.restrictions.users // []) | map(.login) | sort),
        teams: ((.restrictions.teams // []) | map(.slug) | sort),
        apps: ((.restrictions.apps // []) | map(.slug) | sort)
      }
      end
    ),
    required_linear_history: (.required_linear_history | coerce_bool),
    allow_force_pushes: (.allow_force_pushes | coerce_bool),
    allow_deletions: (.allow_deletions | coerce_bool),
    required_conversation_resolution: (.required_conversation_resolution | coerce_bool)
  }'
}

# Narrow the desired (canonical) payload to the same surface.
branch_protection_desired_signature() {
  jq -c --argjson infra_reviews "${1:-false}" '{
    required_status_checks: {
      strict: .required_status_checks.strict,
      contexts: (.required_status_checks.contexts | sort),
      checks: ([.required_status_checks.checks[]? | {context,app_id:(.app_id // null)}] | sort_by(.context))
    },
    enforce_admins: (.enforce_admins // false),
    required_pull_request_reviews: ({
      required_approving_review_count: .required_pull_request_reviews.required_approving_review_count,
      dismiss_stale_reviews: .required_pull_request_reviews.dismiss_stale_reviews,
      require_code_owner_reviews: .required_pull_request_reviews.require_code_owner_reviews
    } + if $infra_reviews then {
      require_last_push_approval: .required_pull_request_reviews.require_last_push_approval,
      bypass_pull_request_allowances: {
        users: ((.required_pull_request_reviews.bypass_pull_request_allowances.users // []) | sort),
        teams: ((.required_pull_request_reviews.bypass_pull_request_allowances.teams // []) | sort),
        apps: ((.required_pull_request_reviews.bypass_pull_request_allowances.apps // []) | sort)
      }
    } else {} end),
    restrictions: (
      if (.restrictions // null) == null
      then null
      else {
        users: ((.restrictions.users // []) | sort),
        teams: ((.restrictions.teams // []) | sort),
        apps: ((.restrictions.apps // []) | sort)
      }
      end
    ),
    required_linear_history: (.required_linear_history // false),
    allow_force_pushes: (.allow_force_pushes // false),
    allow_deletions: (.allow_deletions // false),
    required_conversation_resolution: (.required_conversation_resolution // false)
  }'
}

# Narrow a ruleset response down to the contract surface.
#
# Includes `bypass_actors` so existing rulesets with non-empty bypasses (which
# would silently defeat the no-force-push / no-delete invariants on release/*)
# surface as DRIFT until `--apply` PUTs the empty list back. Surfaced by Codex
# review on PR #91 (P2 finding); closed by B-1-FU-1 2026-06-05.
#
# Also includes `exclude` (defense in depth; BRANCHING.md §2 has no excluded
# refs from release/* protection — any non-empty exclude list would silently
# carve a hole in the wildcard).
ruleset_signature() {
  jq -c '{
    name: .name,
    target: .target,
    enforcement: .enforcement,
    include: ((.conditions.ref_name.include // []) | sort),
    exclude: ((.conditions.ref_name.exclude // []) | sort),
    bypass_actors: ((.bypass_actors // []) | sort_by((.actor_id // 0), (.actor_type // ""), (.bypass_mode // ""))),
    rules: ([.rules[] | {
      type: .type,
      contexts: (
        if .type == "required_status_checks"
        then ([.parameters.required_status_checks[]?.context] | sort)
        else null
        end
      ),
      bindings: (
        if .type == "required_status_checks"
        then ([.parameters.required_status_checks[]? | {context,integration_id:(.integration_id // null)}] | sort_by(.context))
        else null
        end
      ),
      strict: (
        if .type == "required_status_checks"
        then (.parameters.strict_required_status_checks_policy // false)
        else null
        end
      ),
      reviews: (
        if .type == "pull_request" then {
          required_approving_review_count: (.parameters.required_approving_review_count // 0),
          dismiss_stale_reviews_on_push: (.parameters.dismiss_stale_reviews_on_push // false),
          require_code_owner_review: (.parameters.require_code_owner_review // false),
          require_last_push_approval: (.parameters.require_last_push_approval // false),
          required_review_thread_resolution: (.parameters.required_review_thread_resolution // false)
        } else null end
      )
    }] | sort_by(.type))
  }'
}

# ------------------------------------------------------------------------------
# Counters + reporting
# ------------------------------------------------------------------------------

TOTAL=0
OK=0
DRIFT=0
APPLIED=0
FAILED=0
SKIPPED=0
PLANNED=0

# Echoes a single row of the per-repo report. Format is stable so the test
# script can grep for documented verbs without ambiguity.
report_row() {
  local slug="$1"
  local verdict="$2"
  local detail="$3"
  printf '  [%-6s] %-32s %s\n' "$verdict" "$slug" "$detail"
}

# ------------------------------------------------------------------------------
# Per-branch workers
# ------------------------------------------------------------------------------

# Resolve which branches the current invocation targets.
selected_branches() {
  if [ "$TARGET_BRANCH" = "all" ]; then
    printf 'main\ndevelop\nrelease/*\n'
  else
    printf '%s\n' "$TARGET_BRANCH"
  fi
}

process_main() {
  local slug="$1"

  TOTAL=$((TOTAL + 1))

  if [ "$MODE" = "dry-run" ]; then
    report_row "$slug" "PLAN" "would: PUT /repos/$OWNER/$slug/branches/main/protection"
    PLANNED=$((PLANNED + 1))
    return 0
  fi

  if ! gh_branch_exists "$slug" "main"; then
    report_row "$slug" "SKIP" "main branch missing on repo — run scripts/setup/bootstrap-gitflow-branches.sh --apply first"
    SKIPPED=$((SKIPPED + 1))
    return 0
  fi

  local current desired_sig current_sig
  current="$(gh_get_branch_protection "$slug" "main")"
  desired_sig="$(printf '%s' "$MAIN_PROTECTION_JSON" | branch_protection_desired_signature "$INFRA_REVIEWS_ENFORCED")"
  if [ -n "$current" ]; then
    current_sig="$(printf '%s' "$current" | branch_protection_signature "$INFRA_REVIEWS_ENFORCED")"
  else
    current_sig="null"
  fi

  if [ "$current_sig" = "$desired_sig" ]; then
    report_row "$slug" "OK" "main: protection matches BRANCHING.md §2"
    OK=$((OK + 1))
    return 0
  fi

  if [ "$MODE" = "audit-only" ]; then
    report_row "$slug" "DRIFT" "main: protection diverges from BRANCHING.md §2 (run --apply to fix)"
    DRIFT=$((DRIFT + 1))
    return 0
  fi

  # --apply path
  if gh_put_branch_protection "$slug" "main" "$MAIN_PROTECTION_JSON"; then
    report_row "$slug" "APPLY" "main: PUT /branches/main/protection (BRANCHING.md §2)"
    APPLIED=$((APPLIED + 1))
  else
    report_row "$slug" "ERROR" "main: PUT failed — check gh scope (admin) and repo access"
    FAILED=$((FAILED + 1))
  fi
}

process_develop() {
  local slug="$1"

  TOTAL=$((TOTAL + 1))

  if [ "$MODE" = "dry-run" ]; then
    report_row "$slug" "PLAN" "would: PUT /repos/$OWNER/$slug/branches/develop/protection"
    PLANNED=$((PLANNED + 1))
    return 0
  fi

  if ! gh_branch_exists "$slug" "develop"; then
    report_row "$slug" "SKIP" "develop branch missing on repo — run scripts/setup/bootstrap-gitflow-branches.sh --apply first"
    SKIPPED=$((SKIPPED + 1))
    return 0
  fi

  local current desired_sig current_sig
  current="$(gh_get_branch_protection "$slug" "develop")"
  desired_sig="$(printf '%s' "$DEVELOP_PROTECTION_JSON" | branch_protection_desired_signature "$INFRA_REVIEWS_ENFORCED")"
  if [ -n "$current" ]; then
    current_sig="$(printf '%s' "$current" | branch_protection_signature "$INFRA_REVIEWS_ENFORCED")"
  else
    current_sig="null"
  fi

  if [ "$current_sig" = "$desired_sig" ]; then
    report_row "$slug" "OK" "develop: protection matches BRANCHING.md §2"
    OK=$((OK + 1))
    return 0
  fi

  if [ "$MODE" = "audit-only" ]; then
    report_row "$slug" "DRIFT" "develop: protection diverges from BRANCHING.md §2 (run --apply to fix)"
    DRIFT=$((DRIFT + 1))
    return 0
  fi

  # --apply path
  if gh_put_branch_protection "$slug" "develop" "$DEVELOP_PROTECTION_JSON"; then
    report_row "$slug" "APPLY" "develop: PUT /branches/develop/protection (BRANCHING.md §2)"
    APPLIED=$((APPLIED + 1))
  else
    report_row "$slug" "ERROR" "develop: PUT failed — check gh scope (admin) and repo access"
    FAILED=$((FAILED + 1))
  fi
}

process_release_wildcard() {
  local slug="$1"

  TOTAL=$((TOTAL + 1))

  if [ "$MODE" = "dry-run" ]; then
    report_row "$slug" "PLAN" "would: POST /repos/$OWNER/$slug/rulesets (name=$RELEASE_RULESET_NAME, pattern=refs/heads/release/*)"
    PLANNED=$((PLANNED + 1))
    return 0
  fi

  local existing_id existing_body current_sig desired_sig
  existing_id="$(gh_find_release_ruleset_id "$slug")"
  desired_sig="$(printf '%s' "$RELEASE_RULESET_JSON" | ruleset_signature)"

  if [ -n "$existing_id" ]; then
    existing_body="$(gh_get_ruleset_body "$slug" "$existing_id")"
    if [ -n "$existing_body" ]; then
      current_sig="$(printf '%s' "$existing_body" | ruleset_signature)"
    else
      current_sig="null"
    fi

    if [ "$current_sig" = "$desired_sig" ]; then
      report_row "$slug" "OK" "release/*: ruleset '$RELEASE_RULESET_NAME' matches BRANCHING.md §2 (id=$existing_id)"
      OK=$((OK + 1))
      return 0
    fi

    if [ "$MODE" = "audit-only" ]; then
      report_row "$slug" "DRIFT" "release/*: ruleset '$RELEASE_RULESET_NAME' (id=$existing_id) diverges from BRANCHING.md §2"
      DRIFT=$((DRIFT + 1))
      return 0
    fi

    # --apply path: update in place.
    if gh_update_ruleset "$slug" "$existing_id" "$RELEASE_RULESET_JSON"; then
      report_row "$slug" "APPLY" "release/*: PUT /rulesets/$existing_id ($RELEASE_RULESET_NAME)"
      APPLIED=$((APPLIED + 1))
    else
      report_row "$slug" "ERROR" "release/*: ruleset PUT failed (id=$existing_id)"
      FAILED=$((FAILED + 1))
    fi
    return 0
  fi

  # No existing ruleset.
  if [ "$MODE" = "audit-only" ]; then
    report_row "$slug" "DRIFT" "release/*: ruleset '$RELEASE_RULESET_NAME' missing (run --apply to create)"
    DRIFT=$((DRIFT + 1))
    return 0
  fi

  # --apply path: create.
  if gh_create_ruleset "$slug" "$RELEASE_RULESET_JSON"; then
    report_row "$slug" "APPLY" "release/*: POST /rulesets ($RELEASE_RULESET_NAME)"
    APPLIED=$((APPLIED + 1))
  else
    report_row "$slug" "ERROR" "release/*: ruleset POST failed"
    FAILED=$((FAILED + 1))
  fi
}

# Dispatch one (repo, branch) pair to the right worker.
process_pair() {
  local slug="$1"
  local branch="$2"
  case "$branch" in
    main)        process_main "$slug" ;;
    develop)     process_develop "$slug" ;;
    release/*)   process_release_wildcard "$slug" ;;
    *)
      report_row "$slug" "ERROR" "unsupported branch: $branch"
      FAILED=$((FAILED + 1))
      ;;
  esac
}

# ------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------

main() {
  parse_args "$@"
  require_tool jq
  if [ "$MODE" = "audit-only" ] || [ "$MODE" = "apply" ]; then
    require_tool gh
  fi
  require_repos_file

  OWNER="$(jq -r '.owner' "$REPOS_FILE")"

  # Build the repo list. If --repo was passed, restrict to that slug AFTER
  # confirming it's actually in the input file.
  local all_repos
  all_repos="$(jq -r '.repos[]' "$REPOS_FILE")"

  if [ -n "$TARGET_REPO" ]; then
    if ! printf '%s\n' "$all_repos" | grep -qx "$TARGET_REPO"; then
      echo "ERROR: --repo $TARGET_REPO not present in $REPOS_FILE .repos[]" >&2
      exit 1
    fi
    all_repos="$TARGET_REPO"
  fi

  if [ "$MODE" = "print-policy" ]; then
    if [ -z "$TARGET_REPO" ] || [ "$TARGET_BRANCH" = "all" ]; then
      echo "ERROR: --print-policy requires --repo and --branch." >&2
      exit 1
    fi
    select_repository_checks "$TARGET_REPO"
    case "$TARGET_BRANCH" in
      main) printf '%s\n' "$MAIN_PROTECTION_JSON" ;;
      develop) printf '%s\n' "$DEVELOP_PROTECTION_JSON" ;;
      release/*) printf '%s\n' "$RELEASE_RULESET_JSON" ;;
    esac
    exit 0
  fi

  local repo_count branch_list branch_count
  repo_count="$(printf '%s\n' "$all_repos" | wc -l | tr -d ' ')"
  branch_list="$(selected_branches)"
  branch_count="$(printf '%s\n' "$branch_list" | wc -l | tr -d ' ')"

  echo "configure-branch-protection: mode=$MODE owner=$OWNER repos=$repo_count branches=$branch_count"
  echo

  while IFS= read -r slug; do
    [ -z "$slug" ] && continue
    select_repository_checks "$slug"
    while IFS= read -r branch; do
      [ -z "$branch" ] && continue
      process_pair "$slug" "$branch"
    done <<<"$branch_list"
  done <<<"$all_repos"

  echo
  printf 'Summary: total=%d ok=%d drift=%d applied=%d failed=%d skipped=%d planned=%d\n' \
    "$TOTAL" "$OK" "$DRIFT" "$APPLIED" "$FAILED" "$SKIPPED" "$PLANNED"
  echo "[NOTE  ] required_signatures NOT enforced — BRANCHING.md §2 marks signed commits 'encouraged, not blocking' for MVP."

  if [ "$FAILED" -gt 0 ]; then
    exit 3
  fi
  if [ "$MODE" = "audit-only" ] && [ "$DRIFT" -gt 0 ]; then
    exit 2
  fi
  exit 0
}

main "$@"
