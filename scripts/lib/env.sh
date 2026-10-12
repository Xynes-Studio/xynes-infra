#!/usr/bin/env bash
set -euo pipefail

strip_quotes() {
  local s="${1:-}"
  if [[ "$s" == \"*\" && "$s" == *\" ]]; then
    echo "${s:1:${#s}-2}"
    return 0
  fi
  if [[ "$s" == \'*\' && "$s" == *\' ]]; then
    echo "${s:1:${#s}-2}"
    return 0
  fi
  echo "$s"
}

# Loads KEY=VALUE pairs from an env file.
# - Ignores comments/blank lines
# - Supports optional leading 'export '
# - Strips surrounding quotes
# - Does NOT override already-set environment variables
load_env_file_no_override() {
  local file="$1"
  [ -f "$file" ] || return 0

  while IFS= read -r line || [ -n "${line:-}" ]; do
    line="${line%$'\r'}"
    [[ -z "$line" ]] && continue
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^[[:space:]]*export[[:space:]]+ ]] && line="${line#export }"
    [[ "$line" =~ ^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*= ]] || continue

    local key="${line%%=*}"
    local value="${line#*=}"
    key="$(echo "$key" | xargs)"
    value="$(echo "$value" | xargs)"
    value="$(strip_quotes "$value")"

    if [ -z "${!key:-}" ]; then
      export "${key}=${value}"
    fi
  done <"$file"
}

# For host-side tooling (psql/nc), rewrite container-only hostnames to loopback.
normalize_database_url_for_host() {
  local url="${1:-}"
  if [ -z "$url" ]; then
    echo ""
    return 0
  fi

  echo "$url" \
    | sed -E 's#@host\.docker\.internal:#@127.0.0.1:#' \
    | sed -E 's#@db\.local:#@127.0.0.1:#'
}
