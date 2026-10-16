#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
output="$(RUN_FEATURE_FLAGS_LIVE_TESTS=false bash "$root/scripts/test/feature-flags.test.sh")"
[[ "$output" == *'SKIP:'* ]] || { echo 'Expected explicit unprovisioned live skip' >&2; exit 1; }
set +e
output="$(RUN_FEATURE_FLAGS_LIVE_TESTS=true XYNES_FEATURE_FLAGS_GATEWAY_URL='' XYNES_FEATURE_FLAGS_JWT='' bash "$root/scripts/test/feature-flags.test.sh" 2>&1)"
code=$?
set -e
[[ "$code" != 0 && "$output" == *'Explicit gateway URL and JWT are required'* ]] || { echo 'Enabled live cohort silently accepted missing inputs' >&2; exit 1; }
set +e
output="$(RUN_FEATURE_FLAGS_LIVE_TESTS=true XYNES_FEATURE_FLAGS_GATEWAY_URL='file:///inert-do-not-read' XYNES_FEATURE_FLAGS_JWT='inert-fixture-only' bash "$root/scripts/test/feature-flags.test.sh" 2>&1)"
code=$?
set -e
[[ "$code" != 0 && "$output" == *'Expected an http/https gateway base without credentials or query'* ]] || { echo 'Unsafe live gateway URL was not rejected at the boundary' >&2; exit 1; }
fixture="$(mktemp -d "${TMPDIR:-/tmp}/feature-flag-guard.XXXXXX")"
trap 'rm -rf "$fixture"' EXIT
printf '#!/bin/sh\nexit 7\n' > "$fixture/curl"
chmod 700 "$fixture/curl"
set +e
PATH="$fixture:$PATH" RUN_FEATURE_FLAGS_LIVE_TESTS=true XYNES_FEATURE_FLAGS_GATEWAY_URL='http://127.0.0.1:1' XYNES_FEATURE_FLAGS_JWT='inert-fixture-only' bash "$root/scripts/test/feature-flags.test.sh" > "$fixture/output" 2>&1
code=$?
set -e
[[ "$code" != 0 ]] || { echo 'Enabled unreachable live cohort must fail' >&2; exit 1; }
cat > "$fixture/curl" <<'CURL'
#!/bin/sh
for arg in "$@"; do
  case "$arg" in http*) url="$arg";; esac
done
case "$url" in
  */health) exit 0;;
  */me|*/__private_test_flag) printf '{"error":"unauthorized"}\n401';;
  */flags/xynes_auth_oauth_google) printf '{"key":"xynes_auth_oauth_google","value":true}\n200';;
  */flags) printf '{"authenticated":false,"flags":{"xynes_auth_oauth_google":true}}\n200';;
  *) exit 9;;
esac
CURL
set +e
PATH="$fixture:$PATH" RUN_FEATURE_FLAGS_LIVE_TESTS=true XYNES_FEATURE_FLAGS_GATEWAY_URL='http://127.0.0.1:1' XYNES_FEATURE_FLAGS_JWT='inert-fixture-only' bash "$root/scripts/test/feature-flags.test.sh" > "$fixture/output" 2>&1
code=$?
set -e
[[ "$code" != 0 ]] || { echo 'Enabled invalid JWT falsely passed authenticated checks' >&2; exit 1; }
echo 'PASS: feature flag live prerequisites'
