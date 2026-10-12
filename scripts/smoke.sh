#!/usr/bin/env bash
set -euo pipefail

# ═══════════════════════════════════════════════════════════════════════════════
# SMOKE.SH - Master Smoke Test Runner
# ═══════════════════════════════════════════════════════════════════════════════
#
# Consolidates all smoke test scripts into a single entry point.
#
# Available test suites:
#   health    - Health/ready endpoints for all services
#   setup     - Seed workspace and directory-first CMS entry fixtures
#   all       - Comprehensive API tests (documents, CMS, authz, etc.)
#   security  - Security validation (rate limits, body limits, JWT, telemetry)
#   gateway-audit - Canonical gateway access log validation
#   auth      - Authentication flow tests
#   content   - Content/CMS specific tests
#   cms-entry - CMS directory-first entry action tests
#   db        - Database connectivity tests
#
# Usage:
#   ./scripts/smoke.sh [suite] [options]
#   ./scripts/smoke.sh --all [options]      # Run all suites
#   ./scripts/smoke.sh health               # Run only health checks
#   ./scripts/smoke.sh security --verbose   # Run security with verbose
#
# Options are passed through to individual scripts.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# Defaults
RUN_ALL=0
SUITES=()
PASSTHROUGH_ARGS=()

usage() {
  cat <<'EOF'
Usage: ./scripts/smoke.sh [suite...] [options]

Master smoke test runner - consolidates all test suites.

Suites:
  health      Health/ready endpoints (fast, no auth needed)
  setup       Seed workspace and directory-first CMS entry fixtures
  all         Comprehensive API tests (requires auth)
  security    Security validation (rate limits, JWT, telemetry)
  auth        Authentication flow tests
  content     Content/CMS specific tests
  cms-entry   CMS directory-first entry action tests
  db          Database connectivity tests
  gateway-audit  Canonical gateway logging + telemetry persistence checks

Meta options:
  --all       Run all suites in recommended order
  --quick     Run health + security only (CI-friendly)
  --full      Run setup + all + security (full E2E)

Common options (passed to scripts):
  --env-file <path>     Environment file (default: .env.dev)
  --token-file <path>   Token file (suite-specific: Supabase or internal token)
  --verbose             Show detailed output
  --timeout <seconds>   Request timeout (default: 10)
  -h, --help            Show this help

Examples:
  ./scripts/smoke.sh health
  ./scripts/smoke.sh --all --env-file .env.localhost --token-file .tmp/supabase_access_token
  ./scripts/smoke.sh security --verbose
  ./scripts/smoke.sh setup all security --token-file .tmp/supabase_access_token

EOF
}

# Parse arguments
while [ "${1:-}" != "" ]; do
  case "$1" in
    health|setup|all|security|auth|content|cms-entry|db|gateway-audit)
      SUITES+=("$1")
      shift
      ;;
    --all)
      RUN_ALL=1
      shift
      ;;
    --quick)
      SUITES=("health" "security")
      RUN_ALL=0
      shift
      ;;
    --full)
      SUITES=("setup" "all" "security")
      RUN_ALL=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      PASSTHROUGH_ARGS+=("$1")
      shift
      ;;
  esac
done

# If --all, set all suites in recommended order
if [ "$RUN_ALL" = "1" ]; then
  SUITES=("health" "setup" "all" "security" "gateway-audit" "auth" "content" "cms-entry" "db")
fi

