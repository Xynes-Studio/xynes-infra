#!/usr/bin/env bash
set -euo pipefail

# Creates symlinks so all service repos use the env files from xynes-infra.
# - Backs up existing .env.dev/.env.localhost files (if present and not already symlinks)
# - Creates symlinks pointing at ../xynes-infra/.env.dev and ../xynes-infra/.env.localhost
#
# This script does NOT print env contents.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE_DIR="$(cd "${ROOT_DIR}/.." && pwd)"

TS="$(date +%Y%m%d-%H%M%S)"

usage() {
  cat <<'EOF'
Usage: ./scripts/link-env-files.sh [--dry-run]

Centralizes per-repo env files by symlinking:
  <repo>/.env.dev      -> ../xynes-infra/.env.dev
  <repo>/.env.localhost -> ../xynes-infra/.env.localhost

It will back up any existing regular files as:
  .env.dev.bak.<timestamp>
  .env.localhost.bak.<timestamp>

Notes:
- Safe to re-run.
- On some systems (notably Windows), creating symlinks may require elevated permissions.
EOF
}

DRY_RUN=0
if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi
if [ "${1:-}" = "--dry-run" ]; then
  DRY_RUN=1
fi

require_file() {
  local f="$1"
  if [ ! -f "$f" ]; then
    echo "Error: required file missing: $f" >&2
    exit 1
  fi
}

run() {
  if [ "$DRY_RUN" = "1" ]; then
    echo "DRY_RUN: $*"
  else
    "$@"
  fi
}

link_for_repo() {
  local repo="$1"
  local repo_dir="${WORKSPACE_DIR}/${repo}"

  if [ ! -d "$repo_dir" ]; then
    echo "Skip (missing): $repo_dir"
    return 0
  fi

  # Ensure infra env files exist
  require_file "${ROOT_DIR}/.env.dev"
  require_file "${ROOT_DIR}/.env.localhost"

  local target_dev="../xynes-infra/.env.dev"
  local target_localhost="../xynes-infra/.env.localhost"

  for name in .env.dev .env.localhost; do
    local path="${repo_dir}/${name}"

    # If already the correct symlink, nothing to do.
    if [ -L "$path" ]; then
      local dest
      dest="$(readlink "$path" || true)"
      if [ "$name" = ".env.dev" ] && [ "$dest" = "$target_dev" ]; then
        continue
      fi
      if [ "$name" = ".env.localhost" ] && [ "$dest" = "$target_localhost" ]; then
        continue
      fi
      # Wrong symlink: replace (after backup removal)
      run rm -f "$path"
    fi

    # If a regular file exists, back it up.
    if [ -f "$path" ]; then
      run mv "$path" "${path}.bak.${TS}"
    fi

    # Create symlink
    if [ "$name" = ".env.dev" ]; then
      run ln -s "$target_dev" "$path"
    else
      run ln -s "$target_localhost" "$path"
    fi
  done

  echo "Linked env files for: $repo"
}

# Repos that commonly run with bun and expect .env.* beside package.json
REPOS=(
  xynes-gateway
  xynes-accounts-service
  xynes-authz-service
  xynes-doc-service
  xynes-cms-core
  xynes-telemetry-service
  xynes-platform-config
)

for repo in "${REPOS[@]}"; do
  link_for_repo "$repo"
done
