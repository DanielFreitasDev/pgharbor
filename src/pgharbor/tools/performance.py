"""Performance diagnostics: sessions, locks, slow queries, table and index statistics,
bloat, vacuum, sizes, cache, progress of long operations and a reader for any system view.

Nothing here writes. Every statistic comes from the catalogs and ``pg_stat_*`` views, so
the queries are cheap; the few that look at user data (``pgstattuple``) run only when the
caller names the table.
"""

from collections.abc import Awaitable, Callable
from difflib import get_close_matches
from functools import partial
from typing import Any, Literal, TypeVar

import psycopg

from ..app import get_app, tool
from ..errors import PgHarborError
from ..guard import quote_ident
from ..runner import run_query
from ..session import Db, describe_db_error
from ._util import (
    USER_SCHEMA_FILTER,
    clamp,
    grid,
    like_pattern,
    page,
    resolve_relation,
    split_qualified,
)

__all__ = [
    "pg_activity",
    "pg_bloat",
    "pg_cache_io",
    "pg_database_stats",
    "pg_index_stats",
    "pg_locks",
    "pg_progress",
    "pg_sizes",
    "pg_stat_view",
    "pg_table_stats",
    "pg_top_queries",
    "pg_vacuum_status",
]

T = TypeVar("T")

_QUERY_CHARS = 300
_WRAPAROUND = 2147483648  # transaction ids and multixact ids wrap around at 2^31


# --------------------------------------------------------------------------- shared helpers
# The helpers below are also used by tools/health.py.


def secs(expression: str) -> str:
    """SQL: seconds elapsed since a timestamp expression, one decimal."""
    return f"round(extract(epoch FROM (pg_catalog.clock_timestamp() - {expression}))::numeric, 1)"


def clean_query(expression: str, chars: int = _QUERY_CHARS) -> str:
    """SQL: a query text on one line, cut to ``chars`` characters."""
    return f"left(pg_catalog.regexp_replace({expression}, '\\s+', ' ', 'g'), {chars})"


async def attempt(db: Db, step: Callable[[], Awaitable[T]]) -> tuple[T | None, str | None]:
    """Run ``step`` inside a savepoint so that a failing statement cannot abort the
    transaction. Returns ``(result, None)`` or ``(None, reason)``."""
    await db.rows("SAVEPOINT pgharbor_step")
    try:
        result = await step()
    except psycopg.Error as exc:
        await db.rows("ROLLBACK TO SAVEPOINT pgharbor_step")
        await db.rows("RELEASE SAVEPOINT pgharbor_step")
        return None, describe_db_error(exc, db.profile)
    except Exception as exc:
        await db.rows("ROLLBACK TO SAVEPOINT pgharbor_step")
        await db.rows("RELEASE SAVEPOINT pgharbor_step")
        return None, str(exc) or type(exc).__name__
    await db.rows("RELEASE SAVEPOINT pgharbor_step")
    return result, None


async def visibility_note(db: Db) -> str | None:
    """A note when the role cannot see other sessions' details or server-wide statistics."""
    if db.facts.is_superuser:
        return None
    if db.v >= 100000:
        member = await db.value(
            "SELECT pg_catalog.pg_has_role(current_user, 'pg_read_all_stats', 'MEMBER')"
        )
        if member:
            return None
    return (
        f"Role {db.facts.user} is not a superuser or a member of pg_read_all_stats (pg_monitor): "
        "PostgreSQL hides the query text and details of other users' sessions, so some values "
        "below are empty or marked as insufficient privilege."
    )


def size_text(value: float | int | None) -> str | None:
    """Bytes as text, the way ``pg_size_pretty`` prints them."""
    if value is None:
        return None
    number = float(value)
    for unit in ("bytes", "kB", "MB", "GB", "TB", "PB"):
        if abs(number) < 10 * 1024 or unit == "PB":
            return f"{round(number)} {unit}"
        number /= 1024
    return None  # pragma: no cover


_MEMORY_UNITS = {
    "B": 1,
    "kB": 1024,
    "8kB": 8192,
    "16MB": 16 * 1024**2,
    "MB": 1024**2,
    "GB": 1024**3,
    "TB": 1024**4,
}
_TIME_UNITS = {"us": 0.001, "ms": 1, "s": 1000, "min": 60_000, "h": 3_600_000, "d": 86_400_000}


def format_setting(value: str | None, unit: str | None) -> str | None:
    """A ``pg_settings`` value with its unit in the form the server itself prints
    (``8kB`` blocks become ``128MB``, milliseconds become ``1min``)."""
    if value is None or not unit:
        return value
    try:
        number = float(value)
    except ValueError:
        return value
    if number < 0:
        return value
    if unit in _MEMORY_UNITS and number == int(number):
        total = int(number) * _MEMORY_UNITS[unit]
        if total == 0:
            return "0"
        for name in ("TB", "GB", "MB", "kB", "B"):
            if total % _MEMORY_UNITS[name] == 0:
                return f"{total // _MEMORY_UNITS[name]}{name}"
    if unit in _TIME_UNITS:
        milliseconds = number * _TIME_UNITS[unit]
        if milliseconds == int(milliseconds):
            milliseconds = int(milliseconds)
            if milliseconds == 0:
                return "0"
            for name in ("d", "h", "min", "s", "ms"):
                if milliseconds % _TIME_UNITS[name] == 0:
                    return f"{int(milliseconds // _TIME_UNITS[name])}{name}"
    return f"{value}{unit}"


def name_filters(value: str | None) -> tuple[str | None, str | None]:
    """Split a name argument into (exact name, LIKE pattern); at most one is set."""
    if value is None or value == "":
        return None, None
    if any(ch in value for ch in "*?%"):
        return None, like_pattern(value)
    return value, None


def schema_and_table(schema: str | None, table: str | None) -> tuple[str | None, str | None]:
    """Let ``table`` carry its own schema (``shop.orders``, ``shop.order*``) when ``schema``
    is not given."""
    if not table or "." not in table:
        return schema, table
    head, _, rest = table.partition(".")
    if any(ch in head for ch in "*?%"):
        return schema, table
    if any(ch in rest for ch in "*?%"):
        return schema or head, rest
    found, name = split_qualified(table)
    return schema or found, name


async def stats_reset_text(db: Db) -> str:
    """When the statistics were last reset, or ``never reset`` (PostgreSQL 15+ keeps them
    across clean restarts and leaves the date empty until someone resets them)."""
    value = await db.value(
        "SELECT date_trunc('second', stats_reset)::text FROM pg_catalog.pg_stat_database "
        "WHERE datname = current_database()"
    )
    return str(value) if value else "never reset"


def check_choice(name: str, value: str, allowed: dict[str, Any] | tuple[str, ...]) -> None:
    """Direct callers are not validated by the MCP schema: refuse unknown values clearly."""
    if value not in allowed:
        raise PgHarborError(f"{name} must be one of {', '.join(allowed)}; got {value!r}.")


def _pct(part: float, whole: float) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


# ---------------------------------------------------------------------------- pg_activity

_STATES = Literal[
    "active",
    "idle",
    "idle in transaction",
    "idle in transaction (aborted)",
    "fastpath function call",
    "disabled",
]