# Default to help if no suites specified
if [ ${#SUITES[@]} -eq 0 ]; then
  usage
  exit 0
fi

# Track results
SUITE_PASS=""
SUITE_FAIL=""
SUITE_SKIP=""
TOTAL_PASS=0
TOTAL_FAIL=0
TOTAL_SKIP=0

run_suite() {
  local suite="$1"
  local script="${SCRIPT_DIR}/smoke-${suite}.sh"
  
  if [ ! -f "$script" ]; then
    echo -e "${YELLOW}SKIP${NC} smoke-${suite}.sh - script not found"
    SUITE_SKIP="$SUITE_SKIP $suite"
    ((TOTAL_SKIP++)) || true
    return 0
  fi
  
  if [ ! -x "$script" ]; then
    chmod +x "$script"
  fi
  
  echo -e "\n${CYAN}════════════════════════════════════════════════════════════════════${NC}"
  echo -e "${CYAN}  Running: smoke-${suite}.sh${NC}"
  echo -e "${CYAN}════════════════════════════════════════════════════════════════════${NC}\n"
  
  # Build args - some scripts don't support all options
  local args=()
  local skip_next=0
  for i in "${!PASSTHROUGH_ARGS[@]}"; do
    if [ "$skip_next" = "1" ]; then
      skip_next=0
      continue
    fi
    
    local arg="${PASSTHROUGH_ARGS[$i]}"
    local next_arg="${PASSTHROUGH_ARGS[$((i+1))]:-}"
    
    case "$arg" in
      --token-file)
        # Only pass --token-file to scripts that support it.
        if [ "$suite" = "all" ] || [ "$suite" = "security" ] || [ "$suite" = "auth" ] || { [ "$suite" = "cms-entry" ] && [ "$RUN_ALL" != "1" ]; }; then
          args+=("$arg" "$next_arg")
        fi
        skip_next=1
        ;;
      --timeout)
        # smoke-db.sh does not support --timeout
        if [ "$suite" != "db" ]; then
          args+=("$arg" "$next_arg")
        fi
        skip_next=1
        ;;
      --verbose)
        # Only pass --verbose to scripts that support it
        if [ "$suite" = "all" ] || [ "$suite" = "security" ] || [ "$suite" = "setup" ]; then
          args+=("$arg")
        fi
        ;;
      *)
        args+=("$arg")
        ;;
    esac
  done
  
  local output_file
  output_file="$(mktemp)"
  trap 'rm -f "$output_file"' RETURN

  set +e
  "$script" "${args[@]}" 2>&1 | tee "$output_file"
  local exit_code=${PIPESTATUS[0]}
  set -e

  # output_file cleanup handled by RETURN trap above
  
  if [ "$exit_code" -eq 0 ]; then
    local exported_user_id exported_workspace_id exported_workspace_slug
    exported_user_id="$(sed -n 's/^export XS_USER_ID="\([^"]*\)".*/\1/p; s/^export CURRENT_USER_ID="\([^"]*\)".*/\1/p; s/^export USER_ID="\([^"]*\)".*/\1/p' "$output_file" | tail -n 1)"
    exported_workspace_id="$(sed -n 's/^export WORKSPACE_ID="\([^"]*\)".*/\1/p; s/^export CURRENT_WORKSPACE_ID="\([^"]*\)".*/\1/p' "$output_file" | tail -n 1)"
    exported_workspace_slug="$(sed -n 's/^export WORKSPACE_SLUG="\([^"]*\)".*/\1/p' "$output_file" | tail -n 1)"

    if [ -n "$exported_user_id" ]; then
      export XS_USER_ID="$exported_user_id"
    fi
    if [ -n "$exported_workspace_id" ]; then
      export WORKSPACE_ID="$exported_workspace_id"
    fi
    if [ -n "$exported_workspace_slug" ]; then
      export WORKSPACE_SLUG="$exported_workspace_slug"
    fi
  fi

  if [ "$exit_code" -eq 0 ]; then
    SUITE_PASS="$SUITE_PASS $suite"
    ((TOTAL_PASS++)) || true
  else
    SUITE_FAIL="$SUITE_FAIL $suite"
    ((TOTAL_FAIL++)) || true
  fi
  
  return $exit_code
}

echo -e "${BLUE}═══════════════════════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}  SMOKE TEST MASTER RUNNER${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════════════════════════${NC}"
echo -e "Suites: ${SUITES[*]}"
echo -e "Options: ${PASSTHROUGH_ARGS[*]:-<none>}"

# Run each suite
FAILED_SUITES=()
for suite in "${SUITES[@]}"; do
  if ! run_suite "$suite"; then
    FAILED_SUITES+=("$suite")
  fi
done

# Summary
echo -e "\n${BLUE}═══════════════════════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}  MASTER SUMMARY${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════════════════════════${NC}"

for suite in "${SUITES[@]}"; do
  if echo "$SUITE_PASS" | grep -qw "$suite"; then
    echo -e "  ${GREEN}✓${NC} smoke-${suite}.sh"
  elif echo "$SUITE_FAIL" | grep -qw "$suite"; then
    echo -e "  ${RED}✗${NC} smoke-${suite}.sh"
  elif echo "$SUITE_SKIP" | grep -qw "$suite"; then
    echo -e "  ${YELLOW}○${NC} smoke-${suite}.sh (skipped)"
  else
    echo -e "  ${YELLOW}?${NC} smoke-${suite}.sh (unknown)"
  fi
done

echo -e "\n  Suites: ${GREEN}$TOTAL_PASS passed${NC} | ${RED}$TOTAL_FAIL failed${NC} | ${YELLOW}$TOTAL_SKIP skipped${NC}"

if [ "$TOTAL_FAIL" -gt 0 ]; then
  echo -e "\n${RED}SMOKE TESTS FAILED${NC}"
  echo "Failed suites: ${FAILED_SUITES[*]}"
  exit 1
fi

echo -e "\n${GREEN}ALL SMOKE TESTS PASSED${NC}"
exit 0
