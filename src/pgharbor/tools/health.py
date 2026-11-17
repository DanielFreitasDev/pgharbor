"""DBA health tools: the consolidated check-up, the index advisor, configuration search,
sequence exhaustion and WAL/checkpoint behaviour.

Nothing here writes. The check-up runs about two dozen small checks, each inside its own
savepoint, so a check that fails or does not exist on this server version is reported as
`not_available` and the others still run.
"""

from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

import psycopg

from ..app import get_app, tool
from ..errors import PgHarborError
from ..guard import analyze, quote_ident, quote_qualified
from ..runner import estimate_plan, run_query
from ..session import Db, describe_db_error
from ._util import USER_SCHEMA_FILTER, clamp, grid, like_pattern, page
from .performance import (
    attempt,
    find_duplicate_indexes,
    format_setting,
    name_filters,
    schema_and_table,
    size_text,
)

__all__ = [
    "pg_health_check",
    "pg_index_advisor",
    "pg_sequences_health",
    "pg_settings",
    "pg_wal_checkpoints",
]

_WRAPAROUND = 2147483648

# Limits of the check-up. Each pair is (warning, critical) unless said otherwise.
_CONNECTIONS_PERCENT = (80.0, 95.0)
_LONG_TRANSACTION_SECONDS = (300, 3600)
_IDLE_IN_TRANSACTION_SECONDS = (300, 3600)  # any such session is at least "info"
_BLOCKED_SECONDS = (30, 300)  # any blocked session is at least "info"
_WRAPAROUND_PERCENT = (50.0, 75.0)
_DEAD_ROWS = {"min_rows": 1000, "warning_percent": 20.0, "critical_percent": 50.0}
_SEQUENCE_PERCENT = (75.0, 90.0)
_CACHE_HIT_PERCENT = (95.0, 85.0)  # warning below the first, critical below the second
_CACHE_MIN_BLOCKS = 100_000  # below this the ratio says little
_FORCED_CHECKPOINT_PERCENT = (50.0, 80.0)
_CHECKPOINT_MIN_COUNT = 10
_ROLLBACK_PERCENT = (10.0, 30.0)
_ROLLBACK_MIN_TRANSACTIONS = 1000
_DEADLOCKS = 10  # warning from this many since the statistics reset
_TEMP_BYTES_WARNING = 5 * 1024**3
_PREPARED_SECONDS = (3600, 86400)
_RETAINED_WAL_CRITICAL = 1024**3
_NEVER_ANALYZED_WARNING_ROWS = 10_000


# ------------------------------------------------------------------------------ WAL helpers


def _wal_names(db: Db) -> dict[str, str]:
    """Function names that changed from xlog/location (9.6) to wal/lsn (10+)."""
    if db.v >= 100000:
        return {
            "current": "pg_current_wal_lsn",
            "replay": "pg_last_wal_replay_lsn",
            "diff": "pg_wal_lsn_diff",
            "file": "pg_walfile_name",
        }
    return {
        "current": "pg_current_xlog_location",
        "replay": "pg_last_xlog_replay_location",
        "diff": "pg_xlog_location_diff",
        "file": "pg_xlogfile_name",
    }


async def _checkpoint_counters(db: Db) -> dict[str, Any]:
    """Checkpoint counters under one set of names. The checkpoint columns moved from
    pg_stat_bgwriter to pg_stat_checkpointer in PostgreSQL 17."""
    block = "pg_catalog.current_setting('block_size')::bigint"
    if db.has("pg_stat_checkpointer"):
        row = await db.row(
            f"""
            SELECT c.num_timed AS timed, c.num_requested AS requested,
                   c.write_time AS write_ms, c.sync_time AS sync_ms,
                   c.buffers_written * {block} AS checkpoint_bytes,
                   b.buffers_clean * {block} AS bgwriter_bytes, b.maxwritten_clean,
                   b.buffers_alloc, c.stats_reset,
                   extract(epoch FROM pg_catalog.clock_timestamp() - c.stats_reset) AS seconds
              FROM pg_catalog.pg_stat_checkpointer c CROSS JOIN pg_catalog.pg_stat_bgwriter b
            """
        )
    else:
        row = await db.row(
            f"""
            SELECT checkpoints_timed AS timed, checkpoints_req AS requested,
                   checkpoint_write_time AS write_ms, checkpoint_sync_time AS sync_ms,
                   buffers_checkpoint * {block} AS checkpoint_bytes,
                   buffers_clean * {block} AS bgwriter_bytes, maxwritten_clean,
                   buffers_backend * {block} AS backend_bytes, buffers_alloc, stats_reset,
                   extract(epoch FROM pg_catalog.clock_timestamp() - stats_reset) AS seconds
              FROM pg_catalog.pg_stat_bgwriter
            """
        )
    assert row is not None
    return row


def checkpoint_reading(timed: int, requested: int, max_wal_size: str | None = None) -> str:
    """One sentence that says how the checkpoints behave."""
    total = timed + requested
    if total < _CHECKPOINT_MIN_COUNT:
        return (
            f"Only {total} checkpoint(s) since the statistics were reset: too few to judge "
            "how often they are forced."
        )
    percent = 100.0 * requested / total
    size = f" (now {max_wal_size})" if max_wal_size else ""
    if percent >= _FORCED_CHECKPOINT_PERCENT[0]:
        return (
            f"Checkpoints are forced too often: {percent:.0f}% were requested because WAL "
            f"filled up; raise max_wal_size{size} so they follow checkpoint_timeout."
        )
    if percent >= 20.0:
        return (
            f"{percent:.0f}% of the checkpoints are forced by WAL volume; consider a larger "
            f"max_wal_size{size} if write bursts are common."
        )
    return f"Checkpoints are mostly scheduled ({percent:.0f}% forced): no change needed."


# ----------------------------------------------------------------------------- pg_settings

# Settings pgharbor itself applies to its session; the server's own value is not visible.
_PGHARBOR_SETTINGS = (
    "statement_timeout",
    "lock_timeout",
    "idle_in_transaction_session_timeout",
    "standard_conforming_strings",
    "datestyle",
    "intervalstyle",
    "bytea_output",
    "extra_float_digits",
    "default_transaction_read_only",
    "transaction_read_only",
)


