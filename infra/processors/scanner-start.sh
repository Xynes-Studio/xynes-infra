#!/bin/sh
set -eu
scripts=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
sh "$scripts/definitions-ready.sh"
sh "$scripts/definitions-ready.sh" --verify >/dev/null
exec "${XYNES_SCANNER_EXECUTABLE:-/usr/local/bin/xynes-archive-scanner}"