@tool(group="performance", title="Session activity")
async def pg_activity(
    connection: str | None = None,
    state: _STATES | None = None,
    min_duration_seconds: float | None = None,
    include_idle: bool = False,
    database: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Show who is connected right now: sessions from pg_stat_activity with user, database,
    application, client, state, wait event, how long the transaction, query and state have
    lasted, the query text, which sessions block them (`blocked_by`), plus a summary by state
    and the use of max_connections.

    Use it to find long-running queries, sessions stuck "idle in transaction" and blocked
    sessions. Idle sessions are hidden unless `include_idle` is true or `state="idle"` is
    given. `min_duration_seconds` keeps sessions whose transaction or current state is at
    least that old. Your own session is never listed. For the lock tree use pg_locks.
    """
    limit = clamp(limit, 50, 500)
    async with get_app().read(connection) as db:
        client_filter = (
            "a.backend_type = 'client backend'" if db.v >= 100000 else "a.datid IS NOT NULL"
        )
        extra = ""
        if db.v >= 100000:
            extra += ", a.backend_type"
        if db.v >= 130000:
            extra += ", a.leader_pid"
        if db.v >= 140000:
            extra += ", a.query_id::text AS query_id"
        rows = await db.rows(
            f"""
            SELECT a.pid, a.usename AS "user", a.datname AS database,
                   a.application_name AS application, a.client_addr::text AS client,
                   a.state, a.wait_event_type, a.wait_event{extra},
                   {secs("a.xact_start")} AS xact_seconds,
                   {secs("a.query_start")} AS query_seconds,
                   {secs("a.state_change")} AS state_seconds,
                   CASE WHEN coalesce(array_length(pg_catalog.pg_blocking_pids(a.pid), 1), 0) > 0
                        THEN pg_catalog.pg_blocking_pids(a.pid) END AS blocked_by,
                   {clean_query("a.query")} AS query
              FROM pg_catalog.pg_stat_activity a
             WHERE a.pid <> pg_catalog.pg_backend_pid()
               AND ($1::text IS NULL OR a.state = $1)
               AND ($2::text IS NULL OR a.datname = $2)
               AND ($3::float8 IS NULL OR greatest(
                       extract(epoch FROM pg_catalog.clock_timestamp() - a.xact_start),
                       extract(epoch FROM pg_catalog.clock_timestamp() - a.state_change)) >= $3)
               AND ($4::bool OR $1::text IS NOT NULL
                    OR (a.state IS NOT NULL AND a.state <> 'idle'))
             ORDER BY coalesce(a.xact_start, a.state_change) NULLS LAST, a.pid
             LIMIT $5
            """,
            [state, database, min_duration_seconds, include_idle, limit + 1],
        )
        rows, has_more = page(rows, limit)
        by_state = await db.rows(
            """
            SELECT coalesce(state, '(no state)') AS state, count(*) AS sessions
              FROM pg_catalog.pg_stat_activity
             WHERE pid <> pg_catalog.pg_backend_pid()
             GROUP BY 1 ORDER BY 2 DESC, 1
            """
        )
        usage = await db.row(
            f"""
            SELECT count(*) FILTER (WHERE {client_filter}) AS used,
                   pg_catalog.current_setting('max_connections')::int AS max,
                   pg_catalog.current_setting('superuser_reserved_connections')::int AS reserved
              FROM pg_catalog.pg_stat_activity a
            """
        )
        note = await visibility_note(db)
    assert usage is not None
    out: dict[str, Any] = {
        "connection": db.profile.name,
        "connections": {
            "used": usage["used"],
            "max": usage["max"],
            "percent": _pct(usage["used"], usage["max"]),
            "reserved_for_superusers": usage["reserved"],
        },
        "by_state": {r["state"]: r["sessions"] for r in by_state},
        **grid(rows),
        "has_more": has_more,
    }
    if not rows:
        out["message"] = "No session matches (idle sessions are hidden unless include_idle=true)."
    if note:
        out["notes"] = [note]
    return out


# ------------------------------------------------------------------------------- pg_locks

_LOCK_OBJECT = """
CASE l.locktype
     WHEN 'relation' THEN l.relation::pg_catalog.regclass::text
     WHEN 'transactionid' THEN 'transaction ' || l.transactionid::text
     WHEN 'tuple' THEN l.relation::pg_catalog.regclass::text
                       || ' (row ' || l.page::text || ',' || l.tuple::text || ')'
     ELSE l.locktype END
"""


@tool(group="performance", title="Locks and blocking")
async def pg_locks(
    connection: str | None = None, only_blocked: bool = True, limit: int = 50
) -> dict[str, Any]:
    """Show lock waits as a blocking tree: which sessions wait, for how long and for which
    lock or table, who holds it (pid, state, query, how long its transaction has been open),
    the root blockers with how many sessions each one blocks, the strong
    (ACCESS EXCLUSIVE) locks currently granted on user tables, and a count of locks by mode.

    Use it when queries hang or `pg_activity` shows `blocked_by`. The wait time is counted
    from the start of the waiting statement, so it is an upper bound. With
    `only_blocked=false` it also lists the table locks every session holds or wants.
    """
    limit = clamp(limit, 50, 500)
    async with get_app().read(connection) as db:
        edges = await db.rows(
            f"""
            SELECT w.pid AS waiting_pid, w.usename AS waiting_user,
                   {secs("w.query_start")} AS waiting_seconds,
                   {clean_query("w.query", 200)} AS waiting_query,
                   l.locktype AS lock_type, l.mode AS lock_mode, l.object,
                   b.pid AS blocker_pid, b.usename AS blocker_user, b.state AS blocker_state,
                   {secs("b.xact_start")} AS blocker_xact_seconds,
                   {clean_query("b.query", 200)} AS blocker_query
              FROM pg_catalog.pg_stat_activity w
             CROSS JOIN LATERAL unnest(pg_catalog.pg_blocking_pids(w.pid)) AS bp(pid)
              LEFT JOIN pg_catalog.pg_stat_activity b ON b.pid = bp.pid
              LEFT JOIN LATERAL (
                    SELECT l.locktype, l.mode, {_LOCK_OBJECT} AS object
                      FROM pg_catalog.pg_locks l
                     WHERE l.pid = w.pid AND NOT l.granted
                     LIMIT 1) l ON true
             WHERE w.pid <> pg_catalog.pg_backend_pid()
             ORDER BY w.query_start NULLS LAST, w.pid, b.pid
             LIMIT 1000
            """
        )
        waiting = {e["waiting_pid"] for e in edges}
        blockers = {e["blocker_pid"] for e in edges}
        root_pids = sorted(blockers - waiting)
        roots: list[dict[str, Any]] = []
        if root_pids:
            held = await db.rows(
                """
                SELECT DISTINCT l.pid, c.relname AS name, n.nspname AS schema, l.mode
                  FROM pg_catalog.pg_locks l
                  JOIN pg_catalog.pg_class c ON c.oid = l.relation
                  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                 WHERE l.pid = ANY($1::int[]) AND l.granted AND l.locktype = 'relation'
                   AND c.relkind IN ('r', 'p', 'm')
                   AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                 ORDER BY l.pid, 2
                """,
                [root_pids],
            )
            children: dict[int, set[int]] = {}
            for e in edges:
                children.setdefault(e["blocker_pid"], set()).add(e["waiting_pid"])
            for pid in root_pids:
                seen: set[int] = set()
                todo = [pid]
                while todo:
                    for child in children.get(todo.pop(), ()):
                        if child not in seen:
                            seen.add(child)
                            todo.append(child)
                sample = next(e for e in edges if e["blocker_pid"] == pid)
                roots.append(
                    {
                        "pid": pid,
                        "user": sample["blocker_user"],
                        "state": sample["blocker_state"],
                        "xact_seconds": sample["blocker_xact_seconds"],
                        "query": sample["blocker_query"],
                        "blocked_sessions": len(seen),
                        "holds": [
                            f"{h['mode']} on {h['schema']}.{h['name']}"
                            for h in held
                            if h["pid"] == pid
                        ][:6],
                    }
                )
            roots.sort(key=lambda r: -r["blocked_sessions"])
        strong = await db.rows(
            f"""
            SELECT l.pid, n.nspname || '.' || c.relname AS "table", a.usename AS "user",
                   a.state, {secs("a.xact_start")} AS xact_seconds,
                   {clean_query("a.query", 200)} AS query
              FROM pg_catalog.pg_locks l
              JOIN pg_catalog.pg_class c ON c.oid = l.relation
              JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
              LEFT JOIN pg_catalog.pg_stat_activity a ON a.pid = l.pid
             WHERE l.locktype = 'relation' AND l.mode = 'AccessExclusiveLock' AND l.granted
               AND l.pid <> pg_catalog.pg_backend_pid()
               AND n.nspname NOT IN ('pg_catalog', 'information_schema')
               AND n.nspname !~ '^pg_toast'
             ORDER BY a.xact_start NULLS LAST, l.pid
             LIMIT $1
            """,
            [limit],
        )
        modes = await db.rows(
            """
            SELECT mode, count(*) FILTER (WHERE granted) AS granted,
                   count(*) FILTER (WHERE NOT granted) AS waiting
              FROM pg_catalog.pg_locks
             WHERE pid <> pg_catalog.pg_backend_pid() AND locktype <> 'virtualxid'
             GROUP BY mode ORDER BY 2 DESC, 1
            """
        )
        everything: list[dict[str, Any]] = []
        if not only_blocked:
            everything = await db.rows(
                f"""
                SELECT l.pid, a.usename AS "user", a.state, l.mode, l.granted,
                       n.nspname || '.' || c.relname AS "table",
                       {secs("a.xact_start")} AS xact_seconds
                  FROM pg_catalog.pg_locks l
                  JOIN pg_catalog.pg_class c ON c.oid = l.relation
                  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                  LEFT JOIN pg_catalog.pg_stat_activity a ON a.pid = l.pid
                 WHERE l.locktype = 'relation' AND l.pid <> pg_catalog.pg_backend_pid()
                   AND c.relkind IN ('r', 'p', 'm')
                   AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                 ORDER BY l.granted, a.xact_start NULLS LAST, l.pid
                 LIMIT $1
                """,
                [limit + 1],
            )
        note = await visibility_note(db)
    out: dict[str, Any] = {"connection": db.profile.name, "blocked_sessions": len(waiting)}
    if edges:
        out["root_blockers"] = roots
        out["waits"] = grid(
            edges[:limit],
            [
                "waiting_pid",
                "waiting_user",
                "waiting_seconds",
                "waiting_query",
                "lock_type",
                "lock_mode",
                "object",
                "blocker_pid",
                "blocker_user",
                "blocker_state",
                "blocker_xact_seconds",
                "blocker_query",
            ],
        )
        if len(edges) > limit:
            out["has_more"] = True
    else:
        out["message"] = "No session is waiting for a lock."
    out["strong_locks"] = grid(strong, ["pid", "table", "user", "state", "xact_seconds", "query"])
    out["lock_modes"] = grid(modes, ["mode", "granted", "waiting"])
    if not only_blocked:
        shown, more = page(everything, limit)
        out["table_locks"] = grid(
            shown, ["pid", "user", "state", "mode", "granted", "table", "xact_seconds"]
        )
        if more:
            out["table_locks_has_more"] = True
    if note:
        out["notes"] = [note]
    return out


# -------------------------------------------------------------------------- pg_top_queries

_QUERY_ORDER = Literal[
    "total_time",
    "mean_time",
    "calls",
    "rows",
    "shared_blks_read",
    "temp_blks_written",
    "wal_bytes",
]
_QUERY_ORDER_SQL = {
    "total_time": "total_ms",
    "mean_time": "mean_ms",
    "calls": "calls",
    "rows": "rows",
    "shared_blks_read": "blocks_read",
    "temp_blks_written": "temp_blocks",
    "wal_bytes": "wal_bytes",
}


@tool(group="performance", title="Top queries")
async def pg_top_queries(
    connection: str | None = None,
    order_by: _QUERY_ORDER = "total_time",
    limit: int = 20,
    min_calls: int = 1,
) -> dict[str, Any]:
    """Rank the queries of the current database from pg_stat_statements: calls, total and
    mean time, rows, share of the total time, blocks read and hit, temporary file volume,
    WAL generated and planning time (WAL and planning on PostgreSQL 13+).

    Use it to find what is slow or expensive. `order_by` is one of total_time, mean_time,
    calls, rows, shared_blks_read, temp_blks_written or wal_bytes. `min_calls` ignores
    statements that ran fewer times. Needs the pg_stat_statements extension. The query text is
    normalized (constants replaced by $n) and cut; pass it to pg_explain to see the plan.
    """
    check_choice("order_by", order_by, _QUERY_ORDER_SQL)
    limit = clamp(limit, 20, 200)
    min_calls = max(1, int(min_calls))
    async with get_app().read(connection) as db:
        extension = db.need_ext("pg_stat_statements")
        if order_by == "wal_bytes":
            db.need_version(130000, "WAL volume per statement (pg_stat_statements wal_bytes)")
        view = f"{quote_ident(extension.schema)}.pg_stat_statements"
        present = {
            r["attname"]
            for r in await db.rows(
                "SELECT attname FROM pg_catalog.pg_attribute "
                "WHERE attrelid = $1::text::pg_catalog.regclass AND attnum > 0 "
                "AND NOT attisdropped",
                [view],
            )
        }
        new_names = (
            "total_exec_time" in present
        )  # renamed in pg_stat_statements 1.8 (PostgreSQL 13)
        total = "total_exec_time" if new_names else "total_time"
        mean = "mean_exec_time" if new_names else "mean_time"
        maximum = "max_exec_time" if new_names else "max_time"
        wal = "s.wal_bytes" if "wal_bytes" in present else "NULL::numeric"
        plan = "round(s.total_plan_time::numeric, 1)" if "total_plan_time" in present else "NULL"
        wal_text = (
            "pg_catalog.pg_size_pretty(s.wal_bytes)" if "wal_bytes" in present else "NULL::text"
        )
        try:
            rows = await db.rows(
                f"""
                SELECT query_id, "user", calls, total_ms, mean_ms, max_ms, rows,
                       pct_of_total_time, blocks_read, blocks_hit, hit_pct, temp_written,
                       wal_written, plan_ms, query
                  FROM (
                    SELECT s.queryid::text AS query_id,
                           pg_catalog.pg_get_userbyid(s.userid) AS "user", s.calls,
                           round(s.{total}::numeric, 1) AS total_ms,
                           round(s.{mean}::numeric, 2) AS mean_ms,
                           round(s.{maximum}::numeric, 1) AS max_ms, s.rows,
                           round((100 * s.{total} / nullif(sum(s.{total}) OVER (), 0))::numeric, 1)
                               AS pct_of_total_time,
                           s.shared_blks_read AS blocks_read, s.shared_blks_hit AS blocks_hit,
                           round(100.0 * s.shared_blks_hit
                                 / nullif(s.shared_blks_hit + s.shared_blks_read, 0), 1) AS hit_pct,
                           s.temp_blks_written AS temp_blocks,
                           pg_catalog.pg_size_pretty(
                               s.temp_blks_written
                               * pg_catalog.current_setting('block_size')::bigint
                           ) AS temp_written,
                           {wal} AS wal_bytes, {wal_text} AS wal_written, {plan} AS plan_ms,
                           {clean_query("s.query")} AS query
                      FROM {view} s
                     WHERE s.dbid = (SELECT oid FROM pg_catalog.pg_database
                                      WHERE datname = pg_catalog.current_database())
                       AND s.calls >= $1
                  ) ranked
                 ORDER BY {_QUERY_ORDER_SQL[order_by]} DESC NULLS LAST
                 LIMIT $2
                """,
                [min_calls, limit + 1],
            )
        except psycopg.errors.ObjectNotInPrerequisiteState:
            raise PgHarborError(
                "pg_stat_statements is installed but not loaded. Add it to "
                "shared_preload_libraries in postgresql.conf and restart the server; "
                "until then it collects nothing."
            ) from None
        rows, has_more = page(rows, limit)
        note = await visibility_note(db)
    columns = [
        "query_id",
        "user",
        "calls",
        "total_ms",
        "mean_ms",
        "max_ms",
        "rows",
        "pct_of_total_time",
        "blocks_read",
        "blocks_hit",
        "hit_pct",
        "temp_written",
    ]
    if "wal_bytes" in present:
        columns.append("wal_written")
    if "total_plan_time" in present:
        columns.append("plan_ms")
    columns.append("query")
    out: dict[str, Any] = {
        "connection": db.profile.name,
        "ordered_by": order_by,
        **grid(rows, columns),
        "has_more": has_more,
        "notes": [
            "Only statements of the current database. Times are in milliseconds and "
            "accumulate since the statistics were last reset (pg_stat_statements_reset)."
        ],
    }
    if note:
        out["notes"].append(note)
    return out


# ------------------------------------------------------------------------- pg_table_stats

_TABLE_ORDER = Literal[
    "seq_scan",
    "seq_tup_read",
    "idx_scan",
    "live_tuples",
    "dead_tuples",
    "dead_percent",
    "modified_since_analyze",
    "inserts",
    "updates",
    "deletes",
    "cache_hit",
    "size",
]
_TABLE_ORDER_SQL = {
    "seq_scan": "seq_scan DESC",
    "seq_tup_read": "seq_tup_read DESC",
    "idx_scan": "idx_scan DESC",
    "live_tuples": "live_tuples DESC",
    "dead_tuples": "dead_tuples DESC",
    "dead_percent": "dead_pct DESC",
    "modified_since_analyze": "modified_since_analyze DESC",
    "inserts": "inserts DESC",
    "updates": "updates DESC",
    "deletes": "deletes DESC",
    "cache_hit": "cache_hit_pct ASC",
    "size": "bytes DESC",
}


@tool(group="performance", title="Table statistics")
async def pg_table_stats(
    connection: str | None = None,
    schema: str | None = None,
    table: str | None = None,
    order_by: _TABLE_ORDER = "seq_scan",
    limit: int = 50,
) -> dict[str, Any]:
    """Show per-table activity from pg_stat_user_tables: sequential and index scans, rows
    inserted, updated (and HOT updated) and deleted, live and dead rows, rows changed since
    the last analyze, last vacuum/autovacuum/analyze/autoanalyze and the cache hit ratio.

    Use it to find tables read by sequential scans, tables full of dead rows and tables
    that autovacuum has not touched. Filter with `schema` and `table` (name, `schema.name`,
    or a pattern with `*`). `order_by` picks the ranking; `cache_hit` lists the worst first.
    Counters accumulate since the statistics were reset (shown as `counters_since`).
    """
    check_choice("order_by", order_by, _TABLE_ORDER_SQL)
    limit = clamp(limit, 50, 500)
    schema, table = schema_and_table(schema, table)
    exact, like = name_filters(table)
    async with get_app().read(connection) as db:
        rows = await db.rows(
            f"""
            SELECT * FROM (
              SELECT n.nspname AS schema, c.relname AS "table",
                     pg_catalog.pg_total_relation_size(c.oid) AS bytes,
                     s.seq_scan, s.seq_tup_read, coalesce(s.idx_scan, 0) AS idx_scan,
                     round(100.0 * coalesce(s.idx_scan, 0)
                           / nullif(s.seq_scan + coalesce(s.idx_scan, 0), 0), 1) AS idx_scan_pct,
                     s.n_tup_ins AS inserts, s.n_tup_upd AS updates, s.n_tup_del AS deletes,
                     s.n_tup_hot_upd AS hot_updates,
                     s.n_live_tup AS live_tuples, s.n_dead_tup AS dead_tuples,
                     round(100.0 * s.n_dead_tup / nullif(s.n_live_tup + s.n_dead_tup, 0), 1)
                         AS dead_pct,
                     s.n_mod_since_analyze AS modified_since_analyze,
                     date_trunc('second', s.last_vacuum)::text AS last_vacuum,
                     date_trunc('second', s.last_autovacuum)::text AS last_autovacuum,
                     date_trunc('second', s.last_analyze)::text AS last_analyze,
                     date_trunc('second', s.last_autoanalyze)::text AS last_autoanalyze,
                     round(100.0 * io.heap_blks_hit
                           / nullif(io.heap_blks_hit + io.heap_blks_read, 0), 1) AS cache_hit_pct
                FROM pg_catalog.pg_stat_all_tables s
                JOIN pg_catalog.pg_class c ON c.oid = s.relid
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN pg_catalog.pg_statio_all_tables io ON io.relid = s.relid
               WHERE c.relkind IN ('r', 'm', 't')
                 AND ($1::text IS NULL OR n.nspname = $1)
                 AND ($1::text IS NOT NULL OR ({USER_SCHEMA_FILTER}))
                 AND n.nspname !~ '^pg_toast'
                 AND ($2::text IS NULL OR c.relname = $2)
                 AND ($3::text IS NULL OR c.relname LIKE $3)
            ) t
            ORDER BY {_TABLE_ORDER_SQL[order_by]} NULLS LAST, schema, "table"
            LIMIT $4
            """,
            [schema, exact, like, limit + 1],
        )
        rows, has_more = page(rows, limit)
        sizes = {r["bytes"]: size_text(r["bytes"]) for r in rows}
        since = await stats_reset_text(db)
    for row in rows:
        row["size"] = sizes[row.pop("bytes")]
    columns = [
        "schema",
        "table",
        "size",
        "seq_scan",
        "seq_tup_read",
        "idx_scan",
        "idx_scan_pct",
        "inserts",
        "updates",
        "deletes",
        "hot_updates",
        "live_tuples",
        "dead_tuples",
        "dead_pct",
        "modified_since_analyze",
        "last_vacuum",
        "last_autovacuum",
        "last_analyze",
        "last_autoanalyze",
        "cache_hit_pct",
    ]
    out: dict[str, Any] = {"connection": db.profile.name, "ordered_by": order_by}
    if since:
        out["counters_since"] = since
    out.update(grid(rows, columns))
    out["has_more"] = has_more
    if not rows:
        out["hint"] = "No table matched. pg_list_tables shows the tables; check the schema name."
    return out


# ------------------------------------------------------------------------ pg_index_stats

_INDEX_SHOW = Literal["all", "unused", "duplicate", "redundant", "invalid"]
_INDEX_SCAN_CAP = 20000


def _index_signature_rows(rows: list[dict[str, Any]]) -> dict[Any, list[dict[str, Any]]]:
    by_table: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        by_table.setdefault(row["table_oid"], []).append(row)
    return by_table


def find_duplicate_indexes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Indexes that repeat another index of the same table: same method, columns,
    operator classes, collations, options, expressions and predicate. The one that is
    kept (primary key first, then unique, then constraint-backed) is named in `of`."""
    groups: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        key = (
            row["table_oid"],
            row["method"],
            row["key_cols"],
            row["opclasses"],
            row["collations"],
            row["options"],
            row["exprs"],
            row["pred"],
        )
        groups.setdefault(key, []).append(row)
    found: list[dict[str, Any]] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(
            key=lambda r: (not r["primary"], not r["unique"], not r["backs_constraint"], r["index"])
        )
        keeper = members[0]
        for extra in members[1:]:
            found.append({**extra, "of": keeper["index"]})
    return found


def _redundant_indexes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Plain btree indexes whose columns are the leading columns of a longer index."""
    found: list[dict[str, Any]] = []
    for members in _index_signature_rows(rows).values():
        for a in members:
            if (
                a["method"] != "btree"
                or a["unique"]
                or a["primary"]
                or a["backs_constraint"]
                or not a["valid"]
                or a["exprs"]
                or a["natts"] != a["key_count"]
            ):
                continue
            ka = a["key_count"]
            candidates = []
            for b in members:
                if b is a or b["method"] != "btree" or not b["valid"] or b["exprs"]:
                    continue
                if b["pred"] != a["pred"] or b["key_count"] <= ka:
                    continue
                same_prefix = all(
                    a[field].split()[:ka] == b[field].split()[:ka]
                    for field in ("key_cols", "opclasses", "collations", "options")
                )
                if same_prefix:
                    candidates.append(b["index"])
            if candidates:
                found.append({**a, "of": sorted(candidates)[0]})
    return found


@tool(group="performance", title="Index statistics")
async def pg_index_stats(
    connection: str | None = None,
    schema: str | None = None,
    table: str | None = None,
    show: _INDEX_SHOW = "all",
    limit: int = 50,
) -> dict[str, Any]:
    """List indexes with size, scans, tuples read and fetched, unique/primary/valid flags,
    access method and definition, and find the ones to drop: `show="unused"` (never scanned
    and not backing a constraint), `"duplicate"` (same columns, operator classes and
    predicate as another index), `"redundant"` (columns are the leading columns of a longer
    index) or `"invalid"` (a failed CREATE INDEX CONCURRENTLY).

    Filter with `schema` and `table` (name, `schema.name` or a pattern with `*`). "Unused"
    counts scans since the statistics reset (`counters_since`) on this server only: an index
    used just on a replica or by a rare job looks unused here, so check before dropping.
    """
    check_choice("show", show, ("all", "unused", "duplicate", "redundant", "invalid"))
    limit = clamp(limit, 50, 500)
    schema, table = schema_and_table(schema, table)
    exact, like = name_filters(table)
    async with get_app().read(connection) as db:
        key_count = "i.indnkeyatts" if db.v >= 110000 else "i.indnatts"
        last_scan = ", s.last_idx_scan::text AS last_scan" if db.v >= 160000 else ""
        condition = {
            "all": "true",
            "unused": "coalesce(s.idx_scan, 0) = 0 AND i.indisvalid AND NOT i.indisunique "
            "AND NOT i.indisprimary AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint k "
            "WHERE k.conindid = i.indexrelid)",
            "invalid": "NOT i.indisvalid",
            "duplicate": "true",
            "redundant": "true",
        }[show]
        scan_all = show in ("duplicate", "redundant")
        rows = await db.rows(
            f"""
            SELECT n.nspname AS schema, t.relname AS "table", ic.relname AS index,
                   am.amname AS method, pg_catalog.pg_relation_size(i.indexrelid) AS bytes,
                   coalesce(s.idx_scan, 0) AS scans, s.idx_tup_read AS tuples_read,
                   s.idx_tup_fetch AS tuples_fetched{last_scan},
                   i.indisunique AS "unique", i.indisprimary AS "primary", i.indisvalid AS valid,
                   EXISTS (SELECT 1 FROM pg_catalog.pg_constraint k
                            WHERE k.conindid = i.indexrelid) AS backs_constraint,
                   pg_catalog.pg_get_indexdef(i.indexrelid) AS definition,
                   i.indrelid::bigint AS table_oid, i.indkey::text AS key_cols,
                   i.indclass::text AS opclasses, i.indcollation::text AS collations,
                   i.indoption::text AS options,
                   coalesce(pg_catalog.pg_get_expr(i.indexprs, i.indrelid), '') AS exprs,
                   coalesce(pg_catalog.pg_get_expr(i.indpred, i.indrelid), '') AS pred,
                   {key_count}::int AS key_count, i.indnatts::int AS natts
              FROM pg_catalog.pg_index i
              JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid AND ic.relkind = 'i'
              JOIN pg_catalog.pg_class t ON t.oid = i.indrelid
              JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace
              JOIN pg_catalog.pg_am am ON am.oid = ic.relam
              LEFT JOIN pg_catalog.pg_stat_all_indexes s ON s.indexrelid = i.indexrelid
             WHERE {condition}
               AND ($1::text IS NULL OR n.nspname = $1)
               AND ($1::text IS NOT NULL OR ({USER_SCHEMA_FILTER}))
               AND n.nspname !~ '^pg_toast'
               AND ($2::text IS NULL OR t.relname = $2)
               AND ($3::text IS NULL OR t.relname LIKE $3)
             ORDER BY bytes DESC, n.nspname, t.relname, ic.relname
             LIMIT $4
            """,
            [schema, exact, like, (_INDEX_SCAN_CAP if scan_all else limit + 1)],
        )
        since = await stats_reset_text(db)
    notes: list[str] = []
    if scan_all and len(rows) >= _INDEX_SCAN_CAP:
        notes.append(
            f"Only the {_INDEX_SCAN_CAP} largest indexes were compared; narrow with schema/table."
        )
    if show == "duplicate":
        rows = find_duplicate_indexes(rows)
        reason = "same definition as {of}: one of them can go"
    elif show == "redundant":
        rows = _redundant_indexes(rows)
        reason = "its columns are the leading columns of {of}, which serves the same lookups"
    elif show == "unused":
        reason = "never scanned since the statistics reset"
    else:
        reason = None
    if scan_all:
        rows.sort(key=lambda r: (-r["bytes"], r["schema"], r["table"], r["index"]))
    rows, has_more = page(rows, limit)
    for row in rows:
        row["size"] = size_text(row["bytes"])
        if reason:
            row["note"] = reason.format(of=row.get("of"))
            if show in ("duplicate", "unused") and row["backs_constraint"]:
                row["note"] += "; it backs a constraint, so drop the constraint instead"
    columns = [
        "schema",
        "table",
        "index",
        "size",
        "scans",
        "tuples_read",
        "tuples_fetched",
        "unique",
        "primary",
        "valid",
        "method",
        "definition",
    ]
    if db.v >= 160000:
        columns.insert(5, "last_scan")
    if reason:
        columns.append("note")
    out: dict[str, Any] = {"connection": db.profile.name, "show": show}
    if since:
        out["counters_since"] = since
    out.update(grid(rows, columns))
    out["has_more"] = has_more
    if show == "unused":
        notes.append(
            "Unused means no scan since the statistics reset on this server. Indexes used only "
            "on replicas, by monthly jobs or that enforce nothing but are kept for constraints "
            "elsewhere deserve a second look before DROP INDEX."
        )
    if notes:
        out["notes"] = notes
    return out


# ---------------------------------------------------------------------------------- bloat

# The statistical estimate follows the idea of the bloat estimation queries by Jehan-Guillaume
# (ioguix) de Rorthais, https://github.com/ioguix/pgsql-bloat-estimation (BSD 2-clause): the
# expected size of a relation is derived from its row count and the average row width taken
# from the column statistics, and the difference to the real size is the bloat. The queries
# and the arithmetic here are written from scratch for pgharbor.

_PAGE_HEADER = 24
_BTREE_SPECIAL = 16
_FILLFACTOR_OPTION = (
    "substring(array_to_string({alias}.reloptions, ' ') FROM 'fillfactor=([0-9]+)')"
)
_ARCHITECTURE = (
    "CASE WHEN pg_catalog.version() ~ '64-bit|x86_64|aarch64|ppc64|ia64|amd64' THEN 8 ELSE 4 END"
)
_BLOAT_SCAN_CAP = 5000


def _align(value: float, boundary: int) -> int:
    whole = int(value) if value == int(value) else int(value) + 1
    return -(-whole // boundary) * boundary


def _expected_pages(tuples: float, tuple_bytes: int, usable: float, fillfactor: int) -> int:
    per_page = int(usable * fillfactor / 100 / tuple_bytes)
    if per_page <= 0:
        return 0
    whole = int(tuples / per_page)
    return whole if whole == tuples / per_page else whole + 1


def _wasted(pages: int, expected: int, block_size: int) -> tuple[int, float | None]:
    wasted_pages = max(0, pages - expected)
    return wasted_pages * block_size, _pct(wasted_pages, pages)


@tool(group="performance", title="Table and index bloat")
async def pg_bloat(
    connection: str | None = None,
    schema: str | None = None,
    kind: Literal["tables", "indexes", "both"] = "both",
    min_wasted_bytes: int = 0,
    limit: int = 30,
    exact_table: str | None = None,
) -> dict[str, Any]:
    """Estimate how much space tables and btree indexes waste (bloat) and rank them by
    wasted bytes, without scanning anything: the estimate comes from the row counts and
    average row widths in the planner statistics and is usually within 10 to 20 percent.

    `kind` is tables, indexes or both; `schema` narrows the search; `min_wasted_bytes` hides
    small waste. Relations never analyzed are skipped (run ANALYZE). For a measurement
    instead of an estimate pass `exact_table` (`name` or `schema.name`): it reads that table
    with the pgstattuple extension (pgstattuple_approx when available) and also reports the
    density of its btree indexes, at the cost of reading the relation.
    """
    check_choice("kind", kind, ("tables", "indexes", "both"))
    limit = clamp(limit, 30, 200)
    min_wasted = max(0, int(min_wasted_bytes))
    async with get_app().read(connection) as db:
        if exact_table:
            return await _exact_bloat(db, exact_table)
        notes: list[str] = []
        out: dict[str, Any] = {"connection": db.profile.name}
        if kind in ("tables", "both"):
            out["tables"], skipped = await _table_bloat(db, schema, min_wasted, limit)
            if skipped:
                notes.append(
                    f"{skipped} table(s) were skipped because their column statistics are "
                    "missing or incomplete (run ANALYZE, or the role cannot read pg_stats)."
                )
        if kind in ("indexes", "both"):
            out["indexes"], skipped = await _index_bloat(db, schema, min_wasted, limit)
            if skipped:
                notes.append(
                    f"{skipped} btree index(es) were skipped because column statistics are "
                    "missing (run ANALYZE) or they use the name type."
                )
    notes.append(
        "Estimates assume uniform rows and use the sizes recorded by the last VACUUM or "
        "ANALYZE; TOAST data is not counted. Pass exact_table to measure one table."
    )
    out["notes"] = notes
    return out


async def _table_bloat(
    db: Db, schema: str | None, min_wasted: int, limit: int
) -> tuple[dict[str, Any], int]:
    candidates = await db.rows(
        f"""
        SELECT n.nspname AS schema, c.relname AS "table", c.relpages::bigint AS pages,
               greatest(c.reltuples, 0)::float8 AS tuples,
               coalesce({_FILLFACTOR_OPTION.format(alias="c")}::int, 100) AS fillfactor,
               sum((1 - coalesce(s.null_frac, 0)) * coalesce(s.avg_width, 0))::float8 AS width,
               coalesce(max(s.null_frac), 0) > 0 AS has_nulls,
               count(*) AS natts, count(s.attname) AS nstats,
               coalesce(bool_or(a.atttypid = 'pg_catalog.name'::pg_catalog.regtype), false)
                   AS has_name,
               pg_catalog.current_setting('block_size')::int AS block_size,
               {_ARCHITECTURE} AS align
          FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
          JOIN pg_catalog.pg_attribute a
               ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
          LEFT JOIN pg_catalog.pg_stats s
               ON s.schemaname = n.nspname AND s.tablename = c.relname
              AND s.attname = a.attname AND NOT s.inherited
         WHERE c.relkind IN ('r', 'm') AND c.relpages > 0
           AND ($1::text IS NULL OR n.nspname = $1)
           AND ($1::text IS NOT NULL OR ({USER_SCHEMA_FILTER}))
           AND n.nspname !~ '^pg_toast'
         GROUP BY c.oid, n.nspname, c.relname, c.relpages, c.reltuples, c.reloptions
         ORDER BY c.relpages DESC, n.nspname, c.relname
         LIMIT $2
        """,
        [schema, _BLOAT_SCAN_CAP],
    )
    results: list[dict[str, Any]] = []
    skipped = 0
    for row in candidates:
        if row["has_name"] or row["nstats"] < row["natts"]:
            skipped += 1
            continue
        header = 23 + ((row["natts"] + 7) // 8 if row["has_nulls"] else 0)
        tuple_bytes = (
            4 + _align(header, row["align"]) + _align(row["width"], row["align"])
        )  # line pointer + aligned header + aligned data
        usable = row["block_size"] - _PAGE_HEADER
        expected = _expected_pages(row["tuples"], tuple_bytes, usable, row["fillfactor"])
        wasted, wasted_pct = _wasted(row["pages"], expected, row["block_size"])
        if wasted <= 0 or wasted < min_wasted:
            continue
        results.append(
            {
                "schema": row["schema"],
                "table": row["table"],
                "size": size_text(row["pages"] * row["block_size"]),
                "wasted": size_text(wasted),
                "wasted_pct": wasted_pct,
                "fillfactor": row["fillfactor"],
                "_bytes": wasted,
            }
        )
    results.sort(key=lambda r: -r["_bytes"])
    shown, more = page(results, limit)
    table = grid(shown, ["schema", "table", "size", "wasted", "wasted_pct", "fillfactor"])
    table["has_more"] = more
    return table, skipped


async def _index_bloat(
    db: Db, schema: str | None, min_wasted: int, limit: int
) -> tuple[dict[str, Any], int]:
    candidates = await db.rows(
        f"""
        SELECT n.nspname AS schema, t.relname AS "table", ic.relname AS index,
               ic.relpages::bigint AS pages, greatest(ic.reltuples, 0)::float8 AS tuples,
               coalesce({_FILLFACTOR_OPTION.format(alias="ic")}::int, 90) AS fillfactor,
               sum((1 - coalesce(s.null_frac, 0)) * coalesce(s.avg_width, 0))::float8 AS width,
               coalesce(max(s.null_frac), 0) > 0 AS has_nulls,
               count(*) AS natts, count(s.attname) AS nstats,
               coalesce(bool_or(ia.atttypid = 'pg_catalog.name'::pg_catalog.regtype), false)
                   AS has_name,
               pg_catalog.current_setting('block_size')::int AS block_size,
               {_ARCHITECTURE} AS align
          FROM pg_catalog.pg_index i
          JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid AND ic.relkind = 'i'
          JOIN pg_catalog.pg_am am ON am.oid = ic.relam AND am.amname = 'btree'
          JOIN pg_catalog.pg_class t ON t.oid = i.indrelid
          JOIN pg_catalog.pg_namespace n ON n.oid = ic.relnamespace
          JOIN pg_catalog.pg_attribute ia
               ON ia.attrelid = ic.oid AND ia.attnum > 0 AND NOT ia.attisdropped
          LEFT JOIN pg_catalog.pg_attribute ta
               ON ta.attrelid = t.oid AND ta.attnum = i.indkey[ia.attnum - 1]
          LEFT JOIN pg_catalog.pg_stats s
               ON s.schemaname = n.nspname AND NOT s.inherited
              AND s.attname = coalesce(ta.attname, ia.attname)
              AND s.tablename = CASE WHEN i.indkey[ia.attnum - 1] = 0
                                     THEN ic.relname ELSE t.relname END
         WHERE i.indisvalid AND ic.relpages > 1
           AND ($1::text IS NULL OR n.nspname = $1)
           AND ($1::text IS NOT NULL OR ({USER_SCHEMA_FILTER}))
           AND n.nspname !~ '^pg_toast'
         GROUP BY ic.oid, n.nspname, t.relname, ic.relname, ic.relpages, ic.reltuples,
                  ic.reloptions
         ORDER BY ic.relpages DESC, n.nspname, ic.relname
         LIMIT $2
        """,
        [schema, _BLOAT_SCAN_CAP],
    )
    results: list[dict[str, Any]] = []
    skipped = 0
    for row in candidates:
        if row["has_name"] or row["nstats"] < row["natts"]:
            skipped += 1
            continue
        header = 8 + (4 if row["has_nulls"] else 0)
        tuple_bytes = 4 + _align(header, row["align"]) + _align(row["width"], row["align"])
        usable = row["block_size"] - _PAGE_HEADER - _BTREE_SPECIAL
        expected = 1 + _expected_pages(row["tuples"], tuple_bytes, usable, row["fillfactor"])
        wasted, wasted_pct = _wasted(row["pages"], expected, row["block_size"])
        if wasted <= 0 or wasted < min_wasted:
            continue
        results.append(
            {
                "schema": row["schema"],
                "table": row["table"],
                "index": row["index"],
                "size": size_text(row["pages"] * row["block_size"]),
                "wasted": size_text(wasted),
                "wasted_pct": wasted_pct,
                "fillfactor": row["fillfactor"],
                "_bytes": wasted,
            }
        )
    results.sort(key=lambda r: -r["_bytes"])
    shown, more = page(results, limit)
    table = grid(shown, ["schema", "table", "index", "size", "wasted", "wasted_pct", "fillfactor"])
    table["has_more"] = more
    return table, skipped


async def _exact_bloat(db: Db, name: str) -> dict[str, Any]:
    extension = db.need_ext("pgstattuple")
    relation = await resolve_relation(db, name, "rm", what="table or materialized view")
    prefix = quote_ident(extension.schema)
    approximate = bool(
        await db.value(
            """
            SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_proc p
                             JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
                            WHERE p.proname = 'pgstattuple_approx' AND n.nspname = $1)
            """,
            [extension.schema],
        )
    )
    oid = relation["oid"]
    qualified = f"{relation['schema']}.{relation['name']}"
    if approximate:
        row = await db.row(
            f"SELECT * FROM {prefix}.pgstattuple_approx($1::bigint::oid::pg_catalog.regclass)",
            [oid],
        )
        assert row is not None
        measured: dict[str, Any] = {
            "method": "pgstattuple_approx (skips pages the visibility map marks all-visible)",
            "table_size": size_text(row["table_len"]),
            "scanned_percent": round(row["scanned_percent"], 1),
            "live_percent": round(row["approx_tuple_percent"], 1),
            "dead_percent": round(row["dead_tuple_percent"], 1),
            "free_percent": round(row["approx_free_percent"], 1),
            "dead_tuples": int(row["dead_tuple_count"]),
        }
    else:
        row = await db.row(
            f"SELECT * FROM {prefix}.pgstattuple($1::bigint::oid::pg_catalog.regclass)", [oid]
        )
        assert row is not None
        measured = {
            "method": "pgstattuple (full scan)",
            "table_size": size_text(row["table_len"]),
            "live_percent": round(row["tuple_percent"], 1),
            "dead_percent": round(row["dead_tuple_percent"], 1),
            "free_percent": round(row["free_percent"], 1),
            "dead_tuples": int(row["dead_tuple_count"]),
        }
    measured["wasted_percent"] = round(measured["dead_percent"] + measured["free_percent"], 1)
    indexes = await db.rows(
        """
        SELECT ic.oid::bigint AS oid, ic.relname AS index
          FROM pg_catalog.pg_index i
          JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid AND ic.relkind = 'i'
          JOIN pg_catalog.pg_am am ON am.oid = ic.relam AND am.amname = 'btree'
         WHERE i.indrelid = $1 AND i.indisvalid
         ORDER BY ic.relpages DESC, ic.relname
         LIMIT 10
        """,
        [oid],
    )
    index_rows: list[dict[str, Any]] = []
    notes: list[str] = []
    for index in indexes:
        stats, reason = await attempt(
            db,
            partial(
                db.row,
                f"SELECT * FROM {prefix}.pgstatindex($1::bigint::oid::pg_catalog.regclass)",
                [index["oid"]],
            ),
        )
        if stats is None:
            notes.append(f"pgstatindex failed for {index['index']}: {reason}")
            continue
        index_rows.append(
            {
                "index": index["index"],
                "size": size_text(stats["index_size"]),
                "leaf_density_pct": round(stats["avg_leaf_density"], 1),
                "leaf_fragmentation_pct": round(stats["leaf_fragmentation"], 1),
                "empty_pages": stats["empty_pages"],
                "deleted_pages": stats["deleted_pages"],
            }
        )
    notes.append(
        "wasted_percent is dead plus free space inside the table file. A btree leaf density "
        "far below the fillfactor (90 by default) means the index is bloated."
    )
    return {
        "connection": db.profile.name,
        "table": qualified,
        "measured": measured,
        "indexes": grid(
            index_rows,
            [
                "index",
                "size",
                "leaf_density_pct",
                "leaf_fragmentation_pct",
                "empty_pages",
                "deleted_pages",
            ],
        ),
        "notes": notes,
    }


# ---------------------------------------------------------------------------- pg_vacuum_status

_VACUUM_SETTINGS = (
    "autovacuum",
    "track_counts",
    "autovacuum_max_workers",
    "autovacuum_naptime",
    "autovacuum_vacuum_threshold",
    "autovacuum_vacuum_scale_factor",
    "autovacuum_vacuum_insert_threshold",
    "autovacuum_vacuum_insert_scale_factor",
    "autovacuum_vacuum_max_threshold",
    "autovacuum_analyze_threshold",
    "autovacuum_analyze_scale_factor",
    "autovacuum_vacuum_cost_delay",
    "autovacuum_vacuum_cost_limit",
    "autovacuum_freeze_max_age",
    "autovacuum_multixact_freeze_max_age",
    "autovacuum_work_mem",
    "vacuum_cost_delay",
    "vacuum_cost_limit",
    "vacuum_freeze_min_age",
    "vacuum_freeze_table_age",
    "maintenance_work_mem",
)


def _reloption(name: str, cast: str) -> str:
    return (
        f"(SELECT split_part(o, '=', 2)::{cast} FROM unnest(c.reloptions) AS o "
        f"WHERE o LIKE '{name}=%')"
    )


@tool(group="performance", title="Vacuum and wraparound status")
async def pg_vacuum_status(connection: str | None = None, limit: int = 30) -> dict[str, Any]:
    """Report the state of vacuum and autovacuum: its configuration, the tables with the
    most dead rows measured against their autovacuum threshold (honouring per-table
    reloptions), transaction id and multixact age per table and per database as a percentage
    of the wraparound limit, what holds back the oldest xmin (sessions, prepared
    transactions, replication slots), vacuums running now with their progress, and the
    autovacuum workers.

    Use it when tables bloat, autovacuum seems idle, or before a wraparound emergency.
    `limit` caps each list. For the per-table ranking by activity use pg_table_stats.
    """
    limit = clamp(limit, 30, 200)
    async with get_app().read(connection) as db:
        settings = await db.rows(
            """
            SELECT name, setting, unit FROM pg_catalog.pg_settings
             WHERE name = ANY($1::text[]) ORDER BY name
            """,
            [list(_VACUUM_SETTINGS)],
        )
        max_threshold = (
            "pg_catalog.current_setting('autovacuum_vacuum_max_threshold', true)::float8"
            if db.v >= 180000
            else "NULL::float8"
        )
        due = await db.rows(
            f"""
            WITH cfg AS (
              SELECT pg_catalog.current_setting('autovacuum_vacuum_threshold')::float8 AS vt,
                     pg_catalog.current_setting('autovacuum_vacuum_scale_factor')::float8 AS vs,
                     pg_catalog.current_setting('autovacuum_analyze_threshold')::float8 AS at,
                     pg_catalog.current_setting('autovacuum_analyze_scale_factor')::float8 AS asf,
                     {max_threshold} AS vmax),
            t AS (
              SELECT n.nspname AS schema, c.relname AS "table", s.n_live_tup, s.n_dead_tup,
                     s.n_mod_since_analyze, s.last_vacuum, s.last_autovacuum, s.last_autoanalyze,
                     greatest(c.reltuples, 0)::float8 AS reltuples,
                     coalesce({_reloption("autovacuum_enabled", "bool")}, true) AS enabled,
                     coalesce({_reloption("autovacuum_vacuum_threshold", "float8")}, cfg.vt) AS vt,
                     coalesce({_reloption("autovacuum_vacuum_scale_factor", "float8")}, cfg.vs)
                         AS vs,
                     coalesce({_reloption("autovacuum_analyze_threshold", "float8")}, cfg.at) AS at,
                     coalesce({_reloption("autovacuum_analyze_scale_factor", "float8")}, cfg.asf)
                         AS asf,
                     cfg.vmax, pg_catalog.age(c.relfrozenxid) AS xid_age
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_catalog.pg_stat_all_tables s ON s.relid = c.oid
               CROSS JOIN cfg
               WHERE c.relkind IN ('r', 'm') AND s.n_dead_tup > 0
                 AND n.nspname NOT IN ('pg_catalog', 'information_schema')),
            u AS (
              SELECT t.*, least(t.vt + t.vs * t.reltuples, coalesce(t.vmax, 'infinity'))
                          AS vacuum_threshold,
                     t.at + t.asf * t.reltuples AS analyze_threshold FROM t)
            SELECT schema, "table", n_dead_tup AS dead_tuples,
                   round(vacuum_threshold)::bigint AS vacuum_threshold,
                   n_dead_tup > vacuum_threshold AS due,
                   round((n_dead_tup / greatest(vacuum_threshold, 1))::numeric, 2)
                       AS dead_over_threshold,
                   n_live_tup AS live_tuples,
                   round(100.0 * n_dead_tup / nullif(n_live_tup + n_dead_tup, 0), 1) AS dead_pct,
                   n_mod_since_analyze AS modified_since_analyze,
                   round(analyze_threshold)::bigint AS analyze_threshold, enabled AS autovacuum,
                   date_trunc('second', last_autovacuum)::text AS last_autovacuum,
                   date_trunc('second', last_vacuum)::text AS last_vacuum,
                   date_trunc('second', last_autoanalyze)::text AS last_autoanalyze
              FROM u
             ORDER BY n_dead_tup / greatest(vacuum_threshold, 1) DESC, schema, "table"
             LIMIT $1
            """,
            [limit + 1],
        )
        due, due_more = page(due, limit)
        table_age = await db.rows(
            """
            SELECT n.nspname AS schema, c.relname AS "table",
                   greatest(pg_catalog.age(c.relfrozenxid),
                            coalesce(pg_catalog.age(tt.relfrozenxid), 0)) AS xid_age,
                   greatest(pg_catalog.mxid_age(c.relminmxid),
                            coalesce(pg_catalog.mxid_age(tt.relminmxid), 0)) AS multixact_age
              FROM pg_catalog.pg_class c
              JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
              LEFT JOIN pg_catalog.pg_class tt ON tt.oid = c.reltoastrelid
             WHERE c.relkind IN ('r', 'm')
             ORDER BY xid_age DESC, multixact_age DESC, n.nspname, c.relname
             LIMIT $1
            """,
            [limit],
        )
        freeze_max = int(
            await db.value("SELECT pg_catalog.current_setting('autovacuum_freeze_max_age')::bigint")
        )
        database_age = await db.rows(
            """
            SELECT datname AS database, pg_catalog.age(datfrozenxid) AS xid_age,
                   pg_catalog.mxid_age(datminmxid) AS multixact_age
              FROM pg_catalog.pg_database WHERE datallowconn
             ORDER BY 2 DESC, 1
            """
        )
        holders = await db.rows(
            f"""
            SELECT kind, id, detail, xmin_age FROM (
              SELECT 'session' AS kind, a.pid::text AS id,
                     coalesce(a.usename || ' ', '') || coalesce(a.state, '') || ': '
                         || {clean_query("a.query", 80)} AS detail,
                     pg_catalog.age(a.backend_xmin) AS xmin_age
                FROM pg_catalog.pg_stat_activity a
               WHERE a.backend_xmin IS NOT NULL AND a.pid <> pg_catalog.pg_backend_pid()
              UNION ALL
              SELECT 'prepared transaction', p.gid, coalesce(p.owner, ''),
                     pg_catalog.age(p.transaction)
                FROM pg_catalog.pg_prepared_xacts p
              UNION ALL
              SELECT 'replication slot', r.slot_name,
                     r.slot_type || CASE WHEN r.active THEN ', active' ELSE ', inactive' END,
                     greatest(pg_catalog.age(r.xmin), pg_catalog.age(r.catalog_xmin))
                FROM pg_catalog.pg_replication_slots r
               WHERE r.xmin IS NOT NULL OR r.catalog_xmin IS NOT NULL
            ) h
             WHERE xmin_age IS NOT NULL
             ORDER BY xmin_age DESC LIMIT 5
            """
        )
        running = await db.rows(
            """
            SELECT p.pid, p.datname AS database,
                   CASE WHEN p.datname = pg_catalog.current_database()
                        THEN p.relid::pg_catalog.regclass::text ELSE p.relid::text END AS "table",
                   p.phase, p.heap_blks_total, p.heap_blks_scanned, p.heap_blks_vacuumed,
                   round(100.0 * p.heap_blks_scanned / nullif(p.heap_blks_total, 0), 1)
                       AS scanned_pct,
                   p.index_vacuum_count,
                   CASE WHEN a.query LIKE 'autovacuum:%' THEN 'autovacuum' ELSE 'manual' END
                       AS started_by,
                   round(extract(epoch FROM (pg_catalog.clock_timestamp() - a.xact_start))::numeric)
                       AS running_seconds
              FROM pg_catalog.pg_stat_progress_vacuum p
              LEFT JOIN pg_catalog.pg_stat_activity a ON a.pid = p.pid
             ORDER BY a.xact_start NULLS LAST, p.pid
            """
        )
        worker_filter = (
            "a.backend_type = 'autovacuum worker'"
            if db.v >= 100000
            else "a.query LIKE 'autovacuum:%'"
        )
        workers = await db.rows(
            f"""
            SELECT a.pid, a.datname AS database, {clean_query("a.query", 120)} AS query,
                   round(extract(epoch FROM (pg_catalog.clock_timestamp() - a.xact_start))::numeric)
                       AS running_seconds, a.wait_event_type, a.wait_event
              FROM pg_catalog.pg_stat_activity a
             WHERE {worker_filter}
             ORDER BY a.xact_start NULLS LAST, a.pid
            """
        )
    configuration = {
        r["name"]: format_setting(r["setting"], r["unit"]) for r in settings if r["setting"]
    }
    for row in table_age:
        row["wraparound_pct"] = _pct(row["xid_age"], _WRAPAROUND)
        row["forced_vacuum_pct"] = _pct(row["xid_age"], freeze_max)
        row["multixact_wraparound_pct"] = _pct(row["multixact_age"], _WRAPAROUND)
    for row in database_age:
        row["wraparound_pct"] = _pct(row["xid_age"], _WRAPAROUND)
        row["multixact_wraparound_pct"] = _pct(row["multixact_age"], _WRAPAROUND)
    notes: list[str] = []
    if configuration.get("autovacuum") == "off":
        notes.append(
            "autovacuum is OFF: dead rows pile up and transaction ids are not frozen. "
            "Turn it on unless this is deliberate and vacuum runs another way."
        )
    if configuration.get("track_counts") == "off":
        notes.append("track_counts is off: autovacuum cannot tell which tables need work.")
    if any(r["xid_age"] > freeze_max for r in database_age):
        notes.append(
            "A database is older than autovacuum_freeze_max_age: anti-wraparound vacuums "
            "are due or running."
        )
    return {
        "connection": db.profile.name,
        "settings": configuration,
        "tables_by_dead_rows": {
            **grid(
                due,
                [
                    "schema",
                    "table",
                    "dead_tuples",
                    "vacuum_threshold",
                    "due",
                    "dead_over_threshold",
                    "live_tuples",
                    "dead_pct",
                    "modified_since_analyze",
                    "analyze_threshold",
                    "autovacuum",
                    "last_autovacuum",
                    "last_vacuum",
                    "last_autoanalyze",
                ],
            ),
            "has_more": due_more,
        },
        "tables_by_age": grid(
            table_age,
            [
                "schema",
                "table",
                "xid_age",
                "wraparound_pct",
                "forced_vacuum_pct",
                "multixact_age",
                "multixact_wraparound_pct",
            ],
        ),
        "databases_by_age": grid(
            database_age,
            ["database", "xid_age", "wraparound_pct", "multixact_age", "multixact_wraparound_pct"],
        ),
        "oldest_xmin_holders": grid(holders, ["kind", "id", "detail", "xmin_age"]),
        "vacuums_running": grid(
            running,
            [
                "pid",
                "database",
                "table",
                "phase",
                "heap_blks_total",
                "heap_blks_scanned",
                "heap_blks_vacuumed",
                "scanned_pct",
                "index_vacuum_count",
                "started_by",
                "running_seconds",
            ],
        ),
        "autovacuum_workers": {
            "running": len(workers),
            "max": configuration.get("autovacuum_max_workers"),
            "list": workers,
        },
        "notes": notes,
    }


# ------------------------------------------------------------------------------- pg_sizes


@tool(group="performance", title="Sizes")
async def pg_sizes(
    connection: str | None = None,
    scope: Literal["databases", "schemas", "tables", "indexes", "tablespaces"] = "tables",
    schema: str | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    """Find what takes the space: the biggest databases, schemas, tables (with heap data,
    indexes and TOAST shown separately), indexes or tablespaces, largest first.

    `scope` selects the kind of object. `schema` narrows tables and indexes to one schema.
    Partitions are listed as tables of their own. Sizes include all forks of each relation;
    use pg_bloat to see how much of a table is wasted.
    """
    check_choice("scope", scope, ("databases", "schemas", "tables", "indexes", "tablespaces"))
    limit = clamp(limit, 30, 500)
    async with get_app().read(connection) as db:
        if scope == "databases":
            rows = await db.rows(
                """
                SELECT d.datname AS database, pg_catalog.pg_get_userbyid(d.datdba) AS owner,
                       pg_catalog.pg_size_pretty(s.bytes) AS size,
                       pg_catalog.pg_encoding_to_char(d.encoding) AS encoding
                  FROM pg_catalog.pg_database d
                  CROSS JOIN LATERAL (SELECT CASE WHEN pg_catalog.has_database_privilege(
                                                          d.oid, 'CONNECT')
                                                  THEN pg_catalog.pg_database_size(d.oid) END
                                      AS bytes) s
                 WHERE NOT d.datistemplate
                 ORDER BY s.bytes DESC NULLS LAST, d.datname
                 LIMIT $1
                """,
                [limit + 1],
            )
            columns = ["database", "owner", "size", "encoding"]
        elif scope == "schemas":
            rows = await db.rows(
                f"""
                SELECT n.nspname AS schema, count(*) AS relations,
                       pg_catalog.pg_size_pretty(sum(t.total)) AS total,
                       pg_catalog.pg_size_pretty(sum(t.heap)) AS data,
                       pg_catalog.pg_size_pretty(sum(t.idx)) AS indexes
                  FROM pg_catalog.pg_class c
                  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                 CROSS JOIN LATERAL (SELECT pg_catalog.pg_total_relation_size(c.oid) AS total,
                                            pg_catalog.pg_table_size(c.oid) AS heap,
                                            pg_catalog.pg_indexes_size(c.oid) AS idx) t
                 WHERE c.relkind IN ('r', 'm') AND ({USER_SCHEMA_FILTER})
                 GROUP BY n.nspname
                 ORDER BY sum(t.total) DESC, n.nspname
                 LIMIT $1
                """,
                [limit + 1],
            )
            columns = ["schema", "relations", "total", "data", "indexes"]
        elif scope == "tables":
            rows = await db.rows(
                f"""
                SELECT n.nspname AS schema, c.relname AS "table",
                       pg_catalog.pg_size_pretty(s.total) AS total,
                       pg_catalog.pg_size_pretty(s.heap) AS data,
                       pg_catalog.pg_size_pretty(s.idx) AS indexes,
                       pg_catalog.pg_size_pretty(s.toast) AS toast,
                       CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END
                           AS rows_estimate
                  FROM pg_catalog.pg_class c
                  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                 CROSS JOIN LATERAL (
                       SELECT pg_catalog.pg_total_relation_size(c.oid) AS total,
                              pg_catalog.pg_relation_size(c.oid) AS heap,
                              pg_catalog.pg_indexes_size(c.oid) AS idx,
                              coalesce(pg_catalog.pg_total_relation_size(c.reltoastrelid), 0)
                                  AS toast) s
                 WHERE c.relkind IN ('r', 'm')
                   AND ($1::text IS NULL OR n.nspname = $1)
                   AND ($1::text IS NOT NULL OR ({USER_SCHEMA_FILTER}))
                   AND n.nspname !~ '^pg_toast'
                 ORDER BY s.total DESC, n.nspname, c.relname
                 LIMIT $2
                """,
                [schema, limit + 1],
            )
            columns = ["schema", "table", "total", "data", "indexes", "toast", "rows_estimate"]
        elif scope == "indexes":
            rows = await db.rows(
                f"""
                SELECT n.nspname AS schema, t.relname AS "table", ic.relname AS index,
                       pg_catalog.pg_size_pretty(s.bytes) AS size, am.amname AS method,
                       coalesce(st.idx_scan, 0) AS scans
                  FROM pg_catalog.pg_index i
                  JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid AND ic.relkind = 'i'
                  JOIN pg_catalog.pg_class t ON t.oid = i.indrelid
                  JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace
                  JOIN pg_catalog.pg_am am ON am.oid = ic.relam
                  LEFT JOIN pg_catalog.pg_stat_all_indexes st ON st.indexrelid = i.indexrelid
                 CROSS JOIN LATERAL (SELECT pg_catalog.pg_relation_size(ic.oid) AS bytes) s
                 WHERE ($1::text IS NULL OR n.nspname = $1)
                   AND ($1::text IS NOT NULL OR ({USER_SCHEMA_FILTER}))
                   AND n.nspname !~ '^pg_toast'
                 ORDER BY s.bytes DESC, n.nspname, ic.relname
                 LIMIT $2
                """,
                [schema, limit + 1],
            )
            columns = ["schema", "table", "index", "size", "method", "scans"]
        else:
            rows = await db.rows(
                """
                SELECT t.spcname AS tablespace, pg_catalog.pg_get_userbyid(t.spcowner) AS owner,
                       pg_catalog.pg_size_pretty(s.bytes) AS size,
                       nullif(pg_catalog.pg_tablespace_location(t.oid), '') AS location
                  FROM pg_catalog.pg_tablespace t
                 CROSS JOIN LATERAL (SELECT pg_catalog.pg_tablespace_size(t.oid) AS bytes) s
                 ORDER BY s.bytes DESC, t.spcname
                 LIMIT $1
                """,
                [limit + 1],
            )
            columns = ["tablespace", "owner", "size", "location"]
        rows, has_more = page(rows, limit)
        database_size = await db.value(
            "SELECT pg_catalog.pg_size_pretty(pg_catalog.pg_database_size(current_database()))"
        )
    out: dict[str, Any] = {
        "connection": db.profile.name,
        "scope": scope,
        "database": db.facts.database,
        "database_size": database_size,
        **grid(rows, columns),
        "has_more": has_more,
    }
    if scope == "tables":
        out["notes"] = [
            "Partitioned parents hold no data: their partitions are listed individually. "
            "total = data + indexes + toast (+ small free-space and visibility maps)."
        ]
    return out


# ---------------------------------------------------------------------------- pg_cache_io


@tool(group="performance", title="Cache and I/O")
async def pg_cache_io(connection: str | None = None, limit: int = 20) -> dict[str, Any]:
    """Report cache effectiveness and I/O: the buffer cache hit ratio of the database, of
    the tables and of the indexes (worst first among the busy ones), a summary of
    pg_stat_io (PostgreSQL 16+), temporary file use, I/O timings when track_io_timing is
    on, and, if the pg_buffercache extension is installed, the relations that occupy
    shared_buffers.

    A low hit ratio means reads go to the operating system or the disk; it does not count
    the OS page cache, so a lower value is not always slow. `limit` caps each list.
    """
    limit = clamp(limit, 20, 100)
    async with get_app().read(connection) as db:
        database = await db.row(
            """
            SELECT blks_read, blks_hit,
                   round(100.0 * blks_hit / nullif(blks_hit + blks_read, 0), 2) AS hit_pct,
                   temp_files, pg_catalog.pg_size_pretty(temp_bytes) AS temp_size,
                   round(blk_read_time::numeric, 1) AS read_ms,
                   round(blk_write_time::numeric, 1) AS write_ms,
                   date_trunc('second', stats_reset)::text AS stats_reset
              FROM pg_catalog.pg_stat_database WHERE datname = pg_catalog.current_database()
            """
        )
        assert database is not None
        settings = {
            r["name"]: format_setting(r["setting"], r["unit"])
            for r in await db.rows(
                "SELECT name, setting, unit FROM pg_catalog.pg_settings "
                "WHERE name IN ('shared_buffers', 'track_io_timing', 'effective_cache_size', "
                "'work_mem', 'temp_file_limit')"
            )
        }
        tables = await db.rows(
            """
            SELECT schemaname AS schema, relname AS "table",
                   heap_blks_read AS heap_read, heap_blks_hit AS heap_hit,
                   round(100.0 * heap_blks_hit / nullif(heap_blks_hit + heap_blks_read, 0), 1)
                       AS heap_hit_pct,
                   coalesce(idx_blks_read, 0) AS idx_read, coalesce(idx_blks_hit, 0) AS idx_hit,
                   round(100.0 * idx_blks_hit / nullif(idx_blks_hit + idx_blks_read, 0), 1)
                       AS idx_hit_pct
              FROM pg_catalog.pg_statio_user_tables
             WHERE heap_blks_read + heap_blks_hit > 0
             ORDER BY heap_blks_read + coalesce(idx_blks_read, 0) DESC, schemaname, relname
             LIMIT $1
            """,
            [limit],
        )
        indexes = await db.rows(
            """
            SELECT schemaname AS schema, relname AS "table", indexrelname AS index,
                   idx_blks_read AS read, idx_blks_hit AS hit,
                   round(100.0 * idx_blks_hit / nullif(idx_blks_hit + idx_blks_read, 0), 1)
                       AS hit_pct
              FROM pg_catalog.pg_statio_user_indexes
             WHERE idx_blks_read + idx_blks_hit > 0
             ORDER BY idx_blks_read DESC, schemaname, indexrelname
             LIMIT $1
            """,
            [limit],
        )
        io_summary: list[dict[str, Any]] = []
        if db.has("pg_stat_io"):
            io_summary = await db.rows(
                """
                SELECT backend_type, object, sum(reads) AS reads, sum(writes) AS writes,
                       sum(hits) AS hits, sum(evictions) AS evictions, sum(extends) AS extends,
                       sum(fsyncs) AS fsyncs,
                       round(coalesce(sum(read_time), 0)::numeric, 1) AS read_ms,
                       round(coalesce(sum(write_time), 0)::numeric, 1) AS write_ms
                  FROM pg_catalog.pg_stat_io
                 GROUP BY backend_type, object
                HAVING coalesce(sum(reads), 0) + coalesce(sum(writes), 0)
                       + coalesce(sum(extends), 0) + coalesce(sum(hits), 0) > 0
                 ORDER BY coalesce(sum(reads), 0) + coalesce(sum(writes), 0) DESC,
                          backend_type, object
                 LIMIT $1
                """,
                [limit],
            )
        buffers: list[dict[str, Any]] | None = None
        buffer_error: str | None = None
        extension = db.ext("pg_buffercache")
        if extension is not None:
            prefix = quote_ident(extension.schema)
            buffers, buffer_error = await attempt(
                db,
                lambda: db.rows(
                    f"""
                    SELECT n.nspname || '.' || c.relname AS relation,
                           count(*) AS buffers,
                           pg_catalog.pg_size_pretty(
                               count(*) * pg_catalog.current_setting('block_size')::bigint) AS size,
                           round(100.0 * count(*) / (
                               SELECT setting::bigint FROM pg_catalog.pg_settings
                                WHERE name = 'shared_buffers'), 1) AS pct_of_cache,
                           round(avg(b.usagecount), 1) AS avg_usage,
                           count(*) FILTER (WHERE b.isdirty) AS dirty
                      FROM {prefix}.pg_buffercache b
                      JOIN pg_catalog.pg_class c
                           ON b.relfilenode = pg_catalog.pg_relation_filenode(c.oid)
                      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                     WHERE b.reldatabase IN (0, (SELECT oid FROM pg_catalog.pg_database
                                                  WHERE datname = pg_catalog.current_database()))
                     GROUP BY n.nspname, c.relname
                     ORDER BY count(*) DESC, 1
                     LIMIT $1
                    """,
                    [limit],
                ),
            )
    timing_on = settings.get("track_io_timing") == "on"
    out: dict[str, Any] = {
        "connection": db.profile.name,
        "database": {
            "cache_hit_pct": database["hit_pct"],
            "blocks_read": database["blks_read"],
            "blocks_hit": database["blks_hit"],
            "counters_since": database["stats_reset"] or "never reset",
        },
        "temp_files": {"count": database["temp_files"], "size": database["temp_size"]},
        "settings": settings,
        "tables": grid(
            tables,
            [
                "schema",
                "table",
                "heap_read",
                "heap_hit",
                "heap_hit_pct",
                "idx_read",
                "idx_hit",
                "idx_hit_pct",
            ],
        ),
        "indexes": grid(indexes, ["schema", "table", "index", "read", "hit", "hit_pct"]),
    }
    notes = [
        "Hit ratios count PostgreSQL's shared_buffers only; a 'read' may still be served by "
        "the operating system cache. Counters accumulate since the statistics reset."
    ]
    if timing_on:
        out["io_timing_ms"] = {"read": database["read_ms"], "write": database["write_ms"]}
    else:
        notes.append(
            "track_io_timing is off, so read/write times are not collected. Turn it on "
            "(ALTER SYSTEM SET track_io_timing = on; SELECT pg_reload_conf()) to measure I/O "
            "time; the overhead is small on current hardware."
        )
    if db.has("pg_stat_io"):
        out["pg_stat_io"] = grid(
            io_summary,
            [
                "backend_type",
                "object",
                "reads",
                "writes",
                "hits",
                "evictions",
                "extends",
                "fsyncs",
                "read_ms",
                "write_ms",
            ],
        )
    else:
        notes.append("pg_stat_io needs PostgreSQL 16 or newer.")
    if buffers is not None:
        out["shared_buffers_by_relation"] = grid(
            buffers, ["relation", "buffers", "size", "pct_of_cache", "avg_usage", "dirty"]
        )
    elif buffer_error:
        notes.append(f"pg_buffercache could not be read: {buffer_error}")
    else:
        notes.append(
            "Install the pg_buffercache extension (CREATE EXTENSION pg_buffercache) to see "
            "which relations occupy shared_buffers."
        )
    out["notes"] = notes
    return out


# --------------------------------------------------------------------------- pg_progress

_PROGRESS_VIEWS = (
    ("vacuum", "pg_stat_progress_vacuum", (("heap_blks_scanned", "heap_blks_total"),)),
    ("analyze", "pg_stat_progress_analyze", (("sample_blks_scanned", "sample_blks_total"),)),
    ("cluster", "pg_stat_progress_cluster", (("heap_blks_scanned", "heap_blks_total"),)),
    (
        "create_index",
        "pg_stat_progress_create_index",
        (("blocks_done", "blocks_total"), ("tuples_done", "tuples_total")),
    ),
    ("basebackup", "pg_stat_progress_basebackup", (("backup_streamed", "backup_total"),)),
    ("copy", "pg_stat_progress_copy", (("bytes_processed", "bytes_total"),)),
    ("repack", "pg_stat_progress_repack", (("heap_blks_scanned", "heap_blks_total"),)),
)
_RELATION_KEYS = ("relid", "index_relid", "cluster_index_relid", "repack_index_relid")
_IDENTITY_KEYS = ("pid", "datid", "datname", *_RELATION_KEYS)


@tool(group="performance", title="Operation progress")
async def pg_progress(connection: str | None = None) -> dict[str, Any]:
    """Show long operations that are running now with their progress: VACUUM, ANALYZE,
    CLUSTER/VACUUM FULL, CREATE INDEX / REINDEX, base backups, COPY and REPACK, from the
    pg_stat_progress_* views that exist on this server version.

    Each operation lists the process, user, table, phase, how long it has run, percent done
    when it can be computed and the raw counters. When nothing is running it says so. Use
    it to answer "is it stuck or just slow?" for index builds and vacuums.
    """
    async with get_app().read(connection) as db:
        found: list[dict[str, Any]] = []
        available: list[str] = []
        for operation, view, pairs in _PROGRESS_VIEWS:
            if not db.has(view):
                continue
            available.append(operation)
            for item in await db.rows(
                f"SELECT pg_catalog.to_jsonb(p) AS data FROM pg_catalog.{view} p"
            ):
                found.append({"operation": operation, "data": item["data"], "pairs": pairs})
        relation_ids = sorted(
            {
                int(entry["data"][key])
                for entry in found
                for key in _RELATION_KEYS
                if entry["data"].get(key)
            }
        )
        names: dict[int, str] = {}
        if relation_ids:
            names = {
                r["oid"]: r["name"]
                for r in await db.rows(
                    """
                    SELECT c.oid::bigint AS oid, n.nspname || '.' || c.relname AS name
                      FROM pg_catalog.pg_class c
                      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                     WHERE c.oid::bigint = ANY($1::bigint[])
                    """,
                    [relation_ids],
                )
            }
        activity: dict[int, dict[str, Any]] = {}
        if found:
            activity = {
                r["pid"]: r
                for r in await db.rows(
                    f"""
                    SELECT pid, usename AS "user", {clean_query("query", 200)} AS query,
                           round(extract(epoch FROM (pg_catalog.clock_timestamp()
                                                     - coalesce(xact_start, query_start)))::numeric)
                               AS running_seconds
                      FROM pg_catalog.pg_stat_activity WHERE pid = ANY($1::int[])
                    """,
                    [[entry["data"]["pid"] for entry in found]],
                )
            }
    operations: list[dict[str, Any]] = []
    for entry in found:
        data = entry["data"]
        percent = None
        for done_key, total_key in entry["pairs"]:
            total = data.get(total_key) or 0
            if total > 0:
                percent = round(min(100.0, 100.0 * (data.get(done_key) or 0) / total), 1)
                break
        who = activity.get(data["pid"], {})
        shown: dict[str, Any] = {
            "operation": entry["operation"],
            "pid": data["pid"],
            "database": data.get("datname"),
            "user": who.get("user"),
            "table": names.get(int(data["relid"])) if data.get("relid") else None,
            "index": next(
                (
                    names.get(int(data[key]))
                    for key in ("index_relid", "cluster_index_relid", "repack_index_relid")
                    if data.get(key)
                ),
                None,
            ),
            "phase": data.get("phase"),
            "percent": percent,
            "running_seconds": who.get("running_seconds"),
            "counters": {
                key: value
                for key, value in data.items()
                if key not in _IDENTITY_KEYS and key not in ("phase", "command") and value
            },
            "query": who.get("query"),
        }
        if data.get("command"):
            shown["command"] = data["command"]
        operations.append({k: v for k, v in shown.items() if v is not None})
    out: dict[str, Any] = {
        "connection": db.profile.name,
        "checked": available,
        "operations": operations,
    }
    if not operations:
        out["message"] = (
            "No maintenance operation is in progress (checked: " + ", ".join(available) + ")."
        )
    missing = [op for op, view, _pairs in _PROGRESS_VIEWS if op not in available]
    if missing:
        out["not_on_this_version"] = missing
    return out


# ----------------------------------------------------------------------- pg_database_stats


@tool(group="performance", title="Database statistics")
async def pg_database_stats(
    connection: str | None = None, all_databases: bool = False
) -> dict[str, Any]:
    """Show pg_stat_database for the current database (or every database with
    `all_databases=true`): connections, commits and rollbacks with the rollback ratio,
    blocks read and hit with the cache hit ratio, rows returned, fetched, inserted, updated
    and deleted, recovery conflicts, temporary files, deadlocks, I/O times, checksum
    failures (PostgreSQL 12+), session counts and times (14+) and when the statistics were
    last reset.

    Counters accumulate since the reset. Use it for a first look at how a database is used
    and to spot deadlocks, temp-file spills and a high rollback ratio.
    """
    async with get_app().read(connection) as db:
        extra = ""
        if db.v >= 120000:
            extra += ", d.checksum_failures, d.checksum_last_failure::text AS checksum_last_failure"
        if db.v >= 140000:
            extra += """, d.sessions, d.sessions_abandoned, d.sessions_fatal, d.sessions_killed,
                   round((d.active_time / 1000)::numeric) AS active_seconds,
                   round((d.idle_in_transaction_time / 1000)::numeric)
                       AS idle_in_transaction_seconds"""
        rows = await db.rows(
            f"""
            SELECT d.datname AS database, d.numbackends AS connections,
                   d.xact_commit AS commits, d.xact_rollback AS rollbacks,
                   round(100.0 * d.xact_rollback / nullif(d.xact_commit + d.xact_rollback, 0), 2)
                       AS rollback_pct,
                   d.blks_read, d.blks_hit,
                   round(100.0 * d.blks_hit / nullif(d.blks_hit + d.blks_read, 0), 2)
                       AS cache_hit_pct,
                   d.tup_returned AS rows_returned, d.tup_fetched AS rows_fetched,
                   d.tup_inserted AS rows_inserted, d.tup_updated AS rows_updated,
                   d.tup_deleted AS rows_deleted, d.conflicts, d.temp_files,
                   pg_catalog.pg_size_pretty(d.temp_bytes) AS temp_size, d.deadlocks,
                   round(d.blk_read_time::numeric, 1) AS read_ms,
                   round(d.blk_write_time::numeric, 1) AS write_ms{extra},
                   date_trunc('second', d.stats_reset)::text AS stats_reset,
                   CASE WHEN pg_catalog.has_database_privilege(d.datid, 'CONNECT')
                        THEN pg_catalog.pg_size_pretty(pg_catalog.pg_database_size(d.datid)) END
                       AS size
              FROM pg_catalog.pg_stat_database d
              JOIN pg_catalog.pg_database p ON p.oid = d.datid
             WHERE d.datname IS NOT NULL AND NOT p.datistemplate
               AND ($1 OR d.datname = pg_catalog.current_database())
             ORDER BY d.datname
            """,
            [all_databases],
        )
    out: dict[str, Any] = {"connection": db.profile.name}
    if all_databases:
        out.update(grid(rows))
    elif rows:
        out["stats"] = rows[0]
    notes = [
        "Counters accumulate since stats_reset. blk_read/write times stay at 0 unless "
        "track_io_timing is on."
    ]
    if db.v < 120000:
        notes.append("Checksum failure counters need PostgreSQL 12 or newer.")
    if db.v < 140000:
        notes.append("Session counters need PostgreSQL 14 or newer.")
    out["notes"] = notes
    return out


# ------------------------------------------------------------------------------ pg_stat_view

_READABLE_SCHEMAS = ("pg_catalog", "information_schema")


@tool(group="performance", title="Read a system view")
async def pg_stat_view(
    view: str,
    connection: str | None = None,
    columns: list[str] | None = None,
    order_by: str | None = None,
    descending: bool = True,
    limit: int = 50,
) -> dict[str, Any]:
    """Read any system view or catalog table from pg_catalog or information_schema by name,
    for example pg_stat_user_functions, pg_stat_slru, pg_stat_ssl, pg_prepared_xacts,
    pg_stat_wal, pg_stat_replication or pg_replication_slots.

    Use it for everything that has no dedicated tool, and for views added by newer
    PostgreSQL versions. `view` is `name` or `schema.name`; `columns` selects columns (all by
    default); `order_by` sorts by a column, largest first unless `descending=false`. Names
    are checked against the real columns on this server. Catalogs that hold credentials
    (pg_authid, pg_shadow, pg_user_mapping...) are refused.
    """
    limit = clamp(limit, 50, 1000)
    schema, name = split_qualified(view)
    if schema is not None and schema not in _READABLE_SCHEMAS:
        raise PgHarborError(
            f"{view!r} is outside pg_catalog and information_schema. This tool reads system "
            "views only; use pg_describe_table and pg_query for user tables, and "
            "pg_top_queries for pg_stat_statements."
        )
    async with get_app().read(connection) as db:
        found = await db.rows(
            """
            SELECT c.oid::bigint AS oid, n.nspname AS schema, c.relname AS name
              FROM pg_catalog.pg_class c
              JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
             WHERE c.relname = $1 AND n.nspname = ANY($2::text[])
               AND c.relkind IN ('r', 'v', 'm', 'f', 'p')
             ORDER BY (n.nspname = 'pg_catalog') DESC
            """,
            [name, [schema] if schema else list(_READABLE_SCHEMAS)],
        )
        if not found:
            elsewhere = await db.rows(
                """
                SELECT n.nspname || '.' || c.relname AS name
                  FROM pg_catalog.pg_class c
                  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                 WHERE c.relname = $1 AND c.relkind IN ('r', 'v', 'm', 'f', 'p')
                   AND n.nspname !~ '^pg_toast' AND n.nspname <> ALL($2::text[])
                 ORDER BY 1 LIMIT 3
                """,
                [name, list(_READABLE_SCHEMAS)],
            )
            if elsewhere:
                raise PgHarborError(
                    f"{elsewhere[0]['name']} is not a system view, so this tool does not read it. "
                    "Use pg_top_queries for pg_stat_statements, or pg_query for other relations."
                )
            names = [
                r["name"]
                for r in await db.rows(
                    """
                    SELECT n.nspname || '.' || c.relname AS name
                      FROM pg_catalog.pg_class c
                      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                     WHERE n.nspname = ANY($1::text[]) AND c.relkind IN ('r', 'v', 'm', 'f', 'p')
                    """,
                    [list(_READABLE_SCHEMAS)],
                )
            ]
            by_bare_name = {n.split(".", 1)[1]: n for n in names}
            close = get_close_matches(name, list(by_bare_name), n=8, cutoff=0.6)
            similar = [{"name": by_bare_name[c]} for c in close]
            hint = (
                " Similar names: " + ", ".join(r["name"] for r in similar) + "." if similar else ""
            )
            raise PgHarborError(
                f"No view or catalog named {view!r} in pg_catalog or information_schema on "
                f"PostgreSQL {db.facts.label}; it may exist only in a newer or older "
                f"version.{hint}"
            )
        relation = found[0]
        available = [
            r["attname"]
            for r in await db.rows(
                "SELECT attname FROM pg_catalog.pg_attribute "
                "WHERE attrelid = $1 AND attnum > 0 AND NOT attisdropped ORDER BY attnum",
                [relation["oid"]],
            )
        ]
        selected = list(columns) if columns else available
        for label, wanted in (
            ("column", selected),
            ("order_by column", [order_by] if order_by else []),
        ):
            unknown = [c for c in wanted if c not in available]
            if unknown:
                raise PgHarborError(
                    f"Unknown {label} {', '.join(map(repr, unknown))} in "
                    f"{relation['schema']}.{relation['name']}. Available columns: "
                    + ", ".join(available)
                    + "."
                )
        qualified = f"{quote_ident(relation['schema'])}.{quote_ident(relation['name'])}"
        sql = f"SELECT {', '.join(quote_ident(c) for c in selected)} FROM {qualified}"
        if order_by:
            sql += f" ORDER BY {quote_ident(order_by)} {'DESC' if descending else 'ASC'} NULLS LAST"
        result = await run_query(db, sql, max_rows=limit)
    notes = [n for n in result.notes if not n.startswith("Only the first")]
    if result.has_more:
        notes.append(
            f"Only the first {len(result.rows)} rows are shown: raise limit or add order_by."
        )
    out: dict[str, Any] = {
        "connection": db.profile.name,
        "relation": f"{relation['schema']}.{relation['name']}",
        "columns": [c.name for c in result.columns],
        "rows": result.rows,
        "has_more": result.has_more,
    }
    if notes:
        out["notes"] = notes
    return out
