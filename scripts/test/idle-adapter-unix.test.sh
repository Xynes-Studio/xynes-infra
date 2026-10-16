#!/usr/bin/env bash
# Disposable Linux role/peer fixture; no real Docker socket or external network.
set -euo pipefail

if [[ $# == 0 ]]; then
  bash -n "${BASH_SOURCE[0]}"
  echo 'SKIP: disposable Linux idle integration requires explicit --runtime and immutable fixture image'
  exit 0
fi
if [[ $# != 1 || "$1" != --runtime ]]; then
  echo 'Usage: idle-adapter-unix.test.sh [--runtime]' >&2
  exit 1
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
go_bin="${XYNES_GO_BIN:-go}"
image="${XYNES_IDLE_FIXTURE_IMAGE:?Set an immutable local image ID or registry digest for the disposable Linux fixture}"
if [[ ! "$image" =~ ^sha256:[a-f0-9]{64}$ && ! "$image" =~ ^[a-z0-9][a-z0-9._:/-]*@sha256:[a-f0-9]{64}$ ]]; then
  echo 'Refusing mutable fixture image' >&2
  exit 1
fi
if [[ "$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$image")" != linux/amd64 ]]; then
  echo 'Fixture image must already be available for linux/amd64' >&2
  exit 1
fi

fixture_dir="$(mktemp -d "${TMPDIR:-/tmp}/apex-idle-unix.XXXXXX")"
container_id=''
cleanup() {
  if [[ -n "$container_id" ]]; then docker rm --force "$container_id" >/dev/null 2>&1 || true; fi
  rm -rf "$fixture_dir"
}
trap cleanup EXIT
mkdir "$fixture_dir/coverage"
# Only this child directory is writable by the fixture peer; its host parent
# remains 0700. It carries code coverage, never credential/config material.
chmod 0777 "$fixture_dir/coverage"
(
  cd "$repo_root/infra/idle/adapter"
  CGO_ENABLED=0 GOOS=linux GOARCH=amd64 "$go_bin" test -c -cover -o "$fixture_dir/fixture.test"
)
container_id="$(docker create --label com.xynes.apex.adapter=disposable \
  --network none --no-healthcheck --read-only \
  --memory 512m --cpus 1 --pids-limit 64 \
  --cap-drop ALL --cap-add CHOWN --cap-add SETUID --cap-add SETGID --cap-add KILL --cap-add DAC_OVERRIDE \
  --security-opt no-new-privileges --user 0:0 \
  --tmpfs /run:rw,noexec,nosuid,nodev,size=16m,mode=0755 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777 \
  --tmpfs /etc/xynes:rw,noexec,nosuid,nodev,size=16m,mode=0755 \
  --tmpfs /var/lib/xynes:rw,noexec,nosuid,nodev,size=16m,mode=0755 \
  --add-host auth_app_qa:127.0.0.1 \
  --mount "type=bind,source=$fixture_dir/fixture.test,target=/fixture.test,readonly" \
  --mount "type=bind,source=$fixture_dir/coverage,target=/coverage" \
  --entrypoint /fixture.test --env XYNES_IDLE_UNIX_FIXTURE=1 \
  --env XYNES_IDLE_COVER_DIR=/coverage --platform linux/amd64 "$image" \
  -test.v -test.timeout=2m -test.gocoverdir=/coverage -test.coverprofile=/coverage/linux.out)"
docker start --attach "$container_id"
if [[ "$(docker inspect --format '{{.State.ExitCode}}' "$container_id")" != 0 ]]; then
  echo 'Linux adapter fixture failed' >&2
  exit 1
fi
"$go_bin" tool covdata percent -i="$fixture_dir/coverage"
"$go_bin" tool covdata textfmt -i="$fixture_dir/coverage" -o="$fixture_dir/merged.out"
python3 - "$fixture_dir/merged.out" <<'PY'
import pathlib,sys
covered=total=0
for line in pathlib.Path(sys.argv[1]).read_text().splitlines()[1:]:
    _,statements,count=line.split()
    total+=int(statements)
    if int(count)>0:covered+=int(statements)
assert total and covered/total>=.80,'Linux adapter fixture statement coverage below 80%'
print(f'Linux adapter fixture coverage gate: {covered/total:.1%}')
PY
