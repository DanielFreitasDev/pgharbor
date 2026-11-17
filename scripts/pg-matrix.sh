#!/usr/bin/env bash
# Start, stop or inspect the PostgreSQL servers used by the integration tests.
#
#   scripts/pg-matrix.sh up [version ...]     start the servers (all versions by default)
#   scripts/pg-matrix.sh down [version ...]   stop and remove them
#   scripts/pg-matrix.sh status               show what is running
#   scripts/pg-matrix.sh psql <version>       open psql on one server
#   scripts/pg-matrix.sh up extras            PostgreSQL 18 plus third-party extensions
#                                             (hypopg, pgvector, PostGIS, pg_cron, ...);
#                                             builds a local image the first time
#   scripts/pg-matrix.sh up bouncer           PgBouncer (transaction pooling) in front of
#                                             the PostgreSQL 18 test server
#   scripts/pg-matrix.sh up standby           a streaming replica (hot standby) of the
#                                             PostgreSQL 17 test server
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

EXTRAS_IMAGE="pgharbor-test-extras:18"
EXTRAS_PRELOAD="pg_stat_statements,pg_cron,pgaudit,pg_stat_kcache,pg_qualstats,pg_wait_sampling"

port_of() { # 9.6 -> 55496, 13 -> 55413, extras -> 55500
  case "$1" in
    9.6) echo 55496 ;;
    extras) echo 55500 ;;
    *) echo $((55400 + $1)) ;;
  esac
}
name_of() { echo "pgharbor-test-${1//./}"; }
image_of() {
  if [ "$1" = extras ]; then echo "$EXTRAS_IMAGE"; else echo "${IMAGE[$1]:-postgres:$1}"; fi
}

build_extras() {
  docker image inspect "$EXTRAS_IMAGE" >/dev/null 2>&1 && return 0
  echo "building $EXTRAS_IMAGE (first time only)..."
  local here; here="$(cd "$(dirname "$0")/.." && pwd)"
  # apt only reads the lower-case proxy variables.
  docker build -q -t "$EXTRAS_IMAGE" \
    ${http_proxy:+--build-arg http_proxy="${PGHARBOR_DOCKER_PROXY:-$http_proxy}"} \
    ${https_proxy:+--build-arg https_proxy="${PGHARBOR_DOCKER_PROXY:-$https_proxy}"} \
    "$here/tests/docker/extras" >/dev/null
}

versions() {
  if [ "$#" -gt 0 ]; then echo "$@"; else echo "${PGHARBOR_TEST_VERSIONS:-$DEFAULT_VERSIONS}"; fi
}

up_bouncer() {
  local name="pgharbor-test-bouncer" target
  docker ps --format '{{.Names}}' | grep -qx "$name" && { echo "already running  $name  127.0.0.1:55432"; return 0; }
  target="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$(name_of 18)" 2>/dev/null || true)"
  [ -n "$target" ] || { echo "start PostgreSQL 18 first: $0 up 18" >&2; exit 1; }
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --rm --name "$name" \
    -e DB_HOST="$target" -e DB_PORT=5432 -e DB_USER=postgres -e DB_PASSWORD="$PASSWORD" \
    -e DB_NAME=pgharbor_test -e POOL_MODE=transaction -e AUTH_TYPE=scram-sha-256 \
    -e MAX_CLIENT_CONN=50 -e DEFAULT_POOL_SIZE=5 \
    -p 127.0.0.1:55432:5432 edoburu/pgbouncer:latest >/dev/null
  echo "started          $name  127.0.0.1:55432  (PgBouncer -> PostgreSQL 18)"
}

up_standby() {
  # A physical replica of the PostgreSQL 17 server, to exercise the tools on a standby
  # and to give the replication tools something real to report.
  local name="pgharbor-test-standby" primary target
  docker ps --format '{{.Names}}' | grep -qx "$name" && { echo "already running  $name  127.0.0.1:55517"; return 0; }
  primary="$(name_of 17)"
  target="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$primary" 2>/dev/null || true)"
  [ -n "$target" ] || { echo "start PostgreSQL 17 first: $0 up 17" >&2; exit 1; }
  # Let the replica connect: the stock image only allows replication from localhost.
  docker exec "$primary" bash -c 'grep -q "^host replication all all" "$PGDATA/pg_hba.conf" || echo "host replication all all scram-sha-256" >> "$PGDATA/pg_hba.conf"'
  docker exec -e PGPASSWORD="$PASSWORD" "$primary" psql -q -U postgres -h 127.0.0.1 -c 'SELECT pg_reload_conf()' >/dev/null
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --rm --name "$name" -e PGPASSWORD="$PASSWORD" \
    -p 127.0.0.1:55517:5432 --shm-size=256m --entrypoint bash "$(image_of 17)" -c '
      set -e
      mkdir -p "$PGDATA" && chown -R postgres:postgres "$PGDATA" && chmod 700 "$PGDATA"
      gosu postgres pg_basebackup -h '"$target"' -U postgres -D "$PGDATA" -R -X stream
      exec gosu postgres postgres -c hot_standby=on -c shared_preload_libraries='"$PRELOAD"' \
        -c track_io_timing=on -c wal_level=logical -c max_wal_senders=4 \
        -c max_replication_slots=4 -c fsync=off' >/dev/null
  echo "started          $name  127.0.0.1:55517  (standby of PostgreSQL 17)"
  for _ in $(seq 1 60); do
    docker exec -e PGPASSWORD="$PASSWORD" "$name" psql -q -U postgres -h 127.0.0.1 -c 'select 1' >/dev/null 2>&1 && break
    sleep 0.5
  done
}

up() {
  if [ "${1:-}" = bouncer ]; then up_bouncer; return 0; fi
  if [ "${1:-}" = standby ]; then up_standby; return 0; fi
  for v in $(versions "$@"); do
    name="$(name_of "$v")"
    port="$(port_of "$v")"
    if docker ps --format '{{.Names}}' | grep -qx "$name"; then
      echo "already running  $name  127.0.0.1:$port"
      continue
    fi
    docker rm -f "$name" >/dev/null 2>&1 || true
    preload="$PRELOAD"
    extra=()
    if [ "$v" = extras ]; then
      build_extras
      preload="$EXTRAS_PRELOAD"
      extra=(-c cron.database_name=pgharbor_test)
    fi
    docker run -d --rm --name "$name" \
      -e POSTGRES_PASSWORD="$PASSWORD" \
      -p "127.0.0.1:${port}:5432" --shm-size=256m \
      "$(image_of "$v")" \
      -c shared_preload_libraries="$preload" "${extra[@]}" \
      -c track_io_timing=on -c track_functions=all \
      -c wal_level=logical -c max_wal_senders=4 -c max_replication_slots=4 \
      -c fsync=off >/dev/null
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
  [ "$#" -eq 0 ] && { docker rm -f pgharbor-test-extras pgharbor-test-bouncer pgharbor-test-standby >/dev/null 2>&1 || true; }
  for v in $(versions "$@"); do
    [ "$v" = bouncer ] && { docker rm -f pgharbor-test-bouncer >/dev/null 2>&1 && echo "removed pgharbor-test-bouncer"; continue; }
    [ "$v" = standby ] && { docker rm -f pgharbor-test-standby >/dev/null 2>&1 && echo "removed pgharbor-test-standby"; continue; }
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
