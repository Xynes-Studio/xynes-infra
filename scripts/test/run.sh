#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

for t in ./scripts/test/*.test.sh; do
  echo "Running $t"
  bash "$t"
done

echo "All script tests passed."
