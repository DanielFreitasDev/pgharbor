"""Tools that change the database. They exist only where the configuration allows.

None of these tools is listed unless at least one connection has ``access`` set to
``read-write`` or ``admin``, and each one checks the access mode of the connection it is
called with. Every change is confirmed by the user (a dialog, when the client can show
one) and written to the audit log.
"""

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.elicitation import ElicitationResult
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.resolve import Elicit, Resolve

from ..app import get_app, tool
from ..approval import Decision, NoDialog, NotNeeded, ask_write, client_has_dialog, settle
from ..config import Access, ConnectionConfig, WriteConfirmation
from ..errors import PgHarborError, Rejected
from ..guard import Analysis, analyze, quote_ident, quote_literal, quote_qualified
from ..lexer import Kind
from ..runner import plain_result
from ..vetting import vet_statement
from ._util import resolve_relation

__all__ = ["pg_execute", "pg_import_csv", "pg_maintenance", "pg_signal_backend"]

_MAX_IMPORT_BYTES = 512 * 1024 * 1024

# Session and transaction commands never run through pg_execute: pgharbor owns the
# transaction, and anything that changes the session would outlive the call.
_NEVER = frozenset(
    {
        "begin", "start", "commit", "end", "rollback", "abort", "savepoint", "release",
        "prepare", "execute", "deallocate", "set", "reset", "discard", "listen", "unlisten",
        "notify", "lock", "declare", "fetch", "move", "close", "load", "copy",
    }
)  # fmt: skip


def _needs_autocommit(analysis: Analysis) -> bool:
    """Commands PostgreSQL refuses to run inside a transaction block."""
    words = [t.value for t in analysis.tokens if t.kind is Kind.IDENT]
    first = words[0] if words else ""
    second = words[1] if len(words) > 1 else ""
    if first == "vacuum":
        return True
    if first in ("create", "drop", "reindex") and "concurrently" in words:
        return True
    if first in ("create", "drop") and second in ("database", "tablespace"):
        return True
    return first == "alter" and second == "system"


def _minimum_access(analysis: Analysis) -> Access:
    first = analysis.tokens[0].value if analysis.tokens[0].kind is Kind.IDENT else ""
    if analysis.kind in ("query", "show", "explain"):
        raise Rejected(
            "this statement only reads",
            rule="use-pg-query",
            hint="Use pg_query (or pg_explain) for reads.",
        )
    if first in _NEVER:
        raise Rejected(
            f"{analysis.verb} is a session or transaction command; pgharbor manages the "
            "transaction itself and never changes the session",
            rule="session-command",
            hint="For bulk loading use pg_import_csv; for exports use pg_export_query.",
        )
    if analysis.kind == "dml" or first == "call":
        return Access.READ_WRITE
    return Access.ADMIN


@tool(
    group="admin",
    title="Execute a statement that changes the database",
    writes=True,
    destructive=True,
    minimum_access=Access.READ_WRITE,
)
async def pg_execute(
    sql: str,
    connection: str,
    decision: Annotated[ElicitationResult[Decision], Resolve(ask_write)],
    params: list[Any] | None = None,
    dry_run: bool = False,
    confirmed_by_user: bool = False,
) -> dict[str, Any]:
    """Run ONE statement that changes data or structure: INSERT, UPDATE, DELETE, MERGE,
    CALL (connections with access "read-write") or DDL and maintenance such as CREATE,
    ALTER, DROP, GRANT, VACUUM (access "admin").

    The user is asked to confirm before anything is committed. Start with `dry_run=true`:
    the statement really runs inside a transaction that is then rolled back, so you can
    show the user how many rows would change. Bind values with `params` ($1, $2, ...).

    Not available on read-only connections: there, show the SQL to the user as text and
    say that it was not executed. `confirmed_by_user` only matters for clients that cannot
    show a confirmation dialog; set it to true only after the user clearly said yes to
    this exact statement.
    """
    app = get_app()
    profile = app.profile(connection)
    analysis = analyze(sql, max_chars=profile.max_sql_chars)
    app.require_access(profile, _minimum_access(analysis))
    app.require_approval(profile)
    values = params or []
    if len(values) != analysis.param_count:
        raise Rejected(
            f"the statement uses {analysis.param_count} parameter(s) but {len(values)} "
            "value(s) were given",
            rule="parameter-count",
        )
    autocommit = _needs_autocommit(analysis)
    if autocommit and dry_run:
        raise Rejected(
            f"{analysis.verb} cannot run inside a transaction, so it cannot be tried and "
            "rolled back",
            rule="no-dry-run",
            hint="Explain to the user what it will do and run it without dry_run.",
        )
    how = settle(
        decision,
        fallback_allowed=profile.write_confirmation is WriteConfirmation.CONVERSATION,
        stated_by_agent=confirmed_by_user,
        what="this statement",
    )

    if autocommit:
        async with app.sessions.autocommit(profile.name) as db:
            await vet_statement(db, analysis, read_only=False)
            result = await db.raw(analysis.sql, values)
    else:
        async with app.write(profile.name) as db:
            await vet_statement(db, analysis, read_only=False)
            result = await db.raw(analysis.sql, values)
            db.rollback_only = dry_run

    status = (result.command_status or b"").decode("utf-8", errors="replace")
    out: dict[str, Any] = {
        "connection": profile.name,
        "environment": profile.environment,
        "status": status,
        "committed": not dry_run,
    }
    if dry_run:
        out["note"] = "Dry run: the statement ran and was rolled back. Nothing changed."
    else:
        out["confirmed"] = how
    if result.nfields:
        names, rows, more = plain_result(
            result, limit=profile.max_rows, cell=profile.max_cell_chars
        )
        out["returning"] = {"columns": names, "rows": rows, "has_more": more}
    app.audit.record(
        tool="pg_execute",
        connection=profile.name,
        outcome="dry-run" if dry_run else "executed",
        sql=analysis.sql,
        detail=f"{status}; confirmed: {how}",
    )
    return out


