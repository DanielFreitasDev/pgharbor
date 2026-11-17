"""Looking inside the catalog: objects other than tables, DDL, search, dependencies, schemas."""

import contextlib
import contextvars
import difflib
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import psycopg

from ..app import get_app, tool
from ..errors import PgHarborError
from ..guard import quote_ident, quote_literal, quote_qualified
from ..lexer import Kind, LexError, tokenize
from ..runner import run_query
from ..session import Db
from ._util import (
    USER_SCHEMA_FILTER,
    clamp,
    grid,
    like_pattern,
    page,
    relation_kind_label,
    resolve_relation,
)

__all__ = [
    "pg_compare_schemas",
    "pg_dependencies",
    "pg_describe_object",
    "pg_get_ddl",
    "pg_list_objects",
    "pg_list_partitions",
    "pg_relationships",
    "pg_schema_overview",
    "pg_search",
    "pg_search_source",
]

ListKind = Literal[
    "view",
    "materialized_view",
    "sequence",
    "function",
    "procedure",
    "aggregate",
    "trigger",
    "event_trigger",
    "index",
    "type",
    "enum",
    "domain",
    "extension",
    "foreign_table",
    "foreign_server",
    "foreign_data_wrapper",
    "operator",
    "cast",
    "collation",
    "language",
    "tablespace",
    "rule",
    "policy",
    "statistics",
    "publication",
    "subscription",
    "text_search_config",
    "access_method",
    "database",
]

DescribeKind = Literal[
    "function",
    "procedure",
    "aggregate",
    "sequence",
    "type",
    "index",
    "trigger",
    "extension",
    "schema",
    "table",
    "view",
    "materialized_view",
]

DdlKind = Literal[
    "table",
    "view",
    "materialized_view",
    "function",
    "procedure",
    "sequence",
    "type",
    "index",
    "trigger",
    "schema",
]

# ------------------------------------------------------------------------------------ helpers


def _name_parts(name: str, *, maximum: int = 3) -> list[str]:
    """Split ``a``, ``a.b`` or ``a.b.c`` (parts may be double-quoted) into its parts."""
    try:
        tokens = tokenize(name.strip())
    except LexError as exc:
        raise PgHarborError(f"Invalid object name {name!r}: {exc.reason}") from exc
    parts = [t.value for t in tokens if t.kind in (Kind.IDENT, Kind.QIDENT)]
    dots = sum(1 for t in tokens if t.is_punct("."))
    if (
        not parts
        or len(parts) > maximum
        or dots != len(parts) - 1
        or len(tokens) != 2 * len(parts) - 1
    ):
        raise PgHarborError(
            f"Invalid object name {name!r}. Use name or schema.name; quote parts that contain "
            'upper case or special characters, as in "MySchema"."My Name".'
        )
    return parts


def _ext_of(catalog: str, alias: str) -> str:
    """SQL expression: the extension that owns the object, or NULL."""
    return (
        "(SELECT e.extname FROM pg_catalog.pg_depend d "
        "JOIN pg_catalog.pg_extension e ON e.oid = d.refobjid "
        f"WHERE d.classid = 'pg_catalog.{catalog}'::regclass AND d.objid = {alias}.oid "
        "AND d.refclassid = 'pg_catalog.pg_extension'::regclass AND d.deptype = 'e' LIMIT 1)"
    )


def _not_in_extension(catalog: str, alias: str) -> str:
    return (
        "NOT EXISTS (SELECT 1 FROM pg_catalog.pg_depend d "
        f"WHERE d.classid = 'pg_catalog.{catalog}'::regclass AND d.objid = {alias}.oid "
        "AND d.refclassid = 'pg_catalog.pg_extension'::regclass AND d.deptype = 'e')"
    )


def _function_kind_condition(v: int, kind: str, alias: str = "p") -> str:
    """SQL condition selecting plain functions, procedures or aggregates in pg_proc."""
    if kind == "function":
        return f"{alias}.prokind IN ('f', 'w')" if v >= 110000 else f"NOT {alias}.proisagg"
    if kind == "procedure":
        return f"{alias}.prokind = 'p'" if v >= 110000 else "false"
    if kind == "aggregate":
        return f"{alias}.prokind = 'a'" if v >= 110000 else f"{alias}.proisagg"
    return "true"


def _function_kind_label(v: int, alias: str = "p") -> str:
    if v >= 110000:
        return (
            f"CASE {alias}.prokind WHEN 'a' THEN 'aggregate' WHEN 'p' THEN 'procedure' "
            f"WHEN 'w' THEN 'window function' ELSE 'function' END"
        )
    return (
        f"CASE WHEN {alias}.proisagg THEN 'aggregate' WHEN {alias}.proiswindow "
        f"THEN 'window function' ELSE 'function' END"
    )


_VOLATILITY = (
    "CASE {a}.provolatile WHEN 'i' THEN 'immutable' WHEN 's' THEN 'stable' ELSE 'volatile' END"
)
_OWNED_BY = """
    (SELECT nn.nspname || '.' || tc.relname || '.' || a.attname
       FROM pg_catalog.pg_depend d
       JOIN pg_catalog.pg_class tc ON tc.oid = d.refobjid
       JOIN pg_catalog.pg_namespace nn ON nn.oid = tc.relnamespace
       JOIN pg_catalog.pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
      WHERE d.classid = 'pg_catalog.pg_class'::regclass AND d.objid = c.oid
        AND d.refclassid = 'pg_catalog.pg_class'::regclass
                       AND d.deptype IN ('a', 'i')
        AND d.refobjsubid > 0
      LIMIT 1)
"""
_SYSTEM_SCHEMAS = "('pg_catalog', 'information_schema')"


async def _pretty(db: Db, sizes: list[int]) -> dict[int, str]:
    if not sizes:
        return {}
    rows = await db.rows(
        "SELECT b, pg_catalog.pg_size_pretty(b) AS p FROM unnest($1::bigint[]) AS u(b)", [sizes]
    )
    return {r["b"]: r["p"] for r in rows}


def _tidy(row: dict[str, Any]) -> dict[str, Any]:
    """Drop empty values so that an answer only holds what is there."""
    return {k: v for k, v in row.items() if v is not None and v != [] and v != ""}


def _near_names(rows: list[dict[str, Any]], key: str = "name") -> str:
    return (" Similar names: " + ", ".join(str(r[key]) for r in rows) + ".") if rows else ""


# ---------------------------------------------------------------------------- pg_list_objects


@dataclass(frozen=True, slots=True)
class _ListSpec:
    columns: tuple[str, ...]
    sql: Callable[[int, bool], str]
    scoped: bool = True  # has a schema the `schema` filter applies to
    hide_system: bool = True  # hide pg_catalog and information_schema unless asked for
    order: str = "q.schema, q.name"
    minimum: int = 0
    unavailable: str = ""
    optional: tuple[str, ...] = ()  # columns dropped from the answer when always empty


def _sql_view(v: int, _pattern: bool) -> str:
    return f"""
        SELECT n.nspname AS schema, c.relname AS name,
               pg_catalog.pg_get_userbyid(c.relowner) AS owner,
               (SELECT count(*) FROM pg_catalog.pg_attribute a
                 WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped) AS columns,
               left(pg_catalog.obj_description(c.oid, 'pg_class'), 120) AS comment,
               {_ext_of("pg_class", "c")} AS extension
          FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relkind = 'v'"""


def _sql_matview(v: int, _pattern: bool) -> str:
    return """
        SELECT n.nspname AS schema, c.relname AS name,
               pg_catalog.pg_get_userbyid(c.relowner) AS owner,
               c.relispopulated AS populated,
               pg_catalog.pg_size_pretty(pg_catalog.pg_total_relation_size(c.oid)) AS size,
               left(pg_catalog.obj_description(c.oid, 'pg_class'), 120) AS comment
          FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relkind = 'm'"""


def _sql_sequence(v: int, _pattern: bool) -> str:
    if v >= 100000:
        return f"""
            SELECT n.nspname AS schema, c.relname AS name,
                   pg_catalog.format_type(s.seqtypid, NULL) AS data_type,
                   ps.last_value AS last_value, s.seqincrement AS increment,
                   {_OWNED_BY} AS owned_by
              FROM pg_catalog.pg_class c
              JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
              JOIN pg_catalog.pg_sequence s ON s.seqrelid = c.oid
              LEFT JOIN pg_catalog.pg_sequences ps
                     ON ps.schemaname = n.nspname AND ps.sequencename = c.relname
             WHERE c.relkind = 'S'"""
    return f"""
        SELECT n.nspname AS schema, c.relname AS name, 'bigint'::text AS data_type,
               NULL::bigint AS last_value, NULL::bigint AS increment,
               {_OWNED_BY} AS owned_by
          FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relkind = 'S'"""


def _sql_function(kind: str) -> Callable[[int, bool], str]:
    def build(v: int, _pattern: bool) -> str:
        columns = (
            "pg_catalog.pg_get_function_result(p.oid) AS returns,"
            if kind != "procedure"
            else "NULL::text AS returns,"
        )
        return f"""
            SELECT n.nspname AS schema, p.proname AS name,
                   left(pg_catalog.pg_get_function_arguments(p.oid), 200) AS arguments,
                   {columns} l.lanname AS language,
                   {_VOLATILITY.format(a="p")} AS volatility,
                   p.prosecdef AS security_definer,
                   {_ext_of("pg_proc", "p")} AS extension
              FROM pg_catalog.pg_proc p
              JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
              JOIN pg_catalog.pg_language l ON l.oid = p.prolang
             WHERE {_function_kind_condition(v, kind)}"""

    return build


def _sql_aggregate(v: int, _pattern: bool) -> str:
    return f"""
        SELECT n.nspname AS schema, p.proname AS name,
               left(pg_catalog.pg_get_function_arguments(p.oid), 200) AS arguments,
               pg_catalog.pg_get_function_result(p.oid) AS returns,
               pg_catalog.format_type(a.aggtranstype, NULL) AS state_type,
               CASE a.aggkind WHEN 'o' THEN 'ordered-set' WHEN 'h' THEN 'hypothetical-set'
                    ELSE 'normal' END AS aggregate_kind,
               {_ext_of("pg_proc", "p")} AS extension
          FROM pg_catalog.pg_proc p
          JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
          JOIN pg_catalog.pg_aggregate a ON a.aggfnoid = p.oid
         WHERE {_function_kind_condition(v, "aggregate")}"""


def _sql_trigger(v: int, _pattern: bool) -> str:
    cloned = "AND t.tgparentid = 0" if v >= 130000 else ""
    return f"""
        SELECT n.nspname AS schema, c.relname AS "table", t.tgname AS name,
               CASE WHEN t.tgtype & 64 > 0 THEN 'INSTEAD OF' WHEN t.tgtype & 2 > 0 THEN 'BEFORE'
                    ELSE 'AFTER' END AS timing,
               array_to_string(ARRAY[CASE WHEN t.tgtype & 4 > 0 THEN 'INSERT' END,
                                     CASE WHEN t.tgtype & 8 > 0 THEN 'DELETE' END,
                                     CASE WHEN t.tgtype & 16 > 0 THEN 'UPDATE' END,
                                     CASE WHEN t.tgtype & 32 > 0 THEN 'TRUNCATE' END],
                               ' OR ') AS events,
               CASE WHEN t.tgtype & 1 > 0 THEN 'row' ELSE 'statement' END AS level,
               CASE t.tgenabled WHEN 'D' THEN 'disabled' WHEN 'R' THEN 'replica'
                    WHEN 'A' THEN 'always' ELSE 'enabled' END AS state,
               pn.nspname || '.' || p.proname AS function
          FROM pg_catalog.pg_trigger t
          JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
          JOIN pg_catalog.pg_proc p ON p.oid = t.tgfoid
          JOIN pg_catalog.pg_namespace pn ON pn.oid = p.pronamespace
         WHERE NOT t.tgisinternal {cloned}"""


def _sql_event_trigger(v: int, _pattern: bool) -> str:
    return """
        SELECT NULL::text AS schema, e.evtname AS name, e.evtevent AS event,
               CASE e.evtenabled WHEN 'D' THEN 'disabled' WHEN 'R' THEN 'replica'
                    WHEN 'A' THEN 'always' ELSE 'enabled' END AS state,
               e.evtfoid::regproc::text AS function, e.evttags AS tags,
               pg_catalog.pg_get_userbyid(e.evtowner) AS owner
          FROM pg_catalog.pg_event_trigger e WHERE true"""


def _sql_index(v: int, _pattern: bool) -> str:
    return """
        SELECT n.nspname AS schema, t.relname AS "table", c.relname AS name,
               am.amname AS method, i.indisunique AS "unique", i.indisvalid AS valid,
               pg_catalog.pg_size_pretty(pg_catalog.pg_relation_size(c.oid)) AS size,
               array_to_string(ARRAY(
                   SELECT pg_catalog.pg_get_indexdef(i.indexrelid, k, true)
                     FROM generate_series(1, i.indnatts::int) k ORDER BY k), ', ') AS columns,
               i.indpred IS NOT NULL AS partial
          FROM pg_catalog.pg_index i
          JOIN pg_catalog.pg_class c ON c.oid = i.indexrelid
          JOIN pg_catalog.pg_class t ON t.oid = i.indrelid
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
          JOIN pg_catalog.pg_am am ON am.oid = c.relam
         WHERE true"""


def _sql_type(v: int, _pattern: bool) -> str:
    return f"""
        SELECT n.nspname AS schema, t.typname AS name,
               CASE t.typtype WHEN 'b' THEN 'base' WHEN 'c' THEN 'composite' WHEN 'd' THEN 'domain'
                    WHEN 'e' THEN 'enum' WHEN 'p' THEN 'pseudo' WHEN 'r' THEN 'range'
                    WHEN 'm' THEN 'multirange' ELSE t.typtype::text END AS kind,
               pg_catalog.pg_get_userbyid(t.typowner) AS owner,
               left(pg_catalog.obj_description(t.oid, 'pg_type'), 120) AS comment,
               {_ext_of("pg_type", "t")} AS extension
          FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
         WHERE (t.typrelid = 0 OR
                (SELECT c.relkind FROM pg_catalog.pg_class c WHERE c.oid = t.typrelid) = 'c')
           AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_type e
                            WHERE e.oid = t.typelem AND e.typarray = t.oid)"""


def _sql_enum(v: int, _pattern: bool) -> str:
    return f"""
        SELECT n.nspname AS schema, t.typname AS name,
               (ARRAY(SELECT e.enumlabel::text FROM pg_catalog.pg_enum e
                       WHERE e.enumtypid = t.oid ORDER BY e.enumsortorder))[1:50] AS labels,
               left(pg_catalog.obj_description(t.oid, 'pg_type'), 120) AS comment,
               {_ext_of("pg_type", "t")} AS extension
          FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
         WHERE t.typtype = 'e'"""


def _sql_domain(v: int, _pattern: bool) -> str:
    return f"""
        SELECT n.nspname AS schema, t.typname AS name,
               pg_catalog.format_type(t.typbasetype, t.typtypmod) AS base_type,
               t.typnotnull AS not_null,
               t.typdefault AS "default",
               ARRAY(SELECT pg_catalog.pg_get_constraintdef(c.oid) FROM pg_catalog.pg_constraint c
                      WHERE c.contypid = t.oid AND c.contype = 'c' ORDER BY c.conname) AS checks,
               {_ext_of("pg_type", "t")} AS extension
          FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
         WHERE t.typtype = 'd'"""


def _sql_extension(v: int, _pattern: bool) -> str:
    return """
        SELECT n.nspname AS schema, e.extname AS name, e.extversion AS version,
               pg_catalog.pg_get_userbyid(e.extowner) AS owner,
               e.extrelocatable AS relocatable,
               left(pg_catalog.obj_description(e.oid, 'pg_extension'), 120) AS comment
          FROM pg_catalog.pg_extension e JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace
         WHERE true"""


def _sql_foreign_table(v: int, _pattern: bool) -> str:
    return """
        SELECT n.nspname AS schema, c.relname AS name, s.srvname AS server, w.fdwname AS wrapper,
               left(pg_catalog.obj_description(c.oid, 'pg_class'), 120) AS comment
          FROM pg_catalog.pg_foreign_table ft
          JOIN pg_catalog.pg_class c ON c.oid = ft.ftrelid
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
          JOIN pg_catalog.pg_foreign_server s ON s.oid = ft.ftserver
          JOIN pg_catalog.pg_foreign_data_wrapper w ON w.oid = s.srvfdw
         WHERE true"""


def _sql_foreign_server(v: int, _pattern: bool) -> str:
    # Option values can hold credentials: names and values are shown only for options whose
    # name does not look like one.
    return """
        SELECT NULL::text AS schema, s.srvname AS name, w.fdwname AS wrapper,
               s.srvtype AS type, s.srvversion AS version,
               pg_catalog.pg_get_userbyid(s.srvowner) AS owner,
               array_to_string(ARRAY(SELECT o FROM unnest(s.srvoptions) o
                                      WHERE o !~* '^[^=]*(pass|secret|key|token|cred|cert)'),
                               ', ') AS options
          FROM pg_catalog.pg_foreign_server s
          JOIN pg_catalog.pg_foreign_data_wrapper w ON w.oid = s.srvfdw
         WHERE true"""


def _sql_fdw(v: int, _pattern: bool) -> str:
    return """
        SELECT NULL::text AS schema, w.fdwname AS name,
               pg_catalog.pg_get_userbyid(w.fdwowner) AS owner,
               NULLIF(w.fdwhandler::regproc::text, '-') AS handler,
               NULLIF(w.fdwvalidator::regproc::text, '-') AS validator
          FROM pg_catalog.pg_foreign_data_wrapper w WHERE true"""


def _sql_operator(v: int, _pattern: bool) -> str:
    return f"""
        SELECT n.nspname AS schema, o.oprname AS name,
               CASE WHEN o.oprleft = 0 THEN NULL ELSE pg_catalog.format_type(o.oprleft, NULL) END
                   AS "left",
               CASE WHEN o.oprright = 0 THEN NULL ELSE pg_catalog.format_type(o.oprright, NULL) END
                   AS "right",
               pg_catalog.format_type(o.oprresult, NULL) AS result,
               o.oprcode::regproc::text AS function,
               {_ext_of("pg_operator", "o")} AS extension
          FROM pg_catalog.pg_operator o JOIN pg_catalog.pg_namespace n ON n.oid = o.oprnamespace
         WHERE true"""


def _sql_cast(v: int, pattern: bool) -> str:
    hide = "" if pattern else "AND NOT (sn.nspname = 'pg_catalog' AND tn.nspname = 'pg_catalog')"
    return f"""
        SELECT NULL::text AS schema,
               pg_catalog.format_type(c.castsource, NULL) || ' -> '
                   || pg_catalog.format_type(c.casttarget, NULL) AS name,
               pg_catalog.format_type(c.castsource, NULL) AS source,
               pg_catalog.format_type(c.casttarget, NULL) AS target,
               CASE c.castmethod WHEN 'f' THEN c.castfunc::regproc::text WHEN 'i' THEN 'I/O'
                    ELSE 'binary coercible' END AS function,
               CASE c.castcontext WHEN 'i' THEN 'implicit' WHEN 'a' THEN 'assignment'
                    ELSE 'explicit' END AS context
          FROM pg_catalog.pg_cast c
          JOIN pg_catalog.pg_type st ON st.oid = c.castsource
          JOIN pg_catalog.pg_namespace sn ON sn.oid = st.typnamespace
          JOIN pg_catalog.pg_type tt ON tt.oid = c.casttarget
          JOIN pg_catalog.pg_namespace tn ON tn.oid = tt.typnamespace
         WHERE true {hide}"""


def _sql_collation(v: int, _pattern: bool) -> str:
    provider = (
        "CASE c.collprovider WHEN 'i' THEN 'icu' WHEN 'b' THEN 'builtin' WHEN 'c' THEN 'libc' "
        "ELSE 'default' END"
        if v >= 100000
        else "'libc'::text"
    )
    if v >= 170000:
        locale = "coalesce(c.colllocale, c.collcollate)"
    elif v >= 150000:
        locale = "coalesce(c.colliculocale, c.collcollate)"
    else:
        locale = "c.collcollate::text"
    deterministic = "c.collisdeterministic" if v >= 120000 else "true"
    return f"""
        SELECT n.nspname AS schema, c.collname AS name, {provider} AS provider,
               {locale} AS locale, {deterministic} AS deterministic,
               {_ext_of("pg_collation", "c")} AS extension
          FROM pg_catalog.pg_collation c JOIN pg_catalog.pg_namespace n ON n.oid = c.collnamespace
         WHERE true"""


