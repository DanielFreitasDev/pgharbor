"""Security: roles, privileges, a configuration audit and a hunt for sensitive columns.

Nothing here reads a secret. Password hashes are never selected (the password method is
worked out inside SQL from the first characters of the hash), user mapping options and
subscription connection strings are never touched, and the sensitive-column finder looks
at catalog names, comments and types only, never at table data.
"""

import ipaddress
import itertools
import re
import unicodedata
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import psycopg

from ..app import get_app, tool
from ..errors import PgHarborError
from ..session import Db
from ._util import (
    USER_SCHEMA_FILTER,
    clamp,
    grid,
    like_pattern,
    page,
    resolve_relation,
    split_qualified,
)
from ._util import attempt as _attempt

__all__ = ["pg_find_sensitive_columns", "pg_privileges", "pg_roles", "pg_security_audit"]

_NOT_AN_EXTENSION_MEMBER = """NOT EXISTS (SELECT 1 FROM pg_catalog.pg_depend dep
        WHERE dep.classid = '{catalog}'::regclass AND dep.objid = {alias}.oid
          AND dep.deptype = 'e')"""


def _outside_extensions(catalog: str, alias: str) -> str:
    """SQL condition: the object was not created by an extension script."""
    return _NOT_AN_EXTENSION_MEMBER.format(catalog=catalog, alias=alias)


# ------------------------------------------------------------------------------- pg_roles


