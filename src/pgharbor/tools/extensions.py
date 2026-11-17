"""Extensions: what is installed or installable, a report per extension, integrity checks."""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import psycopg

from ..app import get_app, tool
from ..errors import PgHarborError
from ..guard import quote_ident
from ..session import Db
from ._util import USER_SCHEMA_FILTER, grid, like_pattern, page, resolve_relation
from ._util import attempt as _attempt

__all__ = ["pg_extension_report", "pg_extensions", "pg_integrity_check"]

_LIST_ROWS = 50
_SAMPLE = 25


async def _sizes(db: Db, values: list[int | None]) -> dict[int, str]:
    wanted = sorted({v for v in values if v is not None})
    if not wanted:
        return {}
    rows = await db.rows(
        "SELECT b, pg_catalog.pg_size_pretty(b) AS p FROM unnest($1::bigint[]) AS u(b)", [wanted]
    )
    return {r["b"]: r["p"] for r in rows}


# ------------------------------------------------------------------------- pg_extensions


@tool(group="extensions", title="List extensions")
async def pg_extensions(
    connection: str | None = None,
    pattern: str | None = None,
    only_installed: bool = False,
) -> dict[str, Any]:
    """List the extensions installed in this database (version, schema, whether a newer
    version can be installed with ALTER EXTENSION ... UPDATE, comment) and the ones the
    server could install with CREATE EXTENSION.

    Use it to see what a database can do (pg_stat_statements, PostGIS, pgvector, pg_cron...)
    and what is missing. `pattern` filters by name (`*` wildcard); `only_installed` skips the
    available list. For the available ones it says whether installing needs a superuser
    (trusted extensions, PostgreSQL 13+, only need CREATE on the database). pg_extension_report
    describes one installed extension.
    """
    app = get_app()
    async with app.read(connection) as db:
        trusted = "v.trusted" if db.v >= 130000 else "false"
        rows = await db.rows(
            f"""
            SELECT coalesce(a.name, e.extname) AS name, e.extversion AS version,
                   a.default_version, n.nspname AS schema, e.extname IS NOT NULL AS installed,
                   a.name IS NULL AS files_missing,
                   coalesce(v.superuser AND NOT coalesce({trusted}, false), true)
                       AS needs_superuser,
                   left(coalesce(a.comment,
                                 pg_catalog.obj_description(e.oid, 'pg_extension')), 110) AS comment
              FROM pg_catalog.pg_available_extensions a
              FULL JOIN pg_catalog.pg_extension e ON e.extname = a.name
              LEFT JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace
              LEFT JOIN pg_catalog.pg_available_extension_versions v
                     ON v.name = a.name AND v.version = a.default_version
             WHERE ($1::text IS NULL OR coalesce(a.name, e.extname) ILIKE $1)
             ORDER BY 1
            """,
            [like_pattern(pattern)],
        )
        in_use = await db.value(
            "SELECT current_setting('shared_preload_libraries', true)"
            if db.facts.is_superuser
            else "SELECT NULL::text"
        )
    installed = []
    for row in rows:
        if not row["installed"]:
            continue
        update = (
            row["default_version"] if row["default_version"] not in (None, row["version"]) else None
        )
        installed.append({**row, "update_to": update})
    out: dict[str, Any] = {"connection": db.profile.name}
    out["installed"] = grid(installed, ["name", "version", "schema", "update_to", "comment"])
    notes: list[str] = []
    missing = [r["name"] for r in installed if r["files_missing"]]
    if missing:
        notes.append(
            "Installed but their files are gone from the server (package removed?): "
            + ", ".join(missing)
            + "."
        )
    updates = [r["name"] for r in installed if r["update_to"]]
    if updates:
        notes.append(
            "A newer version is installed on the server for: "
            + ", ".join(updates)
            + ". ALTER EXTENSION <name> UPDATE applies it."
        )
    if not only_installed:
        available = [
            {
                **r,
                "install": "superuser" if r["needs_superuser"] else "role with CREATE on database",
            }
            for r in rows
            if not r["installed"]
        ]
        shown, more = page(available, 200)
        out["available"] = grid(shown, ["name", "default_version", "install", "comment"])
        out["available_has_more"] = more
    if not installed and pattern:
        out["hint"] = "No installed extension matched the pattern."
    if in_use is not None:
        out["shared_preload_libraries"] = in_use
    if notes:
        out["notes"] = notes
    return out


# ------------------------------------------------------------------------ extension report


@dataclass(frozen=True)
class _Ext:
    oid: int
    name: str
    version: str
    schema: str

    @property
    def q(self) -> str:
        """The extension's schema, quoted for use in SQL text (the name comes from the catalog)."""
        return quote_ident(self.schema)


_ALIASES = {"pgvector": "vector", "pg_vector": "vector", "pgaudit": "pgaudit", "cron": "pg_cron"}
# Extra setting prefixes (the part before the dot) per extension, besides its own name.
_SETTING_PREFIXES = {
    "pg_cron": ["cron"],
    "vector": ["hnsw", "ivfflat"],
    "pg_partman": ["pg_partman_bgw"],
    "postgis": ["postgis"],
    "pg_stat_statements": ["pg_stat_statements"],
}
# Extensions that only work when their library is loaded at server start.
_NEEDS_PRELOAD = {
    "pg_stat_statements",
    "pg_cron",
    "pgaudit",
    "pg_stat_kcache",
    "pg_qualstats",
    "pg_wait_sampling",
    "timescaledb",
    "citus",
    "pg_stat_monitor",
}
_CATALOG_LABELS = {
    "pg_type": "types",
    "pg_operator": "operators",
    "pg_cast": "casts",
    "pg_opclass": "operator classes",
    "pg_opfamily": "operator families",
    "pg_am": "access methods",
    "pg_language": "languages",
    "pg_foreign_data_wrapper": "foreign data wrappers",
    "pg_event_trigger": "event triggers",
    "pg_namespace": "schemas",
    "pg_collation": "collations",
    "pg_conversion": "conversions",
}
_DEPENDENT = """d.refclassid = 'pg_catalog.pg_extension'::regclass AND d.refobjid = $1
               AND d.deptype = 'e'"""


