#!/usr/bin/env bash
set -euo pipefail
if [[ "${RUN_CMS_DELIVERY_CONTRACT_PARITY:-false}" != true ]]; then
  echo 'SKIP: A5 parity is not provisioned (set RUN_CMS_DELIVERY_CONTRACT_PARITY=true with pinned inputs)'
  exit 0
fi
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export XYNES_INFRA_REPO="$repo_root"
python3 -B "$repo_root/scripts/test/fixtures/cms-content-integrations/parity.py"
