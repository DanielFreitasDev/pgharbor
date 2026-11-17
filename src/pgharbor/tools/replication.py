"""Replication: standbys, slots, WAL receiver, publications and subscriptions in one place."""

from typing import Any

from ..app import get_app, tool
from ..session import Db
from ._util import attempt, grid

__all__ = ["pg_replication"]

_GIB = 1024**3
_SLOT_ROWS = 100
_PUBLICATION_ROWS = 50
_PUBLICATION_TABLES = 20
_SUBSCRIPTION_ROWS = 50

_SETTINGS = (
    "wal_level",
    "max_wal_senders",
    "max_replication_slots",
    "synchronous_standby_names",
    "synchronous_commit",
    "hot_standby",
    "hot_standby_feedback",
    "wal_keep_segments",
    "wal_keep_size",
    "max_slot_wal_keep_size",
    "max_logical_replication_workers",
)


async def _attempt(
    db: Db, notes: list[str], what: str, sql: str, params: list[Any] | None = None
) -> list[dict[str, Any]]:
    """One part of the answer; a part the server refuses is simply empty (with a note)."""
    return await attempt(db, notes, what, sql, params) or []


class _Lsn:
    """Names of the WAL position functions (``xlog``/``location`` before 10, ``wal``/``lsn``)."""

    def __init__(self, db: Db) -> None:
        old = db.v < 100000
        self.current = (
            "pg_catalog.pg_current_xlog_location()" if old else ("pg_catalog.pg_current_wal_lsn()")
        )
        self.replay = (
            "pg_catalog.pg_last_xlog_replay_location()"
            if old
            else "pg_catalog.pg_last_wal_replay_lsn()"
        )
        self.receive = (
            "pg_catalog.pg_last_xlog_receive_location()"
            if old
            else "pg_catalog.pg_last_wal_receive_lsn()"
        )
        self.diff = "pg_catalog.pg_xlog_location_diff" if old else "pg_catalog.pg_wal_lsn_diff"
        self.paused = (
            "pg_catalog.pg_is_xlog_replay_paused()"
            if old
            else "pg_catalog.pg_is_wal_replay_paused()"
        )
        # A standby cannot ask for its current write position; it measures against what
        # it has replayed instead.
        self.reference = self.replay if db.facts.in_recovery else self.current

    def lag(self, column: str) -> str:
        """SQL for how many bytes ``column`` is behind the reference position."""
        return (
            f"CASE WHEN {column} IS NULL THEN NULL "
            f"ELSE greatest(0, {self.diff}({self.reference}, {column}))::bigint END"
        )


def _pretty(sizes: dict[int, str], value: int | None) -> str | None:
    return None if value is None else sizes[value]


async def _sizes(db: Db, values: list[int | None]) -> dict[int, str]:
    wanted = sorted({v for v in values if v is not None})
    if not wanted:
        return {}
    rows = await db.rows(
        "SELECT b, pg_catalog.pg_size_pretty(b) AS p FROM unnest($1::bigint[]) AS u(b)", [wanted]
    )
    return {r["b"]: r["p"] for r in rows}