@tool(group="extensions", title="Extension report")
async def pg_extension_report(extension: str, connection: str | None = None) -> dict[str, Any]:
    """Report on one installed extension: version, schema, the objects it created
    (functions, types, tables, operators), its settings, and extension-specific facts.

    Specific parts exist for pg_stat_statements (entries), pg_buffercache (cache summary),
    pg_trgm (trigram indexes), hypopg (hypothetical indexes), vector/pgvector (vector columns,
    hnsw and ivfflat indexes), postgis (version, geometry columns, SRID, spatial indexes),
    pg_cron (jobs and last runs), pg_partman (configured tables), pgaudit, postgres_fdw and
    dblink (servers, foreign tables, never user mapping options), pgstattuple, pgcrypto and
    timescaledb (hypertables). Any other extension gets the generic part. If the extension is
    not installed the error says how to install it; pg_extensions lists what exists.
    """
    name = _ALIASES.get(extension.strip().lower(), extension.strip().lower())
    app = get_app()
    notes: list[str] = []
    async with app.read(connection) as db:
        ext = await _find_extension(db, name)
        header = await db.row(
            """
            SELECT e.extrelocatable AS relocatable, pg_catalog.pg_get_userbyid(e.extowner) AS owner,
                   a.default_version, pg_catalog.obj_description(e.oid, 'pg_extension') AS comment
              FROM pg_catalog.pg_extension e
              LEFT JOIN pg_catalog.pg_available_extensions a ON a.name = e.extname
             WHERE e.oid = $1
            """,
            [ext.oid],
        )
        assert header is not None
        out: dict[str, Any] = {
            "connection": db.profile.name,
            "extension": {
                "name": ext.name,
                "version": ext.version,
                "schema": ext.schema,
                "owner": header["owner"],
                "relocatable": header["relocatable"],
            },
        }
        if header["default_version"] not in (None, ext.version):
            out["extension"]["update_available"] = header["default_version"]
        if header["comment"]:
            out["extension"]["comment"] = header["comment"]
        out["objects"] = await _objects(db, ext, notes)
        settings = await _settings(db, ext)
        if settings:
            out["settings"] = settings
        if ext.name in _NEEDS_PRELOAD:
            loaded = await _preloaded(db, ext.name)
            if loaded is False:
                notes.append(
                    f"{ext.name} is installed but its library is not in shared_preload_libraries: "
                    "add it and restart the server, otherwise its views and functions fail."
                )
        specific = _SPECIFIC.get(ext.name)
        if specific:
            details = await specific(db, ext, notes)
            if details:
                out["details"] = details
    if notes:
        out["notes"] = notes
    return out


async def _find_extension(db: Db, name: str) -> _Ext:
    row = await db.row(
        """
        SELECT e.oid::bigint AS oid, e.extname AS name, e.extversion AS version,
               n.nspname AS schema
          FROM pg_catalog.pg_extension e
          JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace
         WHERE e.extname = $1
        """,
        [name],
    )
    if row:
        return _Ext(row["oid"], row["name"], row["version"], row["schema"])
    available = await db.row(
        "SELECT default_version FROM pg_catalog.pg_available_extensions WHERE name = $1", [name]
    )
    database = db.facts.database
    if available:
        raise PgHarborError(
            f"The extension '{name}' (version {available['default_version']}) is available on "
            f"this server but not installed in database '{database}'. A role allowed to do so "
            f"installs it with: CREATE EXTENSION {name}; pg_extensions says whether that needs "
            "a superuser."
        )
    near = await db.rows(
        "SELECT name FROM pg_catalog.pg_available_extensions "
        "WHERE name ILIKE $1 ORDER BY 1 LIMIT 8",
        [f"%{name}%"],
    )
    hint = " Similar names: " + ", ".join(r["name"] for r in near) + "." if near else ""
    raise PgHarborError(
        f"The extension '{name}' is not available on this server: its files are not installed "
        f"on the database host, so CREATE EXTENSION would fail.{hint} pg_extensions lists "
        "what can be installed."
    )


