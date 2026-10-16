#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
fixture="$(mktemp -d "${TMPDIR:-/tmp}/domain-env-guard.XXXXXX")"
trap 'rm -rf "$fixture"' EXIT
printf '#!/bin/sh\nexit 1\n' > "$fixture/psql"
chmod 700 "$fixture/psql"
output="$(PATH="$fixture:$PATH" DATABASE_URL='postgres://fixture:inert-password-must-not-appear@127.0.0.1:1/owned_fixture' bash "$root/scripts/test/workspace-admin-integrations-domain-constraint.test.sh")"
[[ "$output" == *'connection details redacted'* && "$output" != *'inert-password-must-not-appear'* ]] || { echo 'Unreachable constraint fixture exposed connection details' >&2; exit 1; }
echo 'PASS: constraint fixture connection details stay redacted'