def _sql_language(v: int, _pattern: bool) -> str:
    return """
        SELECT NULL::text AS schema, l.lanname AS name, l.lanpltrusted AS trusted,
               pg_catalog.pg_get_userbyid(l.lanowner) AS owner,
               NULLIF(l.lanplcallfoid::regproc::text, '-') AS handler,
               left(pg_catalog.obj_description(l.oid, 'pg_language'), 120) AS comment
          FROM pg_catalog.pg_language l WHERE true"""


def _sql_tablespace(v: int, _pattern: bool) -> str:
    # No size here: measuring a tablespace walks every file in it, which takes longer
    # than the statement timeout on a large server. pg_sizes(scope="tablespaces") does it.
    return """
        SELECT NULL::text AS schema, t.spcname AS name,
               pg_catalog.pg_get_userbyid(t.spcowner) AS owner,
               NULLIF(pg_catalog.pg_tablespace_location(t.oid), '') AS location,
               t.spcoptions AS options
          FROM pg_catalog.pg_tablespace t WHERE true"""


def _sql_rule(v: int, _pattern: bool) -> str:
    return """
        SELECT n.nspname AS schema, c.relname AS "table", w.rulename AS name,
               CASE w.ev_type WHEN '1' THEN 'SELECT' WHEN '2' THEN 'UPDATE' WHEN '3' THEN 'INSERT'
                    WHEN '4' THEN 'DELETE' END AS event,
               w.is_instead AS instead,
               CASE w.ev_enabled WHEN 'D' THEN 'disabled' WHEN 'R' THEN 'replica'
                    WHEN 'A' THEN 'always' ELSE 'enabled' END AS state
          FROM pg_catalog.pg_rewrite w
          JOIN pg_catalog.pg_class c ON c.oid = w.ev_class
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE NOT (w.rulename = '_RETURN' AND w.ev_type = '1')"""


def _sql_policy(v: int, _pattern: bool) -> str:
    permissive = "pol.polpermissive" if v >= 100000 else "true"
    return f"""
        SELECT n.nspname AS schema, c.relname AS "table", pol.polname AS name,
               CASE pol.polcmd WHEN 'r' THEN 'SELECT' WHEN 'a' THEN 'INSERT'
                    WHEN 'w' THEN 'UPDATE' WHEN 'd' THEN 'DELETE' ELSE 'ALL' END AS command,
               {permissive} AS permissive,
               CASE WHEN pol.polroles = '{{0}}'::oid[] THEN ARRAY['public']
                    ELSE ARRAY(SELECT r.rolname::text FROM pg_catalog.pg_roles r
                                WHERE r.oid = ANY(pol.polroles) ORDER BY 1) END AS roles,
               left(pg_catalog.pg_get_expr(pol.polqual, pol.polrelid), 200) AS "using",
               left(pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid), 200) AS with_check
          FROM pg_catalog.pg_policy pol
          JOIN pg_catalog.pg_class c ON c.oid = pol.polrelid
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE true"""


def _sql_statistics(v: int, _pattern: bool) -> str:
    return """
        SELECT n.nspname AS schema, c.relname AS "table", s.stxname AS name,
               ARRAY(SELECT CASE k::text WHEN 'd' THEN 'ndistinct' WHEN 'f' THEN 'dependencies'
                                         WHEN 'm' THEN 'mcv' WHEN 'e' THEN 'expressions'
                                         ELSE k::text END
                       FROM unnest(s.stxkind) k) AS kinds,
               ARRAY(SELECT a.attname::text FROM unnest(s.stxkeys::int2[]) WITH ORDINALITY kk(k, o)
                       JOIN pg_catalog.pg_attribute a ON a.attrelid = s.stxrelid AND a.attnum = kk.k
                      ORDER BY kk.o) AS columns,
               pg_catalog.pg_get_userbyid(s.stxowner) AS owner
          FROM pg_catalog.pg_statistic_ext s
          JOIN pg_catalog.pg_class c ON c.oid = s.stxrelid
          JOIN pg_catalog.pg_namespace n ON n.oid = s.stxnamespace
         WHERE true"""


def _sql_publication(v: int, _pattern: bool) -> str:
    truncate = "CASE WHEN p.pubtruncate THEN 'truncate' END" if v >= 110000 else "NULL"
    via_root = "p.pubviaroot" if v >= 130000 else "false"
    schemas = (
        "(SELECT count(*) FROM pg_catalog.pg_publication_namespace pn WHERE pn.pnpubid = p.oid)"
        if v >= 150000
        else "0"
    )
    return f"""
        SELECT NULL::text AS schema, p.pubname AS name,
               pg_catalog.pg_get_userbyid(p.pubowner) AS owner, p.puballtables AS all_tables,
               array_to_string(ARRAY[CASE WHEN p.pubinsert THEN 'insert' END,
                                     CASE WHEN p.pubupdate THEN 'update' END,
                                     CASE WHEN p.pubdelete THEN 'delete' END,
                                     {truncate}], ',') AS operations,
               (SELECT count(*) FROM pg_catalog.pg_publication_rel r WHERE r.prpubid = p.oid)
                   AS tables,
               {schemas} AS schemas, {via_root} AS via_root
          FROM pg_catalog.pg_publication p WHERE true"""


def _sql_subscription(v: int, _pattern: bool) -> str:
    # subconninfo (the connection string, with its password) is never selected.
    return """
        SELECT NULL::text AS schema, s.subname AS name,
               pg_catalog.pg_get_userbyid(s.subowner) AS owner, s.subenabled AS enabled,
               s.subpublications AS publications, s.subslotname AS slot,
               s.subsynccommit AS synchronous_commit
          FROM pg_catalog.pg_subscription s
         WHERE s.subdbid = (SELECT d.oid FROM pg_catalog.pg_database d
                             WHERE d.datname = pg_catalog.current_database())"""


def _sql_text_search_config(v: int, _pattern: bool) -> str:
    return f"""
        SELECT n.nspname AS schema, c.cfgname AS name, p.prsname AS parser,
               pg_catalog.pg_get_userbyid(c.cfgowner) AS owner,
               left(pg_catalog.obj_description(c.oid, 'pg_ts_config'), 120) AS comment,
               {_ext_of("pg_ts_config", "c")} AS extension
          FROM pg_catalog.pg_ts_config c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.cfgnamespace
          JOIN pg_catalog.pg_ts_parser p ON p.oid = c.cfgparser
         WHERE true"""


def _sql_access_method(v: int, _pattern: bool) -> str:
    return """
        SELECT NULL::text AS schema, a.amname AS name,
               CASE a.amtype WHEN 'i' THEN 'index' WHEN 't' THEN 'table' ELSE a.amtype::text
               END AS type,
               a.amhandler::regproc::text AS handler
          FROM pg_catalog.pg_am a WHERE true"""


def _sql_database(v: int, _pattern: bool) -> str:
    return """
        SELECT NULL::text AS schema, d.datname AS name,
               pg_catalog.pg_get_userbyid(d.datdba) AS owner,
               pg_catalog.pg_encoding_to_char(d.encoding) AS encoding,
               d.datcollate::text AS collation,
               CASE WHEN pg_catalog.has_database_privilege(d.oid, 'CONNECT')
                    THEN pg_catalog.pg_size_pretty(pg_catalog.pg_database_size(d.oid)) END AS size,
               d.datistemplate AS is_template, d.datallowconn AS allow_connections,
               d.datconnlimit AS connection_limit
          FROM pg_catalog.pg_database d WHERE true"""


_LIST_KINDS: dict[str, _ListSpec] = {
    "view": _ListSpec(
        ("schema", "name", "owner", "columns", "comment", "extension"),
        _sql_view,
        order="(q.extension IS NOT NULL), q.schema, q.name",
        optional=("comment", "extension"),
    ),
    "materialized_view": _ListSpec(
        ("schema", "name", "owner", "populated", "size", "comment"),
        _sql_matview,
        optional=("comment",),
    ),
    "sequence": _ListSpec(
        ("schema", "name", "data_type", "last_value", "increment", "owned_by"), _sql_sequence
    ),
    "function": _ListSpec(
        (
            "schema",
            "name",
            "arguments",
            "returns",
            "language",
            "volatility",
            "security_definer",
            "extension",
        ),
        _sql_function("function"),
        order="(q.extension IS NOT NULL), q.schema, q.name, q.arguments",
        optional=("extension",),
    ),
    "procedure": _ListSpec(
        ("schema", "name", "arguments", "language", "security_definer", "extension"),
        _sql_function("procedure"),
        order="(q.extension IS NOT NULL), q.schema, q.name, q.arguments",
        minimum=110000,
        unavailable="Procedures (CREATE PROCEDURE) need PostgreSQL 11 or newer",
        optional=("extension",),
    ),
    "aggregate": _ListSpec(
        ("schema", "name", "arguments", "returns", "state_type", "aggregate_kind", "extension"),
        _sql_aggregate,
        order="(q.extension IS NOT NULL), q.schema, q.name, q.arguments",
        optional=("extension",),
    ),
    "trigger": _ListSpec(
        ("schema", "table", "name", "timing", "events", "level", "state", "function"),
        _sql_trigger,
        order='q.schema, q."table", q.name',
    ),
    "event_trigger": _ListSpec(
        ("name", "event", "state", "function", "tags", "owner"),
        _sql_event_trigger,
        scoped=False,
        order="q.name",
        optional=("tags",),
    ),
    "index": _ListSpec(
        ("schema", "table", "name", "method", "unique", "valid", "size", "columns", "partial"),
        _sql_index,
        order='q.schema, q."table", q.name',
    ),
    "type": _ListSpec(
        ("schema", "name", "kind", "owner", "comment", "extension"),
        _sql_type,
        order="(q.extension IS NOT NULL), q.schema, q.name",
        optional=("comment", "extension"),
    ),
    "enum": _ListSpec(
        ("schema", "name", "labels", "comment", "extension"),
        _sql_enum,
        order="(q.extension IS NOT NULL), q.schema, q.name",
        optional=("comment", "extension"),
    ),
    "domain": _ListSpec(
        ("schema", "name", "base_type", "not_null", "default", "checks", "extension"),
        _sql_domain,
        order="(q.extension IS NOT NULL), q.schema, q.name",
        optional=("default", "checks", "extension"),
    ),
    "extension": _ListSpec(
        ("schema", "name", "version", "owner", "relocatable", "comment"),
        _sql_extension,
        hide_system=False,
        order="q.name",
        optional=("comment",),
    ),
    "foreign_table": _ListSpec(
        ("schema", "name", "server", "wrapper", "comment"),
        _sql_foreign_table,
        optional=("comment",),
    ),
    "foreign_server": _ListSpec(
        ("name", "wrapper", "type", "version", "owner", "options"),
        _sql_foreign_server,
        scoped=False,
        order="q.name",
        optional=("type", "version", "options"),
    ),
    "foreign_data_wrapper": _ListSpec(
        ("name", "owner", "handler", "validator"),
        _sql_fdw,
        scoped=False,
        order="q.name",
        optional=("handler", "validator"),
    ),
    "operator": _ListSpec(
        ("schema", "name", "left", "right", "result", "function", "extension"),
        _sql_operator,
        order='(q.extension IS NOT NULL), q.schema, q.name, q."left", q."right"',
        optional=("extension",),
    ),
    "cast": _ListSpec(
        ("source", "target", "function", "context"),
        _sql_cast,
        scoped=False,
        order="q.source, q.target",
    ),
    "collation": _ListSpec(
        ("schema", "name", "provider", "locale", "deterministic", "extension"),
        _sql_collation,
        optional=("extension",),
    ),
    "language": _ListSpec(
        ("name", "trusted", "owner", "handler", "comment"),
        _sql_language,
        scoped=False,
        order="q.name",
        optional=("handler", "comment"),
    ),
    "tablespace": _ListSpec(
        ("name", "owner", "location", "options"),
        _sql_tablespace,
        scoped=False,
        order="q.name",
        optional=("location", "options"),
    ),
    "rule": _ListSpec(
        ("schema", "table", "name", "event", "instead", "state"),
        _sql_rule,
        order='q.schema, q."table", q.name',
    ),
    "policy": _ListSpec(
        ("schema", "table", "name", "command", "permissive", "roles", "using", "with_check"),
        _sql_policy,
        order='q.schema, q."table", q.name',
        optional=("using", "with_check"),
    ),
    "statistics": _ListSpec(
        ("schema", "table", "name", "kinds", "columns", "owner"),
        _sql_statistics,
        order='q.schema, q."table", q.name',
        minimum=100000,
        unavailable="Extended statistics (CREATE STATISTICS) need PostgreSQL 10 or newer",
    ),
    "publication": _ListSpec(
        ("name", "owner", "all_tables", "operations", "tables", "schemas", "via_root"),
        _sql_publication,
        scoped=False,
        order="q.name",
        minimum=100000,
        unavailable="Logical replication publications need PostgreSQL 10 or newer",
        optional=("schemas", "via_root"),
    ),
    "subscription": _ListSpec(
        ("name", "owner", "enabled", "publications", "slot", "synchronous_commit"),
        _sql_subscription,
        scoped=False,
        order="q.name",
        minimum=100000,
        unavailable="Logical replication subscriptions need PostgreSQL 10 or newer",
    ),
    "text_search_config": _ListSpec(
        ("schema", "name", "parser", "owner", "comment", "extension"),
        _sql_text_search_config,
        optional=("comment", "extension"),
    ),
    "access_method": _ListSpec(
        ("name", "type", "handler"), _sql_access_method, scoped=False, order="q.type, q.name"
    ),
    "database": _ListSpec(
        (
            "name",
            "owner",
            "encoding",
            "collation",
            "size",
            "is_template",
            "allow_connections",
            "connection_limit",
        ),
        _sql_database,
        scoped=False,
        order="q.name",
    ),
}