async def _objects(db: Db, ext: _Ext, notes: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    kind = (
        "CASE p.prokind WHEN 'a' THEN 'aggregates' WHEN 'p' THEN 'procedures' "
        "WHEN 'w' THEN 'window functions' ELSE 'functions' END"
        if db.v >= 110000
        else "CASE WHEN p.proisagg THEN 'aggregates' ELSE 'functions' END"
    )
    functions = await _attempt(
        db,
        notes,
        "extension functions",
        f"""
        SELECT {kind} AS kind, count(*) AS count,
               (array_agg(DISTINCT p.proname ORDER BY p.proname))[1:{_SAMPLE + 15}] AS names
          FROM pg_catalog.pg_depend d
          JOIN pg_catalog.pg_proc p ON p.oid = d.objid
         WHERE d.classid = 'pg_catalog.pg_proc'::regclass AND {_DEPENDENT}
         GROUP BY 1 ORDER BY 1
        """,
        [ext.oid],
    )
    for row in functions or []:
        out[row["kind"]] = {"count": row["count"], "names": row["names"]}
    relations = await _attempt(
        db,
        notes,
        "extension relations",
        f"""
        SELECT CASE c.relkind WHEN 'r' THEN 'tables' WHEN 'p' THEN 'tables' WHEN 'v' THEN 'views'
                    WHEN 'm' THEN 'materialized views' WHEN 'f' THEN 'foreign tables'
                    WHEN 'S' THEN 'sequences' WHEN 'i' THEN 'indexes' ELSE 'other relations' END
                   AS kind,
               count(*) AS count,
               (array_agg(n.nspname || '.' || c.relname ORDER BY n.nspname, c.relname))
                   [1:{_SAMPLE}] AS names
          FROM pg_catalog.pg_depend d
          JOIN pg_catalog.pg_class c ON c.oid = d.objid
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE d.classid = 'pg_catalog.pg_class'::regclass AND {_DEPENDENT}
         GROUP BY 1 ORDER BY 1
        """,
        [ext.oid],
    )
    for row in relations or []:
        names = row["names"] if row["kind"] not in ("sequences", "indexes") else None
        out[row["kind"]] = {"count": row["count"], **({"names": names} if names else {})}
    types = await _attempt(
        db,
        notes,
        "extension types",
        f"""
        SELECT count(*) AS count,
               (array_agg(n.nspname || '.' || t.typname ORDER BY t.typname))[1:{_SAMPLE}] AS names
          FROM pg_catalog.pg_depend d
          JOIN pg_catalog.pg_type t ON t.oid = d.objid
          JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
         WHERE d.classid = 'pg_catalog.pg_type'::regclass AND {_DEPENDENT}
           AND t.typcategory <> 'A'
           AND (t.typrelid = 0 OR EXISTS (SELECT 1 FROM pg_catalog.pg_class rc
                                           WHERE rc.oid = t.typrelid AND rc.relkind = 'c'))
        """,
        [ext.oid],
    )
    if types and types[0]["count"]:
        out["types"] = {"count": types[0]["count"], "names": types[0]["names"]}
    operators = await _attempt(
        db,
        notes,
        "extension operators",
        f"""
        SELECT count(*) AS count,
               (array_agg(DISTINCT o.oprname ORDER BY o.oprname))[1:{_SAMPLE}] AS names
          FROM pg_catalog.pg_depend d
          JOIN pg_catalog.pg_operator o ON o.oid = d.objid
         WHERE d.classid = 'pg_catalog.pg_operator'::regclass AND {_DEPENDENT}
        """,
        [ext.oid],
    )
    if operators and operators[0]["count"]:
        out["operators"] = {"count": operators[0]["count"], "names": operators[0]["names"]}
    others = await _attempt(
        db,
        notes,
        "other extension objects",
        f"""
        SELECT d.classid::regclass::text AS catalog, count(*) AS count
          FROM pg_catalog.pg_depend d
         WHERE {_DEPENDENT} AND d.classid NOT IN (
               'pg_catalog.pg_class'::regclass, 'pg_catalog.pg_proc'::regclass,
               'pg_catalog.pg_type'::regclass, 'pg_catalog.pg_operator'::regclass)
         GROUP BY 1 ORDER BY 1
        """,
        [ext.oid],
    )
    for row in others or []:
        label = _CATALOG_LABELS.get(row["catalog"], row["catalog"].removeprefix("pg_"))
        out[label] = {"count": row["count"]}
    return out


async def _settings(db: Db, ext: _Ext) -> dict[str, str]:
    prefixes = [ext.name, *_SETTING_PREFIXES.get(ext.name, [])]
    rows = await db.rows(
        """
        SELECT name, setting, unit FROM pg_catalog.pg_settings
         WHERE split_part(name, '.', 1) = ANY($1::text[]) AND setting IS NOT NULL
         ORDER BY name
        """,
        [prefixes],
    )
    return {r["name"]: r["setting"] + (f" {r['unit']}" if r["unit"] else "") for r in rows}


async def _preloaded(db: Db, library: str) -> bool | None:
    """Is ``library`` in shared_preload_libraries? ``None`` when the role may not read it."""
    value = await _attempt(
        db,
        [],
        "shared_preload_libraries",
        "SELECT current_setting('shared_preload_libraries') AS v",
    )
    if not value or value[0]["v"] is None:
        return None
    entries = {item.strip().strip('"').rsplit("/", 1)[-1] for item in value[0]["v"].split(",")}
    return library in entries


# ------------------------------------------------------------- extension-specific details

_Specific = Callable[[Db, _Ext, list[str]], Awaitable[dict[str, Any]]]


async def _stat_statements(db: Db, ext: _Ext, notes: list[str]) -> dict[str, Any]:
    rows = await _attempt(
        db,
        notes,
        "pg_stat_statements view",
        f"SELECT count(*) AS entries FROM {ext.q}.pg_stat_statements",
    )
    if rows is None:
        return {"active": False}
    out: dict[str, Any] = {"active": True, "entries": rows[0]["entries"]}
    limit = await db.value(
        "SELECT setting FROM pg_catalog.pg_settings WHERE name = $1", ["pg_stat_statements.max"]
    )
    if limit:
        out["max_entries"] = int(limit)
        if int(limit) and rows[0]["entries"] >= int(limit) * 0.95:
            out["hint"] = (
                "The statement table is nearly full: old statements are being evicted. "
                "Raise pg_stat_statements.max (restart needed) to keep more history."
            )
    if db.v >= 140000:
        info = await _attempt(
            db,
            [],
            "pg_stat_statements_info",
            "SELECT dealloc, stats_reset::text AS stats_reset "
            f"FROM {ext.q}.pg_stat_statements_info",
        )
        if info:
            out["evictions"] = info[0]["dealloc"]
            out["stats_reset"] = info[0]["stats_reset"]
    return out


async def _buffercache(db: Db, ext: _Ext, notes: list[str]) -> dict[str, Any]:
    size = await db.row(
        "SELECT (SELECT setting::bigint FROM pg_catalog.pg_settings WHERE name = 'shared_buffers')"
        " AS buffers, current_setting('block_size')::int AS block"
    )
    assert size is not None
    block, buffers = size["block"], size["buffers"]
    out: dict[str, Any] = {"shared_buffers_bytes": buffers * block}
    # Scanning the buffer view touches every buffer; keep that to caches of 4 GiB or less.
    if buffers * block > 4 * 1024**3:
        if db.v < 160000:
            notes.append(
                "shared_buffers is over 4 GB: scanning pg_buffercache would be heavy, so no "
                "summary was taken (PostgreSQL 16+ has pg_buffercache_summary())."
            )
            return out
        rows = await _attempt(
            db,
            notes,
            "pg_buffercache_summary",
            f"SELECT buffers_used AS used, buffers_unused AS unused, buffers_dirty AS dirty, "
            f"buffers_pinned AS pinned, usagecount_avg::float8 AS avg_usage "
            f"FROM {ext.q}.pg_buffercache_summary()",
        )
        if rows:
            out.update(_cache_figures(rows[0], block))
        return out
    rows = await _attempt(
        db,
        notes,
        "pg_buffercache",
        f"""
        SELECT count(*) FILTER (WHERE b.relfilenode IS NOT NULL) AS used,
               count(*) FILTER (WHERE b.relfilenode IS NULL) AS unused,
               count(*) FILTER (WHERE b.isdirty) AS dirty,
               count(*) FILTER (WHERE b.pinning_backends > 0) AS pinned,
               avg(b.usagecount)::float8 AS avg_usage
          FROM {ext.q}.pg_buffercache b
        """,
    )
    if rows is None:
        return out
    out.update(_cache_figures(rows[0], block))
    top = await _attempt(
        db,
        notes,
        "pg_buffercache top relations",
        f"""
        SELECT n.nspname || '.' || c.relname AS relation, count(*) AS buffers,
               count(*) FILTER (WHERE b.isdirty) AS dirty
          FROM {ext.q}.pg_buffercache b
          JOIN pg_catalog.pg_class c ON b.relfilenode = pg_catalog.pg_relation_filenode(c.oid)
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE b.reldatabase = (SELECT oid FROM pg_catalog.pg_database
                                 WHERE datname = pg_catalog.current_database())
         GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 10
        """,
    )
    if top:
        pretty = await _sizes(db, [r["buffers"] * block for r in top])
        for row in top:
            row["cached"] = pretty[row["buffers"] * block]
        out["top_relations"] = grid(top, ["relation", "cached", "buffers", "dirty"])
    return out


def _cache_figures(row: dict[str, Any], block: int) -> dict[str, Any]:
    total = row["used"] + row["unused"]
    return {
        "buffers": total,
        "used": row["used"],
        "used_percent": round(100 * row["used"] / total, 1) if total else None,
        "dirty": row["dirty"],
        "pinned": row["pinned"],
        "avg_usage_count": round(row["avg_usage"], 2) if row["avg_usage"] is not None else None,
        "block_size": block,
    }


async def _trgm(db: Db, ext: _Ext, notes: list[str]) -> dict[str, Any]:
    rows = await _attempt(
        db,
        notes,
        "trigram indexes",
        f"""
        SELECT n.nspname || '.' || ic.relname AS index, n.nspname || '.' || tc.relname AS "table",
               am.amname AS method, i.indisvalid AS valid,
               pg_catalog.pg_relation_size(i.indexrelid)::bigint AS bytes,
               pg_catalog.pg_get_indexdef(i.indexrelid) AS definition
          FROM pg_catalog.pg_index i
          JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid
          JOIN pg_catalog.pg_class tc ON tc.oid = i.indrelid
          JOIN pg_catalog.pg_namespace n ON n.oid = ic.relnamespace
          JOIN pg_catalog.pg_am am ON am.oid = ic.relam
         WHERE EXISTS (SELECT 1 FROM unnest(i.indclass::oid[]) AS k(oid)
                         JOIN pg_catalog.pg_opclass o ON o.oid = k.oid
                        WHERE o.opcname IN ('gin_trgm_ops', 'gist_trgm_ops'))
         ORDER BY 1 LIMIT {_SAMPLE + 1}
        """,
    )
    if rows is None:
        return {}
    shown, more = page(rows, _SAMPLE)
    pretty = await _sizes(db, [r["bytes"] for r in shown])
    for row in shown:
        row["size"] = pretty[row["bytes"]]
    out: dict[str, Any] = {
        "trigram_indexes": grid(shown, ["index", "table", "method", "valid", "size", "definition"])
    }
    if more:
        out["trigram_indexes_has_more"] = True
    if not shown:
        out["hint"] = (
            "No trigram index exists. LIKE '%text%' and similarity searches need one: "
            "CREATE INDEX ... USING gin (column gin_trgm_ops)."
        )
    return out


async def _hypopg(db: Db, ext: _Ext, notes: list[str]) -> dict[str, Any]:
    rows = await _attempt(
        db,
        notes,
        "hypopg",
        f"""
        SELECT h.indexrelid::bigint AS id, h.indexname AS name,
               {ext.q}.hypopg_get_indexdef(h.indexrelid) AS definition,
               {ext.q}.hypopg_relation_size(h.indexrelid) AS bytes
          FROM {ext.q}.hypopg() h ORDER BY h.indexname LIMIT {_SAMPLE}
        """,
    )
    if rows is None:
        return {}
    pretty = await _sizes(db, [r["bytes"] for r in rows])
    for row in rows:
        row["estimated_size"] = pretty[row["bytes"]]
    return {
        "hypothetical_indexes": grid(rows, ["id", "name", "definition", "estimated_size"]),
        "note": (
            "Hypothetical indexes live in the memory of one database session and are never "
            "written to disk. This list shows the session that served this call."
        ),
    }


def _type_columns_sql(ext_q: str, index_methods: str, type_names: str) -> str:
    """Columns whose type belongs to the extension, with a flag for 'has an index of ...'."""
    return f"""
        SELECT n.nspname || '.' || c.relname AS "table", a.attname AS "column",
               pg_catalog.format_type(a.atttypid, a.atttypmod) AS type,
               EXISTS (SELECT 1 FROM pg_catalog.pg_index i
                         JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid
                         JOIN pg_catalog.pg_am am ON am.oid = ic.relam
                        WHERE i.indrelid = c.oid AND a.attnum = ANY(i.indkey::int2[])
                          AND am.amname IN ({index_methods})) AS indexed
          FROM pg_catalog.pg_attribute a
          JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
          JOIN pg_catalog.pg_type t ON t.oid = a.atttypid
          JOIN pg_catalog.pg_depend d ON d.classid = 'pg_catalog.pg_type'::regclass
                                     AND d.objid = t.oid
         WHERE d.refclassid = 'pg_catalog.pg_extension'::regclass AND d.refobjid = $1
           AND d.deptype = 'e' AND t.typname IN ({type_names})
           AND a.attnum > 0 AND NOT a.attisdropped AND c.relkind IN ('r', 'p', 'm')
         ORDER BY 1, 2 LIMIT {_SAMPLE * 2}
    """


async def _index_rows(db: Db, notes: list[str], what: str, methods: str) -> list[dict[str, Any]]:
    rows = await _attempt(
        db,
        notes,
        what,
        f"""
        SELECT n.nspname || '.' || ic.relname AS index, n.nspname || '.' || tc.relname AS "table",
               am.amname AS method, ic.reloptions AS options, i.indisvalid AS valid,
               pg_catalog.pg_relation_size(i.indexrelid)::bigint AS bytes,
               pg_catalog.pg_get_indexdef(i.indexrelid) AS definition
          FROM pg_catalog.pg_index i
          JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid
          JOIN pg_catalog.pg_class tc ON tc.oid = i.indrelid
          JOIN pg_catalog.pg_namespace n ON n.oid = ic.relnamespace
          JOIN pg_catalog.pg_am am ON am.oid = ic.relam
         WHERE am.amname IN ({methods}) ORDER BY 1 LIMIT {_SAMPLE}
        """,
    )
    rows = rows or []
    pretty = await _sizes(db, [r["bytes"] for r in rows])
    for row in rows:
        row["size"] = pretty[row["bytes"]]
    return rows


_DIMENSIONS = re.compile(r"\((\d+)\)")


async def _vector(db: Db, ext: _Ext, notes: list[str]) -> dict[str, Any]:
    columns = await _attempt(
        db,
        notes,
        "vector columns",
        _type_columns_sql(ext.q, "'hnsw', 'ivfflat'", "'vector', 'halfvec', 'sparsevec'"),
        [ext.oid],
    )
    out: dict[str, Any] = {"version": ext.version}
    if columns is not None:
        for row in columns:
            found = _DIMENSIONS.search(row["type"])
            row["dimensions"] = int(found.group(1)) if found else None
        out["vector_columns"] = grid(columns, ["table", "column", "type", "dimensions", "indexed"])
        if any(not r["indexed"] for r in columns):
            out["hint"] = (
                "Columns without an hnsw or ivfflat index are searched sequentially: exact but "
                "slow on large tables."
            )
    indexes = await _index_rows(db, notes, "vector indexes", "'hnsw', 'ivfflat'")
    for row in indexes:
        row["options"] = row["options"] or "defaults"
    out["indexes"] = grid(indexes, ["index", "table", "method", "options", "size", "valid"])
    return out


async def _postgis(db: Db, ext: _Ext, notes: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    version = await _attempt(
        db,
        notes,
        "postgis_full_version",
        f"SELECT {ext.q}.postgis_full_version() AS full, {ext.q}.postgis_lib_version() AS lib",
    )
    if version:
        out["version"] = version[0]["lib"]
        out["full_version"] = version[0]["full"]
    columns = await _attempt(
        db,
        notes,
        "geometry columns",
        f"""
        SELECT g."table", g."column", g.type, g.srid, g.dims,
               EXISTS (SELECT 1 FROM pg_catalog.pg_index i
                         JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid
                         JOIN pg_catalog.pg_am am ON am.oid = ic.relam
                         JOIN pg_catalog.pg_attribute att
                           ON att.attrelid = i.indrelid AND att.attnum = ANY(i.indkey::int2[])
                        WHERE i.indrelid = pg_catalog.to_regclass(g.qualified)
                          AND att.attname = g."column"
                          AND am.amname IN ('gist', 'spgist', 'brin')) AS indexed
          FROM (
            SELECT f_table_schema || '.' || f_table_name AS "table",
                   f_geometry_column::text AS "column", type::text, srid, coord_dimension AS dims,
                   pg_catalog.quote_ident(f_table_schema) || '.' ||
                       pg_catalog.quote_ident(f_table_name) AS qualified
              FROM {ext.q}.geometry_columns
            UNION ALL
            SELECT f_table_schema || '.' || f_table_name, f_geography_column::text,
                   'geography ' || type, srid, coord_dimension,
                   pg_catalog.quote_ident(f_table_schema) || '.' ||
                       pg_catalog.quote_ident(f_table_name)
              FROM {ext.q}.geography_columns
          ) g ORDER BY 1, 2 LIMIT {_SAMPLE * 2}
        """,
    )
    if columns is not None:
        out["spatial_columns"] = grid(
            columns, ["table", "column", "type", "srid", "dims", "indexed"]
        )
        if any(not r["indexed"] for r in columns):
            out["hint"] = (
                "Spatial columns without a GiST index make bounding-box and distance "
                "searches scan the whole table."
            )
    indexes = await _attempt(
        db,
        notes,
        "spatial indexes",
        f"""
        SELECT n.nspname || '.' || ic.relname AS index, n.nspname || '.' || tc.relname AS "table",
               am.amname AS method, pg_catalog.pg_relation_size(i.indexrelid)::bigint AS bytes
          FROM pg_catalog.pg_index i
          JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid
          JOIN pg_catalog.pg_class tc ON tc.oid = i.indrelid
          JOIN pg_catalog.pg_namespace n ON n.oid = ic.relnamespace
          JOIN pg_catalog.pg_am am ON am.oid = ic.relam
         WHERE am.amname IN ('gist', 'spgist', 'brin') AND EXISTS (
               SELECT 1 FROM pg_catalog.pg_attribute att
                 JOIN pg_catalog.pg_type t ON t.oid = att.atttypid
                WHERE att.attrelid = i.indrelid AND att.attnum = ANY(i.indkey::int2[])
                  AND t.typname IN ('geometry', 'geography'))
         ORDER BY 1 LIMIT {_SAMPLE}
        """,
    )
    if indexes is not None:
        pretty = await _sizes(db, [r["bytes"] for r in indexes])
        for row in indexes:
            row["size"] = pretty[row["bytes"]]
        out["spatial_indexes"] = grid(indexes, ["index", "table", "method", "size"])
    return out


async def _cron(db: Db, ext: _Ext, notes: list[str]) -> dict[str, Any]:
    with_runs = """
        SELECT j.jobid, j.jobname AS name, j.schedule, left(j.command, 200) AS command,
               j.database, j.username AS "user", j.active,
               r.status AS last_status, r.start_time::text AS last_start,
               left(r.return_message, 200) AS last_message
          FROM cron.job j
          LEFT JOIN LATERAL (SELECT d.status, d.start_time, d.return_message
                               FROM cron.job_run_details d WHERE d.jobid = j.jobid
                              ORDER BY d.start_time DESC NULLS LAST LIMIT 1) r ON true
         ORDER BY j.jobid LIMIT 51
    """
    without_runs = """
        SELECT j.jobid, j.jobname AS name, j.schedule, left(j.command, 200) AS command,
               j.database, j.username AS "user", j.active
          FROM cron.job j ORDER BY j.jobid LIMIT 51
    """
    jobs = await _attempt(db, [], "cron.job_run_details", with_runs)
    columns = [
        "jobid", "name", "schedule", "command", "database", "user", "active",
        "last_status", "last_start", "last_message",
    ]  # fmt: skip
    if jobs is None:
        notes.append(
            "Run history (cron.job_run_details) is not available in this pg_cron version; "
            "only the jobs are listed."
        )
        jobs = await _attempt(db, notes, "cron.job", without_runs)
        columns = columns[:7]
    if jobs is None:
        return {}
    shown, more = page(jobs, 50)
    out: dict[str, Any] = {"jobs": grid(shown, columns), "jobs_has_more": more}
    if not shown:
        out["hint"] = "No job is scheduled (cron.schedule creates one)."
    failed = await _attempt(
        db,
        [],
        "cron failures",
        "SELECT count(*) AS failed FROM cron.job_run_details WHERE status = 'failed' "
        "AND start_time > pg_catalog.now() - interval '1 day'",
    )
    if failed:
        out["failed_runs_last_24h"] = failed[0]["failed"]
    return out


_PARTMAN_COLUMNS = (
    "parent_table", "control", "partition_type", "partition_interval", "premake", "retention",
    "retention_keep_table", "automatic_maintenance", "infinite_time_partitions",
)  # fmt: skip


async def _partman(db: Db, ext: _Ext, notes: list[str]) -> dict[str, Any]:
    present = await _attempt(
        db,
        notes,
        "part_config",
        "SELECT attname::text AS name FROM pg_catalog.pg_attribute "
        "WHERE attrelid = pg_catalog.to_regclass($1) AND attnum > 0 AND NOT attisdropped",
        [f"{ext.q}.part_config"],
    )
    if not present:
        return {}
    have = {r["name"] for r in present}
    wanted = [c for c in _PARTMAN_COLUMNS if c in have]
    rows = await _attempt(
        db,
        notes,
        "part_config",
        f"""
        SELECT {", ".join(f"pc.{c}" for c in wanted)},
               (SELECT count(*) FROM pg_catalog.pg_inherits i
                 WHERE i.inhparent = pg_catalog.to_regclass(pc.parent_table)) AS partitions
          FROM {ext.q}.part_config pc ORDER BY pc.parent_table LIMIT {_SAMPLE + 1}
        """,
    )
    if rows is None:
        return {}
    shown, more = page(rows, _SAMPLE)
    out: dict[str, Any] = {
        "version": ext.version,
        "configured_tables": grid(shown, [*wanted, "partitions"]),
        "configured_tables_has_more": more,
    }
    if not shown:
        out["hint"] = (
            "No table is managed by pg_partman yet. create_parent() registers a partitioned "
            "table; run_maintenance() (or the background worker) creates and drops partitions."
        )
    return out


async def _pgaudit(db: Db, ext: _Ext, notes: list[str]) -> dict[str, Any]:
    row = await db.row(
        "SELECT current_setting('pgaudit.log', true) AS log, "
        "current_setting('pgaudit.role', true) AS role"
    )
    assert row is not None
    if row["log"] is None:
        return {
            "active": False,
            "hint": "pgaudit is not loaded: add it to shared_preload_libraries.",
        }
    role = row["role"] or None
    out: dict[str, Any] = {"session_audit_classes": row["log"], "object_audit_role": role}
    if row["log"] == "none" and not role:
        out["active"] = False
        out["hint"] = (
            "pgaudit is loaded but audits nothing: set pgaudit.log (for example 'ddl, write') "
            "or pgaudit.role for object-level auditing."
        )
    else:
        out["active"] = True
    return out


_SAFE_SERVER_OPTIONS = {
    "host", "hostaddr", "port", "dbname", "sslmode", "fetch_size", "use_remote_estimate",
    "extensions", "updatable", "async_capable", "application_name", "connect_timeout",
    "batch_size", "keep_connections", "fdw_startup_cost", "fdw_tuple_cost",
}  # fmt: skip


async def _foreign(db: Db, ext: _Ext, notes: list[str]) -> dict[str, Any]:
    only_wrapper = "AND w.fdwname = 'postgres_fdw'" if ext.name == "postgres_fdw" else ""
    servers = await _attempt(
        db,
        notes,
        "foreign servers",
        f"""
        SELECT s.srvname AS server, w.fdwname AS wrapper,
               pg_catalog.pg_get_userbyid(s.srvowner) AS owner, s.srvoptions AS options,
               (SELECT count(*) FROM pg_catalog.pg_user_mappings um
                 WHERE um.srvid = s.oid) AS user_mappings,
               ARRAY(SELECT um.usename::text FROM pg_catalog.pg_user_mappings um
                      WHERE um.srvid = s.oid ORDER BY 1) AS mapped_users,
               (SELECT count(*) FROM pg_catalog.pg_foreign_table ft
                 WHERE ft.ftserver = s.oid) AS foreign_tables
          FROM pg_catalog.pg_foreign_server s
          JOIN pg_catalog.pg_foreign_data_wrapper w ON w.oid = s.srvfdw
         WHERE true {only_wrapper}
         ORDER BY 1 LIMIT {_SAMPLE}
        """,
    )
    if servers is None:
        return {}
    for row in servers:
        shown, hidden = [], 0
        for option in row["options"] or []:
            key = option.split("=", 1)[0]
            if key in _SAFE_SERVER_OPTIONS:
                shown.append(option)
            else:
                hidden += 1
        row["options"] = shown
        if hidden:
            row["other_options"] = hidden
    columns = ["server", "wrapper", "owner", "options"]
    if any("other_options" in row for row in servers):
        columns.append("other_options")
    columns += ["user_mappings", "mapped_users", "foreign_tables"]
    out: dict[str, Any] = {"servers": grid(servers, columns)}
    tables = await _attempt(
        db,
        notes,
        "foreign tables",
        f"""
        SELECT n.nspname || '.' || c.relname AS "table", s.srvname AS server
          FROM pg_catalog.pg_foreign_table ft
          JOIN pg_catalog.pg_class c ON c.oid = ft.ftrelid
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
          JOIN pg_catalog.pg_foreign_server s ON s.oid = ft.ftserver
          JOIN pg_catalog.pg_foreign_data_wrapper w ON w.oid = s.srvfdw
         WHERE true {only_wrapper}
         ORDER BY 1 LIMIT {_SAMPLE * 2}
        """,
    )
    if tables:
        out["foreign_tables"] = grid(tables, ["table", "server"])
    out["note"] = (
        "Only the user names of mappings are listed; mapping options (passwords) and server "
        "options outside a short safe list are never read."
    )
    return out


async def _pgstattuple(db: Db, ext: _Ext, notes: list[str]) -> dict[str, Any]:
    return {
        "tip": (
            "pgstattuple(relation) reads the whole relation to measure dead tuples and free "
            "space; pgstattuple_approx(relation) is cheap but covers tables and materialized "
            "views only; pgstatindex(index) reports b-tree density. Run them on one relation at "
            "a time."
        )
    }


async def _pgcrypto(db: Db, ext: _Ext, notes: list[str]) -> dict[str, Any]:
    return {
        "tip": (
            "crypt() with gen_salt('bf') hashes passwords; pgp_sym_encrypt() and "
            "pgp_pub_encrypt() encrypt values; digest() and hmac() compute hashes. Keys passed "
            "as function arguments end up in logs and pg_stat_statements; gen_random_uuid() "
            "is built in from PostgreSQL 13."
        )
    }


async def _timescale(db: Db, ext: _Ext, notes: list[str]) -> dict[str, Any]:
    rows = await _attempt(
        db,
        notes,
        "timescaledb_information.hypertables",
        """
        SELECT hypertable_schema || '.' || hypertable_name AS hypertable,
               num_dimensions AS dimensions, num_chunks AS chunks,
               compression_enabled AS compression
          FROM timescaledb_information.hypertables ORDER BY 1 LIMIT 50
        """,
    )
    if rows is None:
        return {}
    out: dict[str, Any] = {
        "version": ext.version,
        "hypertables": grid(rows, ["hypertable", "dimensions", "chunks", "compression"]),
    }
    aggregates = await _attempt(
        db,
        [],
        "continuous aggregates",
        "SELECT count(*) AS n FROM timescaledb_information.continuous_aggregates",
    )
    if aggregates:
        out["continuous_aggregates"] = aggregates[0]["n"]
    return out


_SPECIFIC: dict[str, _Specific] = {
    "pg_stat_statements": _stat_statements,
    "pg_buffercache": _buffercache,
    "pg_trgm": _trgm,
    "hypopg": _hypopg,
    "vector": _vector,
    "postgis": _postgis,
    "pg_cron": _cron,
    "pg_partman": _partman,
    "pgaudit": _pgaudit,
    "postgres_fdw": _foreign,
    "dblink": _foreign,
    "pgstattuple": _pgstattuple,
    "pgcrypto": _pgcrypto,
    "timescaledb": _timescale,
}


# ----------------------------------------------------------------------- pg_integrity_check

_SCOPE = f"""($1::text IS NULL OR n.nspname = $1)
       AND ($1::text IS NOT NULL OR ({USER_SCHEMA_FILTER}))"""
_LIMIT = 51
_Check = Callable[[Db, str | None, list[str]], Awaitable[dict[str, Any] | None]]
_SKIPPED: dict[str, Any] = {"skipped": True}


def _problem(
    check: str, severity: str, what: str, fix: str, items: list[dict[str, Any]], columns: list[str]
) -> dict[str, Any]:
    shown, more = page(items, _LIMIT - 1)
    out: dict[str, Any] = {
        "check": check,
        "severity": severity,
        "what": what,
        "fix": fix,
        "items": grid(shown, columns),
    }
    if more:
        out["items_has_more"] = True
    return out


async def _invalid_indexes(db: Db, schema: str | None, notes: list[str]) -> dict[str, Any] | None:
    rows = await _attempt(
        db,
        notes,
        "invalid index check",
        f"""
        SELECT n.nspname || '.' || ic.relname AS index, n.nspname || '.' || tc.relname AS "table",
               i.indisvalid AS valid, i.indisready AS ready
          FROM pg_catalog.pg_index i
          JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid
          JOIN pg_catalog.pg_class tc ON tc.oid = i.indrelid
          JOIN pg_catalog.pg_namespace n ON n.oid = tc.relnamespace
         WHERE (NOT i.indisvalid OR NOT i.indisready) AND {_SCOPE}
         ORDER BY 1 LIMIT {_LIMIT}
        """,
        [schema],
    )
    if rows is None:
        return _SKIPPED
    if not rows:
        return None
    return _problem(
        "invalid_indexes",
        "warning",
        "Indexes the planner cannot use (a failed or interrupted CREATE INDEX CONCURRENTLY, or "
        "one still being built; pg_stat_progress_create_index shows builds in progress).",
        "DROP INDEX the broken one and run CREATE INDEX CONCURRENTLY again.",
        rows,
        ["index", "table", "valid", "ready"],
    )


async def _not_valid_constraints(
    db: Db, schema: str | None, notes: list[str]
) -> dict[str, Any] | None:
    parent = "AND con.conparentid = 0" if db.v >= 110000 else ""
    rows = await _attempt(
        db,
        notes,
        "constraint check",
        f"""
        SELECT n.nspname || '.' || c.relname AS "table", con.conname AS "constraint",
               CASE con.contype WHEN 'f' THEN 'foreign key' WHEN 'c' THEN 'check'
                    WHEN 'n' THEN 'not null' ELSE con.contype::text END AS type
          FROM pg_catalog.pg_constraint con
          JOIN pg_catalog.pg_class c ON c.oid = con.conrelid
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE NOT con.convalidated AND con.conrelid <> 0 {parent} AND {_SCOPE}
         ORDER BY 1, 2 LIMIT {_LIMIT}
        """,
        [schema],
    )
    if rows is None:
        return _SKIPPED
    if not rows:
        return None
    return _problem(
        "not_valid_constraints",
        "warning",
        "Constraints added NOT VALID: new rows are checked, but existing rows may break them.",
        "ALTER TABLE ... VALIDATE CONSTRAINT <name> (it scans the table, without blocking writes).",
        rows,
        ["table", "constraint", "type"],
    )


async def _no_primary_key(db: Db, schema: str | None, notes: list[str]) -> dict[str, Any] | None:
    partition = "AND NOT c.relispartition" if db.v >= 100000 else ""
    rows = await _attempt(
        db,
        notes,
        "primary key check",
        f"""
        SELECT n.nspname || '.' || c.relname AS "table",
               EXISTS (SELECT 1 FROM pg_catalog.pg_index i
                        WHERE i.indrelid = c.oid AND i.indisunique AND i.indpred IS NULL
                          AND i.indisvalid) AS has_unique_index,
               CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END AS estimated_rows
          FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relkind IN ('r', 'p') {partition} AND {_SCOPE}
           AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_index i
                            WHERE i.indrelid = c.oid AND i.indisprimary)
           AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_depend d
                            WHERE d.classid = 'pg_catalog.pg_class'::regclass AND d.objid = c.oid
                              AND d.deptype = 'e')
         ORDER BY 1 LIMIT {_LIMIT}
        """,
        [schema],
    )
    if rows is None:
        return _SKIPPED
    if not rows:
        return None
    return _problem(
        "no_primary_key",
        "warning",
        "Tables without a primary key: rows cannot be identified reliably, and logical "
        "replication of UPDATE/DELETE needs a replica identity.",
        "ALTER TABLE ... ADD PRIMARY KEY (...), or use the unique index as REPLICA IDENTITY.",
        rows,
        ["table", "has_unique_index", "estimated_rows"],
    )


async def _prepared_transactions(
    db: Db, schema: str | None, notes: list[str]
) -> dict[str, Any] | None:
    rows = await _attempt(
        db,
        notes,
        "prepared transaction check",
        f"""
        SELECT gid, owner::text AS owner, database::text AS database, prepared::text AS prepared,
               (pg_catalog.now() - prepared)::text AS age
          FROM pg_catalog.pg_prepared_xacts ORDER BY prepared LIMIT {_LIMIT}
        """,
    )
    if rows is None:
        return _SKIPPED
    if not rows:
        return None
    return _problem(
        "prepared_transactions",
        "warning",
        "Prepared (two-phase) transactions that were never finished keep their locks and hold "
        "back VACUUM until someone commits or rolls them back.",
        "COMMIT PREPARED '<gid>' or ROLLBACK PREPARED '<gid>' once the coordinator is known.",
        rows,
        ["gid", "owner", "database", "prepared", "age"],
    )


async def _disabled_triggers(db: Db, schema: str | None, notes: list[str]) -> dict[str, Any] | None:
    rows = await _attempt(
        db,
        notes,
        "trigger check",
        f"""
        SELECT n.nspname || '.' || c.relname AS "table", t.tgname AS trigger,
               CASE WHEN t.tgisinternal THEN 'foreign key enforcement' ELSE 'user trigger' END
                   AS kind
          FROM pg_catalog.pg_trigger t
          JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE t.tgenabled = 'D' AND {_SCOPE}
         ORDER BY 1, 2 LIMIT {_LIMIT}
        """,
        [schema],
    )
    if rows is None:
        return _SKIPPED
    if not rows:
        return None
    internal = any(r["kind"] != "user trigger" for r in rows)
    return _problem(
        "disabled_triggers",
        "critical" if internal else "warning",
        "Disabled triggers. When they enforce foreign keys, the database no longer guarantees "
        "referential integrity for those tables.",
        "ALTER TABLE ... ENABLE TRIGGER <name> (or ENABLE TRIGGER ALL), then check the data.",
        rows,
        ["table", "trigger", "kind"],
    )


async def _collation_versions(
    db: Db, schema: str | None, notes: list[str]
) -> dict[str, Any] | None:
    if db.v < 100000:
        notes.append(
            "Collation version tracking needs PostgreSQL 10 or newer; that check was skipped."
        )
        return _SKIPPED
    items: list[dict[str, Any]] = []
    collations = await _attempt(
        db,
        notes,
        "collation version check",
        f"""
        SELECT 'collation' AS kind, n.nspname || '.' || c.collname AS object,
               c.collversion AS recorded, pg_catalog.pg_collation_actual_version(c.oid) AS actual
          FROM pg_catalog.pg_collation c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.collnamespace
         WHERE c.collversion IS NOT NULL
           AND c.collversion IS DISTINCT FROM pg_catalog.pg_collation_actual_version(c.oid)
         ORDER BY 2 LIMIT {_LIMIT}
        """,
    )
    if collations is None:
        return _SKIPPED
    items += collations
    if db.v >= 150000:
        database = await _attempt(
            db,
            notes,
            "database collation check",
            """
            SELECT 'database' AS kind, d.datname::text AS object,
                   d.datcollversion AS recorded,
                   pg_catalog.pg_database_collation_actual_version(d.oid) AS actual
              FROM pg_catalog.pg_database d
             WHERE d.datname = pg_catalog.current_database() AND d.datcollversion IS NOT NULL
               AND d.datcollversion
                   IS DISTINCT FROM pg_catalog.pg_database_collation_actual_version(d.oid)
            """,
        )
        items += database or []
    if not items:
        return None
    return _problem(
        "collation_versions",
        "warning",
        "The operating system or ICU library now sorts text differently from when these "
        "collations were created: indexes on text columns that use them can return wrong "
        "results.",
        "REINDEX the indexes that use them (pg_describe_table shows a table's indexes), then "
        "ALTER COLLATION ... REFRESH VERSION (or ALTER DATABASE ... REFRESH COLLATION VERSION).",
        items,
        ["kind", "object", "recorded", "actual"],
    )


async def _data_checksums(db: Db, schema: str | None, notes: list[str]) -> dict[str, Any] | None:
    state = await db.value("SELECT current_setting('data_checksums')")
    if state != "off":
        return None
    return _problem(
        "data_checksums",
        "warning",
        "Data checksums are off: damaged data pages are not detected when they are read.",
        "Enable them with the pg_checksums tool while the server is stopped (PostgreSQL 12+), "
        "or create new clusters with initdb --data-checksums.",
        [{"setting": "data_checksums", "value": state}],
        ["setting", "value"],
    )


async def _checksum_failures(db: Db, schema: str | None, notes: list[str]) -> dict[str, Any] | None:
    if db.v < 120000:
        notes.append(
            "Checksum failure counters need PostgreSQL 12 or newer; that check was skipped."
        )
        return _SKIPPED
    rows = await _attempt(
        db,
        notes,
        "checksum failure check",
        """
        SELECT checksum_failures AS failures, checksum_last_failure::text AS last_failure
          FROM pg_catalog.pg_stat_database
         WHERE datname = pg_catalog.current_database() AND checksum_failures > 0
        """,
    )
    if rows is None:
        return _SKIPPED
    if not rows:
        return None
    return _problem(
        "checksum_failures",
        "critical",
        "Checksum failures were recorded in this database: data pages on disk were corrupted.",
        "Check the storage hardware, restore the affected tables from a backup, and run "
        "pg_integrity_check with index=<btree index> (amcheck) to look for damaged indexes.",
        rows,
        ["failures", "last_failure"],
    )


async def _unowned_sequences(db: Db, schema: str | None, notes: list[str]) -> dict[str, Any] | None:
    rows = await _attempt(
        db,
        notes,
        "sequence ownership check",
        f"""
        SELECT n.nspname || '.' || c.relname AS sequence,
               (SELECT string_agg(DISTINCT tn.nspname || '.' || tc.relname || '.' || att.attname,
                                  ', ')
                  FROM pg_catalog.pg_depend dd
                  JOIN pg_catalog.pg_attrdef ad ON ad.oid = dd.objid
                  JOIN pg_catalog.pg_class tc ON tc.oid = ad.adrelid
                  JOIN pg_catalog.pg_namespace tn ON tn.oid = tc.relnamespace
                  JOIN pg_catalog.pg_attribute att
                    ON att.attrelid = ad.adrelid AND att.attnum = ad.adnum
                 WHERE dd.classid = 'pg_catalog.pg_attrdef'::regclass
                   AND dd.refclassid = 'pg_catalog.pg_class'::regclass
                   AND dd.refobjid = c.oid) AS default_of
          FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relkind = 'S' AND {_SCOPE}
           AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_depend d
                            WHERE d.classid = 'pg_catalog.pg_class'::regclass AND d.objid = c.oid
                              AND d.refclassid = 'pg_catalog.pg_class'::regclass
                              AND d.deptype IN ('a', 'i'))
           AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_depend d
                            WHERE d.classid = 'pg_catalog.pg_class'::regclass AND d.objid = c.oid
                              AND d.deptype = 'e')
         ORDER BY 1 LIMIT {_LIMIT}
        """,
        [schema],
    )
    if rows is None:
        return _SKIPPED
    if not rows:
        return None
    for row in rows:
        row["status"] = (
            f"default of {row['default_of']}, but not owned by it"
            if row["default_of"]
            else "no column uses it"
        )
    return _problem(
        "unowned_sequences",
        "info",
        "Sequences that no column owns. They are not dropped with their table, so one that is "
        "still the default of a column is left behind when the table goes, and one that "
        "nothing uses is an orphan.",
        "ALTER SEQUENCE ... OWNED BY table.column for the ones a column uses; DROP SEQUENCE "
        "the orphans.",
        rows,
        ["sequence", "status"],
    )


_INTEGRITY_CHECKS: tuple[_Check, ...] = (
    _invalid_indexes,
    _not_valid_constraints,
    _no_primary_key,
    _prepared_transactions,
    _disabled_triggers,
    _collation_versions,
    _data_checksums,
    _checksum_failures,
    _unowned_sequences,
)


@tool(group="extensions", title="Integrity check")
async def pg_integrity_check(
    connection: str | None = None,
    schema: str | None = None,
    index: str | None = None,
) -> dict[str, Any]:
    """Look for data integrity problems: invalid indexes, constraints added NOT VALID,
    tables without a primary key, forgotten prepared transactions, disabled triggers
    (including foreign key enforcement), collation versions that changed under the
    server, data checksums off or failing, and sequences that no column owns.

    `schema` limits the object checks to one schema. With `index` (the name of a b-tree
    index) and the amcheck extension (PostgreSQL 10+) it also verifies that index's
    structure with bt_index_check, which needs a superuser and reads the whole index. Nothing
    is changed; each problem comes with a fix to apply yourself.
    """
    app = get_app()
    notes: list[str] = []
    problems: list[dict[str, Any]] = []
    clean: list[str] = []
    async with app.read(connection) as db:
        for check in _INTEGRITY_CHECKS:
            result = await check(db, schema, notes)
            name = check.__name__.removeprefix("_")
            if result is _SKIPPED:
                continue
            if result is None:
                clean.append(name)
            else:
                problems.append(result)
        amcheck = await _amcheck(db, index) if index else None
        amcheck_ready = None if index else await _amcheck_ready(db)
    order = {"critical": 0, "warning": 1, "info": 2}
    problems.sort(key=lambda p: order[p["severity"]])
    total = len(problems) + len(clean)
    out: dict[str, Any] = {
        "connection": db.profile.name,
        "scope": f"schema {schema}" if schema else "all user schemas",
        "summary": (
            f"{len(problems)} problem(s) found in {total} check(s)."
            if problems
            else f"No integrity problems found in {total} check(s)."
        ),
    }
    if problems:
        out["problems"] = problems
    if clean:
        out["clean_checks"] = clean
    if amcheck:
        out["amcheck"] = amcheck
    if amcheck_ready:
        out["amcheck_hint"] = amcheck_ready
    if notes:
        out["notes"] = notes
    return out


async def _amcheck_ready(db: Db) -> str | None:
    if db.v < 100000 or db.ext("amcheck") is None:
        return None
    return (
        "The amcheck extension is installed: pass index=<b-tree index name> to verify one "
        "index (superuser only; it reads the whole index)."
    )


async def _amcheck(db: Db, index: str) -> dict[str, Any]:
    db.need_version(100000, "amcheck")
    row = await db.row(
        "SELECT n.nspname AS schema FROM pg_catalog.pg_extension e "
        "JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'amcheck'"
    )
    if row is None:
        raise PgHarborError(
            f"The extension 'amcheck' is not installed in database '{db.facts.database}'. "
            "A superuser installs it with: CREATE EXTENSION amcheck;"
        )
    relation = await resolve_relation(db, index, kinds="i", what="index")
    label = f"{relation['schema']}.{relation['name']}"
    method = await db.value(
        "SELECT am.amname FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_am am ON am.oid = c.relam WHERE c.oid = $1",
        [relation["oid"]],
    )
    if method != "btree":
        raise PgHarborError(
            f"amcheck verifies b-tree indexes only; {label} is a {method} index. "
            "pg_describe_table lists the indexes of a table with their types."
        )
    size = await db.value(
        "SELECT pg_catalog.pg_size_pretty(pg_catalog.pg_relation_size($1::bigint::oid))",
        [relation["oid"]],
    )
    await db.rows("SAVEPOINT pgh_amcheck")
    try:
        await db.rows(
            f"SELECT count(*) FROM (SELECT {quote_ident(row['schema'])}.bt_index_check("
            "$1::bigint::oid::regclass)) AS checked",
            [relation["oid"]],
        )
    except psycopg.errors.InsufficientPrivilege:
        await db.rows("ROLLBACK TO SAVEPOINT pgh_amcheck")
        raise PgHarborError(
            "amcheck's bt_index_check is restricted to superusers. Use a connection whose role "
            "is a superuser, or GRANT EXECUTE on bt_index_check to the role."
        ) from None
    except psycopg.errors.QueryCanceled:
        await db.rows("ROLLBACK TO SAVEPOINT pgh_amcheck")
        return {
            "index": label,
            "size": size,
            "status": "not finished",
            "message": "The check hit the statement time limit before it finished; the index "
            "is large. Raise the connection's statement timeout and retry.",
        }
    except psycopg.Error as exc:
        await db.rows("ROLLBACK TO SAVEPOINT pgh_amcheck")
        state = exc.sqlstate or ""
        message = exc.diag.message_primary or str(exc)
        if state.startswith("XX"):
            result: dict[str, Any] = {"index": label, "size": size, "status": "CORRUPT"}
            result["message"] = message
            if exc.diag.message_detail:
                result["detail"] = exc.diag.message_detail
            result["fix"] = "REINDEX INDEX CONCURRENTLY the index, then investigate the storage."
            return result
        raise PgHarborError(f"amcheck could not verify {label}: {message}") from None
    finally:
        await db.rows("RELEASE SAVEPOINT pgh_amcheck")
    return {
        "index": label,
        "size": size,
        "status": "ok",
        "checked": "b-tree structure (bt_index_check); the heap was not compared with the index",
    }
