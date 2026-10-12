#!/usr/bin/env bash
# Database helper functions for smoke tests

# ─────────────────────────────────────────────────────────────────────────────
# extract_db_host - Extract host from DATABASE_URL
# Usage: extract_db_host <database_url>
# ─────────────────────────────────────────────────────────────────────────────
extract_db_host() {
  local url="$1"
  echo "$url" | sed -E 's|.*@([^/:]+).*|\1|'
}

# ─────────────────────────────────────────────────────────────────────────────
# extract_db_port - Extract port from DATABASE_URL
# Usage: extract_db_port <database_url>
# ─────────────────────────────────────────────────────────────────────────────
extract_db_port() {
  local url="$1"
  local port
  port="$(echo "$url" | sed -E 's|.*:([0-9]+)/.*|\1|')"
  if [ -z "$port" ] || [ "$port" = "$url" ]; then
    echo "5432"
  else
    echo "$port"
  fi
}

# ─────────────────────────────────────────────────────────────────────────────
# check_ssh_tunnel_binding_hint - Show hint about SSH tunnel binding
# Usage: check_ssh_tunnel_binding_hint
# ─────────────────────────────────────────────────────────────────────────────
check_ssh_tunnel_binding_hint() {
  local db_host
  db_host="${DB_HOST:-$(extract_db_host "${DATABASE_URL:-}")}"

  # If db_host is localhost/127.0.0.1 and we're running in Docker mode,
  # Docker containers can't reach host's loopback without special binding
  if [ "${MODE:-localhost}" = "env" ] || [ "${MODE:-localhost}" = "docker" ]; then
    case "$db_host" in
      localhost|127.0.0.1)
        echo -e "${YELLOW:-}Hint:${NC:-} DATABASE_URL uses $db_host, which is unreachable from Docker."
        echo "      Bind your SSH tunnel to 0.0.0.0 or use host.docker.internal:"
        echo "        ssh -N -L 0.0.0.0:5432:<remote_db_host>:5432 <ssh_host>"
        ;;
    esac
  fi
}

# ─────────────────────────────────────────────────────────────────────────────
# wait_for_db - Wait for database to be reachable
# Usage: wait_for_db <host> <port> [timeout_secs]
# ─────────────────────────────────────────────────────────────────────────────
wait_for_db() {
  local host="$1"
  local port="$2"
  local timeout="${3:-30}"
  local elapsed=0

  while [ "$elapsed" -lt "$timeout" ]; do
    if nc -z "$host" "$port" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done

  return 1
}
