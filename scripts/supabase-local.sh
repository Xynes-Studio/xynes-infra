#!/usr/bin/env bash
set -euo pipefail

# supabase-local.sh
#
# Small helper for local Supabase dev (no tunnel).
#
# Usage:
#   ./scripts/supabase-local.sh start
#   ./scripts/supabase-local.sh stop
#   ./scripts/supabase-local.sh status
#   ./scripts/supabase-local.sh reset
#   ./scripts/supabase-local.sh reset --no-bootstrap

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

load_env_file() {
  local file="$1"
  [ -f "$file" ] || return 0

  while IFS= read -r raw || [ -n "$raw" ]; do
    # Trim leading/trailing whitespace.
    local line="${raw#"${raw%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"

    [ -z "$line" ] && continue
    case "$line" in
      \#*) continue ;;
    esac

    case "$line" in
      *=*)
        local key="${line%%=*}"
        local value="${line#*=}"
        key="${key%"${key##*[![:space:]]}"}"
        value="${value#"${value%%[![:space:]]*}"}"

        # Strip matching surrounding quotes.
        if [ "${value#\"}" != "$value" ] && [ "${value%\"}" != "$value" ]; then
          value="${value#\"}"
          value="${value%\"}"
        elif [ "${value#\'}" != "$value" ] && [ "${value%\'}" != "$value" ]; then
          value="${value#\'}"
          value="${value%\'}"
        fi

        export "$key=$value"
        ;;
    esac
  done < "$file"
}

# Load optional env overrides for Supabase local without eval/sourcing (safe for special chars).
# Prefer .env.local (untracked secrets) over .env (may contain placeholders).
load_env_file "${ROOT_DIR}/.env"
load_env_file "${ROOT_DIR}/.env.local"

cmd="${1:-}"

if [ -z "$cmd" ] || [ "$cmd" = "-h" ] || [ "$cmd" = "--help" ]; then
  cat <<'USAGE'
Usage: ./scripts/supabase-local.sh <command>

Commands:
  start   Start local Supabase (`supabase start`)
  stop    Stop local Supabase (`supabase stop`)
  status  Show local Supabase status (`supabase status`)
  reset   Reset local DB, run secure bootstrap, and reload gateway routes

Reset options:
  --no-bootstrap              only run `supabase db reset --yes`
  --bootstrap-env-file <path> env file for post-reset bootstrap (default: .env.localhost.local)
  --no-restart-gateway        skip gateway restart after bootstrap

Notes:
  The default reset path runs ./scripts/bootstrap-local-db.sh afterward because
  Supabase resets replay infra SQL migrations only. Service-owned DB state
  such as CMS Core Drizzle tables/seeds must be restored separately.
USAGE
  exit 0
fi

shift || true

case "$cmd" in
  start)
    cd "${ROOT_DIR}/supabase"
    supabase start "$@"
    ;;
  stop)
    cd "${ROOT_DIR}/supabase"
    supabase stop "$@"
    ;;
  status)
    cd "${ROOT_DIR}/supabase"
    supabase status "$@"
    ;;
  reset)
    BOOTSTRAP=1
    RESTART_GATEWAY=1
    BOOTSTRAP_ENV_FILE="${XYNES_BOOTSTRAP_ENV_FILE:-.env.localhost.local}"
    SUPABASE_ARGS=()

    while [ "${1:-}" != "" ]; do
      case "$1" in
        --no-bootstrap)
          BOOTSTRAP=0
          shift
          ;;
        --bootstrap-env-file)
          if [ -z "${2:-}" ]; then
            echo "Error: --bootstrap-env-file requires a path" >&2
            exit 2
          fi
          BOOTSTRAP_ENV_FILE="$2"
          shift 2
          ;;
        --no-restart-gateway)
          RESTART_GATEWAY=0
          shift
          ;;
        *)
          SUPABASE_ARGS+=("$1")
          shift
          ;;
      esac
    done

    # Default to non-interactive reset so this is safe to run from scripts/CI.
    (
      cd "${ROOT_DIR}/supabase"
      # `set -u` + bash 3.x (macOS system bash) treats `"${ARR[@]}"` as an
      # unbound-variable error when ARR is empty, even though ARR=() is
      # declared above. Use the conditional-expansion idiom so an empty
      # SUPABASE_ARGS array expands to nothing instead of crashing.
      supabase db reset --yes ${SUPABASE_ARGS[@]+"${SUPABASE_ARGS[@]}"}
    )

    if [ "$BOOTSTRAP" = "1" ]; then
      BOOTSTRAP_ARGS=(--env-file "$BOOTSTRAP_ENV_FILE")
      if [ "$RESTART_GATEWAY" = "1" ]; then
        BOOTSTRAP_ARGS+=(--restart-gateway)
      fi
      # The top-of-script `load_env_file ".env"` / `.env.local` may have
      # exported a hosted/staging DATABASE_URL into the environment (this
      # script is shared with `supabase start/stop/status` which legitimately
      # want those values for Supabase CLI plumbing). The bootstrap below
      # uses `load_env_file_no_override` (first writer wins), so a pre-set
      # DATABASE_URL would block the local override from
      # `--env-file=.env.localhost.local`. Unset DATABASE_URL (and the
      # closely related PG* vars psql honours) before invoking the
      # bootstrap so it can pick up the local-Supabase credentials
      # cleanly. This only affects the child env — the parent shell
      # state is untouched.
      env -u DATABASE_URL -u PGPASSWORD -u PGHOST -u PGPORT -u PGUSER -u PGDATABASE \
        bash "${ROOT_DIR}/scripts/bootstrap-local-db.sh" "${BOOTSTRAP_ARGS[@]}"
    fi
    ;;
  *)
    echo "Unknown command: $cmd" >&2
    exit 2
    ;;
esac
