#!/bin/sh
set -eu
scripts=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
run=${XYNES_UPDATER_RUN_DIRECTORY:-/run/clamav}
pidfile="$run/xynes-updater.pid"
[ -f "$pidfile" ] && [ ! -L "$pidfile" ] && [ "$(wc -c < "$pidfile")" -le 12 ] || exit 1
read -r pid extra < "$pidfile" || exit 1
case "$pid" in ''|*[!0-9]*) exit 1 ;; esac
[ -z "$extra" ] && [ "$pid" -gt 0 ] && kill -0 "$pid" 2>/dev/null || exit 1
exec sh "$scripts/definitions-ready.sh"
