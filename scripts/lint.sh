#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Linting for infra scripts.
# Prefers shellcheck when available; falls back to bash -n syntax checks.

shopt -s nullglob
FILES=(./scripts/*.sh ./scripts/test/*.sh ./scripts/lib/*.sh)

if command -v shellcheck >/dev/null 2>&1; then
  shellcheck -x -S warning "${FILES[@]}"
  echo "PASS: shellcheck"
  exit 0
fi

echo "Note: shellcheck not installed; running bash -n (syntax only)." >&2

for f in "${FILES[@]}"; do
  [ -f "$f" ] || continue
  bash -n "$f"
done

echo "PASS: bash -n"