@tool(group="schema", title="List database objects of one kind")
async def pg_list_objects(
    kind: ListKind,
    connection: str | None = None,
    schema: str | None = None,
    pattern: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """List objects of ONE kind: views, materialized views, sequences, functions,
    procedures, aggregates, triggers, indexes, types, enums, domains, extensions, foreign
    tables and servers, operators, casts, collations, languages, tablespaces, rules,
    policies, statistics, publications, subscriptions, text search configs, access methods
    or databases.

    Each kind returns its own compact columns (a function: arguments, returns, language,
    volatility; a sequence: type, last value, owning column; an index: table, method,
    size). Filter by `schema` and by a name `pattern` (`*` and `?` wildcards; plain text
    matches any name containing it). Kinds the server version does not have return an
    empty list with a note. Tables are listed by pg_list_tables.
    """
    spec = _LIST_KINDS[kind]
    limit = clamp(limit, 100, 1000)
    offset = max(0, int(offset))
    async with get_app().read(connection) as db:
        if db.v < spec.minimum:
            return {
                "connection": db.profile.name,
                "kind": kind,
                "rows": [],
                "notes": [f"{spec.unavailable}; this server is {db.facts.label}."],
            }
        inner = spec.sql(db.v, bool(pattern))
        schema_filter = (
            "($1::text IS NULL OR q.schema = $1)" if spec.scoped else "($1::text IS NULL OR true)"
        )
        system = (
            f"AND ($1::text IS NOT NULL OR (q.schema NOT IN {_SYSTEM_SCHEMAS} "
            "AND q.schema !~ '^pg_(toast|temp)'))"
            if spec.scoped and spec.hide_system
            else ""
        )
        rows = await db.rows(
            f"""
            SELECT q.* FROM ({inner}) q
             WHERE {schema_filter} AND ($2::text IS NULL OR q.name ILIKE $2) {system}
             ORDER BY {spec.order}
             LIMIT $3 OFFSET $4
            """,
            [schema, like_pattern(pattern), limit + 1, offset],
        )
    rows, has_more = page(rows, limit)
    columns = [c for c in spec.columns if spec.scoped or c != "schema"]
    for name in spec.optional:
        if name in columns and all(row.get(name) in (None, [], "") for row in rows):
            columns.remove(name)
    out: dict[str, Any] = {"connection": db.profile.name, "kind": kind}
    out.update(grid(rows, columns))
    out["has_more"] = has_more
    if has_more:
        out["next_offset"] = offset + limit
    notes: list[str] = []
    if schema and not spec.scoped:
        notes.append(f"{kind} objects do not belong to a schema; the schema filter was ignored.")
    if kind == "sequence" and db.v < 100000:
        notes.append(
            "last_value and increment are not listed on PostgreSQL 9.6; "
            "pg_describe_object(kind='sequence') reads them for one sequence."
        )
    if kind == "subscription":
        notes.append("Connection strings are never shown.")
    if not rows and (schema or pattern):
        notes.append(
            "Nothing matched. pg_list_schemas shows the schemas; pg_search looks for a name "
            "everywhere."
        )
    if notes:
        out["notes"] = notes
    return out


# -------------------------------------------------------------------------- pg_describe_object

_USE_DESCRIBE_TABLE = (
    "Tables, views and materialized views are described by pg_describe_table(table=...), "
    "which also shows columns, keys, indexes and triggers."
)


async def _find_functions(db: Db, name: str, kind: str, *, what: str) -> list[dict[str, Any]]:
    """All overloads of a function, procedure or aggregate. ``name`` may carry a signature."""
    condition = _function_kind_condition(db.v, kind)
    base = (
        "SELECT p.oid::int8 AS oid, n.nspname AS schema, p.proname AS name, "
        "pg_catalog.pg_get_function_identity_arguments(p.oid) AS identity "
        "FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace "
    )
    if "(" in name:
        try:
            found = await db.rows(
                base + f"WHERE p.oid = pg_catalog.to_regprocedure($1)::oid AND {condition}", [name]
            )
        except psycopg.errors.Error as exc:
            raise PgHarborError(
                f"Could not read the signature in {name!r}: {exc.diag.message_primary}. "
                "Write it as schema.name(argument types), for example shop.order_total(bigint)."
            ) from None
        if found:
            return found
        raise PgHarborError(
            f"No {what} matches the signature {name!r}. Pass the name alone to see every overload."
        )
    parts = _name_parts(name, maximum=2)
    schema, function = (parts[0], parts[1]) if len(parts) == 2 else (None, parts[0])
    found = await db.rows(
        base
        + f"""WHERE p.proname = $1 AND ($2::text IS NULL OR n.nspname = $2) AND {condition}
              ORDER BY (n.nspname = ANY(pg_catalog.current_schemas(true))) DESC,
                       n.nspname, p.oid""",
        [function, schema],
    )
    if schema is None:
        user = [r for r in found if r["schema"] not in ("pg_catalog", "information_schema")]
        found = user or found
        if len({r["schema"] for r in found}) > 1:
            options = ", ".join(sorted({f"{r['schema']}.{r['name']}" for r in found}))
            raise PgHarborError(
                f"The name {function!r} exists in several schemas: {options}. Pass it as "
                "schema.name."
            )
    if not found:
        near = await db.rows(
            f"""SELECT n.nspname || '.' || p.proname AS name
                  FROM pg_catalog.pg_proc p
                  JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
                 WHERE p.proname ILIKE $1 AND {condition} AND ({USER_SCHEMA_FILTER})
                 ORDER BY length(p.proname), 1 LIMIT 8""",
            [f"%{function}%"],
        )
        raise PgHarborError(
            f"No {what} named {name!r} on this connection.{_near_names(near)} "
            "pg_search looks for a name everywhere."
        )
    return found


async def _find_type(db: Db, name: str) -> dict[str, Any]:
    parts = _name_parts(name, maximum=2)
    schema, type_name = (parts[0], parts[1]) if len(parts) == 2 else (None, parts[0])
    found = await db.rows(
        """
        SELECT t.oid::int8 AS oid, n.nspname AS schema, t.typname AS name
          FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
         WHERE t.typname = $1 AND ($2::text IS NULL OR n.nspname = $2)
         ORDER BY (n.nspname = ANY(pg_catalog.current_schemas(true))) DESC, n.nspname
        """,
        [type_name, schema],
    )
    if schema is None:
        user = [r for r in found if r["schema"] not in ("pg_catalog", "information_schema")]
        if len(user) > 1:
            options = ", ".join(f"{r['schema']}.{r['name']}" for r in user)
            raise PgHarborError(
                f"The type name {type_name!r} exists in several schemas: {options}. "
                "Pass it as schema.name."
            )
        found = user or found
    if not found:
        alias = await db.row(
            """SELECT t.oid::int8 AS oid, n.nspname AS schema, t.typname AS name
                 FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
                WHERE t.oid = pg_catalog.to_regtype($1)::oid""",
            [name],
        )
        if alias:
            return alias
        near = await db.rows(
            """SELECT n.nspname || '.' || t.typname AS name
                 FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
                WHERE t.typname ILIKE $1 AND (t.typrelid = 0) AND t.typelem = 0
                  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                ORDER BY length(t.typname), 1 LIMIT 8""",
            [f"%{type_name}%"],
        )
        raise PgHarborError(
            f"No type named {name!r} on this connection.{_near_names(near)} "
            "pg_list_objects(kind='type') lists types."
        )
    return found[0]


async def _find_trigger(db: Db, name: str) -> dict[str, Any]:
    parts = _name_parts(name, maximum=3)
    trigger = parts[-1]
    table = parts[-2] if len(parts) >= 2 else None
    schema = parts[0] if len(parts) == 3 else None
    found = await db.rows(
        """
        SELECT t.oid::int8 AS oid, n.nspname AS schema, c.relname AS "table", t.tgname AS name
          FROM pg_catalog.pg_trigger t
          JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE t.tgname = $1 AND NOT t.tgisinternal
           AND ($2::text IS NULL OR c.relname = $2) AND ($3::text IS NULL OR n.nspname = $3)
         ORDER BY n.nspname, c.relname
        """,
        [trigger, table, schema],
    )
    if len(found) > 1:
        options = ", ".join(f"{r['schema']}.{r['table']}.{r['name']}" for r in found)
        raise PgHarborError(
            f"The trigger name {trigger!r} exists on several tables: {options}. "
            "Pass it as schema.table.trigger."
        )
    if not found:
        near = await db.rows(
            """SELECT n.nspname || '.' || c.relname || '.' || t.tgname AS name
                 FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
                 JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE t.tgname ILIKE $1 AND NOT t.tgisinternal ORDER BY 1 LIMIT 8""",
            [f"%{trigger}%"],
        )
        raise PgHarborError(
            f"No trigger named {name!r} on this connection.{_near_names(near)} "
            "pg_list_objects(kind='trigger') lists triggers."
        )
    return found[0]


async def _sequence_state(db: Db, schema: str, name: str) -> tuple[int | None, bool, str | None]:
    """(last_value, is_called, note). Reads the sequence through the guarded path."""
    if db.v >= 100000:
        row = await db.row(
            "SELECT last_value FROM pg_catalog.pg_sequences "
            "WHERE schemaname = $1 AND sequencename = $2",
            [schema, name],
        )
        last = row["last_value"] if row else None
        return last, last is not None, None
    try:
        result = await run_query(
            db,
            f"SELECT last_value, is_called FROM {quote_qualified(schema, name)}",
            max_rows=1,
        )
    except PgHarborError as exc:
        return None, False, f"The current value could not be read: {exc}"
    if not result.rows:
        return None, False, None
    last, called = result.rows[0]
    return int(last), bool(called), None


def _clip_source(source: str | None, limit: int) -> tuple[str | None, bool]:
    if source is None:
        return None, False
    if len(source) <= limit:
        return source, False
    return source[:limit], True


_FUNCTION_DETAIL = """
    SELECT p.oid::int8 AS oid, n.nspname AS schema, p.proname AS name,
           pg_catalog.pg_get_function_identity_arguments(p.oid) AS identity,
           pg_catalog.pg_get_function_arguments(p.oid) AS arguments,
           {result} AS returns, {kind} AS kind, l.lanname AS language,
           {volatility} AS volatility, p.proisstrict AS strict, p.prosecdef AS security_definer,
           p.proleakproof AS leakproof,
           CASE p.proparallel WHEN 's' THEN 'safe' WHEN 'r' THEN 'restricted' ELSE 'unsafe' END
               AS parallel,
           p.procost AS cost, CASE WHEN p.proretset THEN p.prorows END AS rows,
           p.proconfig AS config, pg_catalog.pg_get_userbyid(p.proowner) AS owner,
           pg_catalog.obj_description(p.oid, 'pg_proc') AS comment,
           p.proacl::text[] AS privileges,
           p.probin AS library,
           length({source}) AS source_chars, left({source}, $2) AS source
      FROM pg_catalog.pg_proc p
      JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
      JOIN pg_catalog.pg_language l ON l.oid = p.prolang
     WHERE p.oid = ANY($1::oid[])
     ORDER BY p.oid
"""


async def _describe_function(db: Db, name: str, kind: str, max_source_chars: int) -> dict[str, Any]:
    found = await _find_functions(db, name, kind, what=kind)
    source = "p.prosrc"
    if db.v >= 140000:
        source = (
            "CASE WHEN p.prosqlbody IS NOT NULL THEN pg_catalog.pg_get_function_sqlbody(p.oid) "
            "ELSE p.prosrc END"
        )
    rows = await db.rows(
        _FUNCTION_DETAIL.format(
            result="pg_catalog.pg_get_function_result(p.oid)",
            kind=_function_kind_label(db.v),
            volatility=_VOLATILITY.format(a="p"),
            source=source,
        ),
        [[r["oid"] for r in found[:20]], max_source_chars + 1],
    )
    aggregates: dict[int, dict[str, Any]] = {}
    if any(r["kind"] == "aggregate" for r in rows):
        for agg in await db.rows(
            """SELECT a.aggfnoid::oid::int8 AS oid, a.aggtransfn::regproc::text AS transition,
                      NULLIF(a.aggfinalfn::regproc::text, '-') AS final,
                      pg_catalog.format_type(a.aggtranstype, NULL) AS state_type,
                      a.agginitval AS initial_value
                 FROM pg_catalog.pg_aggregate a WHERE a.aggfnoid = ANY($1::oid[])""",
            [[r["oid"] for r in rows]],
        ):
            aggregates[agg.pop("oid")] = agg
    overloads: list[dict[str, Any]] = []
    for row in rows:
        signature = f"{row['schema']}.{row['name']}({row.pop('identity')})"
        text, cut = _clip_source(row["source"], max_source_chars)
        row["source"] = text if row["kind"] != "aggregate" else None
        if cut:
            row["source_truncated"] = (
                f"{row['source_chars']:,} characters in total; "
                "raise max_source_chars or use pg_get_ddl for the whole function"
            )
        if row["language"] not in ("c", "internal"):
            row.pop("library")
        if row["kind"] == "aggregate":
            row["aggregate"] = aggregates.get(row["oid"])
        if row["rows"] is not None:
            row["rows"] = int(row["rows"])
        row["cost"] = int(row["cost"]) if row["cost"] == int(row["cost"]) else row["cost"]
        for flag in ("strict", "leakproof"):
            if not row[flag]:
                del row[flag]
        del row["oid"], row["source_chars"]
        row.pop("schema")
        row.pop("name")
        overloads.append(_tidy({"signature": signature, **row}))
    out: dict[str, Any] = {"connection": db.profile.name, "kind": kind, "overloads": overloads}
    if len(found) > 20:
        out["notes"] = [f"{len(found)} overloads exist; the first 20 are shown."]
    return out


async def _sequence_info(db: Db, oid: int, schema: str, name: str) -> dict[str, Any]:
    """Type, start, minimum, maximum, increment, cache and cycle of a sequence."""
    if db.v >= 100000:
        info = await db.row(
            """SELECT pg_catalog.format_type(s.seqtypid, NULL) AS data_type, s.seqstart AS start,
                      s.seqmin AS minimum, s.seqmax AS maximum, s.seqincrement AS increment,
                      s.seqcache AS cache, s.seqcycle AS cycle
                 FROM pg_catalog.pg_sequence s WHERE s.seqrelid = $1""",
            [oid],
        )
        assert info is not None
        return info
    # PostgreSQL 9.6 keeps the parameters in the sequence itself.
    result = await run_query(
        db,
        "SELECT start_value, min_value, max_value, increment_by, cache_value, is_cycled "
        f"FROM {quote_qualified(schema, name)}",
        max_rows=1,
    )
    start, low, high, step, cache, cycle = result.rows[0]
    return {
        "data_type": "bigint",
        "start": int(start),
        "minimum": int(low),
        "maximum": int(high),
        "increment": int(step),
        "cache": int(cache),
        "cycle": bool(cycle),
    }


async def _describe_sequence(db: Db, name: str) -> dict[str, Any]:
    relation = await resolve_relation(db, name, kinds="S", what="sequence")
    oid = relation["oid"]
    schema, seq = relation["schema"], relation["name"]
    extra = await db.row(
        f"""SELECT pg_catalog.pg_get_userbyid(c.relowner) AS owner,
                   pg_catalog.obj_description(c.oid, 'pg_class') AS comment,
                   {_OWNED_BY} AS owned_by,
                   (SELECT d.deptype = 'i' FROM pg_catalog.pg_depend d
                     WHERE d.classid = 'pg_catalog.pg_class'::regclass AND d.objid = c.oid
                       AND d.refclassid = 'pg_catalog.pg_class'::regclass
                       AND d.deptype IN ('a', 'i')
                     LIMIT 1) AS is_identity
              FROM pg_catalog.pg_class c WHERE c.oid = $1""",
        [oid],
    )
    assert extra is not None
    notes: list[str] = []
    try:
        info = await _sequence_info(db, oid, schema, seq)
    except PgHarborError as exc:
        info = {}
        notes.append(f"The sequence parameters could not be read: {exc}")
    last, called, note = await _sequence_state(db, schema, seq)
    if note:
        notes.append(note)
    out: dict[str, Any] = {
        "connection": db.profile.name,
        "kind": "sequence",
        "name": f"{schema}.{seq}",
        **info,
        "last_value": last if called else None,
        "owner": extra["owner"],
    }
    if info and called and last is not None:
        low, high, step = info["minimum"], info["maximum"], info["increment"]
        used = (last - low + 1) if step > 0 else (high - last + 1)
        out["percent_used"] = round(100.0 * used / (high - low + 1), 2)
        if step > 0 and out["percent_used"] >= 80 and not info["cycle"]:
            notes.append(
                "More than 80% of the range is used and the sequence does not cycle: "
                "nextval() will fail at the maximum. Plan an ALTER SEQUENCE or a wider type."
            )
    elif info and not called:
        out["percent_used"] = 0.0
        notes.append("The sequence has not been used yet (nextval was never called).")
    if extra["owned_by"]:
        out["owned_by"] = extra["owned_by"] + (" (identity column)" if extra["is_identity"] else "")
    if extra["comment"]:
        out["comment"] = extra["comment"]
    if notes:
        out["notes"] = notes
    return _tidy(out)


async def _describe_type(db: Db, name: str) -> dict[str, Any]:
    found = await _find_type(db, name)
    oid = found["oid"]
    head = await db.row(
        """
        SELECT t.typtype AS typtype, t.typlen AS length, t.typcategory AS category,
               t.typrelid::int8 AS relid, t.typelem::int8 AS element, t.typnotnull AS not_null,
               pg_catalog.format_type(t.typbasetype, t.typtypmod) AS base_type,
               t.typdefault AS dflt,
               pg_catalog.pg_get_userbyid(t.typowner) AS owner,
               pg_catalog.obj_description(t.oid, 'pg_type') AS comment,
               (SELECT c.relkind FROM pg_catalog.pg_class c WHERE c.oid = t.typrelid) AS relkind,
               (SELECT a.typname FROM pg_catalog.pg_type a WHERE a.oid = t.typarray) AS array_type,
               t.typinput::regproc::text AS input, t.typoutput::regproc::text AS output,
               t.typdelim AS delimiter
          FROM pg_catalog.pg_type t WHERE t.oid = $1
        """,
        [oid],
    )
    assert head is not None
    if head["relkind"] in ("r", "p", "v", "m", "f"):
        raise PgHarborError(
            f"{found['schema']}.{found['name']} is the row type of a table or view. "
            + _USE_DESCRIBE_TABLE
        )
    kind = {
        "b": "base",
        "c": "composite",
        "d": "domain",
        "e": "enum",
        "p": "pseudo",
        "r": "range",
        "m": "multirange",
    }.get(head["typtype"], head["typtype"])
    if kind == "base" and head["element"] and head["category"] == "A":
        kind = "array"
    out: dict[str, Any] = {
        "connection": db.profile.name,
        "name": f"{found['schema']}.{found['name']}",
        "kind": kind,
        "owner": head["owner"],
    }
    if head["comment"]:
        out["comment"] = head["comment"]
    if kind == "enum":
        out["labels"] = [
            r["label"]
            for r in await db.rows(
                "SELECT enumlabel::text AS label FROM pg_catalog.pg_enum "
                "WHERE enumtypid = $1 ORDER BY enumsortorder",
                [oid],
            )
        ]
    elif kind == "domain":
        out["base_type"] = head["base_type"]
        out["not_null"] = head["not_null"]
        if head["dflt"]:
            out["default"] = head["dflt"]
        out["checks"] = await db.rows(
            """SELECT c.conname AS name, pg_catalog.pg_get_constraintdef(c.oid, true) AS definition
                 FROM pg_catalog.pg_constraint c
                WHERE c.contypid = $1 AND c.contype = 'c' ORDER BY c.conname""",
            [oid],
        )
    elif kind == "composite":
        out["attributes"] = grid(
            await db.rows(
                """SELECT a.attname AS name, pg_catalog.format_type(a.atttypid, a.atttypmod) AS type
                     FROM pg_catalog.pg_attribute a
                    WHERE a.attrelid = $1 AND a.attnum > 0 AND NOT a.attisdropped
                    ORDER BY a.attnum""",
                [head["relid"]],
            ),
            ["name", "type"],
        )
    elif kind == "range":
        multirange = "pg_catalog.format_type(r.rngmultitypid, NULL)" if db.v >= 140000 else "NULL"
        out["range"] = _tidy(
            await db.row(
                f"""SELECT pg_catalog.format_type(r.rngsubtype, NULL) AS subtype,
                           NULLIF(r.rngcanonical::regproc::text, '-') AS canonical,
                           NULLIF(r.rngsubdiff::regproc::text, '-') AS subtype_diff,
                           {multirange} AS multirange
                      FROM pg_catalog.pg_range r WHERE r.rngtypid = $1""",
                [oid],
            )
            or {}
        )
    elif kind == "array":
        out["element_type"] = await db.value(
            "SELECT pg_catalog.format_type($1, NULL)", [head["element"]]
        )
    else:
        out["details"] = {
            "length": head["length"],
            "category": head["category"],
            "input": head["input"],
            "output": head["output"],
        }
    if head["array_type"]:
        out["array_type"] = head["array_type"]
    used_by = await db.rows(
        """
        SELECT n.nspname || '.' || c.relname || '.' || a.attname AS "column"
          FROM pg_catalog.pg_attribute a
          JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE a.atttypid = $1 AND a.attnum > 0 AND NOT a.attisdropped
           AND c.relkind IN ('r', 'p', 'm', 'f') AND n.nspname !~ '^pg_(toast|temp)'
         ORDER BY 1 LIMIT 31
        """,
        [oid],
    )
    if used_by:
        out["used_by_columns"] = [r["column"] for r in used_by[:30]]
        if len(used_by) > 30:
            out["notes"] = ["More than 30 columns use this type; the first 30 are shown."]
    return out


async def _describe_index(db: Db, name: str) -> dict[str, Any]:
    relation = await resolve_relation(db, name, kinds="iI", what="index")
    oid = relation["oid"]
    parent_check = "i.indnullsnotdistinct" if db.v >= 150000 else "false"
    head = await db.row(
        f"""
        SELECT pg_catalog.pg_get_indexdef(i.indexrelid) AS definition, am.amname AS method,
               tn.nspname || '.' || t.relname AS "table", i.indisunique AS "unique",
               i.indisprimary AS "primary", i.indisvalid AS valid, i.indisready AS ready,
               i.indisclustered AS clustered, i.indisreplident AS replica_identity,
               {parent_check} AS nulls_not_distinct,
               pg_catalog.pg_get_expr(i.indpred, i.indrelid) AS predicate,
               array_to_string(ARRAY(
                   SELECT pg_catalog.pg_get_indexdef(i.indexrelid, k, true)
                     FROM generate_series(1, i.indnatts::int) k ORDER BY k), ', ') AS columns,
               i.indnatts::int AS natts,
               {"i.indnkeyatts::int" if db.v >= 110000 else "i.indnatts::int"} AS nkeyatts,
               pg_catalog.pg_size_pretty(pg_catalog.pg_relation_size(i.indexrelid)) AS size,
               (SELECT spcname FROM pg_catalog.pg_tablespace s WHERE s.oid = c.reltablespace)
                   AS tablespace,
               c.reloptions AS options, pg_catalog.obj_description(c.oid, 'pg_class') AS comment,
               s.idx_scan AS scans, s.idx_tup_read AS tuples_read,
               s.idx_tup_fetch AS tuples_fetched,
               (SELECT con.conname || ' (' || CASE con.contype WHEN 'p' THEN 'primary key'
                       WHEN 'u' THEN 'unique' WHEN 'x' THEN 'exclusion' ELSE con.contype::text END
                       || ')' FROM pg_catalog.pg_constraint con WHERE con.conindid = c.oid
                        AND con.contype IN ('p', 'u', 'x') LIMIT 1) AS constraint
          FROM pg_catalog.pg_index i
          JOIN pg_catalog.pg_class c ON c.oid = i.indexrelid
          JOIN pg_catalog.pg_class t ON t.oid = i.indrelid
          JOIN pg_catalog.pg_namespace tn ON tn.oid = t.relnamespace
          JOIN pg_catalog.pg_am am ON am.oid = c.relam
          LEFT JOIN pg_catalog.pg_stat_all_indexes s ON s.indexrelid = i.indexrelid
         WHERE i.indexrelid = $1
        """,
        [oid],
    )
    assert head is not None
    out: dict[str, Any] = {
        "connection": db.profile.name,
        "name": f"{relation['schema']}.{relation['name']}",
        "kind": "partitioned index" if relation["kind"] == "I" else "index",
    }
    include = head["natts"] - head["nkeyatts"]
    for key in (
        "table",
        "definition",
        "method",
        "unique",
        "primary",
        "valid",
        "ready",
        "columns",
        "predicate",
        "constraint",
        "size",
        "tablespace",
        "options",
        "comment",
    ):
        out[key] = head[key]
    if include > 0:
        out["included_columns"] = include
    for flag in ("clustered", "replica_identity", "nulls_not_distinct"):
        if head[flag]:
            out[flag] = True
    out["usage"] = {
        "scans": head["scans"],
        "tuples_read": head["tuples_read"],
        "tuples_fetched": head["tuples_fetched"],
    }
    notes = []
    if not head["valid"]:
        notes.append(
            "The index is INVALID (a CREATE INDEX CONCURRENTLY failed or is running): the planner "
            "ignores it. Drop and recreate it."
        )
    if head["scans"] == 0 and not head["unique"]:
        notes.append(
            "No scan has used this index since statistics were last reset. "
            "pg_stat_reset time and replicas matter before dropping it."
        )
    if notes:
        out["notes"] = notes
    return _tidy(out)


async def _describe_trigger(db: Db, name: str, max_source_chars: int) -> dict[str, Any]:
    found = await _find_trigger(db, name)
    oid = found["oid"]
    transitions = "t.tgoldtable AS old_table, t.tgnewtable AS new_table," if db.v >= 100000 else ""
    head = await db.row(
        f"""
        SELECT pg_catalog.pg_get_triggerdef(t.oid, true) AS definition,
               CASE t.tgenabled WHEN 'D' THEN 'disabled' WHEN 'R' THEN 'replica only'
                    WHEN 'A' THEN 'always' ELSE 'enabled' END AS state,
               CASE WHEN t.tgtype & 64 > 0 THEN 'INSTEAD OF' WHEN t.tgtype & 2 > 0 THEN 'BEFORE'
                    ELSE 'AFTER' END AS timing,
               array_to_string(ARRAY[CASE WHEN t.tgtype & 4 > 0 THEN 'INSERT' END,
                                     CASE WHEN t.tgtype & 8 > 0 THEN 'DELETE' END,
                                     CASE WHEN t.tgtype & 16 > 0 THEN 'UPDATE' END,
                                     CASE WHEN t.tgtype & 32 > 0 THEN 'TRUNCATE' END], ' OR ')
                   AS events,
               CASE WHEN t.tgtype & 1 > 0 THEN 'row' ELSE 'statement' END AS level,
               pg_catalog.pg_get_expr(t.tgqual, t.tgrelid) AS "when",
               ARRAY(SELECT a.attname::text FROM unnest(t.tgattr::int2[]) k
                       JOIN pg_catalog.pg_attribute a ON a.attrelid = t.tgrelid AND a.attnum = k)
                   AS update_of_columns,
               {transitions}
               t.tgconstraint <> 0 AS is_constraint_trigger, t.tgdeferrable AS deferrable,
               t.tginitdeferred AS initially_deferred,
               p.oid::int8 AS function_oid, pn.nspname || '.' || p.proname AS function,
               pg_catalog.obj_description(t.oid, 'pg_trigger') AS comment
          FROM pg_catalog.pg_trigger t
          JOIN pg_catalog.pg_proc p ON p.oid = t.tgfoid
          JOIN pg_catalog.pg_namespace pn ON pn.oid = p.pronamespace
         WHERE t.oid = $1
        """,
        [oid],
    )
    assert head is not None
    function_oid = head.pop("function_oid")
    out: dict[str, Any] = {
        "connection": db.profile.name,
        "name": f"{found['schema']}.{found['table']}.{found['name']}",
        "kind": "trigger",
        "table": f"{found['schema']}.{found['table']}",
        **head,
    }
    if not head["is_constraint_trigger"]:
        for key in ("is_constraint_trigger", "deferrable", "initially_deferred"):
            out.pop(key, None)
    source = "p.prosrc"
    if db.v >= 140000:
        source = (
            "CASE WHEN p.prosqlbody IS NOT NULL THEN pg_catalog.pg_get_function_sqlbody(p.oid) "
            "ELSE p.prosrc END"
        )
    body = await db.row(
        f"""SELECT l.lanname AS language, length({source}) AS chars, left({source}, $2) AS source
              FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_language l ON l.oid = p.prolang
             WHERE p.oid = $1""",
        [function_oid, max_source_chars + 1],
    )
    if body and body["language"] not in ("c", "internal"):
        text, cut = _clip_source(body["source"], max_source_chars)
        out["function_language"] = body["language"]
        out["function_source"] = text
        if cut:
            out["function_source_truncated"] = f"{body['chars']:,} characters in total"
    return _tidy(out)


async def _describe_extension(db: Db, name: str) -> dict[str, Any]:
    (ext_name,) = _name_parts(name, maximum=1)
    head = await db.row(
        """
        SELECT e.oid::int8 AS oid, e.extname AS name, e.extversion AS version,
               n.nspname AS schema, e.extrelocatable AS relocatable,
               pg_catalog.pg_get_userbyid(e.extowner) AS owner,
               pg_catalog.obj_description(e.oid, 'pg_extension') AS comment,
               (SELECT a.default_version FROM pg_catalog.pg_available_extensions a
                 WHERE a.name = e.extname) AS available_version,
               (SELECT array_agg(r.name ORDER BY r.name)
                  FROM pg_catalog.pg_available_extension_versions r
                 WHERE r.name = e.extname AND r.version = e.extversion) IS NOT NULL AS has_files,
               (SELECT a.requires FROM pg_catalog.pg_available_extension_versions a
                 WHERE a.name = e.extname AND a.version = e.extversion LIMIT 1) AS requires
          FROM pg_catalog.pg_extension e JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace
         WHERE e.extname = $1
        """,
        [ext_name],
    )
    if head is None:
        available = await db.row(
            "SELECT default_version FROM pg_catalog.pg_available_extensions WHERE name = $1",
            [ext_name],
        )
        if available:
            raise PgHarborError(
                f"The extension {ext_name!r} is available (version {available['default_version']}) "
                f"but not installed in database '{db.facts.database}'. "
                f"A database owner can install it with: CREATE EXTENSION {ext_name};"
            )
        near = await db.rows(
            "SELECT name FROM pg_catalog.pg_available_extensions WHERE name ILIKE $1 "
            "ORDER BY name LIMIT 8",
            [f"%{ext_name}%"],
        )
        raise PgHarborError(
            f"No extension named {ext_name!r} is installed or available on this server."
            f"{_near_names(near)} pg_list_objects(kind='extension') lists the installed ones."
        )
    members = await db.rows(
        """
        SELECT (pg_catalog.pg_identify_object(d.classid, d.objid, d.objsubid)).type AS type,
               (pg_catalog.pg_identify_object(d.classid, d.objid, d.objsubid)).identity AS identity
          FROM pg_catalog.pg_depend d
         WHERE d.refclassid = 'pg_catalog.pg_extension'::regclass AND d.refobjid = $1
           AND d.deptype = 'e'
        """,
        [head["oid"]],
    )
    counts: dict[str, int] = {}
    for member in members:
        counts[member["type"]] = counts.get(member["type"], 0) + 1
    interesting = [
        m for m in members if m["type"] in ("table", "view", "materialized view", "sequence")
    ]
    out: dict[str, Any] = {
        "connection": db.profile.name,
        "kind": "extension",
        "name": head["name"],
        "version": head["version"],
        "schema": head["schema"],
        "relocatable": head["relocatable"],
        "owner": head["owner"],
        "comment": head["comment"],
        "requires": head["requires"],
        "objects": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "relations": [m["identity"] for m in sorted(interesting, key=lambda m: m["identity"])][:40],
    }
    notes = []
    if head["available_version"] and head["available_version"] != head["version"]:
        notes.append(
            f"Version {head['available_version']} is available on this server; "
            f"ALTER EXTENSION {quote_ident(head['name'])} UPDATE upgrades it."
        )
    if notes:
        out["notes"] = notes
    return _tidy(out)


async def _describe_schema(db: Db, name: str) -> dict[str, Any]:
    (schema,) = _name_parts(name, maximum=1)
    head = await db.row(
        """
        SELECT n.oid::int8 AS oid, pg_catalog.pg_get_userbyid(n.nspowner) AS owner,
               pg_catalog.obj_description(n.oid, 'pg_namespace') AS comment,
               n.nspacl::text[] AS privileges
          FROM pg_catalog.pg_namespace n WHERE n.nspname = $1
        """,
        [schema],
    )
    if head is None:
        near = await db.rows(
            "SELECT nspname AS name FROM pg_catalog.pg_namespace WHERE nspname ILIKE $1 "
            "AND nspname !~ '^pg_(toast|temp)' ORDER BY 1 LIMIT 8",
            [f"%{schema}%"],
        )
        raise PgHarborError(
            f"No schema named {schema!r} on this connection.{_near_names(near)} "
            "pg_list_schemas shows the schemas."
        )
    counts = await db.row(
        """
        SELECT count(*) FILTER (WHERE c.relkind IN ('r', 'p')) AS tables,
               count(*) FILTER (WHERE c.relkind = 'v') AS views,
               count(*) FILTER (WHERE c.relkind = 'm') AS materialized_views,
               count(*) FILTER (WHERE c.relkind = 'S') AS sequences,
               count(*) FILTER (WHERE c.relkind = 'f') AS foreign_tables,
               count(*) FILTER (WHERE c.relkind IN ('i', 'I')) AS indexes,
               coalesce(sum(pg_catalog.pg_total_relation_size(c.oid))
                        FILTER (WHERE c.relkind IN ('r', 'm')), 0)::bigint AS bytes
          FROM pg_catalog.pg_class c WHERE c.relnamespace = $1
        """,
        [head["oid"]],
    )
    assert counts is not None
    functions = await db.value(
        "SELECT count(*) FROM pg_catalog.pg_proc WHERE pronamespace = $1", [head["oid"]]
    )
    types = await db.value(
        """SELECT count(*) FROM pg_catalog.pg_type t
            WHERE t.typnamespace = $1 AND t.typtype IN ('e', 'd', 'r', 'c') AND t.typrelid = 0
               OR (t.typnamespace = $1 AND t.typtype = 'c'
                   AND (SELECT relkind FROM pg_catalog.pg_class c
                        WHERE c.oid = t.typrelid) = 'c')""",
        [head["oid"]],
    )
    size = (await _pretty(db, [counts["bytes"]]))[counts["bytes"]]
    bytes_ = counts.pop("bytes")
    del bytes_
    out: dict[str, Any] = {
        "connection": db.profile.name,
        "kind": "schema",
        "name": schema,
        "owner": head["owner"],
        "comment": head["comment"],
        "objects": {
            **{k: v for k, v in counts.items() if v},
            "functions": functions,
            "types": types,
        },
        "size": size,
        "privileges": head["privileges"],
    }
    out["objects"] = {k: v for k, v in out["objects"].items() if v}
    return _tidy(out)


@tool(group="schema", title="Describe a database object")
async def pg_describe_object(
    name: str,
    kind: DescribeKind,
    connection: str | None = None,
    max_source_chars: int = 3000,
) -> dict[str, Any]:
    """Describe one object that is not a table: a function, procedure, aggregate, sequence,
    type, index, trigger, extension or schema.

    Functions show every overload with arguments, return type, language, volatility,
    security definer, parallel safety, cost, settings, comment and source code. Sequences
    show type, range, last value, percent used and owning column. Types show enum labels,
    domain base type and checks, composite attributes or range subtype. Extensions show
    their version and the objects they created.

    `name` is `name` or `schema.name`; a trigger can be `table.trigger`, and a function
    accepts a signature such as `shop.order_total(bigint)`. `max_source_chars` cuts long
    function bodies. For tables and views use pg_describe_table.
    """
    max_source_chars = max(0, min(int(max_source_chars), 50_000))
    async with get_app().read(connection) as db:
        if kind in ("table", "view", "materialized_view"):
            raise PgHarborError(_USE_DESCRIBE_TABLE)
        if kind in ("function", "procedure", "aggregate"):
            if kind == "procedure":
                db.need_version(110000, "Procedures")
            return await _describe_function(db, name, kind, max_source_chars)
        if kind == "sequence":
            return await _describe_sequence(db, name)
        if kind == "type":
            return await _describe_type(db, name)
        if kind == "index":
            return await _describe_index(db, name)
        if kind == "trigger":
            return await _describe_trigger(db, name, max_source_chars)
        if kind == "extension":
            return await _describe_extension(db, name)
        return await _describe_schema(db, name)


# ------------------------------------------------------------------------------------ pg_get_ddl

# Words that need double quotes as identifiers on this server (filled by pg_get_ddl).
_KEYWORDS: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
    "pgharbor_keywords", default=frozenset()
)
_PLAIN_NAME = re.compile(r"[a-z_][a-z0-9_$]*")


