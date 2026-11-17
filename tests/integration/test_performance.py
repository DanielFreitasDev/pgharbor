"""Performance tools on every PostgreSQL version of the matrix."""

import asyncio
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from pgharbor.app import App, set_app
from pgharbor.config import Config
from pgharbor.tools.performance import (
    find_duplicate_indexes,
    format_setting,
    pg_activity,
    pg_bloat,
    pg_cache_io,
    pg_database_stats,
    pg_index_stats,
    pg_locks,
    pg_progress,
    pg_sizes,
    pg_stat_view,
    pg_table_stats,
    pg_top_queries,
    pg_vacuum_status,
    size_text,
)

from ..conftest import Server, call, profile, rows_as_dicts, superuser

pytestmark = pytest.mark.integration

WORK = "pgh_perf_w"


# ------------------------------------------------------------------------------- helpers


def settle() -> None:
    """Statistics reach the views a moment after the session that produced them ends."""
    time.sleep(1.5)


@contextmanager
def session(server: Server, name: str = "pgh_perf") -> Iterator[psycopg.Connection[Any]]:
    conn = superuser(server)
    conn.execute(f"SET application_name = '{name}'")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def scratch_schema(server: Server, name: str) -> Iterator[None]:
    with superuser(server) as admin:
        admin.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
        admin.execute(f"CREATE SCHEMA {name}")
        try:
            yield
        finally:
            admin.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")


@contextmanager
def blocked_pair(server: Server, schema: str) -> Iterator[tuple[int, int]]:
    """A session holding ACCESS EXCLUSIVE on `schema.t` and another waiting for it.

    Yields (holder pid, waiter pid) once the wait is real.
    """
    with scratch_schema(server, schema):
        holder = superuser(server)
        waiter = superuser(server)
        holder.execute(f"CREATE TABLE {schema}.t (id int)")
        holder.execute("SET application_name = 'pgh_perf_holder'")
        holder.execute("BEGIN")
        holder.execute(f"LOCK TABLE {schema}.t IN ACCESS EXCLUSIVE MODE")
        waiter.execute("SET application_name = 'pgh_perf_waiter'")
        waiter.execute("SET lock_timeout = '30s'")
        outcome: list[BaseException] = []

        def wait_for_lock() -> None:
            try:
                waiter.execute(f"SELECT count(*) FROM {schema}.t")
            except psycopg.Error as exc:  # cancelled when the test ends
                outcome.append(exc)

        thread = threading.Thread(target=wait_for_lock)
        thread.start()
        try:
            holder_pid, waiter_pid = holder.info.backend_pid, waiter.info.backend_pid
            for _ in range(100):
                row = holder.execute(
                    "SELECT cardinality(pg_blocking_pids(%s))", [waiter_pid]
                ).fetchone()
                if row and row[0] > 0:
                    break
                time.sleep(0.1)
            else:
                pytest.fail("the second session never started waiting for the lock")
            yield holder_pid, waiter_pid
        finally:
            holder.execute("ROLLBACK")
            thread.join(15)
            holder.close()
            waiter.close()


@pytest.fixture(scope="session")
def workload(ready: Server) -> Iterator[str]:
    """Tables with known problems in a schema of their own: a bloated table with dead rows
    and autovacuum off, redundant, duplicate, unused and invalid indexes, a table read only
    by sequential scans, and sequences near their limits."""
    schema = WORK
    with superuser(ready) as admin:
        admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        admin.execute(f"CREATE SCHEMA {schema}")
    with superuser(ready) as conn:
        conn.execute(
            f"CREATE TABLE {schema}.bloaty (id int, pad text) WITH (autovacuum_enabled = false)"
        )
        conn.execute(
            f"INSERT INTO {schema}.bloaty SELECT g, repeat('x', 200) FROM generate_series(1, 20000) g"
        )
        conn.execute(f"CREATE INDEX bloaty_id_idx ON {schema}.bloaty (id)")
        conn.execute(f"CREATE INDEX bloaty_id_pad_idx ON {schema}.bloaty (id, pad)")
        conn.execute(f"CREATE UNIQUE INDEX bloaty_uniq ON {schema}.bloaty (id, pad)")
        conn.execute(f"DELETE FROM {schema}.bloaty WHERE id % 10 <> 0")
        conn.execute(f"ANALYZE {schema}.bloaty")

        conn.execute(
            f"CREATE TABLE {schema}.scanme (id int, v text) WITH (autovacuum_enabled = false)"
        )
        conn.execute(
            f"INSERT INTO {schema}.scanme SELECT g, 'v' || g FROM generate_series(1, 12000) g"
        )
        for number in range(25):
            conn.execute(f"SELECT count(*) FROM {schema}.scanme WHERE v = 'v{number}x'")

        conn.execute(f"CREATE TABLE {schema}.inv (v int)")
        conn.execute(f"INSERT INTO {schema}.inv VALUES (1), (1)")
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(f"CREATE UNIQUE INDEX CONCURRENTLY inv_bad_idx ON {schema}.inv (v)")

        conn.execute(f"CREATE TABLE {schema}.serial_t (id serial)")
        if ready.at_least(10):
            conn.execute(f"ALTER SEQUENCE {schema}.serial_t_id_seq AS bigint")
        conn.execute(f"SELECT setval('{schema}.serial_t_id_seq', 2000000000)")
        conn.execute(f"CREATE SEQUENCE {schema}.cyc MAXVALUE 10 CYCLE")
        conn.execute(f"SELECT setval('{schema}.cyc', 9)")
    settle()
    try:
        yield schema
    finally:
        with superuser(ready) as admin:
            admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