# ---------------------------------------------------------------------------- maintenance


def _ask_maintenance(
    connection: str, action: str, target: str | None, ctx: Context[Any, Any]
) -> Elicit[Decision] | NoDialog | NotNeeded:
    profile = _profile_or_none(connection)
    if profile is None or profile.write_confirmation is WriteConfirmation.NONE:
        return NotNeeded()
    if profile.write_confirmation is WriteConfirmation.CONVERSATION or not client_has_dialog(ctx):
        return NoDialog()
    return Elicit(
        f"Claude wants to run maintenance on connection '{profile.name}' "
        f"({profile.environment}: {profile.target()}).\n\n"
        f"Action: {action}\nTarget: {target or 'the whole database'}",
        Decision,
    )


def _ask_signal(
    connection: str, pid: int, action: str, ctx: Context[Any, Any]
) -> Elicit[Decision] | NoDialog | NotNeeded:
    profile = _profile_or_none(connection)
    if profile is None or profile.write_confirmation is WriteConfirmation.NONE:
        return NotNeeded()
    if profile.write_confirmation is WriteConfirmation.CONVERSATION or not client_has_dialog(ctx):
        return NoDialog()
    return Elicit(
        f"Claude wants to {action} the session with pid {pid} on connection "
        f"'{profile.name}' ({profile.environment}: {profile.target()}).",
        Decision,
    )


def _ask_import(
    connection: str, table: str, filename: str, dry_run: bool, ctx: Context[Any, Any]
) -> Elicit[Decision] | NoDialog | NotNeeded:
    profile = _profile_or_none(connection)
    if profile is None or dry_run or profile.write_confirmation is WriteConfirmation.NONE:
        return NotNeeded()
    if profile.write_confirmation is WriteConfirmation.CONVERSATION or not client_has_dialog(ctx):
        return NoDialog()
    return Elicit(
        f"Claude wants to load the file {filename} into table {table} on connection "
        f"'{profile.name}' ({profile.environment}: {profile.target()}).",
        Decision,
    )


def _profile_or_none(connection: str | None) -> ConnectionConfig | None:
    try:
        return get_app().profile(connection)
    except PgHarborError:
        return None


@tool(
    group="admin",
    title="Run maintenance (VACUUM, ANALYZE, REINDEX, REFRESH)",
    writes=True,
    destructive=True,
    minimum_access=Access.ADMIN,
)
async def pg_maintenance(
    action: Literal[
        "analyze",
        "vacuum",
        "vacuum_analyze",
        "vacuum_full",
        "vacuum_freeze",
        "reindex_table",
        "reindex_index",
        "refresh_materialized_view",
        "reset_statistics",
    ],
    connection: str,
    decision: Annotated[ElicitationResult[Decision], Resolve(_ask_maintenance)],
    target: str | None = None,
    concurrently: bool = False,
    timeout_seconds: int | None = None,
    confirmed_by_user: bool = False,
) -> dict[str, Any]:
    """Run a maintenance command on a table, index or materialized view: ANALYZE, VACUUM
    (plain, ANALYZE, FULL, FREEZE), REINDEX, REFRESH MATERIALIZED VIEW, or reset the
    statistics counters.

    `target` is `schema.name`; ANALYZE and VACUUM accept no target to cover the whole
    database. `concurrently` applies to REINDEX (PostgreSQL 12+) and REFRESH, and avoids
    blocking readers. VACUUM FULL and a plain REINDEX lock the table: warn the user.
    Needs a connection with access "admin"; the user confirms every run.
    """
    app = get_app()
    profile = app.profile(connection)
    app.require_access(profile, Access.ADMIN)
    app.require_approval(profile)
    how = settle(
        decision,
        fallback_allowed=profile.write_confirmation is WriteConfirmation.CONVERSATION,
        stated_by_agent=confirmed_by_user,
        what=f"{action} on {target or 'the whole database'}",
    )
    timeout_ms = None
    if timeout_seconds is not None:
        timeout_ms = max(1, min(int(timeout_seconds), 6 * 3600)) * 1000

    async with app.sessions.autocommit(profile.name, timeout_ms=timeout_ms) as db:
        statement = await _maintenance_sql(db, action, target, concurrently)
        await db.rows(statement)
    app.audit.record(
        tool="pg_maintenance",
        connection=profile.name,
        outcome="executed",
        sql=statement,
        detail=f"confirmed: {how}",
    )
    return {"connection": profile.name, "executed": statement, "confirmed": how}


