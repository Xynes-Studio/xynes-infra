#!/bin/sh
# Native Freshclam owns signed/atomic database replacement. Never purge definitions.
set -eu
fail() { echo 'Freshclam updater unavailable' >&2; exit 1; }
case "${1:-}" in ''|--once) ;; *) fail ;; esac
[ "$#" -le 1 ] && [ "$(id -u)" = 0 ] || fail
base=${XYNES_DEFINITIONS_DIRECTORY:-/var/lib/clamav}
run=${XYNES_UPDATER_RUN_DIRECTORY:-/run/clamav}
[ -d "$base" ] && [ ! -L "$base" ] && [ -d "$run" ] && [ ! -L "$run" ] || fail
scripts=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
# Only the named definitions-volume directory is initialized; no recursive chown.
chown 1000:1000 "$base" || fail
chmod 0750 "$base" || fail
umask 077
config=$(mktemp "$run/xynes-freshclam.XXXXXX") || fail
receipt_temp=''
active_pid=''
pidfile="$run/xynes-updater.pid"
[ ! -e "$pidfile" ] && [ ! -L "$pidfile" ] || fail
printf '%s\n' "$$" > "$pidfile"
cleanup() { rm -f "$config" "$pidfile"; if [ -n "$receipt_temp" ]; then rm -f "$receipt_temp"; fi; }
trap cleanup EXIT
stop() {
  trap '' INT TERM
  if [ -n "$active_pid" ]; then kill -TERM "$active_pid" 2>/dev/null || true; wait "$active_pid" 2>/dev/null || true; fi
  exit 0
}
trap stop INT TERM
cat > "$config" <<EOF
DatabaseDirectory $base
DatabaseOwner clamav
DatabaseMirror database.clamav.net
TestDatabases no
ScriptedUpdates yes
Bytecode yes
ConnectTimeout 10
ReceiveTimeout 60
EOF
update() {
  # Signature checking remains native; avoid loading a second full engine in updater RAM.
  timeout --signal=TERM --kill-after=15 600 freshclam --foreground --stdout --config-file="$config" >/dev/null 2>&1 &
  active_pid=$!
  status=0
  wait "$active_pid" || status=$?
  active_pid=''
  [ "$status" = 0 ] || return 1
  daily=$(sh "$scripts/definitions-ready.sh" --verify) || return 1
  checked=$(date +%s) || return 1
  receipt="$base/.xynes-freshclam-success"
  if [ -e "$receipt" ] || [ -L "$receipt" ]; then [ -f "$receipt" ] && [ ! -L "$receipt" ] || return 1; fi
  receipt_temp=$(mktemp "$base/.xynes-check.XXXXXX") || return 1
  printf '%s %s\n' "$checked" "$daily" > "$receipt_temp"
  chmod 0644 "$receipt_temp"
  mv -f "$receipt_temp" "$receipt"
  receipt_temp=''
}
if [ "${1:-}" = --once ]; then update || fail; exit 0; fi
while :; do
  delay=7200
  if ! update; then echo 'Freshclam check failed; retained definitions remain unchanged by supervisor' >&2; delay=300; fi
  sleep "$delay" &
  active_pid=$!
  wait "$active_pid" || true
  active_pid=''
done
