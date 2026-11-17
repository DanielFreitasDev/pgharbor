"""Finding your way around: schemas, tables and the full description of one table."""

from typing import Any, Literal

from ..app import get_app, tool
from ._util import (
    USER_SCHEMA_FILTER,
    clamp,
    grid,
    like_pattern,
    page,
    relation_kind_label,
    resolve_relation,
)

__all__ = ["pg_describe_table", "pg_list_schemas", "pg_list_tables"]


@tool(group="schema", title="List schemas")
async def pg_list_schemas(
    connection: str | None = None, include_system: bool = False
) -> dict[str, Any]:
    """List the schemas (namespaces) of the database with owner, comment, how many tables,
    views, sequences and functions each holds, and total size.

    The first step when exploring a database. System schemas (pg_catalog,
    information_schema) are left out unless `include_system` is true.
    """
    app = get_app()
    async with app.read(connection) as db:
        rows = await db.rows(
            f"""
            SELECT n.nspname AS schema,
                   pg_catalog.pg_get_userbyid(n.nspowner) AS owner,
                   count(c.oid) FILTER (WHERE c.relkind IN ('r', 'p')) AS tables,
                   count(c.oid) FILTER (WHERE c.relkind = 'v') AS views,
                   count(c.oid) FILTER (WHERE c.relkind = 'm') AS materialized_views,
                   count(c.oid) FILTER (WHERE c.relkind = 'S') AS sequences,
                   count(c.oid) FILTER (WHERE c.relkind = 'f') AS foreign_tables,
                   (SELECT count(*) FROM pg_catalog.pg_proc p WHERE p.pronamespace = n.oid)
                       AS functions,
                   coalesce(sum(pg_catalog.pg_total_relation_size(c.oid))
                            FILTER (WHERE c.relkind IN ('r', 'm')), 0)::bigint AS bytes,
                   pg_catalog.obj_description(n.oid, 'pg_namespace') AS comment
              FROM pg_catalog.pg_namespace n
              LEFT JOIN pg_catalog.pg_class c ON c.relnamespace = n.oid
             WHERE n.nspname !~ '^pg_(toast|temp)' AND ($1 OR ({USER_SCHEMA_FILTER}))
             GROUP BY n.oid, n.nspname, n.nspowner
             ORDER BY n.nspname
            """,
            [include_system],
        )
        sizes = (
            {
                r["b"]: r["p"]
                for r in await db.rows(
                    "SELECT b, pg_catalog.pg_size_pretty(b) AS p FROM unnest($1::bigint[]) AS u(b)",
                    [[r["bytes"] for r in rows]],
                )
            }
            if rows
            else {}
        )
    for row in rows:
        row["size"] = sizes.get(row.pop("bytes"))
        if not row["comment"]:
            del row["comment"]
    return {"connection": db.profile.name, "database": db.facts.database, "schemas": rows}