async def _maintenance_sql(db: Any, action: str, target: str | None, concurrently: bool) -> str:
    """Build the command from catalog names: nothing typed by the caller reaches SQL."""
    if action == "reset_statistics":
        if target in (None, "database"):
            return "SELECT pg_catalog.pg_stat_reset()"
        if target == "statements":
            extension = db.need_ext("pg_stat_statements")
            return f"SELECT {quote_ident(extension.schema)}.pg_stat_statements_reset()"
        relation = await resolve_relation(db, target)
        return f"SELECT pg_catalog.pg_stat_reset_single_table_counters({int(relation['oid'])})"

    whole_database = {"analyze": "ANALYZE", "vacuum": "VACUUM", "vacuum_analyze": "VACUUM ANALYZE"}
    if target is None:
        if action in whole_database:
            return whole_database[action]
        raise PgHarborError(f"{action} needs a target (schema.name).")

    if action == "reindex_index":
        relation = await resolve_relation(db, target, "iI", what="index")
        name = quote_qualified(relation["schema"], relation["name"])
        if concurrently:
            db.need_version(120000, "REINDEX CONCURRENTLY")
        return f"REINDEX INDEX {'CONCURRENTLY ' if concurrently else ''}{name}"
    if action == "refresh_materialized_view":
        relation = await resolve_relation(db, target, "m", what="materialized view")
        name = quote_qualified(relation["schema"], relation["name"])
        return f"REFRESH MATERIALIZED VIEW {'CONCURRENTLY ' if concurrently else ''}{name}"

    relation = await resolve_relation(db, target, "rpm")
    name = quote_qualified(relation["schema"], relation["name"])
    if action == "reindex_table":
        if concurrently:
            db.need_version(120000, "REINDEX CONCURRENTLY")
        return f"REINDEX TABLE {'CONCURRENTLY ' if concurrently else ''}{name}"
    prefix = {
        "analyze": "ANALYZE",
        "vacuum": "VACUUM",
        "vacuum_analyze": "VACUUM (ANALYZE)",
        "vacuum_full": "VACUUM (FULL)",
        "vacuum_freeze": "VACUUM (FREEZE)",
    }[action]
    return f"{prefix} {name}"


@tool(
    group="admin",
    title="Cancel or terminate a session",
    writes=True,
    destructive=True,
    minimum_access=Access.ADMIN,
)
async def pg_signal_backend(
    pid: int,
    connection: str,
    decision: Annotated[ElicitationResult[Decision], Resolve(_ask_signal)],
    action: Literal["cancel", "terminate"] = "cancel",
    confirmed_by_user: bool = False,
) -> dict[str, Any]:
    """Cancel the running query of another session (`cancel`) or close the session
    entirely (`terminate`), by process id.

    Find the pid with pg_activity or pg_locks. Prefer `cancel`: it stops the statement and
    leaves the connection alive. `terminate` rolls back that session's transaction and
    disconnects it. Needs a connection with access "admin"; the user confirms.
    """
    app = get_app()
    profile = app.profile(connection)
    app.require_access(profile, Access.ADMIN)
    app.require_approval(profile)
    how = settle(
        decision,
        fallback_allowed=profile.write_confirmation is WriteConfirmation.CONVERSATION,
        stated_by_agent=confirmed_by_user,
        what=f"to {action} session {pid}",
    )
    async with app.write(profile.name, minimum=Access.ADMIN) as db:
        session = await db.row(
            """
            SELECT pid, usename::text AS "user", datname::text AS database,
                   application_name, state, left(query, 300) AS query,
                   pid = pg_catalog.pg_backend_pid() AS own
              FROM pg_catalog.pg_stat_activity WHERE pid = $1
            """,
            [int(pid)],
        )
        if session is None:
            raise PgHarborError(f"There is no session with pid {pid}. pg_activity lists them.")
        if session.pop("own"):
            raise PgHarborError("That is pgharbor's own session.")
        function = "pg_cancel_backend" if action == "cancel" else "pg_terminate_backend"
        delivered = await db.value(f"SELECT pg_catalog.{function}($1)", [int(pid)])
    app.audit.record(
        tool="pg_signal_backend",
        connection=profile.name,
        outcome="executed",
        detail=f"{action} pid {pid}; confirmed: {how}",
    )
    return {
        "connection": profile.name,
        "action": action,
        "signal_sent": bool(delivered),
        "session": session,
        "confirmed": how,
    }


