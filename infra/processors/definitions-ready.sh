#!/bin/sh
# Health uses successful-check time AND signed daily build time, not file mtime.
set -eu
fail() { echo 'ClamAV definitions unavailable or stale' >&2; exit 1; }
case "${1:-}" in ''|--verify) ;; *) fail ;; esac
[ "$#" -le 1 ] || fail
base=${XYNES_DEFINITIONS_DIRECTORY:-/var/lib/clamav}
[ -d "$base" ] && [ ! -L "$base" ] || fail
for family in main daily bytecode; do
  [ ! -L "$base/$family.cld" ] && [ ! -L "$base/$family.cvd" ] || fail
  file="$base/$family.cld"
  [ -e "$file" ] || file="$base/$family.cvd"
  [ -f "$file" ] && [ -s "$file" ] && [ ! -L "$file" ] || fail
  if [ "${1:-}" = --verify ]; then
    info=$(timeout --signal=TERM --kill-after=2 30 sigtool --info "$file" 2>/dev/null) || fail
    printf '%s\n' "$info" | grep -Eq '^Verification OK[.]?$' || fail
    if [ "$family" = daily ]; then
      build=$(printf '%s\n' "$info" | sed -n 's/^Build time: //p')
      [ -n "$build" ] || fail
      daily=$(date -u -d "$build" +%s 2>/dev/null) || fail
    fi
  fi
done
now=$(date +%s) || fail
if [ "${1:-}" != --verify ]; then
  receipt="$base/.xynes-freshclam-success"
  [ -f "$receipt" ] && [ ! -L "$receipt" ] && [ "$(wc -c < "$receipt")" -le 64 ] || fail
  read -r checked daily extra < "$receipt" || fail
  [ -z "$extra" ] || fail
else
  checked=$now
fi
for value in "$now" "$checked" "$daily"; do
  case "$value" in ''|*[!0-9]*) fail ;; esac
  [ "${#value}" -le 10 ] && [ "$value" -gt 0 ] || fail
done
[ "$checked" -le "$now" ] && [ "$daily" -le "$now" ] || fail
[ "$((now - checked))" -le 14400 ] && [ "$((now - daily))" -le 172800 ] || fail
if [ "${1:-}" = --verify ]; then printf '%s\n' "$daily"; fi
