#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
python3 -B "$repo_root/scripts/test/fixtures/cms-content-integrations/pinned-inputs.test.py"
