#!/usr/bin/env bash
# Start, stop or inspect the PostgreSQL servers used by the integration tests.
#
#   scripts/pg-matrix.sh up [version ...]     start the servers (all versions by default)
#   scripts/pg-matrix.sh down [version ...]   stop and remove them
#   scripts/pg-matrix.sh status               show what is running
#   scripts/pg-matrix.sh psql <version>       open psql on one server
#
# Every server is a throw-away Docker container on the default bridge network, listening
# on 127.0.0.1 only. Nothing is stored outside the containers.
#
# Environment:
#   PGHARBOR_TEST_PASSWORD   superuser password (default: pgharbor)
#   PGHARBOR_TEST_VERSIONS   space separated list overriding the default matrix
set -euo pipefail

PASSWORD="${PGHARBOR_TEST_PASSWORD:-pgharbor}"
DEFAULT_VERSIONS="9.6 10 11 12 13 14 15 16 17 18 19"
# Image tag per version. PostgreSQL 19 is still a release candidate.
declare -A IMAGE=([19]="postgres:19rc2")
PRELOAD="pg_stat_statements"

port_of() { # 9.6 -> 55496, 13 -> 55413
  case "$1" in
    9.6) echo 55496 ;;
    *) echo $((55400 + $1)) ;;
  esac
}
name_of() { echo "pgharbor-test-${1//./}"; }
image_of() { echo "${IMAGE[$1]:-postgres:$1}"; }

versions() {
  if [ "$#" -gt 0 ]; then echo "$@"; else echo "${PGHARBOR_TEST_VERSIONS:-$DEFAULT_VERSIONS}"; fi
}

up() {
  for v in $(versions "$@"); do
    name="$(name_of "$v")"
    port="$(port_of "$v")"
    if docker ps --format '{{.Names}}' | grep -qx "$name"; then
      echo "already running  $name  127.0.0.1:$port"
      continue
    fi
    docker rm -f "$name" >/dev/null 2>&1 || true
    docker run -d --rm --name "$name" \
      -e POSTGRES_PASSWORD="$PASSWORD" \
      -p "127.0.0.1:${port}:5432" --shm-size=256m \
      "$(image_of "$v")" \
      -c shared_preload_libraries="$PRELOAD" \
      -c track_io_timing=on -c track_functions=all \
      -c wal_level=logical -c max_wal_senders=4 -c max_replication_slots=4 \
      -c fsync=off -c max_connections=60 >/dev/null
    echo "started          $name  127.0.0.1:$port  ($(image_of "$v"))"
  done
  echo "waiting for the servers to accept connections..."
  for v in $(versions "$@"); do
    name="$(name_of "$v")"
    for _ in $(seq 1 60); do
      if docker exec "$name" pg_isready -q -U postgres 2>/dev/null &&
        docker exec -e PGPASSWORD="$PASSWORD" "$name" psql -q -U postgres -h 127.0.0.1 -c 'select 1' >/dev/null 2>&1; then
        break
      fi
      sleep 0.5
    done
  done
  status
}

down() {
  for v in $(versions "$@"); do
    docker rm -f "$(name_of "$v")" >/dev/null 2>&1 && echo "removed $(name_of "$v")" || true
  done
}

status() {
  docker ps --filter 'name=pgharbor-test-' --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' | sort -V
}

case "${1:-}" in
  up) shift; up "$@" ;;
  down) shift; down "$@" ;;
  status) status ;;
  psql)
    [ "$#" -ge 2 ] || { echo "usage: $0 psql <version>" >&2; exit 2; }
    v="$2"; shift 2
    PGPASSWORD="$PASSWORD" exec psql -h 127.0.0.1 -p "$(port_of "$v")" -U postgres "$@"
    ;;
  *)
    sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