@tool(group="schema", title="List tables")
async def pg_list_tables(
    connection: str | None = None,
    schema: str | None = None,
    pattern: str | None = None,
    kinds: list[Literal["table", "view", "materialized view", "foreign table"]] | None = None,
    order_by: Literal["name", "size", "rows"] = "name",
    include_partitions: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """List tables (and optionally views, materialized views, foreign tables) with
    estimated row count, size on disk and comment.

    Filter by `schema` and by a name `pattern` (`*` and `?` wildcards; plain text matches
    any name containing it). `order_by="size"` finds the biggest tables. Partitions are
    hidden behind their parent unless `include_partitions` is true. Row counts are the
    planner's estimates: instant, and close enough to choose a strategy.
    """
    wanted = kinds or ["table"]
    letters: list[str] = []
    for kind in wanted:
        letters += {
            "table": ["r", "p"],
            "view": ["v"],
            "materialized view": ["m"],
            "foreign table": ["f"],
        }[kind]
    limit = clamp(limit, 100, 1000)
    order = {
        "name": "n.nspname, c.relname",
        "size": "bytes DESC, n.nspname, c.relname",
        "rows": "c.reltuples DESC, n.nspname, c.relname",
    }[order_by]
    app = get_app()
    async with app.read(connection) as db:
        partition_filter = "" if include_partitions or db.v < 100000 else "AND NOT c.relispartition"
        rows = await db.rows(
            f"""
            SELECT n.nspname AS schema, c.relname AS name, c.relkind AS kind,
                   CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END AS rows,
                   coalesce(
                       CASE WHEN c.relkind = 'p' THEN (
                           SELECT sum(pg_catalog.pg_total_relation_size(i.inhrelid))
                             FROM pg_catalog.pg_inherits i WHERE i.inhparent = c.oid)
                       END,
                       pg_catalog.pg_total_relation_size(c.oid))::bigint AS bytes,
                   left(pg_catalog.obj_description(c.oid, 'pg_class'), 160) AS comment,
                   count(*) OVER () AS total
              FROM pg_catalog.pg_class c
              JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
             WHERE c.relkind = ANY($1::text[])
               AND ($2::text IS NULL OR n.nspname = $2)
               AND ($3::text IS NULL OR c.relname ILIKE $3)
               AND ($2::text IS NOT NULL OR ({USER_SCHEMA_FILTER}))
               AND n.nspname !~ '^pg_(toast|temp)'
               {partition_filter}
             ORDER BY {order}
             LIMIT $4 OFFSET $5
            """,
            [letters, schema, like_pattern(pattern), limit + 1, max(0, int(offset))],
        )
        rows, has_more = page(rows, limit)
        total = rows[0]["total"] if rows else 0
        sizes = (
            {
                r["b"]: r["p"]
                for r in await db.rows(
                    "SELECT b, pg_catalog.pg_size_pretty(b) AS p FROM unnest($1::bigint[]) AS u(b)",
                    [[r["bytes"] for r in rows]],
                )
            }
            if rows
            else {}
        )
    for row in rows:
        row["kind"] = relation_kind_label(row["kind"])
        row["size"] = sizes.get(row["bytes"])
        del row["total"]
    out: dict[str, Any] = {"connection": db.profile.name, "total": total}
    out.update(grid(rows, ["schema", "name", "kind", "rows", "size", "comment"]))
    out["has_more"] = has_more
    if has_more:
        out["next_offset"] = offset + limit
    if not rows and (schema or pattern):
        out["hint"] = (
            "Nothing matched. pg_list_schemas shows the schemas; pg_search looks "
            "for a name everywhere."
        )
    return out


_CONSTRAINT_TYPES = {
    "p": "primary key",
    "u": "unique",
    "f": "foreign key",
    "c": "check",
    "x": "exclusion",
    "t": "constraint trigger",
}
_TRIGGER_STATES = {"O": "enabled", "D": "disabled", "R": "replica only", "A": "always"}
_POLICY_COMMANDS = {"r": "SELECT", "a": "INSERT", "w": "UPDATE", "d": "DELETE", "*": "ALL"}
_PERSISTENCE = {"p": "permanent", "u": "unlogged", "t": "temporary"}
_REPLICA_IDENTITY = {"d": "default", "n": "nothing", "f": "full", "i": "index"}


@tool(group="schema", title="Describe a table")
async def pg_describe_table(
    table: str,
    connection: str | None = None,
    detail: Literal["summary", "full"] = "full",
) -> dict[str, Any]:
    """Describe one table, view, materialized view or foreign table: columns with types,
    defaults and comments; primary key, foreign keys (both directions), unique and check
    constraints; indexes with size and usage; triggers; row security policies; partitions;
    size, estimated rows and last vacuum/analyze.

    `table` is `name` or `schema.name`. Read this before writing a query against a table.
    `detail="summary"` returns only columns and keys.
    """
    app = get_app()
    full = detail == "full"
    async with app.read(connection) as db:
        relation = await resolve_relation(db, table)
        oid = relation["oid"]
        v = db.v
        partition = (
            """, c.relispartition AS is_partition,
               CASE WHEN c.relkind = 'p' THEN pg_catalog.pg_get_partkeydef(c.oid) END
                   AS partition_key,
               pg_catalog.pg_get_expr(c.relpartbound, c.oid) AS partition_bound"""
            if v >= 100000
            else ""
        )
        head = await db.row(
            f"""
            SELECT pg_catalog.pg_get_userbyid(c.relowner) AS owner,
                   pg_catalog.obj_description(c.oid, 'pg_class') AS comment,
                   CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END AS rows,
                   c.relpersistence AS persistence, c.reloptions AS options,
                   (SELECT spcname FROM pg_catalog.pg_tablespace t WHERE t.oid = c.reltablespace)
                       AS tablespace,
                   c.relrowsecurity AS row_security, c.relforcerowsecurity AS force_row_security,
                   c.relreplident AS replica_identity,
                   pg_catalog.pg_size_pretty(pg_catalog.pg_total_relation_size(c.oid)) AS total,
                   pg_catalog.pg_size_pretty(pg_catalog.pg_table_size(c.oid)) AS data,
                   pg_catalog.pg_size_pretty(pg_catalog.pg_indexes_size(c.oid)) AS indexes,
                   s.n_live_tup AS live_rows, s.n_dead_tup AS dead_rows,
                   s.seq_scan AS sequential_scans, s.idx_scan AS index_scans,
                   greatest(s.last_vacuum, s.last_autovacuum)::text AS last_vacuum,
                   greatest(s.last_analyze, s.last_autoanalyze)::text AS last_analyze
                   {partition}
              FROM pg_catalog.pg_class c
              LEFT JOIN pg_catalog.pg_stat_all_tables s ON s.relid = c.oid
             WHERE c.oid = $1
            """,
            [oid],
        )
        assert head is not None
        identity = (
            "CASE a.attidentity WHEN 'a' THEN 'identity always' "
            "WHEN 'd' THEN 'identity by default' END"
            if v >= 100000
            else "NULL::text"
        )
        generated = (
            "CASE a.attgenerated WHEN 's' THEN 'generated stored' "
            "WHEN 'v' THEN 'generated virtual' END"
            if v >= 120000
            else "NULL::text"
        )
        columns = await db.rows(
            f"""
            SELECT a.attname AS name,
                   pg_catalog.format_type(a.atttypid, a.atttypmod) AS type,
                   NOT a.attnotnull AS nullable,
                   pg_catalog.pg_get_expr(d.adbin, d.adrelid) AS "default",
                   coalesce({identity}, {generated}) AS extra,
                   pg_catalog.col_description(a.attrelid, a.attnum) AS comment
              FROM pg_catalog.pg_attribute a
              LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
             WHERE a.attrelid = $1 AND a.attnum > 0 AND NOT a.attisdropped
             ORDER BY a.attnum
            """,
            [oid],
        )
        parent_only = "AND con.conparentid = 0" if v >= 110000 else ""
        constraints = await db.rows(
            """
            SELECT con.conname AS name, con.contype AS type,
                   pg_catalog.pg_get_constraintdef(con.oid, true) AS definition,
                   con.convalidated AS valid
              FROM pg_catalog.pg_constraint con
             WHERE con.conrelid = $1 AND con.contype <> 'n'
             ORDER BY pg_catalog.array_position(ARRAY['p','u','f','c','x','t']::"char"[],
                                                con.contype), con.conname
            """,
            [oid],
        )
        indexes: list[dict[str, Any]] = []
        referenced_by: list[dict[str, Any]] = []
        triggers: list[dict[str, Any]] = []
        policies: list[dict[str, Any]] = []
        children: list[dict[str, Any]] = []
        parents: list[dict[str, Any]] = []
        definition = None
        foreign = None
        if full:
            indexes = await db.rows(
                """
                SELECT ic.relname AS name, pg_catalog.pg_get_indexdef(i.indexrelid) AS definition,
                       i.indisvalid AS valid,
                       pg_catalog.pg_size_pretty(pg_catalog.pg_relation_size(i.indexrelid)) AS size,
                       s.idx_scan AS scans
                  FROM pg_catalog.pg_index i
                  JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid
                  LEFT JOIN pg_catalog.pg_stat_all_indexes s ON s.indexrelid = i.indexrelid
                 WHERE i.indrelid = $1
                 ORDER BY i.indisprimary DESC, i.indisunique DESC, ic.relname
                """,
                [oid],
            )
            referenced_by = await db.rows(
                f"""
                SELECT n.nspname || '.' || c.relname AS "table", con.conname AS name,
                       pg_catalog.pg_get_constraintdef(con.oid, true) AS definition
                  FROM pg_catalog.pg_constraint con
                  JOIN pg_catalog.pg_class c ON c.oid = con.conrelid
                  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                 WHERE con.confrelid = $1 AND con.contype = 'f' {parent_only}
                 ORDER BY 1, 2
                 LIMIT 60
                """,
                [oid],
            )
            triggers = await db.rows(
                """
                SELECT t.tgname AS name, pg_catalog.pg_get_triggerdef(t.oid, true) AS definition,
                       t.tgenabled AS state
                  FROM pg_catalog.pg_trigger t
                 WHERE t.tgrelid = $1 AND NOT t.tgisinternal
                 ORDER BY t.tgname
                """,
                [oid],
            )
            permissive = "pol.polpermissive" if v >= 100000 else "true"
            policies = await db.rows(
                f"""
                SELECT pol.polname AS name, pol.polcmd AS command, {permissive} AS permissive,
                       CASE WHEN pol.polroles = '{{0}}'::oid[] THEN ARRAY['public']
                            ELSE ARRAY(SELECT r.rolname::text FROM pg_catalog.pg_roles r
                                        WHERE r.oid = ANY(pol.polroles) ORDER BY 1) END AS roles,
                       pg_catalog.pg_get_expr(pol.polqual, pol.polrelid) AS "using",
                       pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid) AS with_check
                  FROM pg_catalog.pg_policy pol
                 WHERE pol.polrelid = $1
                 ORDER BY pol.polname
                """,
                [oid],
            )
            bound = "pg_catalog.pg_get_expr(c.relpartbound, c.oid)" if v >= 100000 else "NULL::text"
            children = await db.rows(
                f"""
                SELECT n.nspname || '.' || c.relname AS name, {bound} AS bound,
                       CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END AS rows,
                       pg_catalog.pg_size_pretty(pg_catalog.pg_total_relation_size(c.oid)) AS size
                  FROM pg_catalog.pg_inherits i
                  JOIN pg_catalog.pg_class c ON c.oid = i.inhrelid
                  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                 WHERE i.inhparent = $1
                 ORDER BY 1
                 LIMIT 200
                """,
                [oid],
            )
            parents = await db.rows(
                """
                SELECT n.nspname || '.' || c.relname AS name
                  FROM pg_catalog.pg_inherits i
                  JOIN pg_catalog.pg_class c ON c.oid = i.inhparent
                  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                 WHERE i.inhrelid = $1
                 ORDER BY i.inhseqno
                """,
                [oid],
            )
            if relation["kind"] in "vm":
                definition = await db.value(
                    "SELECT pg_catalog.pg_get_viewdef($1::oid, true)", [oid]
                )
            if relation["kind"] == "f":
                foreign = await db.row(
                    """
                    SELECT s.srvname AS server, w.fdwname AS wrapper
                      FROM pg_catalog.pg_foreign_table ft
                      JOIN pg_catalog.pg_foreign_server s ON s.oid = ft.ftserver
                      JOIN pg_catalog.pg_foreign_data_wrapper w ON w.oid = s.srvfdw
                     WHERE ft.ftrelid = $1
                    """,
                    [oid],
                )

    qualified = f"{relation['schema']}.{relation['name']}"
    rule = db.profile.protection_for(relation["schema"], relation["name"])
    protected = set(rule.deny_columns) if rule else set()
    for column in columns:
        if column["name"] in protected:
            column["comment"] = "[protected: cannot be read through pgharbor] " + (
                column["comment"] or ""
            )
    for constraint in constraints:
        constraint["type"] = _CONSTRAINT_TYPES.get(constraint["type"], constraint["type"])
        if constraint["valid"]:
            del constraint["valid"]
    for trigger in triggers:
        trigger["state"] = _TRIGGER_STATES.get(trigger["state"], trigger["state"])
    for policy in policies:
        policy["command"] = _POLICY_COMMANDS.get(policy["command"], policy["command"])

    out: dict[str, Any] = {
        "connection": db.profile.name,
        "table": qualified,
        "kind": relation_kind_label(relation["kind"]),
        "owner": head["owner"],
    }
    if head["comment"]:
        out["comment"] = head["comment"]
    out["estimated_rows"] = head["rows"]
    if relation["kind"] in "rpm":
        out["size"] = {"total": head["total"], "data": head["data"], "indexes": head["indexes"]}
    out["columns"] = grid(columns, ["name", "type", "nullable", "default", "extra", "comment"])
    out["constraints"] = constraints
    if not full:
        return out

    out["indexes"] = indexes
    if referenced_by:
        out["referenced_by"] = referenced_by
    if triggers:
        out["triggers"] = triggers
    if head["row_security"] or policies:
        out["row_security"] = {
            "enabled": head["row_security"],
            "forced_for_owner": head["force_row_security"],
            "policies": policies,
        }
    if head.get("partition_key"):
        out["partition_key"] = head["partition_key"]
    if head.get("partition_bound"):
        out["partition_bound"] = head["partition_bound"]
    if parents:
        out["inherits_from"] = [p["name"] for p in parents]
    if children:
        out["partitions" if head.get("partition_key") else "inherited_by"] = children
    if definition:
        out["definition"] = definition
    if foreign:
        out["foreign_server"] = foreign
    if relation["kind"] in "rpm":
        out["activity"] = {
            "live_rows": head["live_rows"],
            "dead_rows": head["dead_rows"],
            "sequential_scans": head["sequential_scans"],
            "index_scans": head["index_scans"],
            "last_vacuum": head["last_vacuum"],
            "last_analyze": head["last_analyze"],
        }
    storage: dict[str, Any] = {}
    if head["persistence"] != "p":
        storage["persistence"] = _PERSISTENCE.get(head["persistence"], head["persistence"])
    if head["options"]:
        storage["options"] = head["options"]
    if head["tablespace"]:
        storage["tablespace"] = head["tablespace"]
    if head["replica_identity"] != "d":
        storage["replica_identity"] = _REPLICA_IDENTITY.get(head["replica_identity"])
    if storage:
        out["storage"] = storage
    if rule:
        out["pgharbor_rules"] = {
            "columns_never_readable": list(rule.deny_columns),
            "must_filter_by_index": rule.require_index_filter,
            "note": rule.note or None,
        }
    return out
