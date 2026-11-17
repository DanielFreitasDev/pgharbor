"""Catalog-backed checks: is everything this statement can reach safe to run?

:mod:`pgharbor.guard` lists the names a statement mentions. This module asks the server's
own catalog what those names are and refuses the statement when it could:

* call a function with side effects (see :mod:`pgharbor.functions`), directly, through a
  view, through a row-security policy or from inside another function's body;
* read a relation the profile denies (password catalogs by default);
* use a custom operator or cast backed by a volatile function.

Names are looked up **in every schema**, never through ``search_path``: if any function
called ``f`` is unsafe, a call to ``f`` is refused. That is stricter than the server's
name resolution on purpose.

These checks run *before* anything is planned, because planning already executes
IMMUTABLE and STABLE functions.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any

from .config import ConnectionConfig, FunctionPolicy
from .errors import Rejected
from .functions import (
    PLAN_DENY_PATTERN,
    is_always_denied,
    is_denied_when_read_only,
    is_safe_volatile,
)
from .guard import Analysis, name_variants
from .lexer import Kind, LexError, Token, tokenize
from .session import Db

__all__ = ["SECRET_CATALOGS", "Vetting", "relation_denied", "vet_statement"]

_MAX_ROUNDS = 8
_MAX_SOURCE_CHARS = 200_000
_SCANNABLE_LANGUAGES = frozenset({"sql", "plpgsql"})
_SYSTEM_SCHEMAS = frozenset({"pg_catalog", "information_schema"})

# Catalogs that hold credentials or raw data samples. Denied unless the profile sets
# ``expose_secret_catalogs = true``.
SECRET_CATALOGS: frozenset[str] = frozenset(
    {
        "pg_catalog.pg_authid",
        "pg_catalog.pg_shadow",
        "pg_catalog.pg_user_mapping",
        "pg_catalog.pg_user_mappings",
        "pg_catalog.pg_subscription",
        "pg_catalog.pg_largeobject",
        "pg_catalog.pg_statistic",
        "pg_catalog.pg_statistic_ext_data",
    }
)
# Value samples of every column: hidden when the profile protects columns.
_STATISTICS_VIEWS: frozenset[str] = frozenset(
    {"pg_catalog.pg_stats", "pg_catalog.pg_stats_ext", "pg_catalog.pg_stats_ext_exprs"}
)

_PROBE_SQL = """
SELECT json_build_object(
  'functions', (
    SELECT coalesce(json_agg(json_build_object(
             'oid', p.oid::int8,
             'schema', n.nspname,
             'name', p.proname,
             'vol', p.provolatile,
             'lang', l.lanname,
             'rowarg', p.pronargs = 1 AND coalesce((
                 SELECT t.typtype IN ('c', 'p') FROM pg_catalog.pg_type t
                  WHERE t.oid = p.proargtypes[0]), false),
             'src', CASE WHEN n.nspname <> 'pg_catalog' AND l.lanname NOT IN ('c', 'internal')
                         THEN left(p.prosrc, {max_source}) END,
             'srclen', CASE WHEN n.nspname <> 'pg_catalog' AND l.lanname NOT IN ('c', 'internal')
                            THEN length(p.prosrc) END,
             'support', CASE WHEN n.nspname <> 'pg_catalog' THEN (
                 SELECT json_agg(DISTINCT sp.proname)
                   FROM pg_catalog.pg_aggregate a
                   JOIN pg_catalog.pg_proc sp ON sp.oid = ANY (ARRAY[
                        a.aggtransfn::oid, a.aggfinalfn::oid, a.aggcombinefn::oid,
                        a.aggserialfn::oid, a.aggdeserialfn::oid, a.aggmtransfn::oid,
                        a.aggminvtransfn::oid, a.aggmfinalfn::oid])
                  WHERE a.aggfnoid = p.oid) END)), '[]'::json)
      FROM pg_catalog.pg_proc p
      JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
      JOIN pg_catalog.pg_language l ON l.oid = p.prolang
     WHERE p.proname = ANY($1::name[])),
  'relations', (
    WITH RECURSIVE tree(oid) AS (
        SELECT c.oid FROM pg_catalog.pg_class c
         WHERE c.relname = ANY($2::name[]) AND c.relkind IN ('r', 'v', 'm', 'f', 'p')
        UNION
        SELECT d.refobjid
          FROM tree t
          JOIN pg_catalog.pg_class v ON v.oid = t.oid AND v.relkind = 'v'
          JOIN pg_catalog.pg_namespace vn ON vn.oid = v.relnamespace
               AND vn.nspname NOT IN ('pg_catalog', 'information_schema')
          JOIN pg_catalog.pg_rewrite w ON w.ev_class = v.oid
          JOIN pg_catalog.pg_depend d ON d.classid = 'pg_catalog.pg_rewrite'::regclass
               AND d.objid = w.oid AND d.refclassid = 'pg_catalog.pg_class'::regclass
               AND d.refobjid <> v.oid
    )
    SELECT coalesce(json_agg(json_build_object(
             'oid', c.oid::int8,
             'schema', n.nspname,
             'name', c.relname,
             'kind', c.relkind,
             'def', CASE WHEN c.relkind = 'v'
                          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                         THEN pg_catalog.pg_get_viewdef(c.oid) END,
             'policies', CASE WHEN c.relrowsecurity THEN (
                 SELECT string_agg(
                          coalesce(pg_catalog.pg_get_expr(pol.polqual, pol.polrelid), '') || ' , '
                          || coalesce(pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid), ''),
                          ' , ')
                   FROM pg_catalog.pg_policy pol WHERE pol.polrelid = c.oid) END)), '[]'::json)
      FROM tree t
      JOIN pg_catalog.pg_class c ON c.oid = t.oid
      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace),
  'volatile_types', (
    SELECT coalesce(json_agg(DISTINCT t.typname), '[]'::json)
      FROM pg_catalog.pg_type t
      JOIN pg_catalog.pg_namespace tn ON tn.oid = t.typnamespace
     WHERE t.typname = ANY($2::name[]) AND tn.nspname <> 'pg_catalog'
       AND (EXISTS (SELECT 1 FROM pg_catalog.pg_proc p
                     WHERE p.oid = t.typinput AND p.provolatile = 'v')
         OR EXISTS (SELECT 1 FROM pg_catalog.pg_cast ca
                      JOIN pg_catalog.pg_proc p ON p.oid = ca.castfunc
                     WHERE ca.casttarget = t.oid AND p.provolatile = 'v'))),
  'types', (
    WITH RECURSIVE reach(oid) AS (
        -- Types the statement names (a table name is also the name of its row type)
        -- and types in the signatures of the functions it calls...
        SELECT seed.oid
          FROM (SELECT t.oid FROM pg_catalog.pg_type t WHERE t.typname = ANY($2::name[])
                UNION ALL
                SELECT sig.oid
                  FROM pg_catalog.pg_proc p
                 CROSS JOIN LATERAL unnest(p.proargtypes::oid[] || p.prorettype) AS sig(oid)
                 WHERE p.proname = ANY($3::name[])) seed
         -- Looked up one by one: the seeds are few, and a join here makes the planner
         -- read the whole of pg_type on databases with thousands of tables.
         WHERE (SELECT sn.nspname
                  FROM pg_catalog.pg_type st
                  JOIN pg_catalog.pg_namespace sn ON sn.oid = st.typnamespace
                 WHERE st.oid = seed.oid) NOT IN ('pg_catalog', 'information_schema')
        UNION
        -- ...and what those are made of: base of a domain, element of an array,
        -- columns of a row type, subtype of a range. Every branch starts from the
        -- reached type, so that the planner cannot decide to visit all types first.
        SELECT mt.oid
          FROM reach r
         CROSS JOIN LATERAL (
                SELECT t.typbasetype FROM pg_catalog.pg_type t
                 WHERE t.oid = r.oid AND t.typtype = 'd'
                UNION ALL
                SELECT t.typelem FROM pg_catalog.pg_type t
                 WHERE t.oid = r.oid AND t.typcategory = 'A' AND t.typelem <> 0
                UNION ALL
                SELECT a.atttypid
                  FROM pg_catalog.pg_type t
                  JOIN pg_catalog.pg_attribute a ON a.attrelid = t.typrelid
                 WHERE t.oid = r.oid AND t.typrelid <> 0
                   AND a.attnum > 0 AND NOT a.attisdropped
                UNION ALL
                SELECT rg.rngsubtype FROM pg_catalog.pg_range rg WHERE rg.rngtypid = r.oid
              ) AS member(oid)
          JOIN pg_catalog.pg_type mt ON mt.oid = member.oid
          JOIN pg_catalog.pg_namespace mn ON mn.oid = mt.typnamespace
         WHERE mn.nspname NOT IN ('pg_catalog', 'information_schema')
    )
    -- Only the types that carry code written in the database are returned.
    SELECT coalesce(json_agg(json_build_object(
             'oid', coded.oid::int8, 'schema', coded.nspname, 'name', coded.typname,
             'functions', coded.names, 'checks', coded.source)), '[]'::json)
      FROM (
        SELECT r.oid, tn.nspname, t.typname, hooks.names, checks.source
          FROM (SELECT x.oid, (SELECT nullif(xt.typarray, 0) FROM pg_catalog.pg_type xt
                                WHERE xt.oid = x.oid) AS arr
                  FROM reach x OFFSET 0) r
          JOIN pg_catalog.pg_type t ON t.oid = r.oid
          JOIN pg_catalog.pg_namespace tn ON tn.oid = t.typnamespace
         CROSS JOIN LATERAL (
                SELECT json_agg(DISTINCT hp.proname) AS names
                  FROM (SELECT ca.castfunc AS fn FROM pg_catalog.pg_cast ca
                         WHERE ca.castsource IN (r.oid, r.arr) OR ca.casttarget IN (r.oid, r.arr)
                        UNION ALL
                        SELECT o.oprcode::oid FROM pg_catalog.pg_operator o
                         WHERE o.oprleft IN (r.oid, r.arr) OR o.oprright IN (r.oid, r.arr)
                        UNION ALL
                        SELECT ap.amproc::oid FROM pg_catalog.pg_amproc ap
                         WHERE ap.amproclefttype IN (r.oid, r.arr)
                            OR ap.amprocrighttype IN (r.oid, r.arr)
                        UNION ALL
                        SELECT rg.rngcanonical::oid FROM pg_catalog.pg_range rg
                         WHERE rg.rngtypid = r.oid
                        UNION ALL
                        SELECT rg.rngsubdiff::oid FROM pg_catalog.pg_range rg
                         WHERE rg.rngtypid = r.oid) hook
                  JOIN pg_catalog.pg_proc hp ON hp.oid = hook.fn
                  JOIN pg_catalog.pg_language hl ON hl.oid = hp.prolang
                  JOIN pg_catalog.pg_namespace hn ON hn.oid = hp.pronamespace
                 -- Compiled code attached to a type belongs to an extension the owner
                 -- of the database installed; it cannot be read, and its volatility
                 -- label is rarely set. What is followed is code written in the database.
                 WHERE hn.nspname <> 'pg_catalog'
                   AND hl.lanname NOT IN ('c', 'internal')) hooks
         CROSS JOIN LATERAL (
                SELECT string_agg(pg_catalog.pg_get_expr(con.conbin, 0), ' , ') AS source
                  FROM pg_catalog.pg_constraint con
                 WHERE con.contypid = r.oid AND con.conbin IS NOT NULL) checks
      ) coded
     WHERE coded.names IS NOT NULL OR coded.source IS NOT NULL),
  'operators', (
    SELECT coalesce(json_agg(json_build_object(
             'op', o.oprname, 'fn', p.proname, 'schema', pn.nspname,
             'vol', p.provolatile, 'lang', l.lanname)), '[]'::json)
      FROM pg_catalog.pg_operator o
      JOIN pg_catalog.pg_namespace n ON n.oid = o.oprnamespace
      JOIN pg_catalog.pg_proc p ON p.oid = o.oprcode
      JOIN pg_catalog.pg_namespace pn ON pn.oid = p.pronamespace
      JOIN pg_catalog.pg_language l ON l.oid = p.prolang
      LEFT JOIN LATERAL (
            SELECT bool_or(ic.oid IS NOT NULL) AS castable
              FROM pg_catalog.pg_type ot
              JOIN pg_catalog.pg_namespace otn ON otn.oid = ot.typnamespace
              LEFT JOIN pg_catalog.pg_cast ic
                     ON ic.casttarget = ot.oid AND ic.castcontext = 'i'
             WHERE ot.oid IN (o.oprleft, o.oprright)
               AND otn.nspname NOT IN ('pg_catalog', 'information_schema')) own ON true
     WHERE n.nspname <> 'pg_catalog'
       -- Matched by their text: operators over built-in types only. One over a
       -- user-defined type applies where a value of that type exists and is found
       -- through the type (above). The exception is a type that an implicit cast can
       -- produce out of something else: then the text is all there is to go by, and
       -- only code written in the database is followed (compiled code attached to a
       -- type is the extension's own).
       AND (own.castable IS NULL
            OR (own.castable AND l.lanname NOT IN ('c', 'internal'))))
) AS probe
""".replace("{max_source}", str(_MAX_SOURCE_CHARS))


@dataclass(slots=True)
class Vetting:
    """What the catalog told us about the names in a statement."""

    relations: dict[int, dict[str, Any]] = field(default_factory=dict)  # by oid
    functions: dict[int, dict[str, Any]] = field(default_factory=dict)  # by oid
    types: dict[int, dict[str, Any]] = field(default_factory=dict)  # by oid
    rounds: int = 0

    def relation_names(self) -> set[str]:
        return {f"{r['schema']}.{r['name']}" for r in self.relations.values()}


def relation_denied(
    profile: ConnectionConfig, schema: str, name: str, *, configured_only: bool = False
) -> str | None:
    """Return the reason a relation may not be read through free SQL, or None.

    ``configured_only`` is used when judging a query *plan*. A plan shows base tables,
    and PostgreSQL's own views (``pg_roles``, ``pg_stat_activity``...) legitimately read
    ``pg_authid`` while hiding the password. So the built-in list of secret catalogs is
    enforced on the names a statement or a user view mentions, and only the profile's own
    ``deny_relations`` is enforced on plans.
    """
    qualified = f"{schema}.{name}"
    for pattern in profile.deny_relations:
        if fnmatch.fnmatchcase(qualified, pattern) or fnmatch.fnmatchcase(name, pattern):
            return f"{qualified} is listed in deny_relations for this connection"
    if configured_only:
        return None
    if not profile.expose_secret_catalogs and qualified in SECRET_CATALOGS:
        return f"{qualified} holds credentials or raw data samples"
    if qualified in _STATISTICS_VIEWS and any(rule.deny_columns for rule in profile.protect):
        return (
            f"{qualified} shows sample values of every column, including the columns this "
            "connection protects"
        )
    return None


def _matches(patterns: tuple[str, ...], schema: str, name: str) -> bool:
    qualified = f"{schema}.{name}"
    return any(fnmatch.fnmatchcase(qualified, p) or fnmatch.fnmatchcase(name, p) for p in patterns)


def _call_candidates(tokens: list[Token]) -> tuple[set[str], set[str], set[str]]:
    """Return (names in call position, names after a dot, every name) with all variants."""
    calls: set[str] = set()
    attrs: set[str] = set()
    names: set[str] = set()
    last = len(tokens) - 1
    for i, token in enumerate(tokens):
        if not token.is_name:
            continue
        variants = name_variants(token.value, token.kind is Kind.QIDENT)
        names |= variants
        if i < last and tokens[i + 1].is_punct("("):
            calls |= variants
        elif i >= 1 and tokens[i - 1].is_punct("."):
            attrs |= variants
    return calls, attrs, names


async def vet_statement(db: Db, analysis: Analysis, *, read_only: bool) -> Vetting:
    """Check every name in ``analysis`` against the catalog. Raises :class:`Rejected`."""
    profile = db.profile
    permissive = profile.function_policy is FunctionPolicy.PERMISSIVE

    # Fast path, no round trip: names that are never acceptable.
    for call in analysis.calls:
        for name in name_variants(call.name, call.quoted):
            _refuse_by_name(name, read_only=read_only, where="the statement")

    vetting = Vetting()
    calls = set(analysis.call_names)
    attrs = set(analysis.attribute_names)
    names = set(analysis.names)
    checked_calls: set[str] = set()
    checked_names: set[str] = set()
    contexts: dict[str, str] = {}  # name -> where it was found, for messages
    operator_texts = set(analysis.operators)
    operator_catalog: list[dict[str, Any]] = []
    judged_operators: set[tuple[str, str]] = set()
    # Functions written out as calls: the types in their signatures are followed (an
    # argument of a domain type runs its CHECK). Functions reached as the code behind an
    # operator or a cast are judged and read, but their signatures lead nowhere new.
    written_calls = set(calls)

    for round_number in range(1, _MAX_ROUNDS + 1):
        vetting.rounds = round_number
        ask_functions = sorted((calls | attrs) - checked_calls)
        ask_names = sorted(names - checked_names)
        if not ask_functions and not ask_names:
            return vetting
        probe = await db.value(
            _PROBE_SQL, [ask_functions, ask_names, sorted(written_calls & set(ask_functions))]
        )
        checked_calls |= set(ask_functions)
        checked_names |= set(ask_names)

        new_calls: set[str] = set()
        new_names: set[str] = set()
        if round_number == 1:
            operator_catalog = probe["operators"]
            if probe["volatile_types"] and not permissive:
                raise Rejected(
                    "the type "
                    + ", ".join(sorted(probe["volatile_types"]))
                    + " converts values through a VOLATILE function",
                    rule="type-volatile",
                )

        def note(
            label: str,
            found: tuple[set[str], set[str], set[str]],
            into_calls: set[str] = new_calls,
            into_names: set[str] = new_names,
        ) -> None:
            found_calls, found_names, found_operators = found
            for item in found_calls | found_names:
                contexts.setdefault(item, label)
            written_calls.update(found_calls)
            into_calls.update(found_calls)
            into_names.update(found_names)
            operator_texts.update(found_operators)

        for function in probe["functions"]:
            oid = function["oid"]
            if oid in vetting.functions:
                continue
            name = function["name"]
            reached_by_call = name in calls
            reached_by_attribute = name in attrs and function["rowarg"]
            if not (reached_by_call or reached_by_attribute):
                continue
            vetting.functions[oid] = function
            where = contexts.get(name, "the statement")
            _judge_function(function, profile, read_only=read_only, where=where)
            # An aggregate runs its support functions; they are judged like direct calls.
            for support in function.get("support") or ():
                contexts.setdefault(support, f"aggregate {function['schema']}.{name}()")
                new_calls.add(support)
            body = _body_to_scan(function, profile, read_only=read_only, where=where)
            if body is not None:
                label = f"function {function['schema']}.{name}()"
                note(
                    label,
                    _scan_text(
                        body,
                        where=label,
                        read_only=read_only,
                        dynamic_sql_in=function if function["lang"] == "plpgsql" else None,
                    ),
                )

        for relation in probe["relations"]:
            oid = relation["oid"]
            if oid in vetting.relations:
                continue
            vetting.relations[oid] = relation
            reason = relation_denied(profile, relation["schema"], relation["name"])
            if reason:
                raise Rejected(
                    reason,
                    rule="relation-denied",
                    hint="Use the dedicated pg_* tools; they return this information "
                    "without the secret parts.",
                )
            label = f"view {relation['schema']}.{relation['name']}"
            for text in (relation.get("def"), relation.get("policies")):
                if text:
                    note(label, _scan_text(text, where=label, read_only=read_only))

        # Code attached to a type runs without being named: the function behind a cast,
        # an operator or an operator class, and the CHECK of a domain (also when a value
        # merely becomes an argument of that type).
        for kind in probe["types"]:
            oid = kind["oid"]
            if oid in vetting.types:
                continue
            vetting.types[oid] = kind
            qualified = f"{kind['schema']}.{kind['name']}"
            for hook in kind.get("functions") or ():
                contexts.setdefault(hook, f"a cast or operator of type {qualified}")
                new_calls.add(hook)
            if kind.get("checks"):
                label = f"the CHECK of domain {qualified}"
                found_calls, _no_relations, found_operators = _scan_text(
                    kind["checks"], where=label, read_only=read_only
                )
                # A CHECK cannot hold a subquery, so its names are not relations.
                note(label, (found_calls, set(), found_operators))

        # A custom operator is a function call in disguise, wherever it was written:
        # in the statement, in a view, in a function body, in a CHECK.
        for function_name in _operator_functions(
            operator_catalog, operator_texts, judged_operators, profile, read_only=read_only
        ):
            contexts.setdefault(function_name, "a custom operator")
            new_calls.add(function_name)

        calls |= new_calls
        names |= new_names

    raise Rejected(
        "the statement reaches views and functions nested too deeply to verify",
        rule="verification-depth",
        hint="Query the underlying tables directly, or add the trusted functions to "
        "allow_functions in the connection profile.",
    )


def _in(where: str) -> str:
    """Where a name was found, for messages. Nothing to add for the statement itself."""
    return "" if where == "the statement" else f" ({where})"


def _refuse_by_name(name: str, *, read_only: bool, where: str) -> None:
    if is_always_denied(name):
        raise Rejected(
            f"{name}() is not allowed{_in(where)}: it can act outside the database or change "
            "the session, even inside a read-only transaction",
            rule="function-denied",
            hint="pgharbor has dedicated tools for what the server knows: pg_activity and "
            "pg_locks for sessions, pg_settings, pg_sizes, pg_server_info.",
        )
    if read_only and is_denied_when_read_only(name):
        raise Rejected(
            f"{name}() has a side effect and this connection is read-only{_in(where)}",
            rule="function-side-effect",
        )


def _judge_function(
    function: dict[str, Any], profile: ConnectionConfig, *, read_only: bool, where: str
) -> None:
    schema, name, volatility = function["schema"], function["name"], function["vol"]
    qualified = f"{schema}.{name}"

    if _matches(profile.deny_functions, schema, name):
        raise Rejected(
            f"{qualified}() is listed in deny_functions for this connection{_in(where)}",
            rule="function-denied-by-config",
        )
    _refuse_by_name(name, read_only=read_only, where=where)
    if _matches(profile.allow_functions, schema, name):
        return
    if not read_only or profile.function_policy is FunctionPolicy.PERMISSIVE:
        return

    builtin = schema == "pg_catalog"
    if builtin:
        if volatility == "v" and not is_safe_volatile(name):
            raise Rejected(
                f"{name}() is a VOLATILE built-in function that is not on the safe list"
                f"{_in(where)}",
                rule="function-volatile",
            )
        return

    if profile.function_policy is FunctionPolicy.STRICT:
        raise Rejected(
            f'{qualified}() is not a built-in function and function_policy is "strict"{_in(where)}',
            rule="function-not-allowed",
            hint=f'Add "{qualified}" to allow_functions in the connection profile if it is safe.',
        )
    if volatility == "v" and not is_safe_volatile(name, builtin=False):
        raise Rejected(
            f"{qualified}() is declared VOLATILE, so it may change data or state, and this "
            f"connection is read-only{_in(where)}",
            rule="function-volatile",
            hint=f'If it only reads, add "{qualified}" to allow_functions in the '
            "connection profile.",
        )


def _body_to_scan(
    function: dict[str, Any], profile: ConnectionConfig, *, read_only: bool, where: str
) -> str | None:
    """Return the source to inspect, or None when the function needs no inspection."""
    if function["schema"] in _SYSTEM_SCHEMAS or function["lang"] in ("c", "internal"):
        return None
    if not read_only or profile.function_policy is not FunctionPolicy.STANDARD:
        return None
    if _matches(profile.allow_functions, function["schema"], function["name"]):
        return None
    qualified = f"{function['schema']}.{function['name']}"
    hint = f'If it is safe, add "{qualified}" to allow_functions in the connection profile.'
    if function["lang"] not in _SCANNABLE_LANGUAGES:
        raise Rejected(
            f"{qualified}() is written in {function['lang']}, which pgharbor cannot inspect"
            f"{_in(where)}",
            rule="function-unverifiable",
            hint=hint,
        )
    if (function.get("srclen") or 0) > _MAX_SOURCE_CHARS:
        raise Rejected(
            f"{qualified}() is too long to inspect{_in(where)}",
            rule="function-unverifiable",
            hint=hint,
        )
    return function.get("src") or ""


def _scan_text(
    text: str,
    *,
    where: str,
    read_only: bool,
    dynamic_sql_in: dict[str, Any] | None = None,
) -> tuple[set[str], set[str], set[str]]:
    """Scan a view definition, policy expression, CHECK or function body for what it can
    reach: the names in call position, every name, and the operators it uses.

    ``dynamic_sql_in`` is the PL/pgSQL function whose body this is. Such a body can build
    a statement at run time (``EXECUTE``), and then nothing read here says what it will
    reach: not the tables, not the columns, not the functions. No pattern over its
    strings can tell either, since the text may be glued from pieces or come in an
    argument. So it is refused as unverifiable, like a language pgharbor cannot read.
    """
    try:
        tokens = tokenize(text)
    except LexError as exc:
        raise Rejected(
            f"{where} could not be inspected ({exc.reason})",
            rule="function-unverifiable",
            hint="Add it to allow_functions in the connection profile if it is safe.",
        ) from exc
    calls, _attrs, names = _call_candidates(tokens)
    for name in calls:
        _refuse_by_name(name, read_only=read_only, where=f"called inside {where}")
    # Dynamic SQL: a denied call hidden in a string that the body may EXECUTE.
    for token in tokens:
        if token.kind is Kind.STRING:
            hit = PLAN_DENY_PATTERN.search(token.text.lower())
            if hit:
                raise Rejected(
                    f"{where} builds SQL that calls {hit.group(1)}()",
                    rule="function-denied",
                )
    if dynamic_sql_in is not None and any(token.is_word("execute") for token in tokens):
        qualified = f"{dynamic_sql_in['schema']}.{dynamic_sql_in['name']}"
        raise Rejected(
            f"{where} builds SQL at run time (EXECUTE), so pgharbor cannot tell what it "
            "reads or calls",
            rule="function-unverifiable",
            hint=f'If everything it can run is safe, add "{qualified}" to allow_functions '
            "in the connection profile.",
        )
    return calls, names, {token.text for token in tokens if token.kind is Kind.OP}


def _operator_functions(
    catalog: list[dict[str, Any]],
    used: set[str],
    judged: set[tuple[str, str]],
    profile: ConnectionConfig,
    *,
    read_only: bool,
) -> set[str]:
    """Judge the custom operators that the texts seen so far may be using; return the
    functions behind them that still have to be read (those written in the database).

    Operators are matched by text: one defined under a common name (``=``, ``&&``) is
    considered used whenever that text appears. That can only make the check stricter.
    ``judged`` remembers what earlier rounds already handled.
    """
    permissive = profile.function_policy is FunctionPolicy.PERMISSIVE
    to_read: set[str] = set()
    for operator in catalog:
        key = (operator["op"], operator["fn"])
        if key in judged or not any(operator["op"] in text for text in used):
            continue
        judged.add(key)
        if operator["vol"] == "v" and not permissive:
            raise Rejected(
                f"the operator {operator['op']} is implemented by the VOLATILE function "
                f"{operator['fn']}()",
                rule="operator-volatile",
            )
        if operator["lang"] in ("c", "internal"):
            # Nothing to read: the catalog row already says all there is to judge.
            _judge_function(
                {"schema": operator["schema"], "name": operator["fn"], "vol": operator["vol"]},
                profile,
                read_only=read_only,
                where=f"the operator {operator['op']}",
            )
        else:
            to_read.add(operator["fn"])
    return to_read