@tool(group="replication", title="Replication status")
async def pg_replication(connection: str | None = None) -> dict[str, Any]:
    """Show the replication state of the server in one answer: whether it is a primary or
    a standby, connected standbys with lag, replication slots and the WAL they retain,
    the WAL receiver and replay delay on a standby, publications with their tables,
    subscriptions with their statistics, recovery conflicts and the settings that govern
    replication.

    Use it to check that replicas are healthy, to find an abandoned slot that makes the
    disk fill with WAL, or to see what logical replication publishes. Connection strings
    and passwords are never shown. A server without any replication says so plainly.
    """
    app = get_app()
    notes: list[str] = []
    async with app.read(connection) as db:
        lsn = _Lsn(db)
        standby = db.facts.in_recovery
        out: dict[str, Any] = {
            "connection": db.profile.name,
            "role": "standby" if standby else "primary",
        }
        warnings: list[str] = []

        standbys = await _standbys(db, lsn, notes, warnings)
        slots = await _slots(db, lsn, notes, warnings)
        receiver = await _receiver(db, lsn, notes, warnings) if standby else None
        publications = await _publications(db, notes, warnings) if db.v >= 100000 else []
        subscriptions = await _subscriptions(db, notes, warnings) if db.v >= 100000 else []
        conflicts = await _conflicts(db, notes, standby)
        settings = await _settings(db)

    out["summary"] = _summary(
        db, standby, standbys, slots, publications, subscriptions, receiver, settings
    )
    if standbys:
        out["standbys"] = standbys
    if slots:
        out["slots"] = slots
    if receiver:
        out["standby_of"] = receiver
    if publications:
        out["publications"] = publications
    if subscriptions:
        out["subscriptions"] = subscriptions
    if conflicts:
        out["recovery_conflicts"] = conflicts
    out["settings"] = settings
    if warnings:
        out["warnings"] = warnings
    if db.v < 100000:
        notes.append("Publications and subscriptions (logical replication) need PostgreSQL 10+.")
    if notes:
        out["notes"] = notes
    return out


def _summary(
    db: Db,
    standby: bool,
    standbys: dict[str, Any] | None,
    slots: dict[str, Any] | None,
    publications: list[dict[str, Any]],
    subscriptions: list[dict[str, Any]],
    receiver: dict[str, Any] | None,
    settings: dict[str, Any],
) -> str:
    connected = len(standbys["rows"]) if standbys else 0
    slot_count = len(slots["rows"]) if slots else 0
    parts = [
        f"{'Standby (in recovery)' if standby else 'Primary'}",
        f"{connected} standby(s) connected",
        f"{slot_count} replication slot(s)",
    ]
    if db.v >= 100000:
        parts += [f"{len(publications)} publication(s)", f"{len(subscriptions)} subscription(s)"]
    text = ", ".join(parts) + "."
    if standby and receiver is None:
        text += " No WAL receiver is running: this standby restores from an archive or is stalled."
    if not (connected or slot_count or publications or subscriptions or standby):
        text += " Replication is not in use on this server."
        level = settings.get("wal_level")
        if level == "minimal":
            text += " wal_level=minimal: WAL cannot be streamed until it is raised to replica."
        elif settings.get("max_wal_senders") == "0":
            text += " max_wal_senders=0: no standby can connect until it is raised."
    return text


async def _standbys(
    db: Db, lsn: _Lsn, notes: list[str], warnings: list[str]
) -> dict[str, Any] | None:
    old = db.v < 100000
    sent, write, flush, replay = (
        (f"{n}_location" for n in ("sent", "write", "flush", "replay"))
        if old
        else (f"{n}_lsn" for n in ("sent", "write", "flush", "replay"))
    )
    times = (
        "NULL::text AS write_lag, NULL::text AS flush_lag, NULL::text AS replay_lag"
        if old
        else "write_lag::text, flush_lag::text, replay_lag::text"
    )
    reply = "reply_time::text" if db.v >= 120000 else "NULL::text"
    rows = await _attempt(
        db,
        notes,
        "pg_stat_replication",
        f"""
        SELECT pid, usename::text AS "user", application_name AS application,
               client_addr::text AS client, state, sync_state, sync_priority,
               {lsn.lag(sent)} AS sent_lag_bytes, {lsn.lag(replay)} AS replay_lag_bytes,
               {lsn.lag(write)} AS write_lag_bytes, {lsn.lag(flush)} AS flush_lag_bytes,
               {times}, backend_start::text AS connected_since, {reply} AS last_reply
          FROM pg_catalog.pg_stat_replication
         ORDER BY application_name, pid
        """,
    )
    if not rows:
        return None
    if all(r["state"] is None for r in rows):
        notes.append(
            "pg_stat_replication hides the details from this role; superuser or a member of "
            "pg_monitor (pg_read_all_stats) sees state and lag."
        )
    sizes = await _sizes(db, [r["replay_lag_bytes"] for r in rows])
    for row in rows:
        behind = row["replay_lag_bytes"]
        row["replay_lag_bytes"] = _pretty(sizes, behind)
        for hidden in ("sent_lag_bytes", "write_lag_bytes", "flush_lag_bytes"):
            del row[hidden]
        if row["state"] not in (None, "streaming"):
            warnings.append(
                f"standby '{row['application']}' (pid {row['pid']}) is in state "
                f"'{row['state']}', not streaming."
            )
        if behind is not None and behind >= _GIB:
            warnings.append(
                f"standby '{row['application']}' (pid {row['pid']}) is {sizes[behind]} behind "
                "in replay."
            )
    columns = [
        "pid",
        "user",
        "application",
        "client",
        "state",
        "sync_state",
        "sync_priority",
        "replay_lag_bytes",
        "connected_since",
    ]
    if not old:
        columns[8:8] = ["write_lag", "flush_lag", "replay_lag"]
    if db.v >= 120000:
        columns.append("last_reply")
    table = grid(rows, columns)
    table["columns"] = ["lag_behind" if c == "replay_lag_bytes" else c for c in table["columns"]]
    return table