# -------------------------------------------------------------------------------- import


def _import_dir(profile: ConnectionConfig) -> Path:
    return Path(profile.import_dir or "~/pgharbor-imports").expanduser()


async def _file_chunks(path: Path) -> AsyncIterator[bytes]:
    with path.open("rb") as handle:
        while chunk := handle.read(256 * 1024):
            yield chunk


@tool(
    group="admin",
    title="Load a CSV file into a table",
    writes=True,
    destructive=True,
    minimum_access=Access.READ_WRITE,
)
async def pg_import_csv(
    table: str,
    filename: str,
    connection: str,
    decision: Annotated[ElicitationResult[Decision], Resolve(_ask_import)],
    columns: list[str] | None = None,
    header: bool = True,
    delimiter: str = ",",
    null: str = "",
    dry_run: bool = False,
    confirmed_by_user: bool = False,
) -> dict[str, Any]:
    """Bulk-load a local CSV file into an existing table with COPY, in one transaction.

    `filename` is the name of a file inside the import directory of the connection
    (`import_dir` in the configuration, `~/pgharbor-imports` by default): no other path
    is readable. `columns` gives the order of the file's columns when it differs from the
    table. `dry_run=true` loads the file, reports the row count and rolls back. Needs a
    connection with access "read-write" or "admin"; the user confirms.
    """
    app = get_app()
    profile = app.profile(connection)
    app.require_access(profile, Access.READ_WRITE)
    app.require_approval(profile)
    if len(delimiter) != 1 or delimiter in "\r\n\\":
        raise PgHarborError("delimiter must be a single character.")
    if os.sep in filename or (os.altsep and os.altsep in filename) or filename in ("", ".", ".."):
        raise PgHarborError(
            "filename must be a plain file name inside the import directory, without a path."
        )
    folder = _import_dir(profile)
    path = (folder / filename).resolve()
    if not path.is_relative_to(folder.resolve()) or not path.is_file():
        raise PgHarborError(
            f"No file named {filename!r} in the import directory ({folder}). Put the file "
            "there first."
        )
    size = path.stat().st_size
    if size > _MAX_IMPORT_BYTES:
        raise PgHarborError(f"The file has {size:,} bytes; the limit is {_MAX_IMPORT_BYTES:,}.")
    how = settle(
        decision,
        fallback_allowed=profile.write_confirmation is WriteConfirmation.CONVERSATION,
        stated_by_agent=confirmed_by_user,
        what=f"loading {filename} into {table}",
    )
    async with app.write(profile.name) as db:
        relation = await resolve_relation(db, table, "rp")
        target = quote_qualified(relation["schema"], relation["name"])
        column_list = ""
        if columns:
            known = {
                r["attname"]
                for r in await db.rows(
                    "SELECT attname::text FROM pg_catalog.pg_attribute "
                    "WHERE attrelid = $1 AND attnum > 0 AND NOT attisdropped",
                    [relation["oid"]],
                )
            }
            unknown = [c for c in columns if c not in known]
            if unknown:
                raise PgHarborError(
                    f"Unknown column(s) in {relation['schema']}.{relation['name']}: "
                    + ", ".join(unknown)
                )
            column_list = " (" + ", ".join(quote_ident(c) for c in columns) + ")"
        statement = (
            f"COPY {target}{column_list} FROM STDIN WITH (FORMAT csv, "
            f"HEADER {'true' if header else 'false'}, DELIMITER {quote_literal(delimiter)}, "
            f"NULL {quote_literal(null)})"
        )
        loaded = await db.copy_in(statement, _file_chunks(path))
        db.rollback_only = dry_run
    app.audit.record(
        tool="pg_import_csv",
        connection=profile.name,
        outcome="dry-run" if dry_run else "executed",
        sql=statement,
        detail=f"{loaded} rows from {path.name}; confirmed: {how}",
    )
    out: dict[str, Any] = {
        "connection": profile.name,
        "table": f"{relation['schema']}.{relation['name']}",
        "rows_loaded": loaded,
        "file": str(path),
        "committed": not dry_run,
    }
    if dry_run:
        out["note"] = "Dry run: the rows were loaded and rolled back. Nothing changed."
    else:
        out["confirmed"] = how
    return out
