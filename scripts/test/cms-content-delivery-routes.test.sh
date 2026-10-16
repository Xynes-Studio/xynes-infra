#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export XYNES_INFRA_REPO="$repo_root"
contracts="${XYNES_CONTRACTS_REPO:-$repo_root/../xynes-platform-contracts}"
platform="${XYNES_PLATFORM_CONFIG_REPO:-$repo_root/../xynes-platform-config}"
if [[ -n "${XYNES_CONTRACTS_REPO:-}" && ! -f "$contracts/contracts/cms-delivery.v1.json" ]] ||
   [[ -n "${XYNES_PLATFORM_CONFIG_REPO:-}" && ! -f "$platform/src/seeds/routes.ts" ]]; then
  echo "ERROR: Missing explicit CMS delivery parity prerequisite" >&2
  exit 1
fi
if [[ ! -f "$contracts/contracts/cms-delivery.v1.json" || ! -f "$platform/src/seeds/routes.ts" ]]; then
  echo "SKIP: CMS delivery route parity requires platform-contracts and platform-config checkouts"
  exit 0
fi
export XYNES_CONTRACTS_REPO="$contracts"
export XYNES_PLATFORM_CONFIG_REPO="$platform"
python3 "$repo_root/scripts/test/fixtures/cms-delivery/routes-contract.py"