async def _slots(db: Db, lsn: _Lsn, notes: list[str], warnings: list[str]) -> dict[str, Any] | None:
    v = db.v
    extra = ""
    if v >= 100000:
        extra += ", s.temporary"
    if v >= 130000:
        extra += ", s.wal_status, s.safe_wal_size AS safe_wal_bytes"
    if v >= 140000:
        extra += ", s.two_phase"
    if v >= 160000:
        extra += ", s.conflicting"
    if v >= 170000:
        extra += (
            ", s.inactive_since::text AS inactive_since, s.invalidation_reason"
            ", s.failover, s.synced"
        )
    stats_join = ""
    stats_cols = ""
    if v >= 140000:
        stats_join = (
            "LEFT JOIN pg_catalog.pg_stat_replication_slots st ON st.slot_name = s.slot_name"
        )
        stats_cols = ", st.total_bytes AS decoded_bytes, st.spill_bytes"
    rows = await _attempt(
        db,
        notes,
        "pg_replication_slots",
        f"""
        SELECT s.slot_name AS name, s.slot_type AS type, s.plugin, s.database, s.active,
               s.restart_lsn::text AS restart_lsn, s.confirmed_flush_lsn::text AS confirmed_flush,
               {lsn.lag("s.restart_lsn")} AS retained_bytes
               {extra}
               {stats_cols}
          FROM pg_catalog.pg_replication_slots s
          {stats_join}
         ORDER BY s.slot_name
         LIMIT {_SLOT_ROWS + 1}
        """,
    )
    if not rows:
        return None
    if len(rows) > _SLOT_ROWS:
        rows = rows[:_SLOT_ROWS]
        notes.append(f"Only the first {_SLOT_ROWS} slots are shown.")
    sizes = await _sizes(
        db,
        [r["retained_bytes"] for r in rows]
        + [r.get("safe_wal_bytes") for r in rows]
        + [r.get("decoded_bytes") for r in rows]
        + [r.get("spill_bytes") for r in rows],
    )
    for row in rows:
        retained = row["retained_bytes"]
        row["wal_retained"] = _pretty(sizes, retained)
        row["safe_wal"] = _pretty(sizes, row.get("safe_wal_bytes"))
        row["decoded"] = _pretty(sizes, row.get("decoded_bytes"))
        row["spilled"] = _pretty(sizes, row.get("spill_bytes"))
        if not row["active"]:
            size = f" and holds back {row['wal_retained']} of WAL" if retained else ""
            level = "is inactive" + size
            if retained is not None and retained >= _GIB:
                level += ": this can fill the disk, drop it if nothing will reconnect"
            warnings.append(f"slot '{row['name']}' {level}.")
        if row.get("wal_status") in ("unreserved", "lost"):
            warnings.append(
                f"slot '{row['name']}' has wal_status '{row['wal_status']}': the WAL it needs "
                "is about to be (or already was) removed."
            )
        if row.get("invalidation_reason"):
            warnings.append(f"slot '{row['name']}' was invalidated ({row['invalidation_reason']}).")
        if row.get("conflicting"):
            warnings.append(f"slot '{row['name']}' conflicts with recovery on the standby.")
    columns = ["name", "type", "plugin", "database", "active", "restart_lsn", "wal_retained"]
    columns += [
        c
        for c, present in (
            ("temporary", v >= 100000),
            ("confirmed_flush", True),
            ("wal_status", v >= 130000),
            ("safe_wal", v >= 130000),
            ("two_phase", v >= 140000),
            ("conflicting", v >= 160000),
            ("inactive_since", v >= 170000),
            ("invalidation_reason", v >= 170000),
            ("failover", v >= 170000),
            ("synced", v >= 170000),
            ("decoded", v >= 140000),
            ("spilled", v >= 140000),
        )
        if present
    ]
    # Columns that carry no information on this server (all empty) are dropped.
    columns = [c for c in columns if c in columns[:2] or any(r.get(c) is not None for r in rows)]
    return grid(rows, columns)


