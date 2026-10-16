#!/bin/sh
# Empty INSTREAM exercises the public supervisor and its owned native clamd.
set -eu
scripts=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
sh "$scripts/definitions-ready.sh"
reply=$(printf 'zINSTREAM\000\000\000\000\000' | nc -w 5 127.0.0.1 3310 | tr -d '\000') || exit 1
[ "$reply" = 'stream: OK' ] || exit 1
