#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
python3 -B "$repo_root/scripts/test/fixtures/cms-content-integrations/smoke-safety.test.py"
bun test "$repo_root/scripts/test/fixtures/cms-content-integrations/copied-request-unit.test.ts"