async def _receiver(
    db: Db, lsn: _Lsn, notes: list[str], warnings: list[str]
) -> dict[str, Any] | None:
    received = "flushed_lsn" if db.v >= 130000 else "received_lsn"
    origin = (
        "sender_host, sender_port,"
        if db.v >= 110000
        else "NULL AS sender_host, NULL AS sender_port,"
    )
    receiver_rows = await _attempt(
        db,
        notes,
        "pg_stat_wal_receiver",
        f"""
        SELECT status, {origin} slot_name, {received}::text AS received_lsn,
               latest_end_time::text AS latest_end_time,
               last_msg_receipt_time::text AS last_message_received
          FROM pg_catalog.pg_stat_wal_receiver
        """,
    )
    pause = (
        "pg_catalog.pg_get_wal_replay_pause_state()" if db.v >= 140000 else f"{lsn.paused}::text"
    )
    replay_rows = await _attempt(
        db,
        notes,
        "replay position",
        f"""
        SELECT pg_catalog.pg_last_xact_replay_timestamp()::text AS last_replayed_commit,
               (pg_catalog.now() - pg_catalog.pg_last_xact_replay_timestamp())::text AS delay,
               {pause} AS replay_paused,
               CASE WHEN {lsn.receive} IS NULL OR {lsn.replay} IS NULL THEN NULL
                    ELSE greatest(0, {lsn.diff}({lsn.receive}, {lsn.replay}))::bigint END
                    AS backlog_bytes
        """,
    )
    out: dict[str, Any] = {}
    if receiver_rows and receiver_rows[0]["status"] is not None:
        out.update({k: v for k, v in receiver_rows[0].items() if v is not None})
        if out.get("status") != "streaming":
            warnings.append(f"the WAL receiver is in state '{out.get('status')}', not streaming.")
    elif receiver_rows:
        notes.append(
            "The WAL receiver row is empty: either no receiver is running or this role cannot "
            "see it (pg_read_all_stats or superuser is needed)."
        )
    if replay_rows:
        replay = replay_rows[0]
        sizes = await _sizes(db, [replay["backlog_bytes"]])
        replay["backlog"] = _pretty(sizes, replay.pop("backlog_bytes"))
        paused = replay["replay_paused"]
        if paused not in (None, False, "not paused"):
            warnings.append("WAL replay is paused on this standby.")
        replay["replay_paused"] = paused in (True, "paused", "pause requested")
        out["replay"] = {k: v for k, v in replay.items() if v is not None}
    return out or None