def _ident(name: str) -> str:
    """Quote an identifier only when PostgreSQL would require it."""
    if _PLAIN_NAME.fullmatch(name) and name not in _KEYWORDS.get():
        return name
    return quote_ident(name)


def _qualified(schema: str, name: str) -> str:
    return f"{_ident(schema)}.{_ident(name)}"


_TYPE_RANGE = {
    "smallint": 2**15 - 1,
    "integer": 2**31 - 1,
    "bigint": 2**63 - 1,
}
_SERIAL = {"smallint": "smallserial", "integer": "serial", "bigint": "bigserial"}
_RELKIND_DDL = {
    "r": "table",
    "p": "table",
    "v": "view",
    "m": "materialized_view",
    "S": "sequence",
    "i": "index",
    "I": "index",
    "f": "foreign table",
}
_RELKINDS_BY_DDL = {
    "table": "rp",
    "view": "v",
    "materialized_view": "m",
    "sequence": "S",
    "index": "iI",
}


async def _find_relation(db: Db, name: str, kind: str) -> dict[str, Any]:
    """Resolve a relation of the wanted kind; say so when the name belongs to another kind."""
    label = kind.replace("_", " ")
    try:
        return await resolve_relation(db, name, kinds=_RELKINDS_BY_DDL[kind], what=label)
    except PgHarborError as first:
        try:
            other = await resolve_relation(db, name, kinds="rpvmfSiI")
        except PgHarborError:
            raise first from None
        actual = _RELKIND_DDL[other["kind"]]
        hint = (
            f"Use kind='{actual}'."
            if actual in _RELKINDS_BY_DDL
            else "Foreign tables are described by pg_describe_table."
        )
        raise PgHarborError(
            f"{other['schema']}.{other['name']} is a {relation_kind_label(other['kind'])}, "
            f"not a {label}. {hint}"
        ) from None


def _comment_on(target: str, text: str | None) -> list[str]:
    return [f"COMMENT ON {target} IS {quote_literal(text)};"] if text else []


def _identity_options(info: dict[str, Any]) -> str:
    step = info["increment"]
    maximum_type = _TYPE_RANGE.get(info["data_type"], 2**63 - 1)
    options: list[str] = []
    if step > 0:
        if info["start"] != 1:
            options.append(f"START WITH {info['start']}")
        if step != 1:
            options.append(f"INCREMENT BY {step}")
        if info["minimum"] != 1:
            options.append(f"MINVALUE {info['minimum']}")
        if info["maximum"] != maximum_type:
            options.append(f"MAXVALUE {info['maximum']}")
    else:
        options += [
            f"START WITH {info['start']}",
            f"INCREMENT BY {step}",
            f"MINVALUE {info['minimum']}",
            f"MAXVALUE {info['maximum']}",
        ]
    if info["cache"] != 1:
        options.append(f"CACHE {info['cache']}")
    if info["cycle"]:
        options.append("CYCLE")
    return f" ({' '.join(options)})" if options else ""


def _is_plain_serial(info: dict[str, Any], column_type: str, version: int) -> bool:
    """True when a sequence is exactly what ``serial`` of that column type would create."""
    if column_type not in _SERIAL:
        return False
    sequence_type = info["data_type"] if version >= 100000 else "bigint"
    if version >= 100000 and sequence_type != column_type:
        return False
    return (
        info["start"] == 1
        and info["increment"] == 1
        and info["minimum"] == 1
        and info["cache"] == 1
        and not info["cycle"]
        and info["maximum"] == _TYPE_RANGE[sequence_type]
    )


_TABLE_HEAD = """
    SELECT c.relpersistence AS persistence, c.reloptions AS options,
           (SELECT spcname FROM pg_catalog.pg_tablespace t WHERE t.oid = c.reltablespace)
               AS tablespace,
           c.relrowsecurity AS row_security, c.relforcerowsecurity AS force_row_security,
           pg_catalog.obj_description(c.oid, 'pg_class') AS comment,
           c.relkind AS relkind, c.relispopulated AS populated
           {partition}
      FROM pg_catalog.pg_class c WHERE c.oid = $1
"""


async def _ddl_table_columns(
    db: Db, relation: dict[str, Any], notes: list[str]
) -> tuple[list[str], list[str]]:
    """Column definitions, and the COMMENT ON COLUMN statements."""
    oid, schema, table = relation["oid"], relation["schema"], relation["name"]
    v = db.v
    identity = "a.attidentity::text" if v >= 100000 else "''::text"
    generated = "a.attgenerated::text" if v >= 120000 else "''::text"
    columns = await db.rows(
        f"""
        SELECT a.attnum::int AS attnum, a.attname AS name,
               pg_catalog.format_type(a.atttypid, a.atttypmod) AS type, a.attnotnull AS not_null,
               (SELECT quote_ident(cn.nspname) || '.' || quote_ident(co.collname)
                  FROM pg_catalog.pg_collation co
                  JOIN pg_catalog.pg_namespace cn ON cn.oid = co.collnamespace
                 WHERE co.oid = a.attcollation AND a.attcollation <> t.typcollation) AS collation,
               pg_catalog.pg_get_expr(d.adbin, d.adrelid) AS "default",
               {identity} AS identity, {generated} AS generated,
               pg_catalog.col_description(a.attrelid, a.attnum) AS comment
          FROM pg_catalog.pg_attribute a
          JOIN pg_catalog.pg_type t ON t.oid = a.atttypid
          LEFT JOIN pg_catalog.pg_attrdef d
                             ON d.adrelid = a.attrelid AND d.adnum = a.attnum
         WHERE a.attrelid = $1 AND a.attnum > 0 AND NOT a.attisdropped
         ORDER BY a.attnum
        """,
        [oid],
    )
    owned = {
        r["attnum"]: r
        for r in await db.rows(
            """
            SELECT d.refobjsubid::int AS attnum, d.deptype AS deptype, s.oid::int8 AS oid,
                   sn.nspname AS schema, s.relname AS name
              FROM pg_catalog.pg_depend d
              JOIN pg_catalog.pg_class s ON s.oid = d.objid AND s.relkind = 'S'
              JOIN pg_catalog.pg_namespace sn ON sn.oid = s.relnamespace
             WHERE d.classid = 'pg_catalog.pg_class'::regclass
               AND d.refclassid = 'pg_catalog.pg_class'::regclass
               AND d.refobjid = $1 AND d.refobjsubid > 0 AND d.deptype IN ('a', 'i')
            """,
            [oid],
        )
    }
    lines: list[str] = []
    comments: list[str] = []
    for column in columns:
        qualified = f"{_ident(column['name'])}"
        text = f"    {qualified} {column['type']}"
        sequence = owned.get(column["attnum"])
        info: dict[str, Any] | None = None
        if sequence is not None:
            try:
                info = await _sequence_info(
                    db, sequence["oid"], sequence["schema"], sequence["name"]
                )
            except PgHarborError:
                info = None
        default = column["default"]
        serial = False
        if (
            sequence is not None
            and sequence["deptype"] == "a"
            and info is not None
            and default
            and column["not_null"]
            and not column["identity"]
            and sequence["schema"] == schema
            and sequence["name"] == f"{table}_{column['name']}_seq"
            and default.startswith("nextval(")
            and _is_plain_serial(info, column["type"], v)
        ):
            serial = True
            text = f"    {qualified} {_SERIAL[column['type']]}"
        if column["collation"]:
            text += f" COLLATE {column['collation']}"
        if column["identity"] in ("a", "d"):
            clause = "ALWAYS" if column["identity"] == "a" else "BY DEFAULT"
            text += f" GENERATED {clause} AS IDENTITY"
            if info is not None:
                text += _identity_options(info)
        elif column["generated"] in ("s", "v"):
            text += f" GENERATED ALWAYS AS ({default}) " + (
                "STORED" if column["generated"] == "s" else "VIRTUAL"
            )
        elif default and not serial:
            text += f" DEFAULT {default}"
            if sequence is not None and sequence["deptype"] == "a":
                notes.append(
                    f"Column {column['name']} uses the sequence {sequence['schema']}."
                    f"{sequence['name']}, which has custom options; create it first with "
                    "pg_get_ddl(kind='sequence')."
                )
        if column["not_null"] and not serial and column["identity"] not in ("a", "d"):
            text += " NOT NULL"
        lines.append(text)
        comments += _comment_on(
            f"COLUMN {_qualified(schema, table)}.{_ident(column['name'])}",
            column["comment"],
        )
    return lines, comments