@tool(group="security", title="Roles and memberships")
async def pg_roles(
    connection: str | None = None,
    pattern: str | None = None,
    include_system: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    """List database roles (users and groups) with their attributes (superuser, createdb,
    createrole, login, replication, bypassrls), connection limit, password expiry, the
    roles each one belongs to (`member_of`) and its members, plus per-role settings.

    Use it to see who can log in and who holds powerful attributes. Superusers also get
    the password method of each login role (scram-sha-256, md5 or none); the hash itself is
    never returned. `pattern` filters by name (`*` wildcard). Built-in `pg_*` roles are
    hidden unless `include_system` is true. From PostgreSQL 16 memberships show their
    admin/inherit/set options.
    """
    limit = clamp(limit, 100, 500)
    app = get_app()
    notes: list[str] = []
    async with app.read(connection) as db:
        v = db.v
        if v >= 160000:
            flags = (
                "CASE WHEN m.admin_option THEN ' [admin]' ELSE '' END"
                " || CASE WHEN NOT m.inherit_option THEN ' [no inherit]' ELSE '' END"
                " || CASE WHEN NOT m.set_option THEN ' [no set]' ELSE '' END"
            )
            group_flags = ", m.admin_option, m.inherit_option, m.set_option"
        else:
            flags = "CASE WHEN m.admin_option THEN ' [admin]' ELSE '' END"
            group_flags = ", m.admin_option"
        bypass = "r.rolbypassrls" if v >= 90500 else "false"
        rows = await db.rows(
            f"""
            SELECT r.oid::bigint AS oid, r.rolname AS name, r.rolsuper, r.rolinherit,
                   r.rolcreaterole, r.rolcreatedb, r.rolcanlogin, r.rolreplication,
                   {bypass} AS rolbypassrls, r.rolconnlimit,
                   r.rolvaliduntil::text AS valid_until,
                   ARRAY(SELECT g.rolname || {flags}
                           FROM pg_catalog.pg_auth_members m
                           JOIN pg_catalog.pg_roles g ON g.oid = m.roleid
                          WHERE m.member = r.oid
                          GROUP BY g.rolname{group_flags} ORDER BY 1) AS member_of,
                   ARRAY(SELECT u.rolname || {flags}
                           FROM pg_catalog.pg_auth_members m
                           JOIN pg_catalog.pg_roles u ON u.oid = m.member
                          WHERE m.roleid = r.oid
                          GROUP BY u.rolname{group_flags} ORDER BY 1) AS members
              FROM pg_catalog.pg_roles r
             WHERE ($1 OR r.rolname !~ '^pg_')
               AND ($2::text IS NULL OR r.rolname ILIKE $2)
             ORDER BY r.rolname
             LIMIT $3
            """,
            [include_system, like_pattern(pattern), limit + 1],
        )
        rows, has_more = page(rows, limit)
        oids = [r["oid"] for r in rows]
        settings: dict[int, list[str]] = {}
        methods: dict[int, str] = {}
        if oids:
            found = await _attempt(
                db,
                notes,
                "pg_db_role_setting",
                """
                SELECT s.setrole::bigint AS oid,
                       array_agg(CASE WHEN s.setdatabase = 0 THEN c
                                      ELSE '[' || d.datname || '] ' || c END ORDER BY c) AS config
                  FROM pg_catalog.pg_db_role_setting s
                  CROSS JOIN LATERAL unnest(s.setconfig) AS c
                  LEFT JOIN pg_catalog.pg_database d ON d.oid = s.setdatabase
                 WHERE s.setrole::bigint = ANY($1::bigint[])
                   AND (s.setdatabase = 0 OR d.datname = pg_catalog.current_database())
                 GROUP BY s.setrole
                """,
                [oids],
            )
            settings = {r["oid"]: r["config"] for r in found or []}
            if db.facts.is_superuser:
                # Only the first characters of the hash are inspected, and only inside SQL.
                found = await _attempt(
                    db,
                    notes,
                    "pg_authid",
                    """
                    SELECT a.oid::bigint AS oid,
                           CASE WHEN a.rolpassword IS NULL THEN 'none'
                                WHEN left(a.rolpassword, 14) = 'SCRAM-SHA-256$' THEN 'scram-sha-256'
                                WHEN left(a.rolpassword, 3) = 'md5' THEN 'md5'
                                ELSE 'unknown' END AS method
                      FROM pg_catalog.pg_authid a
                     WHERE a.oid::bigint = ANY($1::bigint[]) AND a.rolcanlogin
                    """,
                    [oids],
                )
                methods = {r["oid"]: r["method"] for r in found or []}
            else:
                notes.append(
                    "Password methods (scram-sha-256, md5, none) are visible to superusers only."
                )
    for row in rows:
        oid = row.pop("oid")
        row["attributes"] = [
            label
            for label, on in (
                ("superuser", row["rolsuper"]),
                ("login", row["rolcanlogin"]),
                ("createdb", row["rolcreatedb"]),
                ("createrole", row["rolcreaterole"]),
                ("replication", row["rolreplication"]),
                ("bypassrls", row["rolbypassrls"]),
                ("noinherit", not row["rolinherit"]),
            )
            if on
        ]
        row["connection_limit"] = row["rolconnlimit"] if row["rolconnlimit"] >= 0 else None
        row["password"] = methods.get(oid)
        row["settings"] = settings.get(oid, [])
    columns = [
        "name",
        "attributes",
        "connection_limit",
        "valid_until",
        "password",
        "member_of",
        "members",
        "settings",
    ]
    columns = [c for c in columns if c == "name" or any(r.get(c) not in (None, []) for r in rows)]
    out: dict[str, Any] = {"connection": db.profile.name, "roles": grid(rows, columns)}
    out["has_more"] = has_more
    if not rows:
        out["hint"] = "No role matched. Built-in pg_* roles need include_system=true."
    if notes:
        out["notes"] = notes
    return out


# ------------------------------------------------------------------------- pg_privileges

# For each kind of object: catalog, owner column, ACL column and the letter acldefault()
# uses to rebuild the ACL of an object whose ACL is still the default (NULL).
_ACL_SOURCES = {
    "table": ("pg_catalog.pg_class", "relowner", "relacl", "r"),
    "sequence": ("pg_catalog.pg_class", "relowner", "relacl", "s"),
    "schema": ("pg_catalog.pg_namespace", "nspowner", "nspacl", "n"),
    "function": ("pg_catalog.pg_proc", "proowner", "proacl", "f"),
    "database": ("pg_catalog.pg_database", "datdba", "datacl", "d"),
}

_GRANTEE = "CASE a.grantee WHEN 0 THEN 'PUBLIC' ELSE pg_catalog.pg_get_userbyid(a.grantee) END"


@tool(group="security", title="Who can do what")
async def pg_privileges(
    connection: str | None = None,
    object: str | None = None,
    role: str | None = None,
    kind: Literal["table", "schema", "function", "sequence", "database"] = "table",
    limit: int = 200,
) -> dict[str, Any]:
    """Show access rights, in one of two ways. With `object`: who can do what on that
    table (and its columns), schema, function, sequence or database, owner and PUBLIC
    included. With `role`: everything that role may do (granted directly, through roles it
    inherits, or to PUBLIC), the objects it owns and its default privileges.

    `kind` says what `object` names (default table, views included); a function can be
    `schema.name` or `name(argument types)` when overloaded. Pass `role="public"` to see
    what every role can do. Pass one of `object` and `role`, not both. A superuser
    bypasses all of these checks; the answer says so.
    """
    if (object is None) == (role is None):
        raise PgHarborError(
            "Pass exactly one of `object` (who can access it) or `role` (what that role can "
            "access). pg_roles lists the roles; pg_list_tables lists tables."
        )
    limit = clamp(limit, 200, 1000)
    app = get_app()
    async with app.read(connection) as db:
        if object is not None:
            body = await _object_privileges(db, object, kind)
        else:
            assert role is not None
            body = await _role_privileges(db, role, limit)
    return {"connection": db.profile.name, **body}


async def _function_oid(db: Db, text: str) -> tuple[int, str]:
    if "(" in text:
        await db.rows("SAVEPOINT pgh_part")
        try:
            row = await db.row(
                "SELECT p.oid::bigint AS oid, n.nspname || '.' || p.proname || '(' || "
                "pg_catalog.pg_get_function_identity_arguments(p.oid) || ')' AS signature "
                "FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n "
                "ON n.oid = p.pronamespace WHERE p.oid = pg_catalog.to_regprocedure($1)::oid",
                [text],
            )
        except psycopg.errors.Error as exc:
            await db.rows("ROLLBACK TO SAVEPOINT pgh_part")
            raise PgHarborError(
                f"Could not read {text!r} as a function signature: "
                f"{exc.diag.message_primary}. Use schema.name(type, type)."
            ) from None
        finally:
            await db.rows("RELEASE SAVEPOINT pgh_part")
        if row is None:
            raise PgHarborError(f"No function matches {text!r}. Check the argument types.")
        return row["oid"], row["signature"]
    schema, name = split_qualified(text)
    found = await db.rows(
        """
        SELECT p.oid::bigint AS oid, n.nspname || '.' || p.proname || '(' ||
               pg_catalog.pg_get_function_identity_arguments(p.oid) || ')' AS signature
          FROM pg_catalog.pg_proc p
          JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
         WHERE p.proname = $1 AND ($2::text IS NULL OR n.nspname = $2)
           AND n.nspname !~ '^pg_(toast|temp)'
         ORDER BY (n.nspname = ANY(pg_catalog.current_schemas(true))) DESC, 2
         LIMIT 20
        """,
        [name, schema],
    )
    if not found:
        raise PgHarborError(f"No function named {text!r}. Use pg_search to look for it.")
    if len(found) > 1:
        options = ", ".join(r["signature"] for r in found)
        raise PgHarborError(
            f"{text!r} matches several functions: {options}. Pass one with its argument "
            "types, as in schema.name(integer)."
        )
    return found[0]["oid"], found[0]["signature"]


async def _object_privileges(db: Db, name: str, kind: str) -> dict[str, Any]:
    if kind in ("table", "sequence"):
        relation = await resolve_relation(
            db, name, kinds="rpvmf" if kind == "table" else "S", what=kind
        )
        oid, label = relation["oid"], f"{relation['schema']}.{relation['name']}"
    elif kind == "function":
        oid, label = await _function_oid(db, name)
    else:
        catalog, column = (
            ("pg_catalog.pg_namespace", "nspname")
            if kind == "schema"
            else ("pg_catalog.pg_database", "datname")
        )
        _, plain = split_qualified(name)
        row = await db.row(f"SELECT oid::bigint AS oid FROM {catalog} WHERE {column} = $1", [plain])
        if row is None:
            raise PgHarborError(
                f"No {kind} named {name!r}. "
                + ("pg_list_schemas lists the schemas." if kind == "schema" else "")
            )
        oid, label = row["oid"], plain
    catalog, owner_column, acl_column, letter = _ACL_SOURCES[kind]
    head = await db.row(
        f"""
        SELECT pg_catalog.pg_get_userbyid(o.{owner_column}) AS owner,
               o.{acl_column} IS NULL AS default_acl
          FROM {catalog} o WHERE o.oid = $1
        """,
        [oid],
    )
    assert head is not None
    entries = await db.rows(
        f"""
        SELECT {_GRANTEE} AS grantee, a.privilege_type AS privilege, a.is_grantable AS grantable
          FROM {catalog} o
          CROSS JOIN LATERAL pg_catalog.aclexplode(
              coalesce(o.{acl_column}, pg_catalog.acldefault('{letter}'::"char", o.{owner_column}))
          ) a
         WHERE o.oid = $1
        """,
        [oid],
    )
    out: dict[str, Any] = {"object": label, "kind": kind, "owner": head["owner"]}
    out["privileges"] = _by_grantee(entries, head["owner"])
    if head["default_acl"]:
        out["acl_is_default"] = True
    if kind == "table":
        columns = await db.rows(
            f"""
            SELECT att.attname AS "column", {_GRANTEE} AS grantee, a.privilege_type AS privilege
              FROM pg_catalog.pg_attribute att
              CROSS JOIN LATERAL pg_catalog.aclexplode(att.attacl) a
             WHERE att.attrelid = $1 AND att.attnum > 0 AND NOT att.attisdropped
             ORDER BY att.attnum, 2, 3
            """,
            [oid],
        )
        grouped: dict[tuple[str, str], list[str]] = {}
        for item in columns:
            grouped.setdefault((item["grantee"], item["privilege"]), []).append(item["column"])
        if grouped:
            out["column_privileges"] = grid(
                [{"grantee": g, "privilege": p, "columns": c} for (g, p), c in grouped.items()],
                ["grantee", "privilege", "columns"],
            )
    out["note"] = (
        "Superusers bypass these checks, and a role also holds what the roles it inherits "
        "from hold (pg_privileges with role=... follows memberships)."
    )
    return out


def _by_grantee(entries: list[dict[str, Any]], owner: str) -> dict[str, Any]:
    grouped: dict[str, list[str]] = {}
    for entry in entries:
        text = entry["privilege"] + (" WITH GRANT OPTION" if entry["grantable"] else "")
        bucket = grouped.setdefault(entry["grantee"], [])
        if text not in bucket:
            bucket.append(text)
    rows: list[dict[str, Any]] = [
        {
            "grantee": f"{grantee} (owner)" if grantee == owner else grantee,
            "privileges": sorted(privileges),
        }
        for grantee, privileges in grouped.items()
    ]
    rows.sort(
        key=lambda r: (not r["grantee"].endswith("(owner)"), r["grantee"] != "PUBLIC", r["grantee"])
    )
    return grid(rows, ["grantee", "privileges"])


_USER_NAMESPACE = USER_SCHEMA_FILTER

# The roles whose rights ``$1`` holds: itself plus every role it inherits from.
_CLOSURE_SQL = """
WITH RECURSIVE closure(oid) AS (
    SELECT $1::bigint::oid
  UNION
    SELECT m.roleid
      FROM pg_catalog.pg_auth_members m
      JOIN closure c ON m.member = c.oid
     WHERE {inherits}
)
"""

_ROLE_GRANTS_SQL = (
    _CLOSURE_SQL
    + f""",
grants AS (
    SELECT 1 AS ord,
           CASE c.relkind WHEN 'S' THEN 'sequence' WHEN 'v' THEN 'view'
                WHEN 'm' THEN 'materialized view' WHEN 'f' THEN 'foreign table'
                ELSE 'table' END AS kind,
           n.nspname || '.' || c.relname AS object,
           a.privilege_type, a.is_grantable, a.grantee
      FROM pg_catalog.pg_class c
      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
      CROSS JOIN LATERAL pg_catalog.aclexplode(c.relacl) a
     WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S') AND {_USER_NAMESPACE}
  UNION ALL
    SELECT 2, 'column',
           n.nspname || '.' || c.relname || ' (' ||
               string_agg(att.attname, ', ' ORDER BY att.attnum) || ')',
           a.privilege_type, a.is_grantable, a.grantee
      FROM pg_catalog.pg_attribute att
      JOIN pg_catalog.pg_class c ON c.oid = att.attrelid
      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
      CROSS JOIN LATERAL pg_catalog.aclexplode(att.attacl) a
     WHERE att.attnum > 0 AND NOT att.attisdropped AND {_USER_NAMESPACE}
     GROUP BY n.nspname, c.relname, a.privilege_type, a.is_grantable, a.grantee
  UNION ALL
    SELECT 3, 'schema', n.nspname, a.privilege_type, a.is_grantable, a.grantee
      FROM pg_catalog.pg_namespace n
      CROSS JOIN LATERAL pg_catalog.aclexplode(n.nspacl) a
     WHERE {_USER_NAMESPACE}
  UNION ALL
    SELECT 4, 'function',
           n.nspname || '.' || p.proname || '(' ||
               pg_catalog.pg_get_function_identity_arguments(p.oid) || ')',
           a.privilege_type, a.is_grantable, a.grantee
      FROM pg_catalog.pg_proc p
      JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
      CROSS JOIN LATERAL pg_catalog.aclexplode(p.proacl) a
     WHERE {_USER_NAMESPACE}
  UNION ALL
    SELECT 5, 'database', d.datname, a.privilege_type, a.is_grantable, a.grantee
      FROM pg_catalog.pg_database d
      CROSS JOIN LATERAL pg_catalog.aclexplode(d.datacl) a
)
SELECT g.kind, g.object,
       array_agg(DISTINCT g.privilege_type ||
                 CASE WHEN g.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END) AS privileges,
       CASE WHEN g.grantee = 0 THEN 'PUBLIC'
            WHEN g.grantee = $1::bigint::oid THEN 'direct'
            ELSE 'role ' || pg_catalog.pg_get_userbyid(g.grantee) END AS via
  FROM grants g
 WHERE g.grantee = 0 OR g.grantee IN (SELECT oid FROM closure)
 GROUP BY g.ord, g.kind, g.object, 4
 ORDER BY g.ord, g.object, 4
 LIMIT $2
"""
)

_OWNED_SQL = f"""
SELECT o.kind, o.object, count(*) OVER () AS total
  FROM (
    SELECT CASE c.relkind WHEN 'S' THEN 'sequence' WHEN 'v' THEN 'view'
                WHEN 'm' THEN 'materialized view' WHEN 'f' THEN 'foreign table'
                ELSE 'table' END AS kind,
           n.nspname || '.' || c.relname AS object
      FROM pg_catalog.pg_class c
      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     WHERE c.relowner = $1::bigint::oid AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
       AND {_USER_NAMESPACE}
    UNION ALL
    SELECT 'schema', n.nspname FROM pg_catalog.pg_namespace n
     WHERE n.nspowner = $1::bigint::oid AND {_USER_NAMESPACE}
    UNION ALL
    SELECT 'function', n.nspname || '.' || p.proname || '(' ||
           pg_catalog.pg_get_function_identity_arguments(p.oid) || ')'
      FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
     WHERE p.proowner = $1::bigint::oid AND {_USER_NAMESPACE}
    UNION ALL
    SELECT 'database', d.datname FROM pg_catalog.pg_database d
     WHERE d.datdba = $1::bigint::oid
  ) o
 ORDER BY 1, 2
 LIMIT $2
"""

_DEFAULT_ACL_SQL = f"""
SELECT pg_catalog.pg_get_userbyid(d.defaclrole) AS defined_by,
       coalesce(n.nspname, '(every schema)') AS schema,
       CASE d.defaclobjtype WHEN 'r' THEN 'tables' WHEN 'S' THEN 'sequences'
            WHEN 'f' THEN 'functions' WHEN 'T' THEN 'types' WHEN 'n' THEN 'schemas'
            ELSE d.defaclobjtype::text END AS on_new,
       {_GRANTEE} AS grantee,
       array_agg(DISTINCT a.privilege_type) AS privileges
  FROM pg_catalog.pg_default_acl d
  LEFT JOIN pg_catalog.pg_namespace n ON n.oid = d.defaclnamespace
  CROSS JOIN LATERAL pg_catalog.aclexplode(d.defaclacl) a
 WHERE d.defaclrole = $1::bigint::oid OR a.grantee = $1::bigint::oid
 GROUP BY 1, 2, 3, 4
 ORDER BY 1, 2, 3, 4
 LIMIT 50
"""


async def _role_privileges(db: Db, role: str, limit: int) -> dict[str, Any]:
    public = role.strip().lower() == "public"
    head: dict[str, Any] | None = None
    if not public:
        _, plain = split_qualified(role)
        head = await db.row(
            """
            SELECT r.oid::bigint AS oid, r.rolname AS name, r.rolsuper, r.rolcanlogin
              FROM pg_catalog.pg_roles r WHERE r.rolname = $1
            """,
            [plain],
        )
        if head is None:
            near = await db.rows(
                "SELECT rolname FROM pg_catalog.pg_roles WHERE rolname ILIKE $1 "
                "AND rolname !~ '^pg_' ORDER BY 1 LIMIT 8",
                [f"%{plain}%"],
            )
            hint = (
                " Similar names: " + ", ".join(r["rolname"] for r in near) + "."
                if near
                else " pg_roles lists the roles."
            )
            raise PgHarborError(f"No role named {role!r}.{hint}")
    oid = head["oid"] if head else 0
    inherits = (
        "m.inherit_option"
        if db.v >= 160000
        else "(SELECT r.rolinherit FROM pg_catalog.pg_roles r WHERE r.oid = c.oid)"
    )
    grants = await db.rows(_ROLE_GRANTS_SQL.format(inherits=inherits), [oid, limit + 1])
    grants, has_more = page(grants, limit)
    out: dict[str, Any] = {}
    if head:
        parents = await db.rows(
            _CLOSURE_SQL.format(inherits=inherits)
            + "SELECT r.rolname AS name FROM closure c JOIN pg_catalog.pg_roles r "
            "ON r.oid = c.oid WHERE c.oid <> $1::bigint::oid ORDER BY 1",
            [oid],
        )
        role_info: dict[str, Any] = {"name": head["name"], "login": head["rolcanlogin"]}
        if head["rolsuper"]:
            role_info["superuser"] = True
        if parents:
            role_info["inherits_from"] = [p["name"] for p in parents]
        out["role"] = role_info
        if head["rolsuper"]:
            out["note"] = (
                "This role is a superuser: it bypasses every privilege check, so the grants "
                "below are not what limits it."
            )
    else:
        out["role"] = {"name": "PUBLIC"}
    out["grants"] = grid(grants, ["kind", "object", "privileges", "via"])
    out["grants_has_more"] = has_more
    if head:
        owned = await db.rows(_OWNED_SQL, [oid, 51])
        if owned:
            total = owned[0]["total"]
            out["owns"] = grid(owned[:50], ["kind", "object"])
            if total > 50:
                out["owns_total"] = total
        default_acl = await db.rows(_DEFAULT_ACL_SQL, [oid])
        if default_acl:
            out["default_privileges"] = grid(
                default_acl, ["defined_by", "schema", "on_new", "grantee", "privileges"]
            )
    out.setdefault("notes", []).append(
        "Implicit defaults are not listed: PUBLIC may EXECUTE functions and CONNECT/TEMP on "
        "databases unless revoked, and owners hold all rights on what they own."
    )
    return out


# ------------------------------------------------------------------------ pg_security_audit

_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
_MAX_OBJECTS = 15


def _finding(
    severity: str,
    check: str,
    title: str,
    recommendation: str,
    objects: list[str] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"severity": severity, "check": check, "title": title}
    if objects:
        # Function signatures can run to hundreds of characters; the name is what matters.
        shown = [o if len(o) <= 140 else o[:137] + "..." for o in objects[:_MAX_OBJECTS]]
        if len(objects) > _MAX_OBJECTS:
            shown.append(f"... and {len(objects) - _MAX_OBJECTS} more")
        out["objects"] = shown
    out["recommendation"] = recommendation
    return out


_Check = Callable[[Db, list[str]], Awaitable[list[dict[str, Any]]]]


async def _check_superusers(db: Db, notes: list[str]) -> list[dict[str, Any]]:
    rows = await _attempt(
        db,
        notes,
        "superuser check",
        "SELECT rolname, rolcanlogin FROM pg_catalog.pg_roles WHERE rolsuper ORDER BY 1",
    )
    if not rows:
        return []
    logins = [r["rolname"] for r in rows if r["rolcanlogin"]]
    severity = "warning" if len(logins) > 2 else "info"
    return [
        _finding(
            severity,
            "superusers",
            f"{len(rows)} superuser role(s), {len(logins)} able to log in",
            "Keep superuser for the administrator accounts only. Give applications a role with "
            "just the privileges they need.",
            [f"{r['rolname']}{'' if r['rolcanlogin'] else ' (no login)'}" for r in rows],
        )
    ]


async def _check_role_attributes(db: Db, notes: list[str]) -> list[dict[str, Any]]:
    bypass = "rolbypassrls" if db.v >= 90500 else "false"
    rows = await _attempt(
        db,
        notes,
        "role attribute check",
        f"""
        SELECT rolname, rolcreaterole, rolcreatedb, rolreplication, {bypass} AS rolbypassrls
          FROM pg_catalog.pg_roles
         WHERE NOT rolsuper AND rolname !~ '^pg_'
           AND (rolcreaterole OR rolcreatedb OR rolreplication OR {bypass})
         ORDER BY 1
        """,
    )
    if not rows:
        return []
    spec = (
        (
            "rolcreaterole",
            "CREATEROLE",
            "warning",
            "can create and alter roles, a path to "
            "privilege escalation. Prefer a dedicated administrator role.",
        ),
        (
            "rolreplication",
            "REPLICATION",
            "warning",
            "can stream every change of the cluster. Keep it to replication accounts.",
        ),
        (
            "rolbypassrls",
            "BYPASSRLS",
            "warning",
            "ignores row level security. Remove it unless the role must see every row.",
        ),
        ("rolcreatedb", "CREATEDB", "info", "can create databases."),
    )
    findings = []
    for column, label, severity, advice in spec:
        names = [r["rolname"] for r in rows if r[column]]
        if names:
            findings.append(
                _finding(
                    severity,
                    f"role_{label.lower()}",
                    f"{len(names)} non-superuser role(s) with {label}",
                    f"A role with {label} {advice}"
                    if severity == "warning"
                    else f"{advice} Review whether each one needs it.",
                    names,
                )
            )
    return findings


async def _check_passwords(db: Db, notes: list[str]) -> list[dict[str, Any]]:
    if not db.facts.is_superuser:
        notes.append("Password checks (md5, no password, expired) need a superuser connection.")
        return []
    rows = await _attempt(
        db,
        notes,
        "password check",
        """
        SELECT rolname,
               CASE WHEN rolpassword IS NULL THEN 'none'
                    WHEN left(rolpassword, 14) = 'SCRAM-SHA-256$' THEN 'scram'
                    WHEN left(rolpassword, 3) = 'md5' THEN 'md5'
                    ELSE 'unknown' END AS method,
               coalesce(rolvaliduntil < pg_catalog.now(), false) AS expired
          FROM pg_catalog.pg_authid
         WHERE rolcanlogin ORDER BY 1
        """,
    )
    if not rows:
        return []
    findings = []
    md5 = [r["rolname"] for r in rows if r["method"] == "md5"]
    if md5:
        findings.append(
            _finding(
                "warning" if db.v >= 100000 else "info",
                "password_md5",
                f"{len(md5)} login role(s) with an md5 password hash",
                "md5 hashes are weak and deprecated. Set password_encryption = 'scram-sha-256' "
                "and have each user set the password again (ALTER ROLE ... PASSWORD)."
                if db.v >= 100000
                else "PostgreSQL 9.6 cannot use scram-sha-256; plan an upgrade to 10 or newer.",
                md5,
            )
        )
    none = [r["rolname"] for r in rows if r["method"] == "none"]
    if none:
        findings.append(
            _finding(
                "info",
                "login_without_password",
                f"{len(none)} login role(s) without a stored password",
                "Fine for peer, certificate or external authentication. Otherwise nobody can "
                "log in with it; confirm pg_hba.conf matches the intent.",
                none,
            )
        )
    expired = [r["rolname"] for r in rows if r["expired"]]
    if expired:
        findings.append(
            _finding(
                "info",
                "password_expired",
                f"{len(expired)} login role(s) with an expired password",
                "Renew the validity (ALTER ROLE ... VALID UNTIL) or drop roles nobody uses.",
                expired,
            )
        )
    return findings


async def _check_password_encryption(db: Db, notes: list[str]) -> list[dict[str, Any]]:
    value = await db.value(
        "SELECT setting FROM pg_catalog.pg_settings WHERE name = 'password_encryption'"
    )
    if value not in ("md5", "on", "true", "yes", "1"):
        return []
    if db.v < 100000:
        return [
            _finding(
                "info",
                "password_encryption",
                "password_encryption stores new passwords as md5 (the only choice on 9.6)",
                "Upgrade to PostgreSQL 10 or newer to use scram-sha-256.",
            )
        ]
    return [
        _finding(
            "warning",
            "password_encryption",
            "password_encryption is md5: new passwords are hashed with md5",
            "Set password_encryption = 'scram-sha-256' (postgresql.conf, then reload).",
        )
    ]


async def _check_public_schema(db: Db, notes: list[str]) -> list[dict[str, Any]]:
    rows = await _attempt(
        db,
        notes,
        "schema privilege check",
        f"""
        SELECT n.nspname FROM pg_catalog.pg_namespace n
          CROSS JOIN LATERAL pg_catalog.aclexplode(
              coalesce(n.nspacl, pg_catalog.acldefault('n'::"char", n.nspowner))) a
         WHERE a.grantee = 0 AND a.privilege_type = 'CREATE' AND {USER_SCHEMA_FILTER}
         ORDER BY 1
        """,
    )
    if not rows:
        return []
    names = [r["nspname"] for r in rows]
    return [
        _finding(
            "warning",
            "schema_create_public",
            f"{len(names)} schema(s) where every role may create objects",
            "REVOKE CREATE ON SCHEMA public FROM PUBLIC; (the default since PostgreSQL 15). "
            "Anyone able to create objects there can shadow functions and tables others use.",
            names,
        )
    ]


async def _check_public_grants(db: Db, notes: list[str]) -> list[dict[str, Any]]:
    rows = await _attempt(
        db,
        notes,
        "PUBLIC grants check",
        f"""
        SELECT n.nspname || '.' || c.relname AS object,
               array_agg(DISTINCT a.privilege_type) AS privileges
          FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
          CROSS JOIN LATERAL pg_catalog.aclexplode(c.relacl) a
         WHERE a.grantee = 0 AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
           AND {USER_SCHEMA_FILTER} AND {_outside_extensions("pg_class", "c")}
         GROUP BY 1 ORDER BY 1 LIMIT 200
        """,
    )
    if not rows:
        return []
    writes = {"INSERT", "UPDATE", "DELETE", "TRUNCATE"}
    findings = []
    risky = [r for r in rows if writes & set(r["privileges"])]
    rest = [r for r in rows if not writes & set(r["privileges"])]
    if risky:
        findings.append(
            _finding(
                "critical",
                "public_write",
                f"{len(risky)} relation(s) that every role can modify",
                "REVOKE the write privileges from PUBLIC and grant them to the roles that need "
                "them.",
                [f"{r['object']} ({', '.join(sorted(r['privileges']))})" for r in risky],
            )
        )
    if rest:
        findings.append(
            _finding(
                "warning",
                "public_read",
                f"{len(rest)} relation(s) with privileges granted to PUBLIC",
                "Confirm every role should see these. Otherwise REVOKE from PUBLIC and grant to "
                "specific roles.",
                [f"{r['object']} ({', '.join(sorted(r['privileges']))})" for r in rest],
            )
        )
    return findings


async def _check_security_definer(db: Db, notes: list[str]) -> list[dict[str, Any]]:
    rows = await _attempt(
        db,
        notes,
        "SECURITY DEFINER check",
        f"""
        SELECT n.nspname || '.' || p.proname || '(' ||
                   pg_catalog.pg_get_function_identity_arguments(p.oid) || ')' AS function,
               o.rolsuper AS owner_is_superuser,
               (p.proacl IS NULL OR EXISTS (
                    SELECT 1 FROM pg_catalog.aclexplode(p.proacl) a
                     WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE')) AS public_can_run
          FROM pg_catalog.pg_proc p
          JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
          JOIN pg_catalog.pg_roles o ON o.oid = p.proowner
         WHERE p.prosecdef AND {USER_SCHEMA_FILTER} AND {_outside_extensions("pg_proc", "p")}
           AND NOT EXISTS (SELECT 1 FROM unnest(coalesce(p.proconfig, '{{}}'::text[])) c
                            WHERE c LIKE 'search_path=%')
         ORDER BY 1 LIMIT 200
        """,
    )
    if not rows:
        return []
    findings = []
    worst = [r for r in rows if r["owner_is_superuser"] and r["public_can_run"]]
    rest = [r for r in rows if r not in worst]
    advice = (
        "Add SET search_path = pg_catalog, pg_temp (or a fixed schema) to the function, so a "
        "caller cannot make it run their own objects with the owner's rights."
    )
    if worst:
        findings.append(
            _finding(
                "critical",
                "security_definer_open",
                f"{len(worst)} SECURITY DEFINER function(s) owned by a superuser, open to PUBLIC, "
                "without a fixed search_path",
                advice + " Also REVOKE EXECUTE from PUBLIC.",
                [r["function"] for r in worst],
            )
        )
    if rest:
        findings.append(
            _finding(
                "warning",
                "security_definer_search_path",
                f"{len(rest)} SECURITY DEFINER function(s) without a fixed search_path",
                advice,
                [r["function"] for r in rest],
            )
        )
    return findings


async def _check_row_security(db: Db, notes: list[str]) -> list[dict[str, Any]]:
    rows = await _attempt(
        db,
        notes,
        "row level security check",
        f"""
        SELECT n.nspname || '.' || c.relname AS "table", c.relrowsecurity AS enabled
          FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relkind IN ('r', 'p') AND {USER_SCHEMA_FILTER}
           AND c.relrowsecurity <> EXISTS (SELECT 1 FROM pg_catalog.pg_policy p
                                            WHERE p.polrelid = c.oid)
         ORDER BY 1 LIMIT 200
        """,
    )
    if not rows:
        return []
    findings = []
    no_policy = [r["table"] for r in rows if r["enabled"]]
    not_enforced = [r["table"] for r in rows if not r["enabled"]]
    if not_enforced:
        findings.append(
            _finding(
                "critical",
                "policy_without_rls",
                f"{len(not_enforced)} table(s) have policies but row level security is off",
                "The policies do nothing until you run ALTER TABLE ... ENABLE ROW LEVEL SECURITY.",
                not_enforced,
            )
        )
    if no_policy:
        findings.append(
            _finding(
                "warning",
                "rls_without_policy",
                f"{len(no_policy)} table(s) have row level security on but no policy",
                "With no policy only the owner and superusers see rows. Add the policies the "
                "application needs, or turn the feature off.",
                no_policy,
            )
        )
    return findings


def _cidr(address: str | None, netmask: str | None) -> str:
    """``127.0.0.1`` with mask ``255.255.255.255`` read as ``127.0.0.1/32``."""
    if not address or not netmask:
        return address or ""
    try:
        return str(ipaddress.ip_network(f"{address}/{netmask}", strict=False))
    except ValueError:
        return f"{address}/{netmask}"


def _is_open(address: str | None, netmask: str | None) -> bool:
    if address == "all":
        return True
    # The match-everything address of an IPv4/IPv6 rule, read here, never bound to.
    return address in ("0.0.0.0", "::") and netmask in ("0.0.0.0", "::", None)  # noqa: S104


async def _check_hba(db: Db, notes: list[str]) -> list[dict[str, Any]]:
    if db.v < 100000:
        notes.append("pg_hba rules cannot be read before PostgreSQL 10 (no pg_hba_file_rules).")
        return []
    # The options column is left out on purpose: it can hold LDAP bind passwords.
    rows = await _attempt(
        db,
        notes,
        "pg_hba_file_rules (needs superuser)",
        """
        SELECT line_number, type, array_to_string(database, ',') AS databases,
               array_to_string(user_name, ',') AS users, address, netmask, auth_method, error
          FROM pg_catalog.pg_hba_file_rules ORDER BY line_number
        """,
    )
    if rows is None:
        return []
    buckets: dict[tuple[str, str, str], list[str]] = {}

    def add(severity: str, check: str, title: str, row: dict[str, Any]) -> None:
        where = _cidr(row["address"], row["netmask"])
        text = (
            f"line {row['line_number']}: {row['type']} {row['databases']} {row['users']} "
            f"{where} {row['auth_method']}"
        )
        buckets.setdefault((severity, check, title), []).append(" ".join(text.split()))

    for row in rows:
        if row["error"]:
            add("warning", "hba_error", "pg_hba.conf lines with errors", row)
            continue
        method, open_ = row["auth_method"], _is_open(row["address"], row["netmask"])
        remote = row["type"] != "local"
        if method == "trust":
            if open_ and remote:
                add("critical", "hba_trust_open", "trust authentication open to any address", row)
            else:
                add("warning", "hba_trust", "trust authentication (no password asked)", row)
        elif method == "password":
            if open_ and row["type"] != "hostssl":
                add("critical", "hba_password_open", "cleartext password open to any address", row)
            else:
                add("warning", "hba_password", "cleartext password authentication", row)
        elif method == "md5":
            add("info", "hba_md5", "md5 authentication (weak hash)", row)
    advice = {
        "hba_trust_open": "Replace trust with scram-sha-256 and limit the address range.",
        "hba_trust": "Use peer or scram-sha-256 instead of trust, even for local connections.",
        "hba_password_open": "Use scram-sha-256 over hostssl and restrict the address range.",
        "hba_password": "Use scram-sha-256: 'password' sends the password as clear text.",
        "hba_md5": "Move to scram-sha-256 (PostgreSQL 10+).",
        "hba_error": "Fix the rule; the server ignores lines it cannot parse.",
    }
    return [
        _finding(severity, check, f"{title} ({len(objects)} rule(s))", advice[check], objects)
        for (severity, check, title), objects in buckets.items()
    ]


async def _check_ssl(db: Db, notes: list[str]) -> list[dict[str, Any]]:
    rows = await db.rows(
        "SELECT name, setting FROM pg_catalog.pg_settings WHERE name IN ('ssl', 'listen_addresses')"
    )
    values = {r["name"]: r["setting"] for r in rows}
    if values.get("ssl") != "off":
        return []
    local_only = (values.get("listen_addresses") or "") in ("", "localhost", "127.0.0.1", "::1")
    return [
        _finding(
            "info" if local_only else "warning",
            "ssl_off",
            "ssl is off: connections are not encrypted"
            + (" (the server listens on loopback only)" if local_only else ""),
            "Install a certificate and set ssl = on, then require hostssl rules in pg_hba.conf.",
        )
    ]


async def _check_languages(db: Db, notes: list[str]) -> list[dict[str, Any]]:
    rows = await _attempt(
        db,
        notes,
        "language check",
        "SELECT lanname FROM pg_catalog.pg_language "
        "WHERE NOT lanpltrusted AND lanname NOT IN ('c', 'internal') ORDER BY 1",
    )
    if not rows:
        return []
    return [
        _finding(
            "warning",
            "untrusted_language",
            f"{len(rows)} untrusted procedural language(s) installed",
            "Functions in these languages can read files and run programs as the server's "
            "operating system user. Drop the language if nothing needs it.",
            [r["lanname"] for r in rows],
        )
    ]


async def _check_risky_extensions(db: Db, notes: list[str]) -> list[dict[str, Any]]:
    rows = await _attempt(
        db,
        notes,
        "extension check",
        "SELECT extname FROM pg_catalog.pg_extension "
        "WHERE extname IN ('dblink', 'file_fdw', 'adminpack') ORDER BY 1",
    )
    if not rows:
        return []
    return [
        _finding(
            "info",
            "risky_extensions",
            f"{len(rows)} extension(s) that widen the attack surface",
            "dblink opens connections from the server, file_fdw and adminpack touch the server's "
            "files. Keep them only where needed and limit who may run their functions.",
            [r["extname"] for r in rows],
        )
    ]


async def _check_server_file_roles(db: Db, notes: list[str]) -> list[dict[str, Any]]:
    if db.v < 110000:
        notes.append("pg_*_server_files roles exist from PostgreSQL 11; that check was skipped.")
        return []
    rows = await _attempt(
        db,
        notes,
        "server file role check",
        """
        SELECT g.rolname AS granted, m.rolname AS member
          FROM pg_catalog.pg_auth_members am
          JOIN pg_catalog.pg_roles g ON g.oid = am.roleid
          JOIN pg_catalog.pg_roles m ON m.oid = am.member
         WHERE g.rolname IN ('pg_read_server_files', 'pg_write_server_files',
                             'pg_execute_server_program') AND NOT m.rolsuper
         ORDER BY 1, 2
        """,
    )
    if not rows:
        return []
    findings = []
    for name, severity, power in (
        ("pg_execute_server_program", "critical", "run programs on the server"),
        ("pg_write_server_files", "warning", "write files on the server"),
        ("pg_read_server_files", "warning", "read files on the server"),
    ):
        members = [r["member"] for r in rows if r["granted"] == name]
        if members:
            findings.append(
                _finding(
                    severity,
                    f"member_{name}",
                    f"{len(members)} non-superuser role(s) in {name}",
                    f"These roles can {power} (COPY ... PROGRAM / FILE). REVOKE {name} unless "
                    "it is essential.",
                    members,
                )
            )
    return findings


async def _check_foreign_servers(db: Db, notes: list[str]) -> list[dict[str, Any]]:
    row = await _attempt(
        db,
        notes,
        "foreign server check",
        "SELECT (SELECT count(*) FROM pg_catalog.pg_foreign_server) AS servers, "
        "(SELECT count(*) FROM pg_catalog.pg_user_mappings) AS mappings",
    )
    if not row or not row[0]["servers"]:
        return []
    return [
        _finding(
            "info",
            "foreign_servers",
            f"{row[0]['servers']} foreign server(s) and {row[0]['mappings']} user mapping(s)",
            "User mappings usually hold passwords for other databases. Make sure only trusted "
            "roles can use the servers; their options are never read by pgharbor.",
        )
    ]


async def _check_event_triggers(db: Db, notes: list[str]) -> list[dict[str, Any]]:
    rows = await _attempt(
        db,
        notes,
        "event trigger check",
        "SELECT evtname, evtevent, evtenabled FROM pg_catalog.pg_event_trigger ORDER BY 1",
    )
    if not rows:
        return []
    return [
        _finding(
            "info",
            "event_triggers",
            f"{len(rows)} event trigger(s) run code on DDL commands",
            "Event triggers run with the rights of the role that triggers them. Review their "
            "functions and who owns them.",
            [
                f"{r['evtname']} on {r['evtevent']}"
                + ("" if r["evtenabled"] == "O" else " (not enabled)")
                for r in rows
            ],
        )
    ]


_AUDIT_CHECKS: tuple[_Check, ...] = (
    _check_superusers,
    _check_role_attributes,
    _check_passwords,
    _check_password_encryption,
    _check_public_schema,
    _check_public_grants,
    _check_security_definer,
    _check_row_security,
    _check_hba,
    _check_ssl,
    _check_languages,
    _check_risky_extensions,
    _check_server_file_roles,
    _check_foreign_servers,
    _check_event_triggers,
)


@tool(group="security", title="Security audit")
async def pg_security_audit(connection: str | None = None) -> dict[str, Any]:
    """Audit the database for security weaknesses and rank the findings as critical,
    warning or info, each with a recommendation.

    It checks superusers and powerful role attributes (CREATEROLE, REPLICATION, BYPASSRLS),
    md5 password hashes, PUBLIC privileges and a PUBLIC-writable schema, SECURITY DEFINER
    functions without a fixed search_path, row level security gaps, pg_hba rules with trust or
    cleartext passwords, ssl, untrusted languages, risky extensions, file and program access
    roles, foreign servers and event triggers. Each check runs on its own: one that the
    server or the role cannot do becomes a note and the rest still run. Nothing is changed
    and no secret is read.
    """
    app = get_app()
    notes: list[str] = []
    findings: list[dict[str, Any]] = []
    async with app.read(connection) as db:
        for check in _AUDIT_CHECKS:
            findings += await check(db, notes)
    findings.sort(key=lambda f: _SEVERITY_ORDER[f["severity"]])
    counts = {
        level: sum(1 for f in findings if f["severity"] == level) for level in _SEVERITY_ORDER
    }
    out: dict[str, Any] = {
        "connection": db.profile.name,
        "database": db.facts.database,
        "summary": counts,
        "findings": findings,
    }
    if not findings:
        out["summary_text"] = "No findings."
    if notes:
        out["notes"] = notes
    return out


# ------------------------------------------------------------------ pg_find_sensitive_columns

_CATEGORY_ORDER = (
    "credential",
    "secret",
    "national id",
    "payment card",
    "health",
    "biometric",
    "financial",
    "birth date",
    "email",
    "phone",
    "address",
    "family",
    "network",
    "personal data (comment)",
)

_TOKENS: dict[str, set[str]] = {
    "credential": {"password", "passwd", "pwd", "senha", "passphrase"},
    "secret": {
        "secret",
        "segredo",
        "token",
        "jwt",
        "otp",
        "totp",
        "credential",
        "credentials",
        "credencial",
        "apikey",
        "privatekey",
    },
    "national id": {"cpf", "cnpj", "ssn", "rg", "cnh", "passport", "passaporte", "pis"},
    "payment card": {"cvv", "cvc", "iban"},
    "health": {"diagnosis", "diagnostico", "cid", "prontuario", "medication", "medicamento"},
    "biometric": {"biometric", "biometria", "fingerprint", "facial"},
    "financial": {"salary", "salario", "wage", "income", "renda", "remuneracao", "payroll"},
    "birth date": {"birth", "birthday", "birthdate", "nascimento", "nasc", "dob"},
    "email": {"email", "emails", "mail"},
    "phone": {"phone", "telefone", "fone", "celular", "cellphone", "mobile", "whatsapp", "msisdn"},
    "address": {
        "address",
        "endereco",
        "street",
        "logradouro",
        "cep",
        "zipcode",
        "zip",
        "postal",
        "bairro",
        "rua",
    },
    "network": {"ip", "ipaddr", "ipv4", "ipv6", "latitude", "longitude", "geolocation", "gps"},
}
_PAIRS: dict[str, set[tuple[str, str]]] = {
    "secret": {
        ("api", "key"),
        ("access", "key"),
        ("secret", "key"),
        ("private", "key"),
        ("auth", "key"),
        ("encryption", "key"),
        ("signing", "key"),
        ("chave", "acesso"),
        ("chave", "api"),
        ("chave", "privada"),
        ("chave", "secreta"),
    },
    "national id": {
        ("tax", "id"),
        ("social", "security"),
        ("national", "id"),
        ("titulo", "eleitor"),
        ("documento", "identidade"),
        ("drivers", "license"),
        ("driver", "license"),
    },
    "payment card": {
        ("card", "number"),
        ("cartao", "numero"),
        ("credit", "card"),
        ("cartao", "credito"),
        ("card", "holder"),
        ("cc", "number"),
    },
    "family": {("nome", "mae"), ("nome", "pai"), ("mother", "name"), ("father", "name")},
    "email": {("e", "mail")},
    "network": {("ip", "address")},
}
# Long stems that identify a sensitive column even when the words are glued together
# (apikey, userpassword, telefonecelular).
_STEMS: dict[str, str] = {
    "password": "credential",
    "passwd": "credential",
    "senha": "credential",
    "secret": "secret",
    "segredo": "secret",
    "apikey": "secret",
    "credential": "secret",
    "cnpj": "national id",
    "email": "email",
    "telefone": "phone",
    "celular": "phone",
    "nascimento": "birth date",
    "birthdate": "birth date",
    "salario": "financial",
    "endereco": "address",
    "whatsapp": "phone",
    "passport": "national id",
}
_COMMENT_WORDS = {
    "sensitive",
    "sensivel",
    "pii",
    "lgpd",
    "gdpr",
    "confidential",
    "confidencial",
    "personal",
    "pessoal",
    "sigilo",
    "sigiloso",
}
_ALL_TOKENS = sorted(
    {t for tokens in _TOKENS.values() for t in tokens}
    | {t for pairs in _PAIRS.values() for pair in pairs for t in pair}
    | _COMMENT_WORDS
)
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NETWORK_TYPES = {
    "inet": "IP address",
    "cidr": "network address",
    "macaddr": "MAC address",
    "macaddr8": "MAC address",
}


def _words(text: str) -> list[str]:
    """Lower-case words of an identifier or sentence, without accents, camelCase split."""
    plain = unicodedata.normalize("NFKD", _CAMEL.sub("_", text))
    plain = "".join(ch for ch in plain if not unicodedata.combining(ch)).lower()
    return [w for w in re.split(r"[^a-z0-9]+", plain) if w]


def _match_words(words: list[str]) -> list[tuple[str, str]]:
    """(category, matched text) for every rule the words trigger."""
    found: list[tuple[str, str]] = []
    word_set = set(words)
    for category, tokens in _TOKENS.items():
        for token in sorted(tokens & word_set):
            found.append((category, token))
    pairs = set(itertools.pairwise(words))
    for category, rules in _PAIRS.items():
        for first, second in sorted(rules & pairs):
            found.append((category, f"{first} {second}"))
    return found


def _classify(
    name: str, comment: str | None, type_name: str, custom_type: bool
) -> list[tuple[str, str]]:
    reasons: list[tuple[str, str]] = []
    name_words = _words(name)
    for category, hit in _match_words(name_words):
        reasons.append((category, f"name has '{hit}'"))
    glued = "".join(name_words)
    seen = {category for category, _ in reasons}
    for stem, category in _STEMS.items():
        if category not in seen and stem in glued:
            reasons.append((category, f"name contains '{stem}'"))
            seen.add(category)
    if custom_type:
        for category, hit in _match_words(_words(type_name)):
            if category not in seen:
                reasons.append((category, f"type '{type_name}' has '{hit}'"))
                seen.add(category)
    if type_name in _NETWORK_TYPES and "network" not in seen:
        reasons.append(("network", f"type {type_name} holds an {_NETWORK_TYPES[type_name]}"))
        seen.add("network")
    if comment:
        words = _words(comment)
        for category, hit in _match_words(words):
            if category not in seen:
                reasons.append((category, f"comment mentions '{hit}'"))
                seen.add(category)
        generic = sorted(_COMMENT_WORDS & set(words))
        if generic and not reasons:
            reasons.append(("personal data (comment)", f"comment mentions '{generic[0]}'"))
    return reasons


@tool(group="security", title="Find sensitive columns")
async def pg_find_sensitive_columns(
    connection: str | None = None,
    schema: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Find columns that look like they hold sensitive data, judging by column name, comment
    and type only: passwords, tokens and keys, CPF/CNPJ/SSN, emails, phones, payment cards,
    birth dates, salaries, addresses and similar, in English and Portuguese.

    For each match it gives the table, column, type, category, why it was flagged, whether
    the table has row level security (`rls`) and whether it uses column-level grants
    (`column_grants`). The data itself is never read. Use it to plan masking and access
    control, or before exposing a schema to an application. `schema` narrows the search.
    """
    limit = clamp(limit, 100, 500)
    app = get_app()
    async with app.read(connection) as db:
        rows = await db.rows(
            f"""
            SELECT n.nspname AS schema, c.relname AS "table", a.attname AS name,
                   pg_catalog.format_type(a.atttypid, a.atttypmod) AS type,
                   t.typname AS type_name,
                   t.typnamespace <> 'pg_catalog'::regnamespace AS custom_type,
                   d.description AS comment, c.relrowsecurity AS rls,
                   EXISTS (SELECT 1 FROM pg_catalog.pg_attribute x
                            WHERE x.attrelid = c.oid AND x.attnum > 0
                              AND x.attacl IS NOT NULL) AS column_grants
              FROM pg_catalog.pg_attribute a
              JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
              JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
              JOIN pg_catalog.pg_type t ON t.oid = a.atttypid
              LEFT JOIN pg_catalog.pg_description d
                     ON d.objoid = c.oid AND d.classoid = 'pg_catalog.pg_class'::regclass
                    AND d.objsubid = a.attnum
             WHERE a.attnum > 0 AND NOT a.attisdropped
               AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
               AND ($1::text IS NULL OR n.nspname = $1)
               AND ($1::text IS NOT NULL OR ({USER_SCHEMA_FILTER}))
               AND n.nspname !~ '^pg_(toast|temp)'
               AND {_outside_extensions("pg_class", "c")}
               AND (
                    pg_catalog.regexp_split_to_array(lower(
                        pg_catalog.regexp_replace(a.attname, '([a-z0-9])([A-Z])', '\\1_\\2', 'g')),
                        '[^a-z0-9]+') && $2::text[]
                    OR lower(a.attname) LIKE ANY($3::text[])
                    OR t.typname IN ('inet', 'cidr', 'macaddr', 'macaddr8')
                    OR (t.typnamespace <> 'pg_catalog'::regnamespace
                        AND pg_catalog.regexp_split_to_array(lower(t.typname), '[^a-z0-9]+')
                            && $2::text[])
                    OR pg_catalog.regexp_split_to_array(
                           pg_catalog.translate(lower(coalesce(d.description, '')),
                               'áàâãäéèêëíìîïóòôõöúùûüç', 'aaaaaeeeeiiiiooooouuuuc'),
                           '[^a-z0-9]+') && $2::text[]
                   )
             ORDER BY n.nspname, c.relname, a.attnum
             LIMIT 20001
            """,
            [schema, _ALL_TOKENS, [f"%{stem}%" for stem in _STEMS]],
        )
    scan_cut = len(rows) > 20000
    matches: list[dict[str, Any]] = []
    for row in rows[:20000]:
        reasons = _classify(row["name"], row["comment"], row["type_name"], row["custom_type"])
        if not reasons:
            continue
        reasons.sort(key=lambda r: _CATEGORY_ORDER.index(r[0]))
        matches.append(
            {
                "table": f"{row['schema']}.{row['table']}",
                "column": row["name"],
                "type": row["type"],
                "category": reasons[0][0],
                "why": "; ".join(dict.fromkeys(text for _, text in reasons[:3])),
                "rls": row["rls"],
                "column_grants": row["column_grants"],
            }
        )
    matches.sort(key=lambda m: (_CATEGORY_ORDER.index(m["category"]), m["table"], m["column"]))
    by_category: dict[str, int] = {}
    for match in matches:
        by_category[match["category"]] = by_category.get(match["category"], 0) + 1
    shown, has_more = page(matches, limit)
    out: dict[str, Any] = {
        "connection": db.profile.name,
        "total": len(matches),
        "by_category": by_category,
        **grid(shown, ["table", "column", "type", "category", "why", "rls", "column_grants"]),
        "has_more": has_more,
        "notes": ["Only catalog names, comments and types were examined; no table data was read."],
    }
    if scan_cut:
        out["notes"].append(
            "The catalog has more than 20000 candidate columns; narrow the search with `schema`."
        )
    if not matches:
        out["hint"] = "Nothing looked sensitive by name, comment or type. That is not proof."
    return out