async def _publications(db: Db, notes: list[str], warnings: list[str]) -> list[dict[str, Any]]:
    truncate = "CASE WHEN p.pubtruncate THEN 'truncate' END" if db.v >= 110000 else "NULL"
    via_root = (
        "p.pubviaroot AS via_partition_root" if db.v >= 130000 else "NULL AS via_partition_root"
    )
    rows = await _attempt(
        db,
        notes,
        "pg_publication",
        f"""
        SELECT p.pubname AS name, pg_catalog.pg_get_userbyid(p.pubowner) AS owner,
               p.puballtables AS all_tables,
               concat_ws(',', CASE WHEN p.pubinsert THEN 'insert' END,
                              CASE WHEN p.pubupdate THEN 'update' END,
                              CASE WHEN p.pubdelete THEN 'delete' END,
                              {truncate}
                         ) AS publishes,
               {via_root},
               (SELECT count(*) FROM pg_catalog.pg_publication_tables t
                 WHERE t.pubname = p.pubname) AS table_count,
               ARRAY(SELECT t.schemaname || '.' || t.tablename
                       FROM pg_catalog.pg_publication_tables t
                      WHERE t.pubname = p.pubname ORDER BY 1 LIMIT {_PUBLICATION_TABLES}) AS tables
          FROM pg_catalog.pg_publication p
         ORDER BY p.pubname
         LIMIT {_PUBLICATION_ROWS}
        """,
    )
    for row in rows:
        if row["via_partition_root"] is None:
            del row["via_partition_root"]
        if row["table_count"] > len(row["tables"]):
            row["tables_shown"] = len(row["tables"])
    # Without a replica identity, UPDATE and DELETE on a published table fail at the source.
    stuck = await _attempt(
        db,
        notes,
        "replica identity check",
        """
        SELECT t.pubname, t.schemaname || '.' || t.tablename AS "table"
          FROM pg_catalog.pg_publication_tables t
          JOIN pg_catalog.pg_publication p ON p.pubname = t.pubname
          JOIN pg_catalog.pg_namespace n ON n.nspname = t.schemaname
          JOIN pg_catalog.pg_class c ON c.relnamespace = n.oid AND c.relname = t.tablename
         WHERE (p.pubupdate OR p.pubdelete) AND c.relkind IN ('r', 'p')
           AND (c.relreplident = 'n' OR (c.relreplident = 'd' AND NOT EXISTS (
                SELECT 1 FROM pg_catalog.pg_index i
                 WHERE i.indrelid = c.oid AND i.indisprimary)))
         ORDER BY 1, 2
         LIMIT 10
        """,
    )
    for item in stuck:
        warnings.append(
            f"table {item['table']} is published by '{item['pubname']}' with UPDATE/DELETE but "
            "has no primary key or replica identity: those statements will fail on the source."
        )
    return rows