async def _ddl_table(db: Db, relation: dict[str, Any], notes: list[str]) -> list[str]:
    oid, schema, table = relation["oid"], relation["schema"], relation["name"]
    v = db.v
    qualified = _qualified(schema, table)
    partition = (
        """, c.relispartition AS is_partition,
           CASE WHEN c.relkind = 'p' THEN pg_catalog.pg_get_partkeydef(c.oid) END AS partition_key,
           pg_catalog.pg_get_expr(c.relpartbound, c.oid) AS partition_bound"""
        if v >= 100000
        else ", false AS is_partition, NULL::text AS partition_key, NULL::text AS partition_bound"
    )
    head = await db.row(_TABLE_HEAD.format(partition=partition), [oid])
    assert head is not None
    parents = await db.rows(
        """SELECT pn.nspname AS schema, p.relname AS name
             FROM pg_catalog.pg_inherits i
             JOIN pg_catalog.pg_class p ON p.oid = i.inhparent
             JOIN pg_catalog.pg_namespace pn ON pn.oid = p.relnamespace
            WHERE i.inhrelid = $1 ORDER BY i.inhseqno""",
        [oid],
    )
    is_partition = bool(head["is_partition"])
    column_lines: list[str] = []
    comments: list[str] = []
    if not is_partition:
        column_lines, comments = await _ddl_table_columns(db, relation, notes)
    else:
        _, comments = await _ddl_table_columns(db, relation, notes)
    conflict_clause = "AND con.conparentid = 0" if v >= 110000 else ""
    constraints = await db.rows(
        f"""
        SELECT con.conname AS name, pg_catalog.pg_get_constraintdef(con.oid, true) AS definition
          FROM pg_catalog.pg_constraint con
         WHERE con.conrelid = $1 AND con.contype IN ('p', 'u', 'f', 'c', 'x')
           AND con.conislocal {conflict_clause}
         ORDER BY pg_catalog.array_position(ARRAY['p','u','f','c','x']::"char"[], con.contype),
                  con.conname
        """,
        [oid],
    )
    constraint_lines = [
        f"    CONSTRAINT {_ident(c['name'])} {c['definition']}" for c in constraints
    ]
    prefix = "UNLOGGED " if head["persistence"] == "u" else ""
    statements: list[str] = []
    if is_partition:
        parent = parents[0]
        text = (
            f"CREATE {prefix}TABLE {qualified} PARTITION OF "
            f"{_qualified(parent['schema'], parent['name'])} {head['partition_bound']}"
        )
    else:
        body = column_lines + constraint_lines
        text = f"CREATE {prefix}TABLE {qualified} (" + (
            "\n" + ",\n".join(body) + "\n)" if body else ")"
        )
    if head["partition_key"]:
        text += f" PARTITION BY {head['partition_key']}"
    if parents and not is_partition:
        text += " INHERITS (" + ", ".join(_qualified(p["schema"], p["name"]) for p in parents) + ")"
    if head["options"]:
        text += " WITH (" + ", ".join(head["options"]) + ")"
    if head["tablespace"]:
        text += f" TABLESPACE {_ident(head['tablespace'])}"
    statements.append(text + ";")
    if is_partition:
        statements += [
            f"ALTER TABLE {qualified} ADD CONSTRAINT {_ident(c['name'])} {c['definition']};"
            for c in constraints
        ]
    statements += await _ddl_indexes(db, oid, exclude_constraints=True)
    statements += comments
    statements += _comment_on(f"TABLE {qualified}", head["comment"])
    statements += await _ddl_triggers(db, oid, qualified)
    if head["row_security"]:
        statements.append(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY;")
    if head["force_row_security"]:
        statements.append(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY;")
    statements += await _ddl_policies(db, oid, qualified)
    return statements


async def _ddl_indexes(db: Db, oid: int, *, exclude_constraints: bool) -> list[str]:
    owned_by_constraint = (
        """AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint con
                            WHERE con.conindid = i.indexrelid AND con.contype IN ('p', 'u', 'x')
                              AND con.conrelid = i.indrelid)"""
        if exclude_constraints
        else ""
    )
    cloned = (
        "AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_inherits ih WHERE ih.inhrelid = i.indexrelid)"
        if db.v >= 110000
        else ""
    )
    rows = await db.rows(
        f"""
        SELECT pg_catalog.pg_get_indexdef(i.indexrelid) AS definition, i.indisvalid AS valid,
               ic.relname AS name, pn.nspname AS schema,
               pg_catalog.obj_description(ic.oid, 'pg_class') AS comment
          FROM pg_catalog.pg_index i
          JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid
          JOIN pg_catalog.pg_namespace pn ON pn.oid = ic.relnamespace
         WHERE i.indrelid = $1 {owned_by_constraint} {cloned}
         ORDER BY ic.relname
        """,
        [oid],
    )
    out: list[str] = []
    for row in rows:
        out.append(row["definition"] + ";" + ("" if row["valid"] else " -- INVALID index"))
        out += _comment_on(f"INDEX {_qualified(row['schema'], row['name'])}", row["comment"])
    return out


async def _ddl_triggers(db: Db, oid: int, qualified: str) -> list[str]:
    cloned = "AND t.tgparentid = 0" if db.v >= 130000 else ""
    rows = await db.rows(
        f"""SELECT pg_catalog.pg_get_triggerdef(t.oid, true) AS definition, t.tgname AS name,
                   t.tgenabled AS state
              FROM pg_catalog.pg_trigger t
             WHERE t.tgrelid = $1 AND NOT t.tgisinternal {cloned} ORDER BY t.tgname""",
        [oid],
    )
    out: list[str] = []
    for row in rows:
        out.append(row["definition"] + ";")
        verb = {"D": "DISABLE", "R": "ENABLE REPLICA", "A": "ENABLE ALWAYS"}.get(row["state"])
        if verb:
            out.append(f"ALTER TABLE {qualified} {verb} TRIGGER {_ident(row['name'])};")
    return out


async def _ddl_policies(db: Db, oid: int, qualified: str) -> list[str]:
    permissive = "pol.polpermissive" if db.v >= 100000 else "true"
    rows = await db.rows(
        f"""
        SELECT pol.polname AS name, pol.polcmd AS command, {permissive} AS permissive,
               CASE WHEN pol.polroles = '{{0}}'::oid[] THEN NULL
                    ELSE ARRAY(SELECT pg_catalog.quote_ident(r.rolname::text)
                                 FROM pg_catalog.pg_roles r
                                WHERE r.oid = ANY(pol.polroles) ORDER BY 1) END AS roles,
               pg_catalog.pg_get_expr(pol.polqual, pol.polrelid) AS using_expr,
               pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid) AS check_expr
          FROM pg_catalog.pg_policy pol WHERE pol.polrelid = $1 ORDER BY pol.polname
        """,
        [oid],
    )
    out: list[str] = []
    for row in rows:
        text = f"CREATE POLICY {_ident(row['name'])} ON {qualified}"
        if not row["permissive"]:
            text += " AS RESTRICTIVE"
        command = {"r": "SELECT", "a": "INSERT", "w": "UPDATE", "d": "DELETE"}.get(row["command"])
        if command:
            text += f" FOR {command}"
        if row["roles"]:
            text += " TO " + ", ".join(row["roles"])
        if row["using_expr"]:
            text += f" USING ({row['using_expr']})"
        if row["check_expr"]:
            text += f" WITH CHECK ({row['check_expr']})"
        out.append(text + ";")
    return out


async def _ddl_view(db: Db, relation: dict[str, Any], *, materialized: bool) -> list[str]:
    oid, schema, name = relation["oid"], relation["schema"], relation["name"]
    qualified = _qualified(schema, name)
    head = await db.row(_TABLE_HEAD.format(partition=""), [oid])
    assert head is not None
    definition = (await db.value("SELECT pg_catalog.pg_get_viewdef($1::oid, true)", [oid])).strip()
    definition = definition.removesuffix(";")
    options = f" WITH ({', '.join(head['options'])})" if head["options"] else ""
    if materialized:
        text = f"CREATE MATERIALIZED VIEW {qualified}{options}"
        if head["tablespace"]:
            text += f" TABLESPACE {_ident(head['tablespace'])}"
        text += f" AS\n{definition}\nWITH {'DATA' if head['populated'] else 'NO DATA'};"
    else:
        text = f"CREATE OR REPLACE VIEW {qualified}{options} AS\n{definition};"
    statements = [text]
    if materialized:
        statements += await _ddl_indexes(db, oid, exclude_constraints=False)
    statements += _comment_on(
        f"{'MATERIALIZED VIEW' if materialized else 'VIEW'} {qualified}", head["comment"]
    )
    for col in await db.rows(
        """SELECT a.attname AS name, pg_catalog.col_description(a.attrelid, a.attnum) AS comment
             FROM pg_catalog.pg_attribute a
            WHERE a.attrelid = $1 AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum""",
        [oid],
    ):
        statements += _comment_on(f"COLUMN {qualified}.{_ident(col['name'])}", col["comment"])
    statements += await _ddl_triggers(db, oid, qualified)
    return statements


async def _ddl_function(db: Db, name: str, kind: str) -> list[str]:
    found = await _find_functions(db, name, kind, what=kind)
    rows = await db.rows(
        """SELECT pg_catalog.pg_get_functiondef(p.oid) AS definition,
                  n.nspname || '.' || p.proname || '(' ||
                      pg_catalog.pg_get_function_identity_arguments(p.oid) || ')' AS signature,
                  pg_catalog.obj_description(p.oid, 'pg_proc') AS comment
             FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
            WHERE p.oid = ANY($1::oid[]) ORDER BY p.oid""",
        [[r["oid"] for r in found[:20]]],
    )
    out: list[str] = []
    for row in rows:
        out.append(row["definition"].strip() + ";")
        out += _comment_on(f"{kind.upper()} {row['signature']}", row["comment"])
    return out


async def _ddl_sequence(db: Db, relation: dict[str, Any]) -> list[str]:
    oid, schema, name = relation["oid"], relation["schema"], relation["name"]
    qualified = _qualified(schema, name)
    info = await _sequence_info(db, oid, schema, name)
    extra = await db.row(
        f"""SELECT {_OWNED_BY} AS owned_by,
                   (SELECT d.deptype FROM pg_catalog.pg_depend d
                     WHERE d.classid = 'pg_catalog.pg_class'::regclass AND d.objid = c.oid
                       AND d.refclassid = 'pg_catalog.pg_class'::regclass
                       AND d.deptype IN ('a', 'i')
                     LIMIT 1) AS deptype,
                   (SELECT quote_ident(nn.nspname) || '.' || quote_ident(tc.relname) || '.'
                           || quote_ident(a.attname)
                      FROM pg_catalog.pg_depend d
                      JOIN pg_catalog.pg_class tc ON tc.oid = d.refobjid
                      JOIN pg_catalog.pg_namespace nn ON nn.oid = tc.relnamespace
                      JOIN pg_catalog.pg_attribute a
                        ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
                     WHERE d.classid = 'pg_catalog.pg_class'::regclass AND d.objid = c.oid
                       AND d.refclassid = 'pg_catalog.pg_class'::regclass
                       AND d.deptype IN ('a', 'i') AND d.refobjsubid > 0 LIMIT 1) AS owner_column,
                   pg_catalog.obj_description(c.oid, 'pg_class') AS comment
              FROM pg_catalog.pg_class c WHERE c.oid = $1""",
        [oid],
    )
    assert extra is not None
    text = f"CREATE SEQUENCE {qualified}"
    if db.v >= 100000 and info["data_type"] != "bigint":
        text += f" AS {info['data_type']}"
    text += (
        f" START WITH {info['start']} INCREMENT BY {info['increment']}"
        f" MINVALUE {info['minimum']} MAXVALUE {info['maximum']} CACHE {info['cache']}"
        + (" CYCLE" if info["cycle"] else "")
        + ";"
    )
    statements = [text]
    if extra["owner_column"] and extra["deptype"] == "a":
        statements.append(f"ALTER SEQUENCE {qualified} OWNED BY {extra['owner_column']};")
    elif extra["owner_column"]:
        statements.insert(
            0, f"-- Identity sequence of column {extra['owner_column']}: created with that column."
        )
    statements += _comment_on(f"SEQUENCE {qualified}", extra["comment"])
    return statements


async def _ddl_type(db: Db, name: str) -> list[str]:
    found = await _find_type(db, name)
    oid = found["oid"]
    qualified = _qualified(found["schema"], found["name"])
    head = await db.row(
        """SELECT t.typtype AS typtype, t.typrelid::int8 AS relid, t.typnotnull AS not_null,
                  pg_catalog.format_type(t.typbasetype, t.typtypmod) AS base_type,
                  t.typdefault AS dflt, pg_catalog.obj_description(t.oid, 'pg_type') AS comment,
                  (SELECT c.relkind FROM pg_catalog.pg_class c WHERE c.oid = t.typrelid) AS relkind,
                  (SELECT quote_ident(cn.nspname) || '.' || quote_ident(co.collname)
                     FROM pg_catalog.pg_collation co
                     JOIN pg_catalog.pg_namespace cn ON cn.oid = co.collnamespace
                    WHERE co.oid = t.typcollation AND t.typbasetype <> 0
                      AND t.typcollation <> (SELECT b.typcollation FROM pg_catalog.pg_type b
                                              WHERE b.oid = t.typbasetype)) AS collation
             FROM pg_catalog.pg_type t WHERE t.oid = $1""",
        [oid],
    )
    assert head is not None
    if head["relkind"] in ("r", "p", "v", "m", "f"):
        raise PgHarborError(
            f"{found['schema']}.{found['name']} is the row type of a table or view. "
            "Ask for the table with kind='table' (or 'view')."
        )
    typtype = head["typtype"]
    if typtype == "e":
        labels = await db.rows(
            "SELECT enumlabel::text AS label FROM pg_catalog.pg_enum "
            "WHERE enumtypid = $1 ORDER BY enumsortorder",
            [oid],
        )
        text = (
            f"CREATE TYPE {qualified} AS ENUM ("
            + ", ".join(quote_literal(r["label"]) for r in labels)
            + ");"
        )
    elif typtype == "d":
        text = f"CREATE DOMAIN {qualified} AS {head['base_type']}"
        if head["collation"]:
            text += f" COLLATE {head['collation']}"
        if head["dflt"]:
            text += f" DEFAULT {head['dflt']}"
        if head["not_null"]:
            text += " NOT NULL"
        for check in await db.rows(
            """SELECT c.conname AS name, pg_catalog.pg_get_constraintdef(c.oid, true) AS definition
                 FROM pg_catalog.pg_constraint c
                WHERE c.contypid = $1 AND c.contype = 'c' ORDER BY c.conname""",
            [oid],
        ):
            text += f"\n    CONSTRAINT {_ident(check['name'])} {check['definition']}"
        text += ";"
    elif typtype == "c":
        attributes = await db.rows(
            """SELECT a.attname AS name, pg_catalog.format_type(a.atttypid, a.atttypmod) AS type
                 FROM pg_catalog.pg_attribute a
                WHERE a.attrelid = $1 AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum""",
            [head["relid"]],
        )
        text = (
            f"CREATE TYPE {qualified} AS ("
            + ", ".join(f"{_ident(a['name'])} {a['type']}" for a in attributes)
            + ");"
        )
    elif typtype == "r":
        rng = await db.row(
            """SELECT pg_catalog.format_type(r.rngsubtype, NULL) AS subtype,
                      NULLIF(r.rngcanonical::regproc::text, '-') AS canonical,
                      NULLIF(r.rngsubdiff::regproc::text, '-') AS subdiff
                 FROM pg_catalog.pg_range r WHERE r.rngtypid = $1""",
            [oid],
        )
        assert rng is not None
        options = [f"SUBTYPE = {rng['subtype']}"]
        if rng["canonical"]:
            options.append(f"CANONICAL = {rng['canonical']}")
        if rng["subdiff"]:
            options.append(f"SUBTYPE_DIFF = {rng['subdiff']}")
        text = f"CREATE TYPE {qualified} AS RANGE ({', '.join(options)});"
    else:
        raise PgHarborError(
            f"{found['schema']}.{found['name']} is a base, pseudo or array type, which cannot be "
            "rebuilt from the catalog. Use pg_describe_object to inspect it."
        )
    kind_word = "DOMAIN" if typtype == "d" else "TYPE"
    return [text, *_comment_on(f"{kind_word} {qualified}", head["comment"])]


@tool(group="schema", title="Rebuild the CREATE statement of an object")
async def pg_get_ddl(
    name: str,
    kind: DdlKind = "table",
    connection: str | None = None,
) -> dict[str, Any]:
    """Rebuild the CREATE statement (DDL, as SQL text) of a table, view, materialized view,
    function, procedure, sequence, type (enum, domain, composite, range), index, trigger or
    schema.

    A table comes with its columns (types, defaults, identity and generated columns, NOT
    NULL), constraints, PARTITION BY / PARTITION OF, INHERITS, indexes, comments, triggers,
    row level security and policies. Names are always schema-qualified. Owners and GRANTs
    are not included. `name` is `name` or `schema.name`; a function accepts a signature such
    as `shop.order_total(bigint)`. Use it to copy a structure or to review how an object was
    defined.
    """
    notes: list[str] = []
    async with get_app().read(connection) as db:
        # Deparsing depends on search_path: with only pg_catalog on it every other object
        # is printed schema-qualified, whatever the role's own setting is.
        await db.rows("SELECT pg_catalog.set_config('search_path', 'pg_catalog', true)")
        _KEYWORDS.set(
            frozenset(
                r["word"]
                for r in await db.rows(
                    "SELECT word FROM pg_catalog.pg_get_keywords() WHERE catcode <> 'U'"
                )
            )
        )
        if kind in ("table", "view", "materialized_view", "sequence", "index"):
            relation = await _find_relation(db, name, kind)
            if kind == "table":
                statements = await _ddl_table(db, relation, notes)
            elif kind in ("view", "materialized_view"):
                statements = await _ddl_view(db, relation, materialized=kind != "view")
            elif kind == "sequence":
                statements = await _ddl_sequence(db, relation)
            else:
                statements = await _ddl_indexes_of(db, relation)
            subject = f"{relation['schema']}.{relation['name']}"
        elif kind in ("function", "procedure"):
            if kind == "procedure":
                db.need_version(110000, "Procedures")
            statements = await _ddl_function(db, name, kind)
            subject = name
        elif kind == "type":
            statements = await _ddl_type(db, name)
            subject = name
        elif kind == "trigger":
            found = await _find_trigger(db, name)
            definition = await db.value(
                "SELECT pg_catalog.pg_get_triggerdef($1::oid, true)", [found["oid"]]
            )
            statements = [definition + ";"]
            subject = f"{found['schema']}.{found['table']}.{found['name']}"
        else:
            (schema,) = _name_parts(name, maximum=1)
            row = await db.row(
                """SELECT pg_catalog.pg_get_userbyid(n.nspowner) AS owner,
                          pg_catalog.obj_description(n.oid, 'pg_namespace') AS comment
                     FROM pg_catalog.pg_namespace n WHERE n.nspname = $1""",
                [schema],
            )
            if row is None:
                raise PgHarborError(
                    f"No schema named {schema!r} on this connection. pg_list_schemas shows them."
                )
            statements = [
                f"CREATE SCHEMA {_ident(schema)} AUTHORIZATION {_ident(row['owner'])};",
                *_comment_on(f"SCHEMA {_ident(schema)}", row["comment"]),
            ]
            subject = schema
    out: dict[str, Any] = {
        "connection": db.profile.name,
        "object": subject,
        "kind": kind,
        "ddl": "\n".join(statements),
    }
    notes = list(dict.fromkeys(notes))
    if notes:
        out["notes"] = notes
    return out


async def _ddl_indexes_of(db: Db, relation: dict[str, Any]) -> list[str]:
    row = await db.row(
        """SELECT pg_catalog.pg_get_indexdef(c.oid) AS definition,
                  pg_catalog.obj_description(c.oid, 'pg_class') AS comment
             FROM pg_catalog.pg_class c WHERE c.oid = $1""",
        [relation["oid"]],
    )
    assert row is not None
    qualified = _qualified(relation["schema"], relation["name"])
    return [row["definition"] + ";", *_comment_on(f"INDEX {qualified}", row["comment"])]


# ---------------------------------------------------------------------------------- pg_search

SearchKind = Literal[
    "table",
    "view",
    "materialized_view",
    "foreign_table",
    "sequence",
    "index",
    "column",
    "function",
    "type",
    "enum_label",
    "trigger",
    "constraint",
    "schema",
    "extension",
    "comment",
]

_SEARCH_SCHEMA = (
    "($3::text IS NULL OR {s} = $3) AND ($3::text IS NOT NULL OR "
    f"({{s}} NOT IN {_SYSTEM_SCHEMAS} AND {{s}} !~ '^pg_(toast|temp)'))"
)


def _rank(expression: str) -> str:
    return (
        f"CASE WHEN lower({expression}) = lower($1) THEN 0 "
        f"WHEN strpos(lower({expression}), lower($1)) = 1 THEN 1 ELSE 2 END"
    )


def _search_branch(kind: str, v: int) -> str:
    """One SELECT of the search: kind, schema, name, parent, detail, rank (ORDER BY inside)."""
    scope = _SEARCH_SCHEMA.format(s="n.nspname")
    head = f"(SELECT '{kind}'::text AS kind"

    def relation(relkinds: str, parent: str = "NULL::text") -> str:
        return (
            f"{head}, n.nspname AS schema, c.relname AS name, {parent} AS parent, "
            f"left(pg_catalog.obj_description(c.oid, 'pg_class'), 100) AS detail, "
            f"{_rank('c.relname')} AS rank "
            "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE c.relkind IN ({relkinds}) AND c.relname ILIKE $2 AND {scope} "
            "ORDER BY rank, name LIMIT $4)"
        )

    if kind == "table":
        return relation("'r', 'p'")
    if kind == "view":
        return relation("'v'")
    if kind == "materialized_view":
        return relation("'m'")
    if kind == "foreign_table":
        return relation("'f'")
    if kind == "sequence":
        return relation("'S'")
    if kind == "index":
        return (
            f"{head}, n.nspname AS schema, c.relname AS name, t.relname AS parent, "
            "NULL::text AS detail, "
            f"{_rank('c.relname')} AS rank "
            "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid "
            "JOIN pg_catalog.pg_class t ON t.oid = i.indrelid "
            f"WHERE c.relname ILIKE $2 AND {scope} ORDER BY rank, name LIMIT $4)"
        )
    if kind == "column":
        return (
            f"{head}, n.nspname AS schema, a.attname AS name, c.relname AS parent, "
            "pg_catalog.format_type(a.atttypid, a.atttypmod) AS detail, "
            f"{_rank('a.attname')} AS rank "
            "FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE a.attnum > 0 AND NOT a.attisdropped AND c.relkind IN ('r', 'p', 'v', 'm', 'f') "
            f"AND a.attname ILIKE $2 AND {scope} ORDER BY rank, parent, name LIMIT $4)"
        )
    if kind == "function":
        label = _function_kind_label(v)
        return (
            f"{head}, n.nspname AS schema, p.proname AS name, {label} AS parent, "
            "pg_catalog.pg_get_function_identity_arguments(p.oid) AS detail, "
            f"{_rank('p.proname')} AS rank "
            "FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace "
            f"WHERE p.proname ILIKE $2 AND {scope} ORDER BY rank, name LIMIT $4)"
        )
    if kind == "type":
        return (
            f"{head}, n.nspname AS schema, t.typname AS name, "
            "CASE t.typtype WHEN 'e' THEN 'enum' WHEN 'd' THEN 'domain' WHEN 'c' THEN 'composite' "
            "WHEN 'r' THEN 'range' ELSE 'type' END AS parent, NULL::text AS detail, "
            f"{_rank('t.typname')} AS rank "
            "FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace "
            "WHERE (t.typrelid = 0 OR (SELECT c.relkind FROM pg_catalog.pg_class c "
            "WHERE c.oid = t.typrelid) = 'c') AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_type e "
            "WHERE e.oid = t.typelem AND e.typarray = t.oid) "
            f"AND t.typname ILIKE $2 AND {scope} ORDER BY rank, name LIMIT $4)"
        )
    if kind == "enum_label":
        return (
            f"{head}, n.nspname AS schema, e.enumlabel::text AS name, t.typname AS parent, "
            "NULL::text AS detail, "
            f"{_rank('e.enumlabel::text')} AS rank "
            "FROM pg_catalog.pg_enum e JOIN pg_catalog.pg_type t ON t.oid = e.enumtypid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace "
            f"WHERE e.enumlabel::text ILIKE $2 AND {scope} ORDER BY rank, name LIMIT $4)"
        )
    if kind == "trigger":
        return (
            f"{head}, n.nspname AS schema, t.tgname AS name, c.relname AS parent, "
            "NULL::text AS detail, "
            f"{_rank('t.tgname')} AS rank "
            "FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE NOT t.tgisinternal AND t.tgname ILIKE $2 AND {scope} "
            "ORDER BY rank, name LIMIT $4)"
        )
    if kind == "constraint":
        return (
            f"{head}, n.nspname AS schema, con.conname AS name, c.relname AS parent, "
            "CASE con.contype WHEN 'p' THEN 'primary key' WHEN 'f' THEN 'foreign key' "
            "WHEN 'u' THEN 'unique' WHEN 'c' THEN 'check' WHEN 'x' THEN 'exclusion' "
            "ELSE con.contype::text END AS detail, "
            f"{_rank('con.conname')} AS rank "
            "FROM pg_catalog.pg_constraint con JOIN pg_catalog.pg_class c ON c.oid = con.conrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE con.contype <> 't' AND con.conname ILIKE $2 AND {scope} "
            "ORDER BY rank, name LIMIT $4)"
        )
    if kind == "schema":
        return (
            f"{head}, n.nspname AS schema, n.nspname AS name, NULL::text AS parent, "
            "left(pg_catalog.obj_description(n.oid, 'pg_namespace'), 100) AS detail, "
            f"{_rank('n.nspname')} AS rank "
            "FROM pg_catalog.pg_namespace n "
            f"WHERE n.nspname ILIKE $2 AND {scope} ORDER BY rank, name LIMIT $4)"
        )
    if kind == "extension":
        return (
            f"{head}, n.nspname AS schema, e.extname AS name, NULL::text AS parent, "
            "e.extversion AS detail, "
            f"{_rank('e.extname')} AS rank "
            "FROM pg_catalog.pg_extension e "
            "JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace "
            "WHERE e.extname ILIKE $2 AND ($3::text IS NULL OR n.nspname = $3) "
            "ORDER BY rank, name LIMIT $4)"
        )
    # comment: text inside the comments of user objects
    return (
        f"{head}, NULL::text AS schema, "
        "pg_catalog.pg_describe_object(d.classoid, d.objoid, d.objsubid) AS name, "
        "NULL::text AS parent, left(d.description, 160) AS detail, 2 AS rank "
        "FROM pg_catalog.pg_description d "
        "WHERE d.description ILIKE $2 AND $1::text IS NOT NULL AND d.objoid >= 16384::oid "
        "AND ($3::text IS NULL OR strpos(pg_catalog.pg_describe_object(d.classoid, d.objoid, "
        "d.objsubid), $3 || '.') > 0) ORDER BY name LIMIT $4)"
    )


def _search_label(row: dict[str, Any]) -> str:
    kind, schema, name, parent, detail = (
        row["kind"],
        row["schema"],
        row["name"],
        row["parent"],
        row["detail"],
    )
    if kind == "column":
        return f"{schema}.{parent}.{name} ({detail})"
    if kind == "function":
        return f"{schema}.{name}({detail}) [{parent}]"
    if kind == "index":
        return f"{schema}.{name} on {schema}.{parent}"
    if kind == "type":
        return f"{schema}.{name} [{parent}]"
    if kind == "enum_label":
        return f"'{name}' in {schema}.{parent}"
    if kind in ("trigger", "constraint"):
        return f"{schema}.{parent}.{name}" + (f" ({detail})" if detail else "")
    if kind == "extension":
        return f"{name} {detail} (schema {schema})"
    if kind == "comment":
        return f"{name}: {detail}"
    label = f"{schema}.{name}"
    return label + (f" - {detail}" if detail else "")


@tool(group="schema", title="Search objects by name")
async def pg_search(
    query: str,
    connection: str | None = None,
    kinds: list[SearchKind] | None = None,
    schema: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Find database objects by name: tables, views, columns, functions, indexes,
    sequences, types, enum labels, triggers, constraints, schemas, extensions, and text
    inside comments.

    Use it when you know a word (`invoice`, `customer_id`) but not where it lives. The
    answer is grouped by kind, best matches first (exact name, then prefix, then
    substring), each as `schema.object`. `query` matches anywhere in a name (`*` and `?`
    are wildcards). `kinds` narrows the search, `schema` limits it to one schema, `limit`
    is the maximum per kind. System schemas are skipped unless named.
    """
    if not query or not query.strip():
        raise PgHarborError("Give a name or part of a name to search for.")
    query = query.strip()
    limit = clamp(limit, 50, 500)
    wanted = list(
        dict.fromkeys(
            kinds
            or [
                "table",
                "view",
                "materialized_view",
                "foreign_table",
                "column",
                "function",
                "index",
                "sequence",
                "type",
                "enum_label",
                "trigger",
                "constraint",
                "schema",
                "extension",
                "comment",
            ]
        )
    )
    async with get_app().read(connection) as db:
        sql = " UNION ALL ".join(_search_branch(k, db.v) for k in wanted)
        rows = await db.rows(sql, [query, like_pattern(query), schema, limit + 1])
    results: dict[str, list[str]] = {}
    truncated: list[str] = []
    order = {k: i for i, k in enumerate(wanted)}
    for kind in sorted({r["kind"] for r in rows}, key=lambda k: order[k]):
        group = [r for r in rows if r["kind"] == kind]
        group.sort(key=lambda r: (r["rank"], str(r["schema"]), str(r["parent"]), r["name"]))
        if len(group) > limit:
            truncated.append(kind)
        results[kind] = [_search_label(r) for r in group[:limit]]
    out: dict[str, Any] = {
        "connection": db.profile.name,
        "query": query,
        "total": sum(len(v) for v in results.values()),
        "results": results,
    }
    if truncated:
        out["truncated"] = truncated
        out["notes"] = [
            f"More matches exist for: {', '.join(truncated)}. Narrow with `kinds`, `schema` or a "
            "longer query."
        ]
    if not results:
        out["hint"] = (
            "Nothing matched. Try a shorter fragment (matching is case-insensitive and "
            "anywhere in the name), or `*` wildcards; pass schema='pg_catalog' to search the "
            "system catalog."
        )
    return out


# ----------------------------------------------------------------------------- pg_search_source

SourceKind = Literal["function", "procedure", "view", "materialized_view", "trigger"]


def _source_branch(kind: str, v: int) -> str:
    scope = _SEARCH_SCHEMA.format(s="n.nspname")
    if kind in ("function", "procedure"):
        src = "p.prosrc"
        if v >= 140000:
            src = (
                "CASE WHEN p.prosqlbody IS NOT NULL "
                "THEN pg_catalog.pg_get_function_sqlbody(p.oid) ELSE p.prosrc END"
            )
        return (
            f"SELECT '{kind}'::text AS kind, n.nspname AS schema, "
            "p.proname || '(' || pg_catalog.pg_get_function_identity_arguments(p.oid) || ')' "
            f"AS name, {src} AS src "
            "FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace "
            "JOIN pg_catalog.pg_language l ON l.oid = p.prolang "
            f"WHERE {_function_kind_condition(v, kind)} AND l.lanname NOT IN ('c', 'internal') "
            f"AND {_not_in_extension('pg_proc', 'p')} AND {scope}"
        )
    if kind in ("view", "materialized_view"):
        relkind = "v" if kind == "view" else "m"
        return (
            f"SELECT '{kind}'::text AS kind, n.nspname AS schema, c.relname AS name, "
            "pg_catalog.pg_get_viewdef(c.oid, true) AS src "
            "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE c.relkind = '{relkind}' AND {_not_in_extension('pg_class', 'c')} AND {scope}"
        )
    return (
        "SELECT 'trigger'::text AS kind, n.nspname AS schema, "
        "c.relname || '.' || t.tgname AS name, "
        "pg_catalog.pg_get_triggerdef(t.oid, true) AS src "
        "FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        f"WHERE NOT t.tgisinternal AND {scope}"
    )


@tool(group="schema", title="Search inside source code")
async def pg_search_source(
    pattern: str,
    connection: str | None = None,
    schema: str | None = None,
    kinds: list[SourceKind] | None = None,
    regex: bool = False,
    limit: int = 30,
) -> dict[str, Any]:
    """Find functions, procedures, views, materialized views and triggers whose source code
    contains a text, and return the matching lines with their line numbers.

    Use it to answer "who uses this table or column?" in code that lives inside the
    database, or "where is this function called?". `pattern` is plain text (case
    insensitive); with `regex=true` it is a PostgreSQL regular expression. Line numbers
    count from the first line of a function body or view definition. Code that belongs to
    extensions is skipped. `limit` is the maximum number of objects.
    """
    if not pattern or not pattern.strip():
        raise PgHarborError("Give the text to look for.")
    limit = clamp(limit, 30, 300)
    wanted = list(
        dict.fromkeys(kinds or ["function", "procedure", "view", "materialized_view", "trigger"])
    )
    line_match = "l.t ~* $1" if regex else "strpos(lower(l.t), lower($1)) > 0"
    source_match = "c.src ~* $1" if regex else "strpos(lower(c.src), lower($1)) > 0"
    async with get_app().read(connection) as db:
        branches = [_source_branch(k, db.v) for k in wanted if k != "procedure" or db.v >= 110000]
        if not branches:
            db.need_version(110000, "Procedures")
        sql = f"""
            SELECT c.kind, c.schema, c.name, m.n_lines, m.first_lines
              FROM ({" UNION ALL ".join(branches)}) c
             CROSS JOIN LATERAL (
                   SELECT count(*) AS n_lines,
                          (array_agg(l.n::text || ': ' || left(btrim(l.t), 160) ORDER BY l.n))[1:8]
                              AS first_lines
                     FROM regexp_split_to_table(c.src, chr(10)) WITH ORDINALITY AS l(t, n)
                    WHERE {line_match}) m
             WHERE {source_match}
             ORDER BY c.schema, c.name, c.kind
             LIMIT $2
        """
        try:
            rows = await db.rows(sql, [pattern, limit + 1, schema])
        except psycopg.errors.InvalidRegularExpression as exc:
            raise PgHarborError(
                f"The regular expression {pattern!r} is not valid: {exc.diag.message_primary}. "
                "Escape special characters with a backslash, or use regex=false for plain text."
            ) from None
    rows, has_more = page(rows, limit)
    matches = []
    for row in rows:
        entry: dict[str, Any] = {
            "kind": row["kind"],
            "object": f"{row['schema']}.{row['name']}",
            "matching_lines": row["n_lines"],
        }
        if row["first_lines"]:
            entry["lines"] = row["first_lines"]
        else:
            entry["note"] = "The text spans several lines; read it with pg_get_ddl."
        matches.append(entry)
    out: dict[str, Any] = {
        "connection": db.profile.name,
        "pattern": pattern,
        "matches": matches,
        "has_more": has_more,
    }
    if not matches:
        out["hint"] = (
            "No source code contains it. Only code stored in the database is searched "
            "(functions, procedures, views, materialized views, triggers); tables and "
            "columns are found by pg_search."
        )
    return out


# ------------------------------------------------------------------------------ pg_dependencies

DependencyKind = Literal[
    "table", "view", "materialized_view", "sequence", "index", "function", "type"
]

# OIDs of the system catalogs themselves. They are fixed in every PostgreSQL version and are
# what pg_depend and pg_identify_object use to name the kind of an object.
_CLASS_RELATION = 1259
_CLASS_PROC = 1255
_CLASS_TYPE = 1247
_CLASS_EXTENSION = 3079
_CLASS_REWRITE = 2618
_CLASS_ATTRDEF = 2604
_CLASS_CONSTRAINT = 2606
_CLASS_TRIGGER = 2620
_CLASS_POLICY = 3256
_EXPANDABLE_KINDS = frozenset(
    {
        "table",
        "view",
        "materialized view",
        "sequence",
        "foreign table",
        "function",
        "procedure",
        "aggregate",
        "type",
    }
)
_MAX_DEPENDENCY_ROWS = 200


def _oid_array(values: list[int]) -> str:
    return "{" + ",".join(str(int(v)) for v in values) + "}"


_DEPENDENTS_SQL = """
WITH f(classid, objid) AS (SELECT * FROM unnest($1::oid[], $2::oid[]))
SELECT f.classid::int8 AS fclass, f.objid::int8 AS fobj,
       CASE WHEN w.oid IS NOT NULL THEN 1259 ELSE d.classid::int8 END AS nclass,
       coalesce(w.ev_class, d.objid)::int8 AS nobj,
       CASE WHEN w.oid IS NOT NULL OR d.classid = 'pg_catalog.pg_class'::regclass THEN 0
            ELSE d.objsubid END AS nsub,
       array_agg(DISTINCT d.deptype::text) AS deptypes,
       array_remove(array_agg(DISTINCT a.attname::text), NULL) AS columns
  FROM pg_catalog.pg_depend d
  JOIN f ON d.refclassid = f.classid AND d.refobjid = f.objid
  LEFT JOIN pg_catalog.pg_rewrite w
         ON d.classid = 'pg_catalog.pg_rewrite'::regclass AND w.oid = d.objid
  LEFT JOIN pg_catalog.pg_attribute a
         ON f.classid = 1259 AND a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
        AND d.refobjsubid > 0
 WHERE d.deptype IN ('n', 'a', 'i')
 GROUP BY 1, 2, 3, 4, 5
"""

_DEPENDENCIES_SQL = """
WITH f(classid, objid) AS (SELECT * FROM unnest($1::oid[], $2::oid[])),
src(fclass, fobj, sclass, sobj) AS (
    SELECT f.classid, f.objid, f.classid, f.objid FROM f
    UNION ALL SELECT f.classid, f.objid, 'pg_catalog.pg_rewrite'::regclass::oid, w.oid
                FROM f JOIN pg_catalog.pg_rewrite w ON f.classid = 1259 AND w.ev_class = f.objid
    UNION ALL SELECT f.classid, f.objid, 'pg_catalog.pg_attrdef'::regclass::oid, ad.oid
                FROM f JOIN pg_catalog.pg_attrdef ad ON f.classid = 1259 AND ad.adrelid = f.objid
    UNION ALL SELECT f.classid, f.objid, 'pg_catalog.pg_constraint'::regclass::oid, con.oid
                FROM f JOIN pg_catalog.pg_constraint con
                  ON (f.classid = 1259 AND con.conrelid = f.objid)
                  OR (f.classid = 1247 AND con.contypid = f.objid)
    UNION ALL SELECT f.classid, f.objid, 'pg_catalog.pg_trigger'::regclass::oid, t.oid
                FROM f JOIN pg_catalog.pg_trigger t
                  ON f.classid = 1259 AND t.tgrelid = f.objid AND NOT t.tgisinternal
    UNION ALL SELECT f.classid, f.objid, 'pg_catalog.pg_policy'::regclass::oid, pol.oid
                FROM f JOIN pg_catalog.pg_policy pol ON f.classid = 1259 AND pol.polrelid = f.objid
)
SELECT s.fclass::int8 AS fclass, s.fobj::int8 AS fobj, s.sclass::int8 AS sclass,
       s.sobj::int8 AS sobj, d.objsubid AS ssub,
       d.refclassid::int8 AS nclass, d.refobjid::int8 AS nobj,
       array_agg(DISTINCT d.deptype::text) AS deptypes
  FROM src s
  JOIN pg_catalog.pg_depend d ON d.classid = s.sclass AND d.objid = s.sobj
 WHERE (d.deptype IN ('n', 'a') AND d.refclassid IN (1259, 1247, 1255))
    OR (d.deptype = 'e' AND d.refclassid = 3079 AND s.sclass = s.fclass)
 GROUP BY 1, 2, 3, 4, 5, 6, 7
"""

_IDENTIFY_SQL = """
SELECT u.i::int AS i, id.type, id.schema, id.name, id.identity, c.relkind::text AS relkind,
       c.reltype::int8 AS reltype
  FROM unnest($1::oid[], $2::oid[], $3::int4[]) WITH ORDINALITY AS u(c, o, s, i)
 CROSS JOIN LATERAL pg_catalog.pg_identify_object(u.c, u.o, u.s) AS id
  LEFT JOIN pg_catalog.pg_class c ON u.c = 1259 AND c.oid = u.o
"""


async def _identify(db: Db, keys: list[tuple[int, int, int]]) -> list[dict[str, Any]]:
    if not keys:
        return []
    rows = await db.rows(
        _IDENTIFY_SQL,
        [
            _oid_array([k[0] for k in keys]),
            _oid_array([k[1] for k in keys]),
            _oid_array([k[2] for k in keys]),
        ],
    )
    return sorted(rows, key=lambda r: r["i"])


def _effect(deptypes: set[str]) -> str:
    if "n" in deptypes:
        return "needs CASCADE"
    return "dropped with it"


async def _walk(
    db: Db,
    roots: list[tuple[int, int]],
    aliases: dict[tuple[int, int], str],
    *,
    dependents: bool,
    max_depth: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Breadth-first walk over pg_depend. Returns the rows and whether they were cut."""
    seen: set[tuple[int, int]] = set(roots)
    frontier = list(roots)
    out: list[dict[str, Any]] = []
    for depth in range(1, max_depth + 1):
        if not frontier:
            break
        raw = await db.rows(
            _DEPENDENTS_SQL if dependents else _DEPENDENCIES_SQL,
            [_oid_array([c for c, _ in frontier]), _oid_array([o for _, o in frontier])],
        )
        candidates: dict[tuple[int, int, int], dict[str, Any]] = {}
        for row in raw:
            key = (row["nclass"], row["nobj"], row.get("nsub", 0) if dependents else 0)
            if (key[0], key[1]) in seen:
                continue
            entry = candidates.setdefault(
                key,
                {
                    "parent": aliases.get((row["fclass"], row["fobj"]), "?"),
                    "deptypes": set(),
                    "columns": set(),
                    "via": None,
                },
            )
            entry["deptypes"] |= set(row["deptypes"])
            entry["columns"] |= set(row.get("columns") or [])
            if not dependents:
                entry["via_key"] = (row["sclass"], row["sobj"], row["ssub"])
        keys = list(candidates)
        via_keys = sorted({c["via_key"] for c in candidates.values() if "via_key" in c})
        identities = await _identify(db, keys)
        via_text: dict[tuple[int, int, int], str] = {}
        for key, info in zip(via_keys, await _identify(db, via_keys), strict=True):
            via_text[key] = f"{info['type']} {info['identity']}"
        next_frontier: list[tuple[int, int]] = []
        for key, info in zip(keys, sorted(identities, key=lambda r: r["i"]), strict=True):
            entry = candidates[key]
            kind, schema = info["type"], info["schema"]
            if schema in ("pg_toast",) or info["relkind"] == "t":
                continue
            if dependents and entry["deptypes"] == {"i"} and kind in ("type", "trigger"):
                continue
            if not dependents and (
                kind == "index" or schema in ("pg_catalog", "information_schema")
            ):
                continue
            seen.add((key[0], key[1]))
            aliases[(key[0], key[1])] = f"{kind} {info['identity']}"
            node: dict[str, Any] = {
                "depth": depth,
                "kind": kind,
                "object": info["identity"],
            }
            if dependents:
                detail = []
                if entry["columns"]:
                    detail.append("uses " + ", ".join(sorted(entry["columns"])))
                if depth > 1:
                    detail.append("through " + entry["parent"])
                node["detail"] = "; ".join(detail) or None
                node["effect"] = _effect(entry["deptypes"])
            else:
                parts = []
                via = via_text.get(entry.get("via_key", (0, 0, 0)))
                if via and not via.endswith(info["identity"]):
                    parts.append(via)
                if depth > 1:
                    parts.append("through " + entry["parent"])
                node["detail"] = "; ".join(parts) or None
            out.append(node)
            if kind in _EXPANDABLE_KINDS:
                next_frontier.append((key[0], key[1]))
                if key[0] == _CLASS_RELATION and info["reltype"]:
                    next_frontier.append((_CLASS_TYPE, info["reltype"]))
                    aliases[(_CLASS_TYPE, info["reltype"])] = aliases[(key[0], key[1])]
        frontier = next_frontier
        if len(out) > _MAX_DEPENDENCY_ROWS:
            return out[:_MAX_DEPENDENCY_ROWS], True
    return out, False


@tool(group="schema", title="Object dependencies")
async def pg_dependencies(
    name: str,
    kind: DependencyKind = "table",
    connection: str | None = None,
    direction: Literal["both", "dependents", "dependencies"] = "both",
    max_depth: int = 3,
) -> dict[str, Any]:
    """Show what depends on an object and what it depends on, to answer "what breaks if I
    drop or change this?".

    `dependents` are the views, materialized views, foreign keys from other tables,
    functions that take the table's row type, sequences, indexes, triggers and column
    defaults that use the object, followed through views of views up to `max_depth`.
    `dependencies` are the tables, sequences, functions, types and extensions the object
    needs. Each row says whether dropping the object needs CASCADE or removes it along with
    the object. `name` is `name` or `schema.name`; a function needs its signature when it
    has overloads.
    """
    max_depth = clamp(max_depth, 3, 6)
    async with get_app().read(connection) as db:
        if kind in ("function", "type"):
            if kind == "function":
                found = await _find_functions(db, name, "any", what="function")
                if len(found) > 1:
                    options = ", ".join(
                        f"{r['schema']}.{r['name']}({r['identity']})" for r in found
                    )
                    raise PgHarborError(
                        f"{name!r} has several overloads: {options}. Pass one of these signatures."
                    )
                chosen, root_class = found[0], _CLASS_PROC
            else:
                chosen, root_class = await _find_type(db, name), _CLASS_TYPE
            roots = [(root_class, chosen["oid"])]
            label = await _identify(db, [(root_class, chosen["oid"], 0)])
            subject = label[0]["identity"]
            actual_kind = label[0]["type"]
        else:
            relation = await _find_relation(db, name, kind)
            roots = [(_CLASS_RELATION, relation["oid"])]
            reltype = await db.value(
                "SELECT reltype::int8 FROM pg_catalog.pg_class WHERE oid = $1", [relation["oid"]]
            )
            subject = f"{relation['schema']}.{relation['name']}"
            actual_kind = relation_kind_label(relation["kind"])
        aliases = {roots[0]: f"{actual_kind} {subject}"}
        dependent_roots = list(roots)
        if kind not in ("function", "type") and reltype:
            dependent_roots.append((_CLASS_TYPE, reltype))
            aliases[(_CLASS_TYPE, reltype)] = aliases[roots[0]]
        out: dict[str, Any] = {
            "connection": db.profile.name,
            "object": subject,
            "kind": actual_kind,
        }
        notes: list[str] = []
        if direction in ("both", "dependents"):
            rows, cut = await _walk(
                db, dependent_roots, dict(aliases), dependents=True, max_depth=max_depth
            )
            out["dependents"] = grid(rows, ["depth", "kind", "object", "detail", "effect"])
            blocking: dict[str, int] = {}
            together: dict[str, int] = {}
            for row in rows:
                bucket = blocking if row["effect"] == "needs CASCADE" else together
                bucket[row["kind"]] = bucket.get(row["kind"], 0) + 1
            out["drop_impact"] = {
                "blocks_drop_without_cascade": blocking,
                "dropped_with_it": together,
            }
            if cut:
                notes.append(f"Only the first {_MAX_DEPENDENCY_ROWS} dependents are shown.")
        if direction in ("both", "dependencies"):
            rows, cut = await _walk(db, roots, dict(aliases), dependents=False, max_depth=max_depth)
            out["dependencies"] = grid(rows, ["depth", "kind", "object", "detail"])
            if cut:
                notes.append(f"Only the first {_MAX_DEPENDENCY_ROWS} dependencies are shown.")
        if (
            direction in ("both", "dependents")
            and not out["dependents"]["rows"]
            and kind not in ("index",)
        ):
            notes.append("Nothing depends on it: it can be dropped without CASCADE.")
        notes.append(
            "Dependencies recorded by the server only: code inside function bodies, dynamic "
            "SQL and applications are not tracked. pg_search_source finds text in function "
            "bodies."
        )
        out["notes"] = notes
    return out


# -------------------------------------------------------------------------- pg_relationships

_FK_ACTIONS = {
    "a": "NO ACTION",
    "r": "RESTRICT",
    "c": "CASCADE",
    "n": "SET NULL",
    "d": "SET DEFAULT",
}


def _mermaid_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", value) or "_"


def _mermaid(edges: list[dict[str, Any]], columns: list[dict[str, Any]]) -> str:
    schemas = {e["from_schema"] for e in edges} | {e["to_schema"] for e in edges}
    qualify = len(schemas) > 1

    def entity(schema: str, table: str) -> str:
        return _mermaid_name(f"{schema}_{table}" if qualify else table)

    lines = ["erDiagram"]
    by_table: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for column in columns:
        by_table.setdefault((column["schema"], column["table"]), []).append(column)
    for (schema, table), items in sorted(by_table.items()):
        lines.append(f"    {entity(schema, table)} {{")
        for column in items:
            keys = ", ".join(k for k, flag in (("PK", column["pk"]), ("FK", column["fk"])) if flag)
            lines.append(
                f"        {_mermaid_name(column['type'])} {_mermaid_name(column['name'])}"
                + (f" {keys}" if keys else "")
            )
        lines.append("    }")
    for edge in edges:
        left = "||" if edge["required"] else "|o"
        right = "o|" if edge["one_to_one"] else "o{"
        label = "+".join(edge["from_columns"]).replace('"', "'")
        lines.append(
            f"    {entity(edge['to_schema'], edge['to_table'])} {left}--{right} "
            f'{entity(edge["from_schema"], edge["from_table"])} : "{label}"'
        )
    return "\n".join(lines)


@tool(group="schema", title="Foreign key relationships")
async def pg_relationships(
    connection: str | None = None,
    schema: str | None = None,
    table: str | None = None,
    format: Literal["json", "mermaid"] = "json",
    limit: int = 200,
) -> dict[str, Any]:
    """Show the foreign key graph: which table references which, through which columns,
    with ON DELETE / ON UPDATE actions.

    `table` limits it to the relationships that touch one table (both directions);
    `schema` to those that touch a schema; with neither it covers every user schema.
    `format="mermaid"` returns a Mermaid `erDiagram` (entities with their key columns and
    the relationships) that can be pasted into documentation or a Markdown viewer.
    """
    limit = clamp(limit, 200, 2000)
    async with get_app().read(connection) as db:
        table_oid = None
        if table:
            relation = await resolve_relation(db, table)
            table_oid = relation["oid"]
        parent_only = "AND con.conparentid = 0" if db.v >= 110000 else ""
        rows = await db.rows(
            f"""
            SELECT con.conname AS name, ns.nspname AS from_schema, c.relname AS from_table,
                   c.oid::int8 AS from_oid, r.oid::int8 AS to_oid,
                   ARRAY(SELECT a.attname::text FROM unnest(con.conkey) WITH ORDINALITY k(n, o)
                           JOIN pg_catalog.pg_attribute a
                             ON a.attrelid = con.conrelid AND a.attnum = k.n
                          ORDER BY k.o) AS from_columns,
                   rn.nspname AS to_schema, r.relname AS to_table,
                   ARRAY(SELECT a.attname::text FROM unnest(con.confkey) WITH ORDINALITY k(n, o)
                           JOIN pg_catalog.pg_attribute a
                             ON a.attrelid = con.confrelid AND a.attnum = k.n
                          ORDER BY k.o) AS to_columns,
                   con.confdeltype::text AS on_delete, con.confupdtype::text AS on_update,
                   NOT EXISTS (SELECT 1 FROM unnest(con.conkey) k
                                 JOIN pg_catalog.pg_attribute a
                                   ON a.attrelid = con.conrelid AND a.attnum = k
                                WHERE NOT a.attnotnull) AS required,
                   EXISTS (SELECT 1 FROM pg_catalog.pg_index i
                            WHERE i.indrelid = con.conrelid AND i.indisunique AND i.indisvalid
                              AND i.indpred IS NULL
                              AND (SELECT array_agg(x ORDER BY x) FROM unnest(i.indkey::int2[]) x)
                                = (SELECT array_agg(x ORDER BY x) FROM unnest(con.conkey) x))
                       AS one_to_one,
                   NOT con.convalidated AS not_validated
              FROM pg_catalog.pg_constraint con
              JOIN pg_catalog.pg_class c ON c.oid = con.conrelid
              JOIN pg_catalog.pg_namespace ns ON ns.oid = c.relnamespace
              JOIN pg_catalog.pg_class r ON r.oid = con.confrelid
              JOIN pg_catalog.pg_namespace rn ON rn.oid = r.relnamespace
             WHERE con.contype = 'f' {parent_only}
               AND ($1::int8 IS NULL OR con.conrelid = $1 OR con.confrelid = $1)
               AND ($2::text IS NULL OR ns.nspname = $2 OR rn.nspname = $2)
               AND ($2::text IS NOT NULL OR $1::int8 IS NOT NULL
                    OR (ns.nspname NOT IN {_SYSTEM_SCHEMAS} AND ns.nspname !~ '^pg_(toast|temp)'))
             ORDER BY ns.nspname, c.relname, con.conname
             LIMIT $3
            """,
            [table_oid, schema, limit + 1],
        )
        rows, has_more = page(rows, limit)
        columns: list[dict[str, Any]] = []
        if format == "mermaid" and rows:
            oids = sorted({r["from_oid"] for r in rows} | {r["to_oid"] for r in rows})
            fk_filter = "AND f.conparentid = 0" if db.v >= 110000 else ""
            columns = await db.rows(
                f"""
                SELECT * FROM (
                    SELECT a.attrelid::int8 AS oid, n.nspname AS schema, c.relname AS "table",
                           a.attname::text AS name, a.attnum,
                           pg_catalog.format_type(a.atttypid, NULL) AS type,
                           EXISTS (SELECT 1 FROM pg_catalog.pg_constraint p
                                    WHERE p.conrelid = a.attrelid AND p.contype = 'p'
                                      AND a.attnum = ANY(p.conkey)) AS pk,
                           EXISTS (SELECT 1 FROM pg_catalog.pg_constraint f
                                    WHERE f.conrelid = a.attrelid AND f.contype = 'f'
                                      AND a.attnum = ANY(f.conkey) {fk_filter}) AS fk
                      FROM pg_catalog.pg_attribute a
                      JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
                      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                     WHERE a.attrelid = ANY($1::oid[]) AND a.attnum > 0 AND NOT a.attisdropped
                ) q WHERE q.pk OR q.fk ORDER BY q.schema, q.table, q.attnum
                """,
                [_oid_array(oids)],
            )
    out: dict[str, Any] = {"connection": db.profile.name}
    tables = {(r["from_schema"], r["from_table"]) for r in rows} | {
        (r["to_schema"], r["to_table"]) for r in rows
    }
    out["tables"] = len(tables)
    out["relationships"] = len(rows)
    if format == "mermaid":
        out["format"] = "mermaid"
        out["diagram"] = _mermaid(rows, columns)
    else:
        shaped = [
            {
                "constraint": r["name"],
                "from": f"{r['from_schema']}.{r['from_table']}",
                "from_columns": r["from_columns"],
                "to": f"{r['to_schema']}.{r['to_table']}",
                "to_columns": r["to_columns"],
                "on_delete": _FK_ACTIONS.get(r["on_delete"], r["on_delete"]),
                "on_update": _FK_ACTIONS.get(r["on_update"], r["on_update"]),
                "required": r["required"],
            }
            for r in rows
        ]
        out.update(
            grid(
                shaped,
                [
                    "constraint",
                    "from",
                    "from_columns",
                    "to",
                    "to_columns",
                    "on_delete",
                    "on_update",
                    "required",
                ],
            )
        )
    out["has_more"] = has_more
    notes = []
    if has_more:
        notes.append(f"Only the first {limit} relationships are shown; raise `limit` or filter.")
    if not rows:
        notes.append(
            "No foreign keys found"
            + (" for that filter" if schema or table else "")
            + ". Tables can be related without declared constraints; pg_describe_table shows "
            "the declared ones."
        )
    if notes:
        out["notes"] = notes
    return out


# ------------------------------------------------------------------------------- schema overview

_TYPE_SHORT = (
    (re.compile(r"^character varying"), "varchar"),
    (re.compile(r"^character"), "char"),
    (re.compile(r"^timestamp with time zone"), "timestamptz"),
    (re.compile(r"^timestamp without time zone"), "timestamp"),
    (re.compile(r"^time with time zone"), "timetz"),
    (re.compile(r"^time without time zone"), "time"),
    (re.compile(r"^double precision"), "float8"),
    (re.compile(r"^integer"), "int"),
    (re.compile(r"^boolean"), "bool"),
)


def _short_type(type_name: str, schema: str) -> str:
    for pattern, replacement in _TYPE_SHORT:
        type_name = pattern.sub(replacement, type_name)
    return type_name.removeprefix(f"{schema}.")


@tool(group="schema", title="Compact schema overview")
async def pg_schema_overview(
    schema: str,
    connection: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Give a compact map of one schema that costs few tokens: one line per table in the
    form `orders(id bigint PK, customer_id int FK->customers.id, status order_status)`
    with primary keys, foreign key targets, unique columns and the estimated row count,
    then the views (with their columns) and the number of functions, procedures and
    sequences.

    Use it first when you need to write queries against a schema. Partitions are folded
    into their parent. `limit` caps the number of tables. For one table in full detail use
    pg_describe_table.
    """
    limit = clamp(limit, 100, 500)
    async with get_app().read(connection) as db:
        namespace = await db.row(
            "SELECT n.oid::int8 AS oid, "
            "pg_catalog.obj_description(n.oid, 'pg_namespace') AS comment "
            "FROM pg_catalog.pg_namespace n WHERE n.nspname = $1",
            [schema],
        )
        if namespace is None:
            near = await db.rows(
                "SELECT nspname AS name FROM pg_catalog.pg_namespace WHERE nspname ILIKE $1 "
                "AND nspname !~ '^pg_(toast|temp)' ORDER BY 1 LIMIT 8",
                [f"%{schema}%"],
            )
            if not near:
                near = await db.rows(
                    f"SELECT n.nspname AS name FROM pg_catalog.pg_namespace n "
                    f"WHERE {USER_SCHEMA_FILTER} ORDER BY 1 LIMIT 12"
                )
            raise PgHarborError(
                f"No schema named {schema!r} on this connection.{_near_names(near)}"
            )
        partition_key = (
            "CASE WHEN c.relkind = 'p' THEN pg_catalog.pg_get_partkeydef(c.oid) END"
            if db.v >= 100000
            else "NULL::text"
        )
        tables = await db.rows(
            f"""
            SELECT c.oid::int8 AS oid, c.relname AS name, c.relkind::text AS relkind,
                   CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END AS rows,
                   (SELECT count(*) FROM pg_catalog.pg_inherits i WHERE i.inhparent = c.oid)
                       AS children,
                   {partition_key} AS partition_key
              FROM pg_catalog.pg_class c
             WHERE c.relnamespace = $1 AND c.relkind IN ('r', 'p', 'f')
               AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_inherits i WHERE i.inhrelid = c.oid)
             ORDER BY c.relname LIMIT $2
            """,
            [namespace["oid"], limit + 1],
        )
        tables, has_more = page(tables, limit)
        oids = [t["oid"] for t in tables]
        columns = (
            await db.rows(
                """SELECT a.attrelid::int8 AS oid, a.attnum::int AS num, a.attname::text AS name,
                          pg_catalog.format_type(a.atttypid, a.atttypmod) AS type
                     FROM pg_catalog.pg_attribute a
                    WHERE a.attrelid = ANY($1::oid[]) AND a.attnum > 0 AND NOT a.attisdropped
                    ORDER BY a.attrelid, a.attnum""",
                [_oid_array(oids)],
            )
            if oids
            else []
        )
        parent_only = "AND con.conparentid = 0" if db.v >= 110000 else ""
        constraints = (
            await db.rows(
                f"""
                SELECT con.conrelid::int8 AS oid, con.contype::text AS type,
                       con.conkey::int4[] AS keys, con.confkey::int4[] AS target_keys,
                       rn.nspname AS target_schema, r.relname AS target_table,
                       ARRAY(SELECT a.attname::text FROM unnest(con.confkey) WITH ORDINALITY k(n, o)
                               JOIN pg_catalog.pg_attribute a
                                 ON a.attrelid = con.confrelid AND a.attnum = k.n
                              ORDER BY k.o) AS target_columns
                  FROM pg_catalog.pg_constraint con
                  LEFT JOIN pg_catalog.pg_class r ON r.oid = con.confrelid
                  LEFT JOIN pg_catalog.pg_namespace rn ON rn.oid = r.relnamespace
                 WHERE con.conrelid = ANY($1::oid[]) AND con.contype IN ('p', 'u', 'f')
                   {parent_only}
                """,
                [_oid_array(oids)],
            )
            if oids
            else []
        )
        # Attribute numbers are needed to match constraint keys with column names.
        numbers = (
            await db.rows(
                """SELECT a.attrelid::int8 AS oid, a.attnum::int AS num, a.attname::text AS name
                     FROM pg_catalog.pg_attribute a
                    WHERE a.attrelid = ANY($1::oid[]) AND a.attnum > 0 AND NOT a.attisdropped""",
                [_oid_array(oids)],
            )
            if oids
            else []
        )
        views = await db.rows(
            """
            SELECT c.relname AS name, c.relkind::text AS relkind,
                   ARRAY(SELECT a.attname::text FROM pg_catalog.pg_attribute a
                          WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
                          ORDER BY a.attnum) AS columns
              FROM pg_catalog.pg_class c
             WHERE c.relnamespace = $1 AND c.relkind IN ('v', 'm') ORDER BY c.relname LIMIT $2
            """,
            [namespace["oid"], limit + 1],
        )
        views, more_views = page(views, limit)
        function_condition = _function_kind_condition(db.v, "function")
        counts = await db.row(
            f"""
            SELECT (SELECT count(*) FROM pg_catalog.pg_proc p
                     WHERE p.pronamespace = $1 AND {function_condition}
                       AND {_not_in_extension("pg_proc", "p")}) AS functions,
                   (SELECT count(*) FROM pg_catalog.pg_proc p
                     WHERE p.pronamespace = $1 AND {_function_kind_condition(db.v, "procedure")})
                       AS procedures,
                   (SELECT count(*) FROM pg_catalog.pg_class c
                     WHERE c.relnamespace = $1 AND c.relkind = 'S') AS sequences
            """,
            [namespace["oid"]],
        )
    assert counts is not None
    name_of = {(n["oid"], n["num"]): n["name"] for n in numbers}
    by_table: dict[int, list[dict[str, Any]]] = {}
    for column in columns:
        by_table.setdefault(column["oid"], []).append(column)
    pk: dict[int, set[str]] = {}
    unique: dict[int, set[str]] = {}
    foreign: dict[tuple[int, str], str] = {}
    for con in constraints:
        names = [name_of.get((con["oid"], k), "?") for k in con["keys"]]
        if con["type"] == "p":
            pk.setdefault(con["oid"], set()).update(names)
        elif con["type"] == "u" and len(names) == 1:
            unique.setdefault(con["oid"], set()).update(names)
        elif con["type"] == "f":
            target = con["target_table"]
            if con["target_schema"] != schema:
                target = f"{con['target_schema']}.{target}"
            for own, theirs in zip(names, con["target_columns"], strict=False):
                foreign[(con["oid"], own)] = f"{target}.{theirs}"
    lines: list[str] = []
    for table in tables:
        parts: list[str] = []
        items = by_table.get(table["oid"], [])
        for column in items[:30]:
            text = f"{column['name']} {_short_type(column['type'], schema)}"
            if column["name"] in pk.get(table["oid"], ()):
                text += " PK"
            if column["name"] in unique.get(table["oid"], ()):
                text += " UQ"
            target = foreign.get((table["oid"], column["name"]))
            if target:
                text += f" FK->{target}"
            parts.append(text)
        if len(items) > 30:
            parts.append(f"...+{len(items) - 30}")
        line = f"{table['name']}({', '.join(parts)})"
        if table["rows"] is not None and table["relkind"] != "f":
            line += f" ~{table['rows']}"
        if table["relkind"] == "f":
            line += " [foreign table]"
        elif table["partition_key"]:
            line += f" [partitioned {table['partition_key']}, {table['children']} partitions]"
        elif table["children"]:
            line += f" [{table['children']} child tables]"
        lines.append(line)
    out: dict[str, Any] = {"connection": db.profile.name, "schema": schema}
    if namespace["comment"]:
        out["comment"] = namespace["comment"]
    out["tables"] = lines
    if views:
        out["views"] = [
            f"{v['name']}({', '.join(v['columns'][:12])}"
            f"{', ...' if len(v['columns']) > 12 else ''})"
            + (" [materialized]" if v["relkind"] == "m" else "")
            for v in views
        ]
    out["functions"] = counts["functions"]
    if counts["procedures"]:
        out["procedures"] = counts["procedures"]
    out["sequences"] = counts["sequences"]
    out["has_more"] = has_more or more_views
    notes = ["Row counts after ~ are planner estimates."]
    if has_more or more_views:
        notes.append(f"Only the first {limit} tables/views are listed; raise `limit`.")
    if counts["functions"] or counts["procedures"]:
        notes.append("pg_list_objects(kind='function') lists the functions.")
    out["notes"] = notes
    return out


# ------------------------------------------------------------------------------ pg_compare_schemas

CompareKind = Literal[
    "tables", "columns", "indexes", "constraints", "views", "functions", "sequences", "types"
]
_COMPARE_ALL: tuple[str, ...] = (
    "tables",
    "columns",
    "indexes",
    "constraints",
    "views",
    "functions",
    "sequences",
    "types",
)
# Categories whose entries belong to a table: when the table itself exists on one side only
# the table is reported and its columns, indexes and constraints are not repeated.
_PER_TABLE = ("columns", "indexes", "constraints")


async def _snapshot(db: Db, schema: str, include: set[str]) -> dict[str, dict[str, dict[str, Any]]]:
    """Describe the objects of one schema as ``{category: {key: {field: value}}}``."""
    exists = await db.value("SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = $1", [schema])
    if not exists:
        raise PgHarborError(
            f"The schema {schema!r} does not exist on connection '{db.profile.name}'. "
            "pg_list_schemas shows the schemas."
        )
    # With the schema first on the search_path, definitions print without the schema name,
    # so two schemas with different names can be compared.
    await db.rows(
        "SELECT pg_catalog.set_config('search_path', "
        "pg_catalog.quote_ident($1) || ', pg_catalog', true)",
        [schema],
    )
    v = db.v
    out: dict[str, dict[str, dict[str, Any]]] = {}

    def keyed(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {r.pop("key"): r for r in rows}

    not_extension = _not_in_extension("pg_class", "c")
    out["tables"] = keyed(
        await db.rows(
            f"""SELECT c.relname AS key,
                       CASE c.relkind WHEN 'p' THEN 'partitioned table' ELSE 'table' END AS kind
                  FROM pg_catalog.pg_class c
                  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = $1 AND c.relkind IN ('r', 'p') AND {not_extension}""",
            [schema],
        )
    )
    if "columns" in include:
        identity = "a.attidentity::text" if v >= 100000 else "''::text"
        generated = "a.attgenerated::text" if v >= 120000 else "''::text"
        out["columns"] = keyed(
            await db.rows(
                f"""SELECT c.relname || '.' || a.attname AS key, c.relname AS "table",
                           pg_catalog.format_type(a.atttypid, a.atttypmod) AS type,
                           NOT a.attnotnull AS nullable,
                           pg_catalog.pg_get_expr(d.adbin, d.adrelid) AS "default",
                           CASE {identity} WHEN 'a' THEN 'identity always'
                                WHEN 'd' THEN 'identity by default' END AS identity,
                           CASE {generated} WHEN 's' THEN 'stored' WHEN 'v' THEN 'virtual' END
                               AS generated
                      FROM pg_catalog.pg_attribute a
                      JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
                      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                      LEFT JOIN pg_catalog.pg_attrdef d
                             ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                     WHERE n.nspname = $1 AND c.relkind IN ('r', 'p') AND a.attnum > 0
                       AND NOT a.attisdropped AND {_not_in_extension("pg_class", "c")}""",
                [schema],
            )
        )
    if "indexes" in include:
        out["indexes"] = keyed(
            await db.rows(
                f"""SELECT ic.relname AS key, t.relname AS "table",
                           pg_catalog.pg_get_indexdef(i.indexrelid) AS definition
                      FROM pg_catalog.pg_index i
                      JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid
                      JOIN pg_catalog.pg_class t ON t.oid = i.indrelid
                      JOIN pg_catalog.pg_namespace n ON n.oid = ic.relnamespace
                     WHERE n.nspname = $1 AND t.relkind IN ('r', 'p', 'm')
                       AND {_not_in_extension("pg_class", "t")}""",
                [schema],
            )
        )
    if "constraints" in include:
        cloned = "AND con.conparentid = 0" if v >= 110000 else ""
        out["constraints"] = keyed(
            await db.rows(
                f"""SELECT c.relname || '.' || con.conname AS key, c.relname AS "table",
                           pg_catalog.pg_get_constraintdef(con.oid, true) AS definition
                      FROM pg_catalog.pg_constraint con
                      JOIN pg_catalog.pg_class c ON c.oid = con.conrelid
                      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                     WHERE n.nspname = $1 AND con.contype IN ('p', 'u', 'f', 'c', 'x')
                       AND con.conislocal {cloned} AND {_not_in_extension("pg_class", "c")}""",
                [schema],
            )
        )
    if "views" in include:
        out["views"] = keyed(
            await db.rows(
                f"""SELECT c.relname AS key,
                           CASE c.relkind WHEN 'm' THEN 'materialized view' ELSE 'view' END AS kind,
                           pg_catalog.pg_get_viewdef(c.oid, true) AS definition
                      FROM pg_catalog.pg_class c
                      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                     WHERE n.nspname = $1 AND c.relkind IN ('v', 'm')
                       AND {_not_in_extension("pg_class", "c")}""",
                [schema],
            )
        )
    if "functions" in include:
        source = "p.prosrc"
        if v >= 140000:
            source = (
                "CASE WHEN p.prosqlbody IS NOT NULL "
                "THEN pg_catalog.pg_get_function_sqlbody(p.oid) ELSE p.prosrc END"
            )
        out["functions"] = keyed(
            await db.rows(
                f"""SELECT p.proname || '(' || pg_catalog.pg_get_function_identity_arguments(p.oid)
                           || ')' AS key,
                           pg_catalog.md5({source}) AS body_md5,
                           pg_catalog.pg_get_function_result(p.oid) AS returns,
                           l.lanname AS language,
                           {_VOLATILITY.format(a="p")} AS volatility,
                           p.prosecdef AS security_definer
                      FROM pg_catalog.pg_proc p
                      JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
                      JOIN pg_catalog.pg_language l ON l.oid = p.prolang
                     WHERE n.nspname = $1 AND {_not_in_extension("pg_proc", "p")}""",
                [schema],
            )
        )
    if "sequences" in include:
        if v >= 100000:
            out["sequences"] = keyed(
                await db.rows(
                    """SELECT c.relname AS key, pg_catalog.format_type(s.seqtypid, NULL) AS type,
                              s.seqstart AS start, s.seqincrement AS increment, s.seqmin AS minimum,
                              s.seqmax AS maximum, s.seqcache AS cache, s.seqcycle AS cycle
                         FROM pg_catalog.pg_class c
                         JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                         JOIN pg_catalog.pg_sequence s ON s.seqrelid = c.oid
                        WHERE n.nspname = $1 AND c.relkind = 'S'""",
                    [schema],
                )
            )
        else:
            out["sequences"] = keyed(
                await db.rows(
                    """SELECT c.relname AS key FROM pg_catalog.pg_class c
                         JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                        WHERE n.nspname = $1 AND c.relkind = 'S'""",
                    [schema],
                )
            )
    if "types" in include:
        out["types"] = keyed(
            await db.rows(
                f"""SELECT t.typname AS key,
                           CASE t.typtype WHEN 'e' THEN 'enum' WHEN 'd' THEN 'domain'
                                WHEN 'c' THEN 'composite' WHEN 'r' THEN 'range' ELSE 'other' END
                               AS kind,
                           CASE t.typtype
                             WHEN 'e' THEN (SELECT string_agg(e.enumlabel::text, ', '
                                                    ORDER BY e.enumsortorder)
                                              FROM pg_catalog.pg_enum e WHERE e.enumtypid = t.oid)
                             WHEN 'd' THEN pg_catalog.format_type(t.typbasetype, t.typtypmod)
                                  || CASE WHEN t.typnotnull THEN ' NOT NULL' ELSE '' END
                                  || coalesce(' DEFAULT ' || t.typdefault, '')
                                  || coalesce((SELECT ' ' || string_agg(
                                         pg_catalog.pg_get_constraintdef(k.oid, true), ' '
                                         ORDER BY k.conname)
                                       FROM pg_catalog.pg_constraint k
                                      WHERE k.contypid = t.oid AND k.contype = 'c'), '')
                             WHEN 'c' THEN (SELECT string_agg(a.attname || ' '
                                         || pg_catalog.format_type(a.atttypid, a.atttypmod), ', '
                                         ORDER BY a.attnum)
                                       FROM pg_catalog.pg_attribute a
                                      WHERE a.attrelid = t.typrelid AND a.attnum > 0
                                        AND NOT a.attisdropped)
                             WHEN 'r' THEN (SELECT pg_catalog.format_type(r.rngsubtype, NULL)
                                              FROM pg_catalog.pg_range r WHERE r.rngtypid = t.oid)
                           END AS definition
                      FROM pg_catalog.pg_type t
                      JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
                     WHERE n.nspname = $1 AND t.typtype IN ('e', 'd', 'r', 'c')
                       AND (t.typrelid = 0 OR (SELECT c.relkind FROM pg_catalog.pg_class c
                                                WHERE c.oid = t.typrelid) = 'c')
                       AND {_not_in_extension("pg_type", "t")}""",
                [schema],
            )
        )
    return out


def _change(source: Any, target: Any) -> Any:
    """How one field differs: a [source, target] pair, or a short line diff for long text."""
    if (
        isinstance(source, str)
        and isinstance(target, str)
        and ("\n" in source or "\n" in target or len(source) > 160 or len(target) > 160)
    ):
        lines = list(
            difflib.unified_diff(source.splitlines(), target.splitlines(), lineterm="", n=0)
        )[2:]
        lines = [line for line in lines if not line.startswith("@@")]
        cut = len(lines) > 12
        return {"diff": [*lines[:12], *(["..."] if cut else [])]}
    return [source, target]


def _compare_category(
    source: dict[str, dict[str, Any]],
    target: dict[str, dict[str, Any]],
    *,
    skip_in_source: set[str] | None = None,
    skip_in_target: set[str] | None = None,
) -> dict[str, Any]:
    """Compare one category. ``skip_*`` hold tables missing on the other side."""
    only_source = sorted(
        k
        for k in source.keys() - target.keys()
        if not (skip_in_source and source[k].get("table") in skip_in_source)
    )
    only_target = sorted(
        k
        for k in target.keys() - source.keys()
        if not (skip_in_target and target[k].get("table") in skip_in_target)
    )
    different = []
    for key in sorted(source.keys() & target.keys()):
        changes = {
            field: _change(source[key].get(field), target[key].get(field))
            for field in source[key].keys() | target[key].keys()
            if source[key].get(field) != target[key].get(field)
        }
        if changes:
            different.append({"object": key, "changes": dict(sorted(changes.items()))})
    result: dict[str, Any] = {}
    if only_source:
        result["only_in_source"] = only_source
    if only_target:
        result["only_in_target"] = only_target
    if different:
        result["different"] = different
    return result


@tool(group="schema", title="Compare two schemas or environments")
async def pg_compare_schemas(
    source_connection: str,
    target_connection: str,
    schema: str,
    target_schema: str | None = None,
    include: list[CompareKind] | None = None,
) -> dict[str, Any]:
    """Compare one schema on two connections (for example development and production, or
    before and after a migration): what exists only in the source, only in the target, and
    what differs.

    Compared: tables, columns (type, nullability, default, identity), indexes, constraints,
    views (definition), functions (signature, language, volatility and a hash of the body),
    sequences and types.

    `schema` is the schema on the source; `target_schema` the schema on the target when it
    has another name. `include` limits the comparison to some of the kinds. Both
    connections are read only, and both follow their approval rules. Nothing is changed.
    """
    wanted = set(include or _COMPARE_ALL)
    target_name = target_schema or schema
    app = get_app()
    same_server = app.profile(source_connection).name == app.profile(target_connection).name
    async with contextlib.AsyncExitStack() as stack:
        source_db = await stack.enter_async_context(app.read(source_connection))
        # One connection compared with itself needs only one transaction (and one pool slot).
        target_db = (
            source_db
            if same_server
            else await stack.enter_async_context(app.read(target_connection))
        )
        source = await _snapshot(source_db, schema, wanted)
        target = await _snapshot(target_db, target_name, wanted)
    missing_in_target = set(source["tables"]) - set(target["tables"])
    missing_in_source = set(target["tables"]) - set(source["tables"])
    differences: dict[str, Any] = {}
    for category in _COMPARE_ALL:
        if category not in wanted:
            continue
        result = _compare_category(
            source[category],
            target[category],
            skip_in_source=missing_in_target if category in _PER_TABLE else None,
            skip_in_target=missing_in_source if category in _PER_TABLE else None,
        )
        if result:
            differences[category] = result
    out: dict[str, Any] = {
        "source": {
            "connection": source_db.profile.name,
            "schema": schema,
            "server": source_db.facts.label,
        },
        "target": {
            "connection": target_db.profile.name,
            "schema": target_name,
            "server": target_db.facts.label,
        },
        "identical": not differences,
        "summary": {
            category: {k: len(v) for k, v in result.items()}
            for category, result in differences.items()
        },
        "differences": differences,
    }
    notes: list[str] = []
    if source_db.facts.major != target_db.facts.major:
        notes.append(
            "The servers run different PostgreSQL versions; definitions printed by the server "
            "(views, indexes, constraints) can differ in formatting without a real change."
        )
    if "sequences" in wanted and min(source_db.v, target_db.v) < 100000:
        notes.append("Sequence parameters are compared only between PostgreSQL 10 and newer.")
    if not differences:
        notes.append(
            "No differences found in: " + ", ".join(k for k in _COMPARE_ALL if k in wanted)
        )
    if notes:
        out["notes"] = notes
    return out


# ---------------------------------------------------------------------------- pg_list_partitions


@tool(group="schema", title="List partitions")
async def pg_list_partitions(table: str, connection: str | None = None) -> dict[str, Any]:
    """Show the partitions of a partitioned table as a tree: each partition with its bounds
    (FOR VALUES ..., DEFAULT), its own partition key when it is partitioned again, estimated
    rows and size on disk, plus the partitioning strategy (range, list, hash) and key.

    On PostgreSQL 9.6, which has no declarative partitioning, it shows the table inheritance
    children and their CHECK constraints instead. A table without partitions or children
    answers with an empty list.
    """
    async with get_app().read(connection) as db:
        relation = await resolve_relation(db, table, kinds="rp")
        oid = relation["oid"]
        bound = "pg_catalog.pg_get_expr(c.relpartbound, c.oid)" if db.v >= 100000 else "NULL::text"

        def partition_key(alias: str) -> str:
            if db.v < 100000:
                return "NULL::text"
            return (
                f"CASE WHEN {alias}.relkind = 'p' "
                f"THEN pg_catalog.pg_get_partkeydef({alias}.oid) END"
            )

        is_partition = "c.relispartition" if db.v >= 100000 else "false"
        rows = await db.rows(
            f"""
            WITH RECURSIVE tree(oid, level, path) AS (
                SELECT $1::oid, 0, ARRAY[]::name[]
                UNION ALL
                SELECT ch.oid, t.level + 1, t.path || ch.relname
                  FROM tree t
                  JOIN pg_catalog.pg_inherits i ON i.inhparent = t.oid
                  JOIN pg_catalog.pg_class ch ON ch.oid = i.inhrelid
                 WHERE t.level < 8
            )
            SELECT t.level, n.nspname || '.' || c.relname AS name, c.relkind::text AS relkind,
                   {is_partition} AS is_partition,
                   coalesce({bound},
                            (SELECT string_agg(pg_catalog.pg_get_constraintdef(con.oid, true), '; ')
                               FROM pg_catalog.pg_constraint con
                              WHERE con.conrelid = c.oid AND con.contype = 'c' AND con.conislocal))
                       AS bound,
                   {partition_key("c")} AS partition_key,
                   CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END AS rows,
                   pg_catalog.pg_total_relation_size(c.oid)::bigint AS bytes
              FROM tree t
              JOIN pg_catalog.pg_class c ON c.oid = t.oid
              JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
             WHERE t.level > 0
             ORDER BY t.path
             LIMIT 501
            """,
            [oid],
        )
        head = await db.row(
            f"SELECT {partition_key('r')} AS partition_key, r.relkind::text AS relkind "
            "FROM pg_catalog.pg_class r WHERE r.oid = $1",
            [oid],
        )
        assert head is not None
        sizes = await _pretty(db, [r["bytes"] for r in rows])
        total = await db.value(
            "SELECT pg_catalog.pg_size_pretty($1::bigint)", [sum(r["bytes"] for r in rows[:500])]
        )
    cut = len(rows) > 500
    rows = rows[:500]
    qualified = f"{relation['schema']}.{relation['name']}"
    out: dict[str, Any] = {"connection": db.profile.name, "table": qualified}
    declarative = head["relkind"] == "p"
    out["mechanism"] = "declarative partitioning" if declarative else "table inheritance"
    if declarative and head["partition_key"]:
        out["partition_key"] = head["partition_key"]
        out["strategy"] = head["partition_key"].split()[0].lower()
    leaves = [r for r in rows if r["relkind"] != "p"]
    out["partition_count"] = len(leaves)
    shaped = [
        {
            "level": r["level"],
            "name": r["name"],
            "kind": "partitioned table"
            if r["relkind"] == "p"
            else "partition"
            if r["is_partition"]
            else "child table",
            "bound": r["bound"],
            "partition_key": r["partition_key"],
            "rows": r["rows"],
            "size": sizes.get(r["bytes"]),
        }
        for r in rows
    ]
    out.update(grid(shaped, ["level", "name", "kind", "bound", "partition_key", "rows", "size"]))
    if rows:
        out["total_size"] = total
    notes: list[str] = []
    if cut:
        notes.append("More than 500 partitions exist; only the first 500 are listed.")
    if db.v < 100000:
        notes.append(
            "PostgreSQL 9.6 has no declarative partitioning: this is the inheritance tree, and "
            "the bounds are the CHECK constraints of each child."
        )
    if not rows:
        notes.append(f"{qualified} is not partitioned and has no child tables.")
    notes.append("Row counts are planner estimates.")
    out["notes"] = notes
    return out