@tool(group="health", title="Search settings")
async def pg_settings(
    connection: str | None = None,
    pattern: str | None = None,
    category: str | None = None,
    changed_only: bool = False,
    pending_restart_only: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    """Search server configuration parameters (GUCs): current value with unit, default,
    where the value comes from (default, configuration file with line, ALTER SYSTEM, ...),
    whether changing it needs a restart, whether a restart is pending, and a short
    description. Also lists errors found in the configuration files and the per-database and
    per-role overrides (ALTER DATABASE/ROLE ... SET).

    `pattern` matches parameter names (and descriptions), with `*` wildcards; `category`
    filters by group (for example "Write-Ahead Log"); `changed_only` hides parameters still at
    their default; `pending_restart_only` shows what waits for a restart.
    """
    limit = clamp(limit, 100, 500)
    like = like_pattern(pattern)
    category_like = like_pattern(category)
    async with get_app().read(connection) as db:
        rows = await db.rows(
            """
            SELECT name, setting, unit, boot_val, source, context, pending_restart, category,
                   left(short_desc, 160) AS description,
                   CASE WHEN sourcefile IS NOT NULL
                        THEN sourcefile || ':' || sourceline::text END AS file
              FROM pg_catalog.pg_settings
             WHERE ($1::text IS NULL OR name ILIKE $1 OR short_desc ILIKE $1)
               AND ($2::text IS NULL OR category ILIKE $2)
               AND (NOT $3::bool OR (source NOT IN ('default', 'override')
                    AND NOT (name = ANY($6::text[]) AND source IN ('session', 'client'))))
               AND (NOT $4::bool OR pending_restart)
             ORDER BY coalesce(name ILIKE $1, false) DESC, category, name
             LIMIT $5
            """,
            [
                like,
                category_like,
                changed_only,
                pending_restart_only,
                limit + 1,
                list(_PGHARBOR_SETTINGS),
            ],
        )
        rows, has_more = page(rows, limit)
        file_errors: list[dict[str, Any]] = []
        overridden = 0
        file_note: str | None = None
        found, reason = await attempt(
            db,
            lambda: db.rows(
                """
                SELECT sourcefile AS file, sourceline AS line, name, setting, error, applied
                  FROM pg_catalog.pg_file_settings
                 WHERE NOT applied
                 ORDER BY sourcefile, sourceline
                """
            ),
        )
        if found is not None:
            file_errors = [r for r in found if r["error"]][:20]
            overridden = sum(1 for r in found if not r["error"])
        else:
            file_note = (
                "The configuration files could not be checked for errors "
                f"(pg_file_settings is for superusers): {reason}"
            )
        overrides: list[dict[str, Any]] = []
        if not pending_restart_only:
            overrides = await db.rows(
                """
                SELECT coalesce(d.datname, '(all databases)') AS database,
                       coalesce(r.rolname, '(all roles)') AS role, s.setconfig AS settings
                  FROM pg_catalog.pg_db_role_setting s
                  LEFT JOIN pg_catalog.pg_database d ON d.oid = s.setdatabase
                  LEFT JOIN pg_catalog.pg_roles r ON r.oid = s.setrole
                 WHERE ($1::text IS NULL OR EXISTS (
                           SELECT 1 FROM unnest(s.setconfig) AS c WHERE c ILIKE $1))
                 ORDER BY 1, 2
                 LIMIT 50
                """,
                [like],
            )
    notes: list[str] = []
    own_names: list[str] = []
    table: list[dict[str, Any]] = []
    for row in rows:
        own = row["name"] in _PGHARBOR_SETTINGS and row["source"] in ("session", "client")
        table.append(
            {
                "name": row["name"],
                "value": format_setting(row["setting"], row["unit"]),
                "default": format_setting(row["boot_val"], row["unit"]),
                "source": "pgharbor session" if own else row["source"],
                "file": row["file"],
                "restart": row["context"] == "postmaster",
                "context": row["context"],
                "pending_restart": row["pending_restart"],
                "category": row["category"],
                "description": row["description"],
            }
        )
        if own:
            own_names.append(row["name"])
    out: dict[str, Any] = {"connection": db.profile.name, "count": len(table)}
    out.update(
        grid(
            table,
            [
                "name",
                "value",
                "default",
                "source",
                "file",
                "restart",
                "pending_restart",
                "category",
                "description",
            ],
        )
    )
    out["has_more"] = has_more
    if file_errors:
        out["config_file_errors"] = grid(file_errors, ["file", "line", "name", "setting", "error"])
    if overridden:
        out["config_file_entries_overridden_by_later_lines"] = overridden
    if overrides:
        out["database_and_role_settings"] = grid(overrides, ["database", "role", "settings"])
    if own_names:
        notes.append(
            f"{', '.join(own_names)}: pgharbor sets these for its own session, so the value shown "
            "is pgharbor's, not the server's."
        )
    if file_note:
        notes.append(file_note)
    if not table:
        out["hint"] = (
            "No parameter matched. Try a shorter pattern such as 'wal', or leave pattern empty."
        )
    if notes:
        out["notes"] = notes
    return out


# ---------------------------------------------------------------------- sequences (shared)

_COLUMN_LIMITS = {21: 32767, 23: 2147483647, 20: 9223372036854775807}  # int2, int4, int8
_COLUMN_TYPES = {21: "smallint", 23: "integer", 20: "bigint"}
_SEQUENCES_96_CAP = 200

_OWNER_JOIN = """
  LEFT JOIN LATERAL (
        SELECT tn.nspname || '.' || tc.relname || '.' || a.attname AS owner,
               a.atttypid::int AS type_oid
          FROM pg_catalog.pg_depend d
          JOIN pg_catalog.pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
          JOIN pg_catalog.pg_class tc ON tc.oid = a.attrelid
          JOIN pg_catalog.pg_namespace tn ON tn.oid = tc.relnamespace
         WHERE d.classid = 'pg_catalog.pg_class'::pg_catalog.regclass AND d.objid = c.oid
           AND d.refclassid = 'pg_catalog.pg_class'::pg_catalog.regclass
           AND d.deptype IN ('a', 'i')
         LIMIT 1) o ON true
"""


async def _read_sequences(db: Db) -> tuple[list[dict[str, Any]], list[str]]:
    """Every sequence with its current position, limit and owning column."""
    notes: list[str] = []
    if db.v >= 100000:
        rows = await db.rows(
            f"""
            SELECT n.nspname AS schema, c.relname AS sequence,
                   pg_catalog.format_type(s.seqtypid, NULL) AS type, s.seqmin AS min_value,
                   s.seqmax AS max_value, s.seqincrement AS increment_by, s.seqcycle AS cycle,
                   CASE WHEN pg_catalog.has_sequence_privilege(c.oid, 'SELECT,USAGE')
                        THEN pg_catalog.pg_sequence_last_value(c.oid) END AS last_value,
                   pg_catalog.has_sequence_privilege(c.oid, 'SELECT,USAGE') AS readable,
                   o.owner, o.type_oid
              FROM pg_catalog.pg_sequence s
              JOIN pg_catalog.pg_class c ON c.oid = s.seqrelid
              JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
              {_OWNER_JOIN}
             WHERE n.nspname !~ '^pg_(toast|temp)'
             ORDER BY n.nspname, c.relname
            """
        )
        if any(not r["readable"] for r in rows):
            notes.append(
                "Some sequences could not be read: the role lacks USAGE or SELECT on them "
                "(or on their schema)."
            )
        return rows, notes
    # PostgreSQL 9.6 has no pg_sequences view: list the sequences from the catalog and read
    # each one (a sequence is a one-row relation there).
    catalog = await db.rows(
        f"""
        SELECT n.nspname AS schema, c.relname AS sequence, c.oid::bigint AS oid,
               o.owner, o.type_oid,
               pg_catalog.has_schema_privilege(n.oid, 'USAGE')
                   AND pg_catalog.has_sequence_privilege(c.oid, 'SELECT') AS readable
          FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
          {_OWNER_JOIN}
         WHERE c.relkind = 'S' AND n.nspname !~ '^pg_(toast|temp)'
         ORDER BY n.nspname, c.relname
         LIMIT $1
        """,
        [_SEQUENCES_96_CAP + 1],
    )
    if len(catalog) > _SEQUENCES_96_CAP:
        catalog = catalog[:_SEQUENCES_96_CAP]
        notes.append(
            f"Only the first {_SEQUENCES_96_CAP} sequences (by name) were read: PostgreSQL 9.6 "
            "needs one read per sequence."
        )
    if any(not r["readable"] for r in catalog):
        notes.append(
            "Some sequences could not be read: the role lacks USAGE or SELECT on them "
            "(or on their schema)."
        )
        catalog = [r for r in catalog if r["readable"]]
    if not catalog:
        return [], notes
    union = "\nUNION ALL\n".join(
        f"SELECT {r['oid']}::bigint AS oid, last_value, min_value, max_value, increment_by, "
        f"is_cycled, is_called FROM {quote_qualified(r['schema'], r['sequence'])}"
        for r in catalog
    )
    state = await run_query(db, union, max_rows=_SEQUENCES_96_CAP + 1, max_cell_chars=64)
    columns = [c.name for c in state.columns]
    by_oid = {row[0]: dict(zip(columns, row, strict=True)) for row in state.rows}
    rows = []
    for item in catalog:
        info = by_oid.get(item["oid"])
        if info is None:
            continue
        rows.append(
            {
                "schema": item["schema"],
                "sequence": item["sequence"],
                "type": "bigint",
                "min_value": info["min_value"],
                "max_value": info["max_value"],
                "increment_by": info["increment_by"],
                "cycle": info["is_cycled"],
                "last_value": info["last_value"] if info["is_called"] else None,
                "owner": item["owner"],
                "type_oid": item["type_oid"],
            }
        )
    return rows, notes


def _sequence_usage(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """How much of the usable range each sequence has consumed. The usable range stops at
    the end of the sequence or at the maximum of the column it feeds, whichever is first."""
    out: list[dict[str, Any]] = []
    for row in rows:
        ascending = row["increment_by"] > 0
        low, high = row["min_value"], row["max_value"]
        column_max = _COLUMN_LIMITS.get(row["type_oid"]) if row["owner"] else None
        source = "sequence"
        if ascending and column_max is not None and column_max < high:
            high, source = column_max, f"column type ({_COLUMN_TYPES[row['type_oid']]})"
        last = row["last_value"]
        if last is None:
            used, remaining = 0.0, None
        elif ascending:
            span = max(high - low, 1)
            used = 100.0 * (last - low) / span
            remaining = max(high - last, 0) // row["increment_by"]
        else:
            span = max(high - low, 1)
            used = 100.0 * (high - last) / span
            remaining = max(last - low, 0) // -row["increment_by"]
        out.append(
            {
                **row,
                "usable_max": high,
                "used_pct": round(max(0.0, min(100.0, used)), 1),
                "values_left": remaining,
                "limit_source": source,
            }
        )
    out.sort(key=lambda r: (-r["used_pct"], r["schema"], r["sequence"]))
    return out


@tool(group="health", title="Sequence exhaustion")
async def pg_sequences_health(
    connection: str | None = None, threshold_percent: int = 50, limit: int = 50
) -> dict[str, Any]:
    """Find sequences that are running out of numbers: how much of the usable range each one
    has consumed, how many values are left, and whether the column it feeds (serial or
    identity) is a smaller integer type than the sequence, which makes the column overflow
    first.

    Only sequences at or above `threshold_percent` of their range are listed (default 50).
    Sequences that cycle wrap around instead of failing and are flagged. A smallint or
    integer key that is close to its limit needs an ALTER COLUMN ... TYPE bigint long
    before the sequence ends.
    """
    limit = clamp(limit, 50, 500)
    threshold = max(0, min(int(threshold_percent), 100))
    async with get_app().read(connection) as db:
        rows, notes = await _read_sequences(db)
    usage = _sequence_usage(rows)
    flagged = [r for r in usage if r["used_pct"] >= threshold]
    shown, has_more = page(flagged, limit)
    for row in shown:
        row["column"] = row["owner"]
    mismatches = [
        {
            "column": r["owner"],
            "column_type": _COLUMN_TYPES[r["type_oid"]],
            "sequence": f"{r['schema']}.{r['sequence']}",
            "sequence_max": r["max_value"],
            "used_pct": r["used_pct"],
        }
        for r in usage
        if r["owner"]
        and r["type_oid"] in _COLUMN_LIMITS
        and r["increment_by"] > 0
        and _COLUMN_LIMITS[r["type_oid"]] < r["max_value"]
    ]
    out: dict[str, Any] = {
        "connection": db.profile.name,
        "sequences_checked": len(usage),
        "threshold_percent": threshold,
        **grid(
            shown,
            [
                "schema",
                "sequence",
                "type",
                "last_value",
                "usable_max",
                "used_pct",
                "values_left",
                "cycle",
                "column",
                "limit_source",
            ],
        ),
        "has_more": has_more,
    }
    if mismatches:
        mismatches.sort(key=lambda m: -m["used_pct"])
        out["column_smaller_than_sequence"] = grid(
            mismatches[:limit],
            ["column", "column_type", "sequence", "sequence_max", "used_pct"],
        )
        notes.append(
            "column_smaller_than_sequence lists integer or smallint columns fed by a sequence "
            "that could go higher: the column overflows first, so used_pct is measured against "
            "the column's maximum. On PostgreSQL 9.6 every serial column is in this situation."
        )
    if any(r["cycle"] for r in shown):
        notes.append("Sequences marked cycle=true wrap around at the end instead of failing.")
    if not shown:
        out["message"] = f"No sequence has used {threshold}% of its range."
    if notes:
        out["notes"] = notes
    return out


# -------------------------------------------------------------------------- pg_wal_checkpoints


@tool(group="health", title="WAL and checkpoints")
async def pg_wal_checkpoints(connection: str | None = None) -> dict[str, Any]:
    """Explain how checkpoints and WAL behave: scheduled versus forced checkpoints and the
    buffers they wrote (pg_stat_bgwriter up to PostgreSQL 16, pg_stat_checkpointer from 17),
    WAL volume (pg_stat_wal, 14+), the archiver (pg_stat_archiver), the current WAL position
    and the WAL-related settings, ending with a one-line reading such as "forced
    checkpoints: raise max_wal_size".

    Use it for write-heavy servers, I/O spikes every few minutes and archiving problems.
    Counters accumulate since the statistics reset.
    """
    async with get_app().read(connection) as db:
        counters = await _checkpoint_counters(db)
        names = _wal_names(db)
        wal: dict[str, Any] | None = None
        if db.has("pg_stat_wal"):
            wal = await db.row(
                """
                SELECT wal_records AS records, wal_fpi AS full_page_images,
                       pg_catalog.pg_size_pretty(wal_bytes) AS generated, wal_buffers_full,
                       wal_bytes::float8 AS bytes,
                       extract(epoch FROM pg_catalog.clock_timestamp() - stats_reset) AS seconds
                  FROM pg_catalog.pg_stat_wal
                """
            )
        archiver = await db.row(
            """
            SELECT archived_count, last_archived_wal,
                   date_trunc('second', last_archived_time)::text AS last_archived_time,
                   failed_count, last_failed_wal,
                   date_trunc('second', last_failed_time)::text AS last_failed_time,
                   coalesce(last_failed_time > last_archived_time, last_failed_time IS NOT NULL)
                       AS failing
              FROM pg_catalog.pg_stat_archiver
            """
        )
        position: dict[str, Any] = {"in_recovery": db.facts.in_recovery}
        if db.facts.in_recovery:
            position["replayed_lsn"] = await db.value(
                f"SELECT pg_catalog.{names['replay']}()::text"
            )
        else:
            current = await db.row(
                f"""
                SELECT pg_catalog.{names["current"]}()::text AS lsn,
                       pg_catalog.{names["file"]}(pg_catalog.{names["current"]}()) AS file
                """
            )
            assert current is not None
            position["current_lsn"], position["current_file"] = current["lsn"], current["file"]
        wanted = (
            "wal_level",
            "max_wal_size",
            "min_wal_size",
            "checkpoint_timeout",
            "checkpoint_completion_target",
            "checkpoint_warning",
            "wal_buffers",
            "full_page_writes",
            "wal_compression",
            "wal_log_hints",
            "synchronous_commit",
            "fsync",
            "wal_sync_method",
            "wal_writer_delay",
            "wal_keep_segments",
            "wal_keep_size",
            "max_slot_wal_keep_size",
            "max_wal_senders",
            "archive_mode",
            "archive_timeout",
            "track_wal_io_timing",
        )
        settings = {
            r["name"]: format_setting(r["setting"], r["unit"])
            for r in await db.rows(
                "SELECT name, setting, unit FROM pg_catalog.pg_settings "
                "WHERE name = ANY($1::text[]) ORDER BY name",
                [list(wanted)],
            )
        }
    timed, requested = counters["timed"], counters["requested"]
    total = timed + requested
    seconds = counters["seconds"] or 0
    checkpoints: dict[str, Any] = {
        "scheduled": timed,
        "forced": requested,
        "forced_pct": round(100.0 * requested / total, 1) if total else None,
        "average_minutes_between": round(seconds / total / 60, 1) if total and seconds else None,
        "write_seconds": round((counters["write_ms"] or 0) / 1000, 1),
        "sync_seconds": round((counters["sync_ms"] or 0) / 1000, 1),
        "written_by_checkpoints": size_text(counters["checkpoint_bytes"]),
        "written_by_bgwriter": size_text(counters["bgwriter_bytes"]),
        "bgwriter_stopped_at_limit": counters["maxwritten_clean"],
        "buffers_allocated": counters["buffers_alloc"],
        "counters_since": str(counters["stats_reset"])[:19],
    }
    if "backend_bytes" in counters:
        checkpoints["written_by_backends"] = size_text(counters["backend_bytes"])
    out: dict[str, Any] = {
        "connection": db.profile.name,
        "reading": checkpoint_reading(timed, requested, settings.get("max_wal_size")),
        "checkpoints": checkpoints,
    }
    notes: list[str] = []
    if wal is not None:
        generated = {
            "records": wal["records"],
            "full_page_images": wal["full_page_images"],
            "generated": wal["generated"],
            "buffers_full": wal["wal_buffers_full"],
        }
        if wal["seconds"]:
            generated["per_hour"] = size_text(wal["bytes"] * 3600 / wal["seconds"])
        out["wal"] = generated
    else:
        notes.append("WAL volume statistics (pg_stat_wal) need PostgreSQL 14 or newer.")
    if archiver is not None:
        failing = bool(archiver.pop("failing"))
        out["archiver"] = {k: v for k, v in archiver.items() if v is not None}
        if failing:
            out["reading"] += (
                " WAL archiving is failing: the last attempt failed after the last success, "
                "so WAL piles up in pg_wal until it is fixed."
            )
    out["position"] = position
    out["settings"] = settings
    if db.facts.in_recovery:
        notes.append("This server is a standby: the position is the last replayed LSN.")
    if notes:
        out["notes"] = notes
    return out


# ------------------------------------------------------------------------------ pg_index_advisor

_SEQ_SCAN_MIN_ROWS = 10_000
_SEQ_SCAN_MIN_SCANS = 20
_SEQ_SCAN_MIN_PERCENT = 90.0

_FOREIGN_KEY_SQL = """
SELECT n.nspname AS schema, c.relname AS "table", con.conname AS constraint,
       rn.nspname || '.' || rc.relname AS references,
       cols.names AS columns,
       CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END AS rows_estimate,
       pg_catalog.pg_size_pretty(pg_catalog.pg_relation_size(c.oid)) AS size,
       'CREATE INDEX ON ' || pg_catalog.quote_ident(n.nspname) || '.'
           || pg_catalog.quote_ident(c.relname) || ' (' || cols.quoted || ')' AS suggestion
  FROM pg_catalog.pg_constraint con
  JOIN pg_catalog.pg_class c ON c.oid = con.conrelid
  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_catalog.pg_class rc ON rc.oid = con.confrelid
  JOIN pg_catalog.pg_namespace rn ON rn.oid = rc.relnamespace
 CROSS JOIN LATERAL (
       SELECT string_agg(a.attname, ', ' ORDER BY k.ord) AS names,
              string_agg(pg_catalog.quote_ident(a.attname), ', ' ORDER BY k.ord) AS quoted
         FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
         JOIN pg_catalog.pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
       ) cols
 WHERE con.contype = 'f' {parent_only}
   AND c.relkind IN ('r', 'p') {not_partition}
   AND ($1::text IS NULL OR n.nspname = $1)
   AND ($1::text IS NOT NULL OR ({user_schemas}))
   AND ($2::text IS NULL OR c.relname = $2)
   AND ($3::text IS NULL OR c.relname LIKE $3)
   AND NOT EXISTS (
       SELECT 1 FROM pg_catalog.pg_index i
        WHERE i.indrelid = con.conrelid AND i.indisvalid AND i.indpred IS NULL
          AND (string_to_array(i.indkey::text, ' ')::int2[])[1:cardinality(con.conkey)]
              @> con.conkey
          AND con.conkey @>
              (string_to_array(i.indkey::text, ' ')::int2[])[1:cardinality(con.conkey)])
 ORDER BY c.reltuples DESC, n.nspname, c.relname, con.conname
 LIMIT $4
"""


def _hypothetical_commands(commands: list[str], max_chars: int) -> list[str]:
    """Each item must be exactly one CREATE [UNIQUE] INDEX command."""
    if len(commands) > 10:
        raise PgHarborError("At most 10 hypothetical indexes can be evaluated at once.")
    checked: list[str] = []
    for command in commands:
        analysis = analyze(command, max_chars=max_chars)  # one statement, balanced, no tricks
        starts_ok = (
            len(analysis.tokens) >= 3
            and analysis.tokens[0].is_word("create")
            and (
                analysis.tokens[1].is_word("index")
                or (analysis.tokens[1].is_word("unique") and analysis.tokens[2].is_word("index"))
            )
        )
        if not starts_ok:
            raise PgHarborError(
                f"{command!r} is not a CREATE INDEX command. Each item of hypothetical_indexes "
                "must be a single statement starting with CREATE INDEX (or CREATE UNIQUE INDEX)."
            )
        checked.append(analysis.sql)
    return checked


def _scan_labels(summary: Any) -> list[str]:
    return [f"{scan.node_type} on {scan.qualified}" for scan in summary.scans]


async def _compare_plans(
    db: Db, sql: str, commands: list[str], extension_schema: str
) -> dict[str, Any]:
    prefix = quote_ident(extension_schema)
    before = await estimate_plan(db, sql)
    created: list[dict[str, Any]] = []
    await db.rows("SAVEPOINT pgharbor_hypopg")
    try:
        for command in commands:
            try:
                row = await db.row(
                    f"SELECT indexrelid::bigint AS oid, indexname AS name "
                    f"FROM {prefix}.hypopg_create_index($1)",
                    [command],
                )
            except psycopg.Error as exc:
                raise PgHarborError(
                    f"hypopg could not create {command!r}: {describe_db_error(exc, db.profile)}"
                ) from None
            assert row is not None
            created.append({"name": row["name"], "definition": command, "oid": row["oid"]})
        after = await estimate_plan(db, sql)
        for item in created:
            size = await db.value(
                f"SELECT pg_catalog.pg_size_pretty({prefix}.hypopg_relation_size($1::bigint::oid))",
                [item["oid"]],
            )
            item["estimated_size"] = size
            del item["oid"]
    finally:
        # hypopg keeps its indexes in backend memory, outside the transaction: always clear them.
        await db.rows("ROLLBACK TO SAVEPOINT pgharbor_hypopg")
        await db.rows(f"SELECT {prefix}.hypopg_reset()")
        await db.rows("RELEASE SAVEPOINT pgharbor_hypopg")
    improvement = (
        round(100.0 * (before.total_cost - after.total_cost) / before.total_cost, 1)
        if before.total_cost
        else 0.0
    )
    if improvement >= 1.0:
        verdict = (
            f"The planner would use the hypothetical index(es): estimated cost drops "
            f"{improvement}% ({before.total_cost:.2f} to {after.total_cost:.2f}). Create them "
            "for real only after checking the write overhead."
        )
    else:
        verdict = (
            "The planner does not benefit from these indexes for this query: the estimated "
            "cost is the same. Check that the index columns match the filter, join and ORDER BY "
            "of the query, and that the table is large enough for an index to pay off."
        )
    return {
        "hypothetical_indexes": created,
        "before": {"cost": round(before.total_cost, 2), "scans": _scan_labels(before)},
        "after": {"cost": round(after.total_cost, 2), "scans": _scan_labels(after)},
        "improvement_percent": improvement,
        "verdict": verdict,
    }


@tool(group="health", title="Index advisor")
async def pg_index_advisor(
    connection: str | None = None,
    sql: str | None = None,
    table: str | None = None,
    hypothetical_indexes: list[str] | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Suggest indexes. Without `sql` it lists foreign keys with no supporting index (which
    make deletes and joins on the parent scan the child table) and large tables read almost
    only by sequential scans, each with a ready CREATE INDEX suggestion where one is clear.
    With `sql` and `hypothetical_indexes` (a list of CREATE INDEX commands) it compares the
    planner's estimated cost of the query before and after the indexes, without creating
    anything: it needs the hypopg extension.

    `table` (name, `schema.name` or pattern) narrows the heuristics. The query is checked by
    the same rules as pg_query and is never executed, only planned. Indexes speed up reads and
    slow down writes: add only the ones a real query needs.
    """
    limit = clamp(limit, 20, 200)
    async with get_app().read(connection) as db:
        if sql is not None or hypothetical_indexes:
            if not sql:
                raise PgHarborError(
                    "hypothetical_indexes needs the `sql` query to evaluate them against."
                )
            if not hypothetical_indexes:
                raise PgHarborError(
                    "Give hypothetical_indexes: a list of CREATE INDEX commands to test against "
                    "the query. Without them pg_index_advisor (no sql) lists heuristic findings."
                )
            extension = db.need_ext("hypopg")
            commands = _hypothetical_commands(hypothetical_indexes, db.profile.max_sql_chars)
            result = await _compare_plans(db, sql, commands, extension.schema)
            return {"connection": db.profile.name, "query": sql, **result}

        schema, name = schema_and_table(None, table)
        exact, like = name_filters(name)
        foreign_keys = await db.rows(
            _FOREIGN_KEY_SQL.format(
                parent_only="AND con.conparentid = 0" if db.v >= 110000 else "",
                not_partition="AND NOT c.relispartition" if db.v >= 100000 else "",
                user_schemas=USER_SCHEMA_FILTER,
            ),
            [schema, exact, like, limit + 1],
        )
        foreign_keys, more_keys = page(foreign_keys, limit)
        scans = await db.rows(
            f"""
            SELECT n.nspname AS schema, c.relname AS "table", s.n_live_tup AS rows,
                   pg_catalog.pg_size_pretty(pg_catalog.pg_relation_size(c.oid)) AS size,
                   s.seq_scan, coalesce(s.idx_scan, 0) AS idx_scan, s.seq_tup_read,
                   round(s.seq_tup_read::numeric / nullif(s.seq_scan, 0)) AS rows_per_seq_scan,
                   round(100.0 * s.seq_scan / nullif(s.seq_scan + coalesce(s.idx_scan, 0), 0), 1)
                       AS seq_scan_pct
              FROM pg_catalog.pg_stat_all_tables s
              JOIN pg_catalog.pg_class c ON c.oid = s.relid
              JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
             WHERE c.relkind IN ('r', 'm') AND s.n_live_tup >= $1 AND s.seq_scan >= $2
               AND 100.0 * s.seq_scan / nullif(s.seq_scan + coalesce(s.idx_scan, 0), 0) >= $3
               AND ($4::text IS NULL OR n.nspname = $4)
               AND ($4::text IS NOT NULL OR ({USER_SCHEMA_FILTER}))
               AND ($5::text IS NULL OR c.relname = $5)
               AND ($6::text IS NULL OR c.relname LIKE $6)
             ORDER BY s.seq_tup_read DESC, n.nspname, c.relname
             LIMIT $7
            """,
            [
                _SEQ_SCAN_MIN_ROWS,
                _SEQ_SCAN_MIN_SCANS,
                _SEQ_SCAN_MIN_PERCENT,
                schema,
                exact,
                like,
                limit + 1,
            ],
        )
        scans, more_scans = page(scans, limit)
    out: dict[str, Any] = {
        "connection": db.profile.name,
        "foreign_keys_without_index": {
            **grid(
                foreign_keys,
                [
                    "schema",
                    "table",
                    "constraint",
                    "columns",
                    "references",
                    "rows_estimate",
                    "size",
                    "suggestion",
                ],
            ),
            "has_more": more_keys,
        },
        "large_tables_read_by_sequential_scans": {
            **grid(
                scans,
                [
                    "schema",
                    "table",
                    "rows",
                    "size",
                    "seq_scan",
                    "idx_scan",
                    "seq_tup_read",
                    "rows_per_seq_scan",
                    "seq_scan_pct",
                ],
            ),
            "has_more": more_scans,
        },
        "notes": [
            "A foreign key column without an index slows DELETE/UPDATE on the referenced table "
            "and joins from it; small or rarely joined tables may not need one.",
            f"Sequential-scan candidates: at least {_SEQ_SCAN_MIN_ROWS} rows, "
            f"{_SEQ_SCAN_MIN_SCANS} scans and {_SEQ_SCAN_MIN_PERCENT:.0f}% of reads by "
            "sequential scan since the "
            "statistics reset. pg_top_queries shows which queries cause them; test a candidate "
            "index with pg_index_advisor(sql=..., hypothetical_indexes=[...]) (needs hypopg).",
        ],
    }
    return out


# -------------------------------------------------------------------------- pg_health_check

_SEVERITY_RANK = {"critical": 0, "warning": 1, "not_available": 2, "info": 3, "ok": 4}


def _finding(
    check: str,
    severity: str,
    value: Any,
    threshold: str,
    message: str,
    see: str,
) -> dict[str, Any]:
    return {
        "check": check,
        "severity": severity,
        "value": value,
        "threshold": threshold,
        "message": message,
        "see": see,
    }


def _level(value: float, limits: tuple[float, float]) -> str:
    warning, critical = limits
    if value >= critical:
        return "critical"
    if value >= warning:
        return "warning"
    return "ok"


def _names(items: list[str], shown: int = 5) -> str:
    text = ", ".join(items[:shown])
    return text + (f" and {len(items) - shown} more" if len(items) > shown else "")


def _duration(seconds: float) -> str:
    if seconds >= 86400:
        return f"{seconds / 86400:.1f} days"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} hours"
    if seconds >= 60:
        return f"{seconds / 60:.0f} min"
    return f"{seconds:.0f} s"


def _client_filter(db: Db) -> str:
    """SQL condition on alias ``a`` selecting ordinary client sessions."""
    if db.v >= 100000:
        return "a.backend_type = 'client backend'"
    return "a.datid IS NOT NULL AND a.query NOT LIKE 'autovacuum:%'"


async def _check_connections(db: Db) -> dict[str, Any]:
    row = await db.row(
        f"""
        SELECT count(*) FILTER (WHERE {_client_filter(db)}) AS used,
               pg_catalog.current_setting('max_connections')::int AS max
          FROM pg_catalog.pg_stat_activity a
        """
    )
    assert row is not None
    percent = round(100.0 * row["used"] / row["max"], 1)
    return _finding(
        "connections",
        _level(percent, _CONNECTIONS_PERCENT),
        percent,
        f"warning >= {_CONNECTIONS_PERCENT[0]:.0f}%, critical >= {_CONNECTIONS_PERCENT[1]:.0f}%",
        f"{row['used']} of {row['max']} connections in use ({percent}%).",
        "pg_activity",
    )


async def _check_long_transactions(db: Db) -> dict[str, Any]:
    rows = await db.rows(
        f"""
        SELECT a.pid, a.state, a.usename,
               extract(epoch FROM pg_catalog.clock_timestamp() - a.xact_start) AS seconds
          FROM pg_catalog.pg_stat_activity a
         WHERE a.xact_start IS NOT NULL AND a.pid <> pg_catalog.pg_backend_pid()
           AND {_client_filter(db)}
         ORDER BY a.xact_start LIMIT 50
        """
    )
    warning, critical = _LONG_TRANSACTION_SECONDS
    long = [r for r in rows if r["seconds"] >= warning]
    oldest = rows[0]["seconds"] if rows else 0
    severity = _level(oldest, _LONG_TRANSACTION_SECONDS)
    message = (
        f"{len(long)} transaction(s) open for more than {_duration(warning)}; the oldest is "
        f"pid {rows[0]['pid']} ({rows[0]['usename']}, {rows[0]['state']}) open for "
        f"{_duration(oldest)}. Long transactions hold back vacuum and keep locks."
        if long
        else "No transaction has been open for long."
    )
    return _finding(
        "long_transactions",
        severity,
        round(oldest),
        f"oldest open transaction, warning >= {warning} s, critical >= {critical} s",
        message,
        "pg_activity",
    )


async def _check_idle_in_transaction(db: Db) -> dict[str, Any]:
    rows = await db.rows(
        f"""
        SELECT a.pid, a.usename,
               extract(epoch FROM pg_catalog.clock_timestamp() - a.state_change) AS seconds
          FROM pg_catalog.pg_stat_activity a
         WHERE a.state LIKE 'idle in transaction%' AND a.pid <> pg_catalog.pg_backend_pid()
           AND {_client_filter(db)}
         ORDER BY a.state_change LIMIT 50
        """
    )
    warning, critical = _IDLE_IN_TRANSACTION_SECONDS
    oldest = rows[0]["seconds"] if rows else 0
    if not rows:
        severity, message = "ok", "No session is idle in transaction."
    else:
        severity = _level(oldest, _IDLE_IN_TRANSACTION_SECONDS)
        if severity == "ok":
            severity = "info"
        message = (
            f"{len(rows)} session(s) idle in transaction; the oldest, pid {rows[0]['pid']} "
            f"({rows[0]['usename']}), for {_duration(oldest)}. They keep locks and block vacuum; "
            "fix the application or set idle_in_transaction_session_timeout."
        )
    return _finding(
        "idle_in_transaction",
        severity,
        len(rows),
        f"sessions, warning when one waits >= {warning} s, critical >= {critical} s",
        message,
        "pg_activity",
    )


async def _check_blocked_sessions(db: Db) -> dict[str, Any]:
    rows = await db.rows(
        """
        SELECT a.pid, extract(epoch FROM pg_catalog.clock_timestamp() - a.query_start) AS seconds,
               pg_catalog.pg_blocking_pids(a.pid) AS blockers
          FROM pg_catalog.pg_stat_activity a
         WHERE a.pid <> pg_catalog.pg_backend_pid()
           AND coalesce(array_length(pg_catalog.pg_blocking_pids(a.pid), 1), 0) > 0
         ORDER BY a.query_start LIMIT 50
        """
    )
    warning, critical = _BLOCKED_SECONDS
    if not rows:
        return _finding(
            "blocked_sessions",
            "ok",
            0,
            "sessions waiting for a lock",
            "No session is waiting for a lock.",
            "pg_locks",
        )
    longest = max(r["seconds"] or 0 for r in rows)
    severity = _level(longest, _BLOCKED_SECONDS)
    if severity == "ok":
        severity = "info"
    return _finding(
        "blocked_sessions",
        severity,
        len(rows),
        f"sessions, warning when one waits >= {warning} s, critical >= {critical} s",
        f"{len(rows)} session(s) wait for a lock held by another session (longest wait about "
        f"{_duration(longest)}).",
        "pg_locks",
    )


async def _check_wraparound(db: Db) -> dict[str, Any]:
    rows = await db.rows(
        """
        SELECT datname, pg_catalog.age(datfrozenxid) AS xid_age,
               pg_catalog.mxid_age(datminmxid) AS mxid_age
          FROM pg_catalog.pg_database WHERE datallowconn ORDER BY 2 DESC
        """
    )
    worst = max(rows, key=lambda r: max(r["xid_age"], r["mxid_age"]))
    xid_pct = round(100.0 * worst["xid_age"] / _WRAPAROUND, 1)
    mxid_pct = round(100.0 * worst["mxid_age"] / _WRAPAROUND, 1)
    percent = max(xid_pct, mxid_pct)
    return _finding(
        "xid_wraparound",
        _level(percent, _WRAPAROUND_PERCENT),
        percent,
        f"% of the 2^31 limit, warning >= {_WRAPAROUND_PERCENT[0]:.0f}%, "
        f"critical >= {_WRAPAROUND_PERCENT[1]:.0f}%",
        f"Oldest database: {worst['datname']}, transaction ids {xid_pct}% and multixact ids "
        f"{mxid_pct}% of the way to wraparound. The server stops accepting writes near 100%.",
        "pg_vacuum_status",
    )


async def _check_dead_tuples(db: Db) -> dict[str, Any]:
    rows = await db.rows(
        f"""
        SELECT n.nspname || '.' || c.relname AS name, s.n_dead_tup,
               round(100.0 * s.n_dead_tup / nullif(s.n_live_tup + s.n_dead_tup, 0), 1) AS pct
          FROM pg_catalog.pg_stat_all_tables s
          JOIN pg_catalog.pg_class c ON c.oid = s.relid
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE s.n_dead_tup >= $1 AND ({USER_SCHEMA_FILTER})
           AND 100.0 * s.n_dead_tup / nullif(s.n_live_tup + s.n_dead_tup, 0) >= $2
         ORDER BY s.n_dead_tup DESC LIMIT 20
        """,
        [_DEAD_ROWS["min_rows"], _DEAD_ROWS["warning_percent"]],
    )
    if not rows:
        return _finding(
            "dead_tuples",
            "ok",
            0,
            f"tables with >= {_DEAD_ROWS['min_rows']} dead rows and >= "
            f"{_DEAD_ROWS['warning_percent']:.0f}% dead",
            "No table has a large share of dead rows.",
            "pg_vacuum_status",
        )
    critical = any(
        r["pct"] >= _DEAD_ROWS["critical_percent"] and r["n_dead_tup"] >= 100_000 for r in rows
    )
    return _finding(
        "dead_tuples",
        "critical" if critical else "warning",
        len(rows),
        f"tables, warning at {_DEAD_ROWS['warning_percent']:.0f}% dead rows, critical at "
        f"{_DEAD_ROWS['critical_percent']:.0f}% with 100000+ dead rows",
        f"{len(rows)} table(s) with many dead rows: "
        + _names([f"{r['name']} ({r['pct']}%)" for r in rows])
        + ". Autovacuum is not keeping up.",
        "pg_vacuum_status",
    )


async def _check_invalid_indexes(db: Db) -> dict[str, Any]:
    rows = await db.rows(
        """
        SELECT n.nspname || '.' || ic.relname AS name
          FROM pg_catalog.pg_index i
          JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid
          JOIN pg_catalog.pg_namespace n ON n.oid = ic.relnamespace
         WHERE NOT i.indisvalid ORDER BY 1 LIMIT 50
        """
    )
    return _finding(
        "invalid_indexes",
        "warning" if rows else "ok",
        len(rows),
        "any invalid index",
        (
            f"{len(rows)} invalid index(es): {_names([r['name'] for r in rows])}. A failed "
            "CREATE INDEX CONCURRENTLY leaves them: they slow writes and are never used; drop "
            "and recreate them."
        )
        if rows
        else "No invalid index.",
        "pg_index_stats",
    )


_SIGNATURE_SQL = f"""
SELECT n.nspname AS schema, t.relname AS "table", ic.relname AS index, am.amname AS method,
       i.indrelid::bigint AS table_oid, i.indkey::text AS key_cols,
       i.indclass::text AS opclasses, i.indcollation::text AS collations,
       i.indoption::text AS options,
       coalesce(pg_catalog.pg_get_expr(i.indexprs, i.indrelid), '') AS exprs,
       coalesce(pg_catalog.pg_get_expr(i.indpred, i.indrelid), '') AS pred,
       i.indisunique AS "unique", i.indisprimary AS "primary",
       EXISTS (SELECT 1 FROM pg_catalog.pg_constraint k
                WHERE k.conindid = i.indexrelid) AS backs_constraint
  FROM pg_catalog.pg_index i
  JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid AND ic.relkind = 'i'
  JOIN pg_catalog.pg_class t ON t.oid = i.indrelid
  JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace
  JOIN pg_catalog.pg_am am ON am.oid = ic.relam
 WHERE ({USER_SCHEMA_FILTER})
 ORDER BY n.nspname, t.relname, ic.relname
 LIMIT 20000
"""


async def _check_duplicate_indexes(db: Db) -> dict[str, Any]:
    duplicates = find_duplicate_indexes(await db.rows(_SIGNATURE_SQL))
    return _finding(
        "duplicate_indexes",
        "warning" if duplicates else "ok",
        len(duplicates),
        "any index identical to another",
        (
            f"{len(duplicates)} duplicate index(es): "
            + _names([f"{d['schema']}.{d['index']} (same as {d['of']})" for d in duplicates])
            + ". Each costs disk space and slows every write."
        )
        if duplicates
        else "No duplicate index.",
        "pg_index_stats",
    )


async def _check_sequences(db: Db) -> dict[str, Any]:
    rows, _notes = await _read_sequences(db)
    usage = [r for r in _sequence_usage(rows) if not r["cycle"]]
    worst = usage[0] if usage else None
    percent = worst["used_pct"] if worst else 0.0
    risky = [r for r in usage if r["used_pct"] >= _SEQUENCE_PERCENT[0]]
    return _finding(
        "sequences",
        _level(percent, _SEQUENCE_PERCENT),
        percent,
        f"% of range used, warning >= {_SEQUENCE_PERCENT[0]:.0f}%, "
        f"critical >= {_SEQUENCE_PERCENT[1]:.0f}%",
        (
            f"{len(risky)} sequence(s) above {_SEQUENCE_PERCENT[0]:.0f}%: "
            + _names([f"{r['schema']}.{r['sequence']} ({r['used_pct']}%)" for r in risky])
            + ". When one ends, inserts fail."
        )
        if risky
        else f"{len(usage)} sequence(s) checked; the fullest is at {percent}%.",
        "pg_sequences_health",
    )


async def _check_cache_hit(db: Db) -> dict[str, Any]:
    row = await db.row(
        """
        SELECT blks_hit, blks_read,
               round(100.0 * blks_hit / nullif(blks_hit + blks_read, 0), 2) AS pct
          FROM pg_catalog.pg_stat_database WHERE datname = pg_catalog.current_database()
        """
    )
    assert row is not None
    limits = f"warning < {_CACHE_HIT_PERCENT[0]:.0f}%, critical < {_CACHE_HIT_PERCENT[1]:.0f}%"
    if row["blks_hit"] + row["blks_read"] < _CACHE_MIN_BLOCKS or row["pct"] is None:
        return _finding(
            "cache_hit_ratio",
            "info",
            row["pct"],
            limits,
            "Too little I/O since the statistics reset to judge the cache hit ratio.",
            "pg_cache_io",
        )
    percent = float(row["pct"])
    severity = (
        "critical"
        if percent < _CACHE_HIT_PERCENT[1]
        else "warning"
        if percent < _CACHE_HIT_PERCENT[0]
        else "ok"
    )
    return _finding(
        "cache_hit_ratio",
        severity,
        percent,
        limits,
        f"{percent}% of block requests were served from shared_buffers.",
        "pg_cache_io",
    )


async def _check_checkpoints(db: Db) -> dict[str, Any]:
    counters = await _checkpoint_counters(db)
    timed, requested = counters["timed"], counters["requested"]
    total = timed + requested
    limits = (
        f"% forced, warning >= {_FORCED_CHECKPOINT_PERCENT[0]:.0f}%, "
        f"critical >= {_FORCED_CHECKPOINT_PERCENT[1]:.0f}% (judged from {_CHECKPOINT_MIN_COUNT} "
        "checkpoints)"
    )
    if total < _CHECKPOINT_MIN_COUNT:
        return _finding(
            "checkpoints",
            "info",
            None,
            limits,
            checkpoint_reading(timed, requested),
            "pg_wal_checkpoints",
        )
    percent = round(100.0 * requested / total, 1)
    reading = checkpoint_reading(timed, requested)
    return _finding(
        "checkpoints",
        _level(percent, _FORCED_CHECKPOINT_PERCENT),
        percent,
        limits,
        reading,
        "pg_wal_checkpoints",
    )


async def _check_replication_slots(db: Db) -> dict[str, Any]:
    names = _wal_names(db)
    retained = (
        "NULL::numeric"
        if db.facts.in_recovery
        else f"pg_catalog.{names['diff']}(pg_catalog.{names['current']}(), restart_lsn)"
    )
    status = "wal_status" if db.v >= 130000 else "NULL::text"
    rows = await db.rows(
        f"""
        SELECT slot_name, active, {retained} AS retained_bytes, {status} AS wal_status
          FROM pg_catalog.pg_replication_slots ORDER BY slot_name LIMIT 100
        """
    )
    inactive = [r for r in rows if not r["active"]]
    lost = [r for r in rows if r["wal_status"] == "lost"]
    heavy = [r for r in inactive if (r["retained_bytes"] or 0) >= _RETAINED_WAL_CRITICAL]
    if not inactive and not lost:
        return _finding(
            "replication_slots",
            "ok",
            0,
            "inactive slots",
            f"{len(rows)} replication slot(s), all active.",
            "pg_stat_view(view='pg_replication_slots')",
        )
    severity = "critical" if heavy or lost else "warning"
    details = _names(
        [f"{r['slot_name']} (holding {size_text(r['retained_bytes'])})" for r in inactive]
    )
    return _finding(
        "replication_slots",
        severity,
        len(inactive),
        f"inactive slots, critical when one holds >= {size_text(_RETAINED_WAL_CRITICAL)} of WAL "
        "or is lost",
        f"{len(inactive)} inactive replication slot(s): {details}. An inactive slot keeps WAL "
        "on disk until it is used again or dropped"
        + (f"; {len(lost)} slot(s) lost their WAL" if lost else "")
        + ".",
        "pg_stat_view(view='pg_replication_slots')",
    )


async def _check_never_analyzed(db: Db) -> dict[str, Any]:
    rows = await db.rows(
        f"""
        SELECT n.nspname || '.' || c.relname AS name, s.n_live_tup
          FROM pg_catalog.pg_stat_all_tables s
          JOIN pg_catalog.pg_class c ON c.oid = s.relid
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE s.last_analyze IS NULL AND s.last_autoanalyze IS NULL AND s.n_live_tup > 0
           AND c.relkind IN ('r', 'm') AND ({USER_SCHEMA_FILTER})
         ORDER BY s.n_live_tup DESC LIMIT 50
        """
    )
    if not rows:
        return _finding(
            "never_analyzed",
            "ok",
            0,
            "tables with rows and no analyze",
            "Every table with rows has been analyzed.",
            "pg_table_stats",
        )
    big = rows[0]["n_live_tup"] >= _NEVER_ANALYZED_WARNING_ROWS
    return _finding(
        "never_analyzed",
        "warning" if big else "info",
        len(rows),
        f"tables with rows never analyzed, warning when one has >= {_NEVER_ANALYZED_WARNING_ROWS}",
        f"{len(rows)} table(s) were never analyzed: "
        + _names([r["name"] for r in rows])
        + ". The planner works without statistics; run ANALYZE.",
        "pg_table_stats",
    )


async def _check_no_primary_key(db: Db) -> dict[str, Any]:
    partition = "AND NOT c.relispartition" if db.v >= 100000 else ""
    inherited = (
        ""
        if db.v >= 100000
        else "AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_inherits h WHERE h.inhrelid = c.oid)"
    )
    rows = await db.rows(
        f"""
        SELECT n.nspname || '.' || c.relname AS name
          FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relkind IN ('r', 'p') AND ({USER_SCHEMA_FILTER}) {partition} {inherited}
           AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint k
                            WHERE k.conrelid = c.oid AND k.contype = 'p')
         ORDER BY 1 LIMIT 100
        """
    )
    return _finding(
        "tables_without_primary_key",
        "info" if rows else "ok",
        len(rows),
        "any table without a primary key",
        (
            f"{len(rows)} table(s) without a primary key: {_names([r['name'] for r in rows])}. "
            "Without one, rows cannot be addressed reliably, logical replication needs a replica "
            "identity and duplicates slip in."
        )
        if rows
        else "Every table has a primary key.",
        "pg_describe_table",
    )


async def _check_deadlocks(db: Db) -> dict[str, Any]:
    row = await db.row(
        """
        SELECT deadlocks, date_trunc('second', stats_reset)::text AS since
          FROM pg_catalog.pg_stat_database WHERE datname = pg_catalog.current_database()
        """
    )
    assert row is not None
    count = row["deadlocks"]
    severity = "ok" if count == 0 else "warning" if count >= _DEADLOCKS else "info"
    return _finding(
        "deadlocks",
        severity,
        count,
        f"deadlocks since the statistics reset, warning >= {_DEADLOCKS}",
        f"{count} deadlock(s) since {row['since'] or 'the server started'}."
        + (" Look for sessions that lock the same rows in a different order." if count else ""),
        "pg_database_stats",
    )


async def _check_temp_files(db: Db) -> dict[str, Any]:
    row = await db.row(
        """
        SELECT temp_files, temp_bytes FROM pg_catalog.pg_stat_database
         WHERE datname = pg_catalog.current_database()
        """
    )
    assert row is not None
    files, size = row["temp_files"], row["temp_bytes"]
    severity = "ok" if files == 0 else "warning" if size >= _TEMP_BYTES_WARNING else "info"
    return _finding(
        "temporary_files",
        severity,
        size,
        f"bytes spilled to disk, warning >= {size_text(_TEMP_BYTES_WARNING)}",
        f"{files} temporary file(s) totalling {size_text(size)} since the statistics reset."
        + (
            " Sorts and hashes exceed work_mem; see pg_top_queries(order_by='temp_blks_written')."
            if files
            else ""
        ),
        "pg_top_queries",
    )


async def _check_rollbacks(db: Db) -> dict[str, Any]:
    row = await db.row(
        """
        SELECT xact_commit, xact_rollback FROM pg_catalog.pg_stat_database
         WHERE datname = pg_catalog.current_database()
        """
    )
    assert row is not None
    total = row["xact_commit"] + row["xact_rollback"]
    limits = (
        f"% rolled back, warning >= {_ROLLBACK_PERCENT[0]:.0f}%, "
        f"critical >= {_ROLLBACK_PERCENT[1]:.0f}% (judged from {_ROLLBACK_MIN_TRANSACTIONS} "
        "transactions)"
    )
    percent = round(100.0 * row["xact_rollback"] / total, 1) if total else 0.0
    if total < _ROLLBACK_MIN_TRANSACTIONS:
        return _finding(
            "rollback_ratio",
            "info",
            percent,
            limits,
            f"Only {total} transactions since the statistics reset: too few to judge.",
            "pg_database_stats",
        )
    return _finding(
        "rollback_ratio",
        _level(percent, _ROLLBACK_PERCENT),
        percent,
        limits,
        f"{percent}% of {total} transactions ended in rollback"
        + (
            "; errors or retries in the application are likely."
            if percent >= _ROLLBACK_PERCENT[0]
            else "."
        ),
        "pg_database_stats",
    )


async def _check_pending_restart(db: Db) -> dict[str, Any]:
    rows = await db.rows(
        "SELECT name FROM pg_catalog.pg_settings WHERE pending_restart ORDER BY name"
    )
    return _finding(
        "settings_pending_restart",
        "warning" if rows else "ok",
        len(rows),
        "any setting changed but not yet in effect",
        f"{len(rows)} setting(s) wait for a restart: {_names([r['name'] for r in rows])}."
        if rows
        else "No setting waits for a restart.",
        "pg_settings",
    )


async def _check_autovacuum(db: Db) -> dict[str, Any]:
    state = {
        r["name"]: r["setting"]
        for r in await db.rows(
            "SELECT name, setting FROM pg_catalog.pg_settings "
            "WHERE name IN ('autovacuum', 'track_counts')"
        )
    }
    disabled_tables = await db.rows(
        f"""
        SELECT n.nspname || '.' || c.relname AS name
          FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relkind IN ('r', 'm') AND ({USER_SCHEMA_FILTER})
           AND c.reloptions::text[] && ARRAY['autovacuum_enabled=false', 'autovacuum_enabled=off']
         ORDER BY 1 LIMIT 50
        """
    )
    if state.get("autovacuum") == "off" or state.get("track_counts") == "off":
        which = "autovacuum" if state.get("autovacuum") == "off" else "track_counts"
        return _finding(
            "autovacuum",
            "critical",
            which + "=off",
            "autovacuum and track_counts on",
            f"{which} is off, so dead rows are never cleaned and transaction ids are never "
            "frozen automatically.",
            "pg_vacuum_status",
        )
    if disabled_tables:
        return _finding(
            "autovacuum",
            "warning",
            len(disabled_tables),
            "tables with autovacuum disabled",
            f"autovacuum is disabled on {len(disabled_tables)} table(s): "
            + _names([r["name"] for r in disabled_tables])
            + ".",
            "pg_vacuum_status",
        )
    return _finding(
        "autovacuum",
        "ok",
        "on",
        "autovacuum and track_counts on",
        "autovacuum is on for the server and for every table.",
        "pg_vacuum_status",
    )


async def _check_unsafe_settings(db: Db) -> dict[str, Any]:
    rows = await db.rows(
        "SELECT name, setting FROM pg_catalog.pg_settings "
        "WHERE name IN ('fsync', 'full_page_writes') AND setting = 'off' ORDER BY name"
    )
    return _finding(
        "durability_settings",
        "critical" if rows else "ok",
        [r["name"] for r in rows] or None,
        "fsync and full_page_writes on",
        (
            f"{_names([r['name'] for r in rows])} is off: a crash or power loss can corrupt the "
            "database. Acceptable only for throw-away data."
        )
        if rows
        else "fsync and full_page_writes are on.",
        "pg_settings",
    )


async def _check_prepared_transactions(db: Db) -> dict[str, Any]:
    rows = await db.rows(
        """
        SELECT gid, extract(epoch FROM pg_catalog.clock_timestamp() - prepared) AS seconds
          FROM pg_catalog.pg_prepared_xacts ORDER BY prepared LIMIT 50
        """
    )
    if not rows:
        return _finding(
            "prepared_transactions",
            "ok",
            0,
            "prepared transactions",
            "No prepared transaction.",
            "pg_stat_view(view='pg_prepared_xacts')",
        )
    oldest = rows[0]["seconds"]
    severity = _level(oldest, _PREPARED_SECONDS)
    if severity == "ok":
        severity = "info"
    return _finding(
        "prepared_transactions",
        severity,
        len(rows),
        f"prepared transactions, warning when one is older than {_duration(_PREPARED_SECONDS[0])}, "
        f"critical older than {_duration(_PREPARED_SECONDS[1])}",
        f"{len(rows)} prepared transaction(s); the oldest ({rows[0]['gid']}) is "
        f"{_duration(oldest)} old. An orphaned one holds locks and blocks vacuum until it is "
        "committed or rolled back.",
        "pg_stat_view(view='pg_prepared_xacts')",
    )


async def _check_archiver(db: Db) -> dict[str, Any]:
    row = await db.row(
        """
        SELECT failed_count, last_failed_wal,
               coalesce(last_failed_time > last_archived_time, last_failed_time IS NOT NULL)
                   AS failing
          FROM pg_catalog.pg_stat_archiver
        """
    )
    assert row is not None
    if row["failing"]:
        return _finding(
            "wal_archiving",
            "warning",
            row["failed_count"],
            "last attempt failed after the last success",
            f"WAL archiving is failing (last failed file {row['last_failed_wal']}); WAL piles "
            "up in pg_wal until the archive command works again.",
            "pg_wal_checkpoints",
        )
    return _finding(
        "wal_archiving",
        "ok",
        row["failed_count"],
        "last attempt failed after the last success",
        "No current archiving failure.",
        "pg_wal_checkpoints",
    )


_CHECKS: tuple[tuple[str, Callable[[Db], Awaitable[dict[str, Any]]], str], ...] = (
    ("connections", _check_connections, "pg_activity"),
    ("long_transactions", _check_long_transactions, "pg_activity"),
    ("idle_in_transaction", _check_idle_in_transaction, "pg_activity"),
    ("blocked_sessions", _check_blocked_sessions, "pg_locks"),
    ("xid_wraparound", _check_wraparound, "pg_vacuum_status"),
    ("dead_tuples", _check_dead_tuples, "pg_vacuum_status"),
    ("invalid_indexes", _check_invalid_indexes, "pg_index_stats"),
    ("duplicate_indexes", _check_duplicate_indexes, "pg_index_stats"),
    ("sequences", _check_sequences, "pg_sequences_health"),
    ("cache_hit_ratio", _check_cache_hit, "pg_cache_io"),
    ("checkpoints", _check_checkpoints, "pg_wal_checkpoints"),
    ("replication_slots", _check_replication_slots, "pg_stat_view"),
    ("never_analyzed", _check_never_analyzed, "pg_table_stats"),
    ("tables_without_primary_key", _check_no_primary_key, "pg_describe_table"),
    ("deadlocks", _check_deadlocks, "pg_database_stats"),
    ("temporary_files", _check_temp_files, "pg_top_queries"),
    ("rollback_ratio", _check_rollbacks, "pg_database_stats"),
    ("settings_pending_restart", _check_pending_restart, "pg_settings"),
    ("autovacuum", _check_autovacuum, "pg_vacuum_status"),
    ("durability_settings", _check_unsafe_settings, "pg_settings"),
    ("prepared_transactions", _check_prepared_transactions, "pg_stat_view"),
    ("wal_archiving", _check_archiver, "pg_wal_checkpoints"),
)


_CHECK_BUDGET_MS = 10_000


@tool(group="health", title="Health check")
async def pg_health_check(connection: str | None = None) -> dict[str, Any]:
    """Run a quick DBA check-up and return findings ranked by severity: connection use, long
    transactions, sessions idle in transaction, blocked sessions, transaction id wraparound,
    dead rows, invalid and duplicate indexes, sequences near their end, cache hit ratio,
    forced checkpoints, inactive replication slots, tables never analyzed or without a primary
    key, deadlocks, temporary files, rollback ratio, settings waiting for a restart,
    autovacuum, fsync/full_page_writes and orphaned prepared transactions.

    Start here when asked "is this database healthy?". Every finding has a severity
    (critical, warning, info, ok), the measured value, the threshold used and the tool that
    goes deeper. A check that cannot run on this server version or with this role is reported
    as `not_available` and the others still run. It only reads statistics and catalogs.
    """
    app = get_app()
    # A check-up must stay quick on a struggling server: each check gets a short budget,
    # and after two of them run out of time the rest are skipped instead of queued up.
    budget = min(app.profile(connection).statement_timeout, _CHECK_BUDGET_MS)
    async with app.read(connection, timeout_ms=budget) as db:
        findings: list[dict[str, Any]] = []
        timeouts = 0
        for name, check, see in _CHECKS:
            found: dict[str, Any] | None
            reason: str | None
            if timeouts >= 2:
                found, reason = None, "skipped, because two earlier checks ran out of time"
            else:
                found, reason = await attempt(db, partial(check, db))
                if found is None and reason and "time limit" in reason:
                    timeouts += 1
            if found is None:
                found = _finding(
                    name,
                    "not_available",
                    None,
                    "",
                    f"Not available on this server or for this role: {reason}",
                    see,
                )
            findings.append(found)
    findings.sort(key=lambda f: (_SEVERITY_RANK[f["severity"]], f["check"]))
    counts = {severity: 0 for severity in _SEVERITY_RANK}
    for finding in findings:
        counts[finding["severity"]] += 1
    status = next((s for s in ("critical", "warning", "info") if counts[s]), "ok")
    return {
        "connection": db.profile.name,
        "database": db.facts.database,
        "server": db.facts.label,
        "status": status,
        "summary": counts,
        **grid(findings, ["check", "severity", "value", "threshold", "message", "see"]),
    }