async def _subscriptions(db: Db, notes: list[str], warnings: list[str]) -> list[dict[str, Any]]:
    v = db.v
    extra = []
    if v >= 140000:
        extra.append("s.subbinary AS binary")
        extra.append(
            "CASE WHEN s.substream THEN 'on' ELSE 'off' END AS streaming"
            if v < 160000
            else "CASE s.substream WHEN 'f' THEN 'off' WHEN 't' THEN 'on' "
            "WHEN 'p' THEN 'parallel' END AS streaming"
        )
    if v >= 150000:
        extra.append(
            "CASE s.subtwophasestate WHEN 'd' THEN 'disabled' WHEN 'p' THEN 'pending' "
            "WHEN 'e' THEN 'enabled' END AS two_phase"
        )
        extra.append("s.subdisableonerr AS disable_on_error")
        extra.append("NULLIF(s.subskiplsn, '0/0')::text AS skip_lsn")
    if v >= 160000:
        extra += ["s.suborigin AS origin", "s.subpasswordrequired AS password_required"]
        extra.append("s.subrunasowner AS run_as_owner")
    if v >= 170000:
        extra.append("s.subfailover AS failover")
    stats_cols = ""
    stats_join = ""
    if v >= 150000:
        sync = (
            "st2.sync_error_count AS sync_errors"
            if v < 190000
            else "st2.sync_table_error_count + st2.sync_seq_error_count AS sync_errors"
        )
        stats_cols = f", st2.apply_error_count AS apply_errors, {sync}"
        if v >= 180000:
            stats_cols += (
                ", st2.confl_insert_exists + st2.confl_update_origin_differs"
                " + st2.confl_update_exists + st2.confl_update_missing"
                " + st2.confl_delete_origin_differs + st2.confl_delete_missing"
                " + st2.confl_multiple_unique_conflicts AS conflicts"
            )
        stats_join = (
            "LEFT JOIN pg_catalog.pg_stat_subscription_stats st2 ON st2.subname = s.subname"
        )
    leader = "AND st.leader_pid IS NULL" if v >= 160000 else ""
    # Before 14 only a superuser may read this column.
    readable = v >= 140000 or db.facts.is_superuser
    sync_commit = ", s.subsynccommit AS synchronous_commit" if readable else ""
    rows = await _attempt(
        db,
        notes,
        "pg_subscription",
        f"""
        SELECT s.subname AS name, pg_catalog.pg_get_userbyid(s.subowner) AS owner,
               s.subenabled AS enabled, s.subpublications AS publications,
               s.subslotname AS slot{sync_commit}
               {"".join(", " + e for e in extra)},
               st.pid AS worker_pid, st.received_lsn::text AS received_lsn,
               st.latest_end_lsn::text AS latest_end_lsn,
               st.last_msg_receipt_time::text AS last_message_received
               {stats_cols}
          FROM pg_catalog.pg_subscription s
          LEFT JOIN pg_catalog.pg_stat_subscription st
                 ON st.subname = s.subname AND st.relid IS NULL {leader}
          {stats_join}
         WHERE s.subdbid = (SELECT oid FROM pg_catalog.pg_database
                             WHERE datname = pg_catalog.current_database())
         ORDER BY s.subname
         LIMIT {_SUBSCRIPTION_ROWS}
        """,
    )
    for row in rows:
        for key in [k for k, value in row.items() if value is None]:
            del row[key]
        if row.get("enabled") and "worker_pid" not in row:
            warnings.append(
                f"subscription '{row['name']}' is enabled but no apply worker is running."
            )
        if not row.get("enabled"):
            warnings.append(f"subscription '{row['name']}' is disabled.")
        errors = (row.get("apply_errors") or 0) + (row.get("sync_errors") or 0)
        if errors:
            warnings.append(
                f"subscription '{row['name']}' has {errors} apply/sync error(s) recorded."
            )
    return rows


async def _conflicts(db: Db, notes: list[str], standby: bool) -> dict[str, Any] | None:
    logical = ", confl_active_logicalslot AS logical_slot" if db.v >= 160000 else ""
    rows = await _attempt(
        db,
        notes,
        "pg_stat_database_conflicts",
        f"""
        SELECT confl_tablespace AS tablespace, confl_lock AS lock, confl_snapshot AS snapshot,
               confl_bufferpin AS bufferpin, confl_deadlock AS deadlock
               {logical}
          FROM pg_catalog.pg_stat_database_conflicts
         WHERE datname = pg_catalog.current_database()
        """,
    )
    if not rows:
        return None
    counts = rows[0]
    # The counters only move on a standby; on a primary they are zero and say nothing.
    if standby or any(counts.values()):
        return counts
    return None


async def _settings(db: Db) -> dict[str, Any]:
    rows = await db.rows(
        """
        SELECT name, setting, unit FROM pg_catalog.pg_settings
         WHERE name = ANY($1::text[]) ORDER BY name
        """,
        [list(_SETTINGS)],
    )
    return {r["name"]: r["setting"] for r in rows}
