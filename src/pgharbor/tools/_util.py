"""Helpers shared by the tool modules."""

from typing import Any

import psycopg

from ..errors import PgHarborError
from ..lexer import Kind, LexError, tokenize
from ..session import Db

__all__ = [
    "USER_SCHEMA_FILTER",
    "attempt",
    "clamp",
    "grid",
    "like_pattern",
    "page",
    "relation_kind_label",
    "resolve_relation",
    "split_qualified",
]

# SQL condition selecting schemas that belong to the user, for an alias ``n`` of
# pg_namespace. Temporary and TOAST schemas are never interesting.
USER_SCHEMA_FILTER = (
    "n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname !~ '^pg_(toast|temp)'"
)

_RELATION_KINDS = {
    "r": "table",
    "p": "partitioned table",
    "v": "view",
    "m": "materialized view",
    "f": "foreign table",
    "S": "sequence",
    "i": "index",
    "I": "partitioned index",
    "c": "composite type",
    "t": "toast table",
}


def relation_kind_label(relkind: str) -> str:
    return _RELATION_KINDS.get(relkind, relkind)


def clamp(value: int | None, default: int, maximum: int) -> int:
    """A limit between 1 and ``maximum``; ``default`` when not given."""
    if value is None:
        return default
    return max(1, min(int(value), maximum))


def page(rows: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], bool]:
    """Queries fetch ``limit + 1`` rows; return the first ``limit`` and whether more exist."""
    return rows[:limit], len(rows) > limit


def grid(rows: list[dict[str, Any]], columns: list[str] | None = None) -> dict[str, Any]:
    """Compact tabular form: column names once, then one array per row."""
    if columns is None:
        columns = list(rows[0]) if rows else []
    return {"columns": columns, "rows": [[row.get(c) for c in columns] for row in rows]}


def like_pattern(pattern: str | None) -> str | None:
    """Accept shell wildcards (``*``, ``?``) as well as SQL ones (``%``, ``_``).

    A pattern without any wildcard matches names that *contain* it.
    """
    if pattern is None or pattern == "":
        return None
    if not any(ch in pattern for ch in "*?%"):
        return f"%{pattern}%"
    return pattern.replace("*", "%").replace("?", "_")


def split_qualified(name: str) -> tuple[str | None, str]:
    """Split ``schema.name`` (either part may be double-quoted) into its two parts.

    Unquoted parts are lowered, as PostgreSQL does.
    """
    try:
        tokens = tokenize(name)
    except LexError as exc:
        raise PgHarborError(f"Invalid object name {name!r}: {exc.reason}") from exc
    parts = [t for t in tokens if t.kind in (Kind.IDENT, Kind.QIDENT)]
    dots = [t for t in tokens if t.is_punct(".")]
    if len(parts) == 1 and not dots and len(tokens) == 1:
        return None, parts[0].value
    if len(parts) == 2 and len(dots) == 1 and len(tokens) == 3:
        return parts[0].value, parts[1].value
    raise PgHarborError(
        f"Invalid object name {name!r}. Use name or schema.name; quote parts that contain "
        'upper case or special characters, as in "MySchema"."My Table".'
    )


async def attempt(
    db: Db, notes: list[str], what: str, sql: str, params: list[Any] | None = None
) -> list[dict[str, Any]] | None:
    """Run one part of an answer so that its failure costs only that part.

    Tools that assemble an answer from many catalog queries (an audit, a health check)
    use this for each of them: when the server refuses one (missing privilege, a view
    that does not exist in this version), the reason goes into ``notes`` and ``None`` is
    returned, and the tool carries on with the rest. A savepoint keeps the transaction
    usable after the error.

    A statement timeout is not swallowed: grinding through twenty more parts at thirty
    seconds each would be worse than stopping.
    """
    await db.rows("SAVEPOINT pgharbor_part")
    try:
        rows = await db.rows(sql, params or [])
    except psycopg.errors.QueryCanceled:
        raise
    except psycopg.Error as exc:
        await db.rows("ROLLBACK TO SAVEPOINT pgharbor_part")
        await db.rows("RELEASE SAVEPOINT pgharbor_part")
        reason = exc.diag.message_primary or str(exc).splitlines()[0]
        notes.append(f"{what} unavailable: {reason}.")
        return None
    await db.rows("RELEASE SAVEPOINT pgharbor_part")
    return rows


_RESOLVE_SQL = """
SELECT c.oid::int8 AS oid, n.nspname AS schema, c.relname AS name, c.relkind AS kind
  FROM pg_catalog.pg_class c
  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relname = $1
   AND ($2::text IS NULL OR n.nspname = $2)
   AND c.relkind = ANY($3::text[])
   AND n.nspname !~ '^pg_(toast|temp)'
 ORDER BY (n.nspname = ANY(pg_catalog.current_schemas(true))) DESC, n.nspname
"""

_SUGGEST_SQL = """
SELECT n.nspname || '.' || c.relname AS name
  FROM pg_catalog.pg_class c
  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relname ILIKE $1 AND c.relkind = ANY($2::text[])
   AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname !~ '^pg_(toast|temp)'
 ORDER BY length(c.relname), 1
 LIMIT 8
"""


async def resolve_relation(
    db: Db, name: str, kinds: str = "rpvmf", *, what: str = "table"
) -> dict[str, Any]:
    """Find one relation by ``name`` or ``schema.name``.

    Without a schema the name is looked up in every schema; if it exists in more than one
    the caller is asked to choose. ``kinds`` is a string of ``pg_class.relkind`` letters.
    """
    schema, relation = split_qualified(name)
    found = await db.rows(_RESOLVE_SQL, [relation, schema, list(kinds)])
    if schema is None:
        # A user table hides a catalog of the same name; several user tables are ambiguous.
        user = [r for r in found if r["schema"] not in ("pg_catalog", "information_schema")]
        if len(user) > 1:
            options = ", ".join(f"{r['schema']}.{r['name']}" for r in user)
            raise PgHarborError(
                f"The name {relation!r} exists in several schemas: {options}. "
                "Pass it as schema.name."
            )
        found = user or found
    if not found:
        near = await db.rows(_SUGGEST_SQL, [f"%{relation}%", list(kinds)])
        hint = (
            " Similar names: " + ", ".join(r["name"] for r in near) + "."
            if near
            else " Use pg_search to look for it."
        )
        raise PgHarborError(f"No {what} named {name!r} on this connection.{hint}")
    return found[0]
