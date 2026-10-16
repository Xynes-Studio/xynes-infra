#!/bin/sh
# Fresh official-Postgres initialization only; never print or put a password in argv.
set -eu
refuse() { printf '%s\n' 'Auth bootstrap inputs refused' >&2; exit 1; }
case "${AUTH_DATABASE_PASSWORD:-}" in *[!0-9a-f]*|'') refuse ;; esac
[ "${#AUTH_DATABASE_PASSWORD}" -eq 64 ] || refuse
[ "${AUTH_DATABASE_PASSWORD}" != "${POSTGRES_PASSWORD:-}" ] || refuse
[ "${POSTGRES_USER:-}" = postgres ] || refuse
[ "${POSTGRES_DB:-}" = postgres ] || refuse
psql --no-psqlrc --no-password --quiet --set=ON_ERROR_STOP=1 \
  --username postgres --dbname postgres --file /opt/apex-auth/bootstrap.sql
