# Compatibility

## PostgreSQL

Every version below runs the full integration suite on every commit, in CI and locally
(`scripts/pg-matrix.sh up && pytest`).

| Version | Status upstream (November 2026) | pgharbor |
|---|---|---|
| 19 | release candidate | tested against the release candidate |
| 18 | current | tested |
| 17, 16, 15 | supported | tested |
| 14, 13, 12, 11, 10 | end of life | tested |
| 9.6 | end of life | tested; the oldest version supported |
| 9.5 and older | end of life | refused at connection time |

Old servers are first-class: plenty of production still runs on them. Tools build their
catalog queries for the version they are connected to.

### Features that depend on the version

A tool that needs something the server does not have says so in its answer instead of
failing with a SQL error.

| Feature | Since | Where it shows |
|---|---|---|
| `idle_in_transaction_session_timeout`, `pg_blocking_pids`, vacuum progress | 9.6 | baseline |
| Declarative partitioning, identity columns, `pg_sequence`, logical replication (publications, subscriptions), extended statistics, `pg_hba_file_rules`, `backend_type`, WAL function names (`pg_current_wal_lsn`) | 10 | schema, replication, security, activity |
| Procedures (`prokind`), predefined roles for server files | 11 | objects, security audit |
| Generated columns, `REINDEX CONCURRENTLY`, progress of `CREATE INDEX` and `CLUSTER`, checksum failure counters | 12 | schema, maintenance, progress |
| `pg_stat_statements` planning time and WAL, `leader_pid`, progress of `ANALYZE` and base backup, slot `wal_status` | 13 | top queries, activity, replication |
| `query_id`, `pg_stat_wal`, `pg_stat_replication_slots`, session statistics, `pg_locks.waitstart`, progress of `COPY` | 14 | activity, WAL, locks |
| `pg_stat_subscription_stats`, database collation version | 15 | replication, integrity |
| `pg_stat_io`, role membership options, slot `conflicting` | 16 | cache and I/O, roles, replication |
| `pg_stat_checkpointer`, `pg_wait_events`, slot invalidation reason | 17 | WAL and checkpoints |
| Virtual generated columns, NOT NULL as catalogued constraints, asynchronous I/O views | 18 | schema |
| `pg_stat_lock`, `pg_stat_progress_repack`, `pg_stat_recovery` | 19 | reachable through `pg_stat_view` |

Views that have no dedicated tool, in any version including future ones, are readable by
name with `pg_stat_view`.

### Extensions

Extensions are detected at run time. Tools that use one say how to install it when it is
missing.

| Extension | Used by |
|---|---|
| `pg_stat_statements` | `pg_top_queries`, health checks |
| `pgstattuple` | exact bloat in `pg_bloat` |
| `pg_buffercache` | `pg_cache_io` |
| `hypopg` | `pg_index_advisor` (what-if indexes) |
| `amcheck` | `pg_integrity_check` |
| `pgvector`, PostGIS, `pg_cron`, `pg_partman`, `pg_trgm`, `pgaudit`, `postgres_fdw`, `dblink`, TimescaleDB | `pg_extension_report` |

### Managed services and poolers

pgharbor needs nothing installed on the server and no superuser. It works wherever a
PostgreSQL connection works: self-hosted, containers, and managed services that speak
the PostgreSQL protocol (they usually restrict some views; tools report what is missing).

**PgBouncer** in transaction pooling mode is tested (`scripts/pg-matrix.sh up bouncer`). Poolers refuse the `options`
start-up parameter and do not keep session state, so pgharbor applies its settings
inside every transaction. One consequence: commands that cannot run inside a
transaction (`VACUUM`, `CREATE INDEX CONCURRENTLY`, through the admin tools) need a
direct connection or session pooling.

**Standbys** (hot standby) work for every read tool; the suite runs them against a
streaming replica (`scripts/pg-matrix.sh up standby`).

## Python

3.11, 3.12, 3.13 and 3.14. The unit suite runs on each.

## MCP

| Revision | Status |
|---|---|
| 2026-07-28 | supported; negotiated when the client offers it |
| 2025-11-25 | supported |

Both are exercised by the test suite through the official SDK's client, including the
dialogs used for approval and confirmation. Transport: stdio.

## Operating systems

Linux, macOS and Windows. pgharbor is pure Python; the PostgreSQL driver (psycopg 3)
ships binary wheels for the common platforms and falls back to the system's libpq.

The integration suite, which needs the PostgreSQL containers, runs on Linux. On macOS
and Windows CI runs the unit suite and starts the server over stdio; on Windows it also
connects to a real PostgreSQL, because the driver needs a different event loop there
(pgharbor selects it by itself).