@pytest.fixture
async def bare_app(ready: Server) -> Iterator[App]:
    """A server whose database has no extensions at all."""
    name = "pgh_perf_bare"
    with superuser(ready, "postgres") as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {name}")
        admin.execute(f"CREATE DATABASE {name}")
    config = Config.model_validate(
        {
            "connections": {"bare": profile(ready, "bare", dbname=name)},
            "audit": {"enabled": False},
            "source": "tests",
        }
    )
    application = App(config)
    set_app(application)
    try:
        yield application
    finally:
        await application.close()
        set_app(None)
        with superuser(ready, "postgres") as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {name}")


def by_name(table: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
    return {row[key]: row for row in rows_as_dicts(table)}


# -------------------------------------------------------------------------- pure helpers


def test_size_text_matches_pg_size_pretty() -> None:
    assert size_text(0) == "0 bytes"
    assert size_text(10239) == "10239 bytes"
    assert size_text(10240) == "10 kB"
    assert size_text(8192 * 128) == "1024 kB"
    assert size_text(1024**3) == "1024 MB"
    assert size_text(None) is None


def test_duplicate_detection_keeps_primary_then_unique() -> None:
    base = {
        "table_oid": 1,
        "method": "btree",
        "key_cols": "1",
        "opclasses": "1978",
        "collations": "0",
        "options": "0",
        "exprs": "",
        "pred": "",
        "unique": False,
        "primary": False,
        "backs_constraint": False,
        "schema": "s",
        "table": "t",
    }
    rows = [
        {**base, "index": "a_plain"},
        {**base, "index": "b_unique", "unique": True, "backs_constraint": True},
        {**base, "index": "c_other_columns", "key_cols": "2"},
        {**base, "index": "d_partial", "pred": "(x > 1)"},
    ]
    found = find_duplicate_indexes(rows)
    assert [(r["index"], r["of"]) for r in found] == [("a_plain", "b_unique")]


@pytest.mark.parametrize(
    ("value", "unit", "shown"),
    [
        ("16384", "8kB", "128MB"),
        ("4096", "kB", "4MB"),
        ("1024", "MB", "1GB"),
        ("300000", "ms", "5min"),
        ("200", "ms", "200ms"),
        ("60", "s", "1min"),
        ("-1", "kB", "-1"),
        ("0", "ms", "0"),
        ("on", None, "on"),
        ("0.2", None, "0.2"),
    ],
)
def test_format_setting(value: str, unit: str | None, shown: str) -> None:
    assert format_setting(value, unit) == shown


async def test_format_setting_agrees_with_the_server(ready: Server) -> None:
    """Every setting that has a unit prints the way SHOW prints it."""
    with superuser(ready) as conn:
        rows = conn.execute(
            "SELECT name, setting, unit, current_setting(name) FROM pg_settings "
            "WHERE unit IS NOT NULL AND setting <> '-1' AND name NOT LIKE 'tcp\\_%'"
        ).fetchall()
    assert len(rows) > 30
    wrong = [(n, s, u, c, format_setting(s, u)) for n, s, u, c in rows if format_setting(s, u) != c]
    assert not wrong, wrong


# ------------------------------------------------------------------------------ pg_activity


async def test_activity_lists_sessions_and_hides_idle(app: Any, ready: Server) -> None:
    with session(ready, "pgh_perf_idle_tx") as busy, session(ready, "pgh_perf_idle"):
        busy.execute("BEGIN")
        busy.execute("SELECT 1 AS pgh_perf_marker")
        result = await call(pg_activity, connection="ro")
        rows = {r["application"]: r for r in rows_as_dicts(result)}
        assert "pgh_perf_idle" not in rows
        mine = rows["pgh_perf_idle_tx"]
        assert mine["state"] == "idle in transaction"
        assert mine["database"] == "pgharbor_test"
        assert mine["user"] == "postgres"
        assert mine["pid"] == busy.info.backend_pid
        assert "pgh_perf_marker" in mine["query"]
        assert mine["xact_seconds"] is not None
        assert mine["xact_seconds"] >= 0
        assert mine["blocked_by"] is None
        assert result["connections"]["used"] >= 3
        assert result["connections"]["max"] >= result["connections"]["used"]
        assert result["by_state"]["idle in transaction"] >= 1
        assert result["by_state"]["idle"] >= 1

        # Columns that exist only on newer versions.
        columns = result["columns"]
        assert ("backend_type" in columns) == ready.at_least(10)
        assert ("leader_pid" in columns) == ready.at_least(13)
        assert ("query_id" in columns) == ready.at_least(14)

        everyone = await call(pg_activity, connection="ro", include_idle=True)
        assert {"pgh_perf_idle", "pgh_perf_idle_tx"} <= {
            r["application"] for r in rows_as_dicts(everyone)
        }
        only_idle = await call(pg_activity, connection="ro", state="idle")
        apps = {r["application"] for r in rows_as_dicts(only_idle)}
        assert "pgh_perf_idle" in apps
        assert "pgh_perf_idle_tx" not in apps

        old = await call(pg_activity, connection="ro", min_duration_seconds=3600)
        assert "pgh_perf_idle_tx" not in {r["application"] for r in rows_as_dicts(old)}
        elsewhere = await call(pg_activity, connection="ro", database="postgres")
        assert "pgh_perf_idle_tx" not in {r["application"] for r in rows_as_dicts(elsewhere)}
        busy.execute("ROLLBACK")


async def test_activity_never_lists_its_own_session(app: Any) -> None:
    result = await call(pg_activity, connection="ro", include_idle=True)
    assert all("pg_stat_activity" not in (r["query"] or "") for r in rows_as_dicts(result))


async def test_activity_shows_who_blocks_whom(app: Any, ready: Server) -> None:
    with blocked_pair(ready, "pgh_perf_act") as (holder, waiter):
        result = await call(pg_activity, connection="ro", include_idle=True)
        by_pid = {r["pid"]: r for r in rows_as_dicts(result)}
        assert by_pid[waiter]["blocked_by"] == [holder]
        assert by_pid[waiter]["wait_event_type"] == "Lock"
        assert by_pid[holder]["blocked_by"] is None


async def test_activity_for_a_role_without_privileges(app: Any) -> None:
    result = await call(pg_activity, connection="limited")
    assert result["connections"]["max"] > 0
    if result["connections"]["used"]:
        assert "notes" in result
        assert "pg_read_all_stats" in result["notes"][0]


# --------------------------------------------------------------------------------- pg_locks


async def test_locks_builds_the_blocking_tree(app: Any, ready: Server) -> None:
    with blocked_pair(ready, "pgh_perf_lock") as (holder, waiter):
        result = await call(pg_locks, connection="ro")
        assert result["blocked_sessions"] >= 1
        wait = next(w for w in rows_as_dicts(result["waits"]) if w["waiting_pid"] == waiter)
        assert wait["blocker_pid"] == holder
        assert wait["lock_type"] == "relation"
        assert wait["lock_mode"] == "AccessShareLock"
        assert wait["object"] == "pgh_perf_lock.t"
        assert "pgh_perf_lock.t" in wait["waiting_query"]
        assert wait["blocker_state"] == "idle in transaction"
        assert wait["waiting_seconds"] >= 0

        root = next(r for r in result["root_blockers"] if r["pid"] == holder)
        assert root["blocked_sessions"] >= 1
        assert "AccessExclusiveLock on pgh_perf_lock.t" in root["holds"]

        strong = {(r["pid"], r["table"]) for r in rows_as_dicts(result["strong_locks"])}
        assert (holder, "pgh_perf_lock.t") in strong
        modes = by_name(result["lock_modes"], "mode")
        assert modes["AccessExclusiveLock"]["granted"] >= 1
        assert modes["AccessShareLock"]["waiting"] >= 1

        full = await call(pg_locks, connection="ro", only_blocked=False)
        held = {(r["pid"], r["mode"], r["granted"]) for r in rows_as_dicts(full["table_locks"])}
        assert (holder, "AccessExclusiveLock", True) in held
        assert (waiter, "AccessShareLock", False) in held


async def test_locks_without_waits(app: Any, ready: Server) -> None:
    with session(ready) as quiet:
        quiet.execute("SELECT 1")
        result = await call(pg_locks, connection="ro")
    assert result["blocked_sessions"] == 0 or "waits" in result
    assert result["lock_modes"]["columns"] == ["mode", "granted", "waiting"]
    if result["blocked_sessions"] == 0:
        assert result["message"] == "No session is waiting for a lock."
        assert "root_blockers" not in result


# ------------------------------------------------------------------------- pg_top_queries


async def test_top_queries(app: Any, ready: Server) -> None:
    with session(ready) as conn:
        for _ in range(450):
            conn.execute(
                "SELECT count(*) FROM shop.no_pk WHERE code <> label OR label = code OR code IS NULL"
            )
    result = await call(pg_top_queries, connection="ro", order_by="calls", limit=200, min_calls=400)
    rows = rows_as_dicts(result)
    marker = next(r for r in rows if "label = code OR code IS NULL" in r["query"])
    assert marker["calls"] >= 450
    assert marker["user"] == "postgres"
    assert marker["total_ms"] >= 0
    assert marker["mean_ms"] >= 0
    assert marker["rows"] >= 450
    assert 0 <= marker["pct_of_total_time"] <= 100
    assert marker["blocks_hit"] > 0
    assert marker["hit_pct"] is not None
    assert marker["temp_written"] == "0 bytes"
    calls = [r["calls"] for r in rows]
    assert calls == sorted(calls, reverse=True)
    assert ("wal_written" in result["columns"]) == ready.at_least(13)
    assert ("plan_ms" in result["columns"]) == ready.at_least(13)
    assert result["ordered_by"] == "calls"

    slow = rows_as_dicts(await call(pg_top_queries, connection="ro", order_by="mean_time"))
    means = [r["mean_ms"] for r in slow]
    assert means == sorted(means, reverse=True)
    most_rows = rows_as_dicts(await call(pg_top_queries, connection="ro", order_by="rows"))
    assert [r["rows"] for r in most_rows] == sorted((r["rows"] for r in most_rows), reverse=True)
    for order in ("total_time", "shared_blks_read", "temp_blks_written"):
        assert (await call(pg_top_queries, connection="ro", order_by=order, limit=3))["rows"]

    nothing = await call(pg_top_queries, connection="ro", min_calls=10**9)
    assert nothing["rows"] == []
    assert (await call(pg_top_queries, connection="ro", limit=2))["has_more"] is True


async def test_top_queries_wal_bytes_by_version(app: Any, ready: Server) -> None:
    if ready.at_least(13):
        result = await call(pg_top_queries, connection="ro", order_by="wal_bytes", limit=5)
        assert any(r["wal_written"] not in (None, "0 bytes") for r in rows_as_dicts(result))
    else:
        with pytest.raises(ToolError, match="needs PostgreSQL 13 or newer"):
            await call(pg_top_queries, connection="ro", order_by="wal_bytes")


async def test_top_queries_errors(app: Any, bare_app: Any) -> None:
    with pytest.raises(ToolError, match="CREATE EXTENSION pg_stat_statements"):
        await call(pg_top_queries, connection="bare")
    with pytest.raises(ToolError, match="order_by must be one of"):
        await call(pg_top_queries, connection="bare", order_by="nonsense")


# ------------------------------------------------------------------------- pg_table_stats


async def test_table_stats_of_the_sample_database(app: Any) -> None:
    result = await call(pg_table_stats, connection="ro", schema="shop", order_by="inserts")
    tables = by_name(result, "table")
    orders = tables["orders"]
    assert orders["inserts"] >= 6000
    assert orders["live_tuples"] >= 5000
    assert orders["size"].endswith("kB") or orders["size"].endswith("MB")
    assert orders["last_analyze"] is not None
    assert orders["cache_hit_pct"] is None or 0 <= orders["cache_hit_pct"] <= 100
    inserts = [r["inserts"] for r in rows_as_dicts(result)]
    assert inserts == sorted(inserts, reverse=True)
    assert result["counters_since"]
    assert "pg_catalog" not in {r["schema"] for r in rows_as_dicts(result)}

    one = await call(pg_table_stats, connection="ro", table="shop.customers")
    assert [r["table"] for r in rows_as_dicts(one)] == ["customers"]
    pattern = await call(pg_table_stats, connection="ro", schema="shop", table="order*")
    assert {r["table"] for r in rows_as_dicts(pattern)} == {"orders", "order_items"}
    empty = await call(pg_table_stats, connection="ro", table="no_such_table_here")
    assert empty["rows"] == []
    assert "hint" in empty
    with pytest.raises(ToolError, match="order_by must be one of"):
        await call(pg_table_stats, connection="ro", order_by="bogus")


async def test_table_stats_sees_scans_and_dead_rows(app: Any, workload: str) -> None:
    scanned = await call(pg_table_stats, connection="ro", schema=workload, order_by="seq_scan")
    tables = by_name(scanned, "table")
    assert rows_as_dicts(scanned)[0]["table"] == "scanme"
    assert tables["scanme"]["seq_scan"] >= 25
    assert tables["scanme"]["seq_tup_read"] >= 25 * 12000
    assert tables["scanme"]["idx_scan"] == 0
    assert tables["scanme"]["idx_scan_pct"] == 0
    assert tables["scanme"]["live_tuples"] >= 12000
    assert tables["scanme"]["last_analyze"] is None

    dead = await call(pg_table_stats, connection="ro", schema=workload, order_by="dead_tuples")
    bloaty = by_name(dead, "table")["bloaty"]
    assert rows_as_dicts(dead)[0]["table"] == "bloaty"
    assert bloaty["dead_tuples"] >= 15000
    assert bloaty["dead_pct"] >= 50
    assert bloaty["last_analyze"] is not None
    assert bloaty["last_autovacuum"] is None


# ------------------------------------------------------------------------ pg_index_stats


async def test_index_stats_describes_indexes(app: Any, ready: Server) -> None:
    result = await call(pg_index_stats, connection="ro", schema="shop", table="orders")
    indexes = by_name(result, "index")
    assert set(indexes) >= {
        "orders_pkey",
        "orders_customer_idx",
        "orders_customer_dup_idx",
        "orders_placed_idx",
        "orders_paid_idx",
    }
    pkey = indexes["orders_pkey"]
    assert pkey["primary"] is True
    assert pkey["unique"] is True
    assert pkey["valid"] is True
    assert pkey["method"] == "btree"
    assert pkey["definition"].startswith("CREATE UNIQUE INDEX orders_pkey ON shop.orders")
    assert pkey["scans"] > 0 or pkey["tuples_read"] is not None
    assert indexes["orders_customer_idx"]["unique"] is False
    assert ("last_scan" in result["columns"]) == ready.at_least(16)
    sizes = [indexes[name]["size"] for name in indexes]
    assert all(s.endswith(("bytes", "kB", "MB")) for s in sizes)
    gin = await call(pg_index_stats, connection="ro", schema="shop", table="products")
    assert by_name(gin, "index")["products_attrs_idx"]["method"] == "gin"
    with pytest.raises(ToolError, match="show must be one of"):
        await call(pg_index_stats, connection="ro", show="bogus")


async def test_index_stats_finds_duplicates(app: Any, workload: str) -> None:
    sample = rows_as_dicts(
        await call(pg_index_stats, connection="ro", schema="shop", show="duplicate")
    )
    found = [r for r in sample if r["table"] == "orders"]
    assert len(found) == 1
    assert {found[0]["index"], found[0]["note"].split()[3].rstrip(":")} == {
        "orders_customer_idx",
        "orders_customer_dup_idx",
    }
    assert "same definition as" in found[0]["note"]

    mine = rows_as_dicts(
        await call(pg_index_stats, connection="ro", schema=workload, show="duplicate")
    )
    assert [r["index"] for r in mine] == ["bloaty_id_pad_idx"]
    assert "bloaty_uniq" in mine[0]["note"]


async def test_index_stats_finds_redundant_unused_and_invalid(app: Any, workload: str) -> None:
    redundant = rows_as_dicts(
        await call(pg_index_stats, connection="ro", schema=workload, show="redundant")
    )
    assert [r["index"] for r in redundant] == ["bloaty_id_idx"]
    assert "leading columns of bloaty_id_pad_idx" in redundant[0]["note"]

    unused = rows_as_dicts(
        await call(pg_index_stats, connection="ro", schema=workload, show="unused")
    )
    names = {r["index"] for r in unused}
    assert {"bloaty_id_idx", "bloaty_id_pad_idx"} <= names
    assert "bloaty_uniq" not in names  # a unique index enforces a rule even when never read
    assert all(r["scans"] == 0 for r in unused)

    invalid = rows_as_dicts(
        await call(pg_index_stats, connection="ro", schema=workload, show="invalid")
    )
    assert [(r["index"], r["valid"]) for r in invalid] == [("inv_bad_idx", False)]

    everything = await call(pg_index_stats, connection="ro", schema=workload, limit=2)
    assert len(everything["rows"]) == 2
    assert everything["has_more"] is True


# ------------------------------------------------------------------------------- pg_bloat


async def test_bloat_estimate(app: Any, workload: str) -> None:
    result = await call(pg_bloat, connection="ro", schema=workload)
    table = by_name(result["tables"], "table")["bloaty"]
    assert table["wasted_pct"] >= 60
    assert table["fillfactor"] == 100
    assert table["wasted"].endswith("MB") or table["wasted"].endswith("kB")
    index = by_name(result["indexes"], "index")
    assert index["bloaty_id_idx"]["wasted_pct"] >= 40
    assert index["bloaty_id_idx"]["table"] == "bloaty"
    assert "estimates" in result["notes"][-1].lower()
    # A table that was never analyzed or vacuumed has no recorded size and is left out.
    assert "scanme" not in by_name(result["tables"], "table")

    tables_only = await call(pg_bloat, connection="ro", schema=workload, kind="tables")
    assert "indexes" not in tables_only
    indexes_only = await call(pg_bloat, connection="ro", schema=workload, kind="indexes")
    assert "tables" not in indexes_only
    nothing = await call(pg_bloat, connection="ro", schema=workload, min_wasted_bytes=10**12)
    assert nothing["tables"]["rows"] == []
    assert nothing["indexes"]["rows"] == []
    ordered = rows_as_dicts(result["tables"])
    assert len(ordered) >= 1
    with pytest.raises(ToolError, match="kind must be one of"):
        await call(pg_bloat, connection="ro", kind="bogus")


async def test_bloat_exact_measurement(app: Any, workload: str) -> None:
    result = await call(pg_bloat, connection="ro", exact_table=f"{workload}.bloaty")
    assert result["table"] == f"{workload}.bloaty"
    measured = result["measured"]
    assert measured["method"].startswith("pgstattuple")
    assert measured["dead_percent"] >= 60
    assert measured["wasted_percent"] >= 60
    assert measured["dead_tuples"] >= 15000
    indexes = by_name(result["indexes"], "index")
    assert set(indexes) >= {"bloaty_id_idx", "bloaty_id_pad_idx", "bloaty_uniq"}
    assert 0 < indexes["bloaty_id_idx"]["leaf_density_pct"] <= 100
    with pytest.raises(ToolError, match="No table"):
        await call(pg_bloat, connection="ro", exact_table="shop.nothing_like_this")
    with pytest.raises(ToolError, match="No table or materialized view"):
        await call(pg_bloat, connection="ro", exact_table="shop.order_summary")


async def test_bloat_exact_needs_the_extension(bare_app: Any) -> None:
    with pytest.raises(ToolError, match="CREATE EXTENSION pgstattuple"):
        await call(pg_bloat, connection="bare", exact_table="pg_class")
    empty = await call(pg_bloat, connection="bare")
    assert empty["tables"]["rows"] == []


# ------------------------------------------------------------------------ pg_vacuum_status


async def test_vacuum_status(app: Any, ready: Server, workload: str) -> None:
    result = await call(pg_vacuum_status, connection="ro", limit=200)
    settings = result["settings"]
    assert settings["autovacuum"] == "on"
    assert settings["autovacuum_naptime"] == "1min"
    assert settings["autovacuum_freeze_max_age"] == "200000000"
    assert ("autovacuum_vacuum_insert_threshold" in settings) == ready.at_least(13)

    due = by_name(result["tables_by_dead_rows"], "table")
    assert due["bloaty"]["due"] is True
    assert due["bloaty"]["dead_over_threshold"] > 1
    assert due["bloaty"]["autovacuum"] is False  # per-table reloption autovacuum_enabled=false
    assert due["bloaty"]["vacuum_threshold"] >= 50 + 0.2 * 1000
    assert due["bloaty"]["last_autovacuum"] is None
    assert due["bloaty"]["analyze_threshold"] >= 50

    ages = rows_as_dicts(result["tables_by_age"])
    assert ages
    assert all(a["xid_age"] >= 0 for a in ages)
    assert [a["xid_age"] for a in ages] == sorted((a["xid_age"] for a in ages), reverse=True)
    assert all(0 <= a["wraparound_pct"] <= 100 for a in ages)
    databases = by_name(result["databases_by_age"], "database")
    assert "pgharbor_test" in databases
    assert databases["pgharbor_test"]["wraparound_pct"] < 50
    assert result["autovacuum_workers"]["max"] == "3"
    assert result["vacuums_running"]["columns"][:4] == ["pid", "database", "table", "phase"]


async def test_vacuum_status_names_what_holds_back_xmin(app: Any, ready: Server) -> None:
    with session(ready, "pgh_perf_xmin") as holder:
        holder.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
        holder.execute("SELECT count(*) FROM shop.no_pk")
        holder.execute("SELECT txid_current()")
        with session(ready) as other:
            other.execute("CREATE TABLE IF NOT EXISTS public.pgh_perf_xmin_t (a int)")
            other.execute("INSERT INTO public.pgh_perf_xmin_t VALUES (1)")
            other.execute("DROP TABLE public.pgh_perf_xmin_t")
        result = await call(pg_vacuum_status, connection="ro")
        holders = rows_as_dicts(result["oldest_xmin_holders"])
        assert any(
            h["kind"] == "session" and h["id"] == str(holder.info.backend_pid) for h in holders
        )
        holder.execute("ROLLBACK")


# --------------------------------------------------------------------------------- pg_sizes


async def test_sizes_by_scope(app: Any, workload: str) -> None:
    databases = by_name(await call(pg_sizes, connection="ro", scope="databases"), "database")
    assert {"pgharbor_test", "postgres"} <= set(databases)
    assert databases["pgharbor_test"]["owner"] == "postgres"
    assert databases["pgharbor_test"]["size"].endswith("MB")

    schemas = by_name(await call(pg_sizes, connection="ro", scope="schemas"), "schema")
    assert {"shop", "vault", "events", workload} <= set(schemas)
    assert schemas["shop"]["relations"] >= 7
    assert "pg_catalog" not in schemas

    tables = await call(pg_sizes, connection="ro", scope="tables", schema="vault")
    assert tables["columns"] == [
        "schema",
        "table",
        "total",
        "data",
        "indexes",
        "toast",
        "rows_estimate",
    ]
    files = by_name(tables, "table")["files"]
    assert files["rows_estimate"] == 20000
    assert files["total"].endswith("kB") or files["total"].endswith("MB")
    assert rows_as_dicts(tables)[0]["table"] == "files"
    default = rows_as_dicts(await call(pg_sizes, connection="ro", limit=3))
    assert len(default) == 3

    indexes = by_name(
        await call(pg_sizes, connection="ro", scope="indexes", schema="shop"), "index"
    )
    assert indexes["orders_pkey"]["table"] == "orders"
    assert indexes["products_tags_idx"]["method"] == "gin"

    spaces = by_name(await call(pg_sizes, connection="ro", scope="tablespaces"), "tablespace")
    assert {"pg_default", "pg_global"} <= set(spaces)
    with pytest.raises(ToolError, match="scope must be one of"):
        await call(pg_sizes, connection="ro", scope="bogus")


async def test_sizes_has_more(app: Any) -> None:
    result = await call(pg_sizes, connection="ro", scope="tables", limit=1)
    assert len(result["rows"]) == 1
    assert result["has_more"] is True


# ----------------------------------------------------------------------------- pg_cache_io


async def test_cache_io(app: Any, ready: Server) -> None:
    result = await call(pg_cache_io, connection="ro", limit=50)
    assert 0 <= result["database"]["cache_hit_pct"] <= 100
    assert result["database"]["blocks_hit"] > 0
    assert result["settings"]["track_io_timing"] == "on"
    assert result["settings"]["shared_buffers"]
    assert set(result["io_timing_ms"]) == {"read", "write"}
    assert "count" in result["temp_files"]
    assert "size" in result["temp_files"]
    tables = by_name(result["tables"], "table")
    assert tables["orders"]["heap_hit"] > 0
    assert 0 <= tables["orders"]["heap_hit_pct"] <= 100
    indexes = by_name(result["indexes"], "index")
    assert "orders_pkey" in indexes
    assert ("pg_stat_io" in result) == ready.at_least(16)
    if ready.at_least(16):
        io = rows_as_dicts(result["pg_stat_io"])
        assert any(r["backend_type"] == "client backend" and r["object"] == "relation" for r in io)
    else:
        assert any("pg_stat_io needs PostgreSQL 16" in n for n in result["notes"])
    buffers = by_name(result["shared_buffers_by_relation"], "relation")
    assert buffers
    assert all(b["buffers"] > 0 and 0 <= b["pct_of_cache"] <= 100 for b in buffers.values())
    assert any(name.startswith(("shop.", "vault.", "pg_catalog.")) for name in buffers)


async def test_cache_io_without_buffercache(bare_app: Any) -> None:
    result = await call(pg_cache_io, connection="bare")
    assert "shared_buffers_by_relation" not in result
    assert any("CREATE EXTENSION pg_buffercache" in n for n in result["notes"])
    assert result["tables"]["columns"][0] == "schema"


# ---------------------------------------------------------------------------- pg_progress


def test_progress_views_by_version_is_documented() -> None:
    from pgharbor.tools.performance import _PROGRESS_VIEWS

    assert [name for name, _view, _pairs in _PROGRESS_VIEWS] == [
        "vacuum",
        "analyze",
        "cluster",
        "create_index",
        "basebackup",
        "copy",
        "repack",
    ]


async def test_progress_nothing_running(app: Any, ready: Server) -> None:
    result = await call(pg_progress, connection="ro")
    expected = ["vacuum"]
    if ready.at_least(12):
        expected += ["cluster", "create_index"]
    if ready.at_least(13):
        expected += ["analyze", "basebackup"]
    if ready.at_least(14):
        expected += ["copy"]
    if ready.at_least(19):
        expected += ["repack"]
    assert sorted(result["checked"]) == sorted(expected)
    missing = {"vacuum", "analyze", "cluster", "create_index", "basebackup", "copy", "repack"}
    assert set(result.get("not_on_this_version", [])) == missing - set(expected)
    if not result["operations"]:
        assert result["message"].startswith("No maintenance operation is in progress")


async def test_progress_follows_a_running_vacuum(app: Any, ready: Server) -> None:
    schema = "pgh_perf_vac"
    with scratch_schema(ready, schema):
        setup = superuser(ready)
        setup.execute(
            f"CREATE TABLE {schema}.slow (id int, pad text) WITH (autovacuum_enabled=off)"
        )
        setup.execute(
            f"INSERT INTO {schema}.slow SELECT g, repeat('y', 200) FROM generate_series(1, 30000) g"
        )
        setup.execute(f"DELETE FROM {schema}.slow WHERE id % 2 = 0")
        setup.close()

        worker = superuser(ready)
        worker.execute("SET application_name = 'pgh_perf_vacuum'")
        worker.execute("SET vacuum_cost_delay = 100")  # throttled: one page costs a 100 ms sleep
        worker.execute("SET vacuum_cost_limit = 1")
        worker_pid = worker.info.backend_pid
        done: list[BaseException] = []

        def run_vacuum() -> None:
            try:
                worker.execute(f"VACUUM {schema}.slow")
            except psycopg.Error as exc:  # cancelled by the test
                done.append(exc)

        thread = threading.Thread(target=run_vacuum)
        thread.start()
        try:
            operation: dict[str, Any] | None = None
            for _ in range(100):
                result = await call(pg_progress, connection="ro")
                operation = next((o for o in result["operations"] if o["pid"] == worker_pid), None)
                if operation:
                    break
                await asyncio.sleep(0.1)
            assert operation is not None, "the vacuum never showed up in pg_progress"
            assert operation["operation"] == "vacuum"
            assert operation["table"] == f"{schema}.slow"
            assert operation["database"] == "pgharbor_test"
            assert operation["phase"]
            assert 0 <= operation["percent"] <= 100
            assert operation["counters"]["heap_blks_total"] > 100
            assert operation["user"] == "postgres"
            assert "VACUUM" in operation["query"]

            status = await call(pg_vacuum_status, connection="ro")
            running = by_name(status["vacuums_running"], "pid")
            assert running[worker_pid]["table"] == f"{schema}.slow"
            assert running[worker_pid]["started_by"] == "manual"
        finally:
            with superuser(ready) as admin:
                admin.execute("SELECT pg_cancel_backend(%s)", [worker_pid])
            thread.join(30)
            worker.close()
        assert done, "the vacuum should have been cancelled"


# -------------------------------------------------------------------- pg_database_stats


async def test_database_stats(app: Any, ready: Server) -> None:
    result = await call(pg_database_stats, connection="ro")
    stats = result["stats"]
    assert stats["database"] == "pgharbor_test"
    assert stats["commits"] > 0
    assert stats["rows_inserted"] >= 40000
    assert 0 <= stats["rollback_pct"] <= 100
    assert 0 <= stats["cache_hit_pct"] <= 100
    assert stats["deadlocks"] >= 0
    assert stats["temp_files"] >= 0
    assert "stats_reset" in stats  # empty on PostgreSQL 15+ until someone resets the statistics
    assert stats["size"].endswith("MB")
    assert ("checksum_failures" in stats) == ready.at_least(12)
    assert ("sessions" in stats) == ready.at_least(14)
    if ready.at_least(14):
        assert stats["sessions"] >= 1
        assert stats["active_seconds"] >= 0
    notes = " ".join(result["notes"])
    assert ("PostgreSQL 12 or newer" in notes) == (not ready.at_least(12))
    assert ("PostgreSQL 14 or newer" in notes) == (not ready.at_least(14))

    everything = await call(pg_database_stats, connection="ro", all_databases=True)
    databases = by_name(everything, "database")
    assert {"pgharbor_test", "postgres"} <= set(databases)
    assert "template0" not in databases


# ------------------------------------------------------------------------------ pg_stat_view


async def test_stat_view_reads_system_views(app: Any, ready: Server) -> None:
    ssl = await call(pg_stat_view, connection="ro", view="pg_stat_ssl")
    assert {"pid", "ssl"} <= set(ssl["columns"])
    assert ssl["relation"] == "pg_catalog.pg_stat_ssl"

    prepared = await call(pg_stat_view, connection="ro", view="pg_prepared_xacts")
    assert prepared["columns"] == ["transaction", "gid", "prepared", "owner", "database"]

    functions = await call(pg_stat_view, connection="ro", view="pg_stat_user_functions")
    assert functions["columns"][:3] == ["funcid", "schemaname", "funcname"]

    tables = await call(
        pg_stat_view,
        connection="ro",
        view="information_schema.tables",
        columns=["table_schema", "table_name"],
        limit=5,
    )
    assert tables["columns"] == ["table_schema", "table_name"]
    assert len(tables["rows"]) == 5
    assert tables["has_more"] is True
    assert any("raise limit or add order_by" in n for n in tables["notes"])

    databases = await call(
        pg_stat_view,
        connection="ro",
        view="pg_stat_database",
        columns=["datname", "xact_commit"],
        order_by="xact_commit",
        limit=50,
    )
    commits = [row[1] for row in databases["rows"]]
    assert commits == sorted(commits, reverse=True)
    ascending = await call(
        pg_stat_view,
        connection="ro",
        view="pg_stat_database",
        columns=["datname", "xact_commit"],
        order_by="xact_commit",
        descending=False,
        limit=50,
    )
    low = [row[1] for row in ascending["rows"]]
    assert low == sorted(low)

    classes = await call(
        pg_stat_view, connection="ro", view="pg_catalog.pg_class", columns=["relname"], limit=1
    )
    assert classes["rows"]


async def test_stat_view_versions(app: Any, ready: Server) -> None:
    if ready.at_least(13):
        slru = await call(pg_stat_view, connection="ro", view="pg_stat_slru")
        assert "blks_hit" in slru["columns"]
    else:
        with pytest.raises(ToolError, match="No view or catalog named 'pg_stat_slru'"):
            await call(pg_stat_view, connection="ro", view="pg_stat_slru")
    if ready.at_least(14):
        wal = await call(pg_stat_view, connection="ro", view="pg_stat_wal")
        assert wal["rows"]
        assert "wal_bytes" in wal["columns"]
    else:
        with pytest.raises(ToolError, match=r"PostgreSQL 1?\d.*it may exist only in a newer"):
            await call(pg_stat_view, connection="ro", view="pg_stat_wal")


@pytest.mark.parametrize("view", ["pg_authid", "pg_shadow", "pg_catalog.pg_authid"])
async def test_stat_view_refuses_catalogs_with_credentials(app: Any, view: str) -> None:
    with pytest.raises(ToolError, match="holds credentials"):
        await call(pg_stat_view, connection="ro", view=view)


async def test_stat_view_refuses_other_secret_catalogs(app: Any) -> None:
    for view in ("pg_user_mapping", "pg_statistic"):
        with pytest.raises(ToolError, match="Refused"):
            await call(pg_stat_view, connection="ro", view=view)


async def test_stat_view_error_messages(app: Any) -> None:
    with pytest.raises(ToolError, match=r"Unknown column 'bogus'.*Available columns: .*pid"):
        await call(pg_stat_view, connection="ro", view="pg_stat_activity", columns=["pid", "bogus"])
    with pytest.raises(ToolError, match="Unknown order_by column 'nope'"):
        await call(pg_stat_view, connection="ro", view="pg_stat_activity", order_by="nope")
    with pytest.raises(ToolError, match="outside pg_catalog and information_schema"):
        await call(pg_stat_view, connection="ro", view="shop.orders")
    with pytest.raises(ToolError, match=r"not a system view.*pg_top_queries"):
        await call(pg_stat_view, connection="ro", view="pg_stat_statements")
    with pytest.raises(ToolError, match=r"Similar names: .*pg_stat_database"):
        await call(pg_stat_view, connection="ro", view="pg_stat_databse")
    with pytest.raises(ToolError, match="Invalid object name"):
        await call(pg_stat_view, connection="ro", view="a.b.c")


async def test_stat_view_quotes_what_it_cannot_trust(app: Any) -> None:
    with pytest.raises(ToolError, match="Invalid object name"):
        await call(pg_stat_view, connection="ro", view='pg_stat_activity" ; DROP TABLE x; --')
    with pytest.raises(ToolError, match="Invalid object name"):
        await call(pg_stat_view, connection="ro", view="pg_stat_activity; DROP TABLE x")
    with pytest.raises(ToolError, match="Unknown column"):
        await call(
            pg_stat_view,
            connection="ro",
            view="pg_stat_activity",
            columns=['pid" FROM pg_authid --'],
        )
