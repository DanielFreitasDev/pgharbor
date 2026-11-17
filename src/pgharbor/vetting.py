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

_MAX_ROUNDS = 5
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
                            THEN length(p.prosrc) END)), '[]'::json)
      FROM pg_catalog.pg_proc p
      JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
      JOIN pg_catalog.pg_language l ON l.oid = p.prolang
     WHERE p.proname = ANY($1::text[])),
  'relations', (
    WITH RECURSIVE tree(oid) AS (
        SELECT c.oid FROM pg_catalog.pg_class c
         WHERE c.relname = ANY($2::text[]) AND c.relkind IN ('r', 'v', 'm', 'f', 'p')
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
     WHERE t.typname = ANY($2::text[]) AND tn.nspname <> 'pg_catalog'
       AND (EXISTS (SELECT 1 FROM pg_catalog.pg_proc p
                     WHERE p.oid = t.typinput AND p.provolatile = 'v')
         OR EXISTS (SELECT 1 FROM pg_catalog.pg_cast ca
                      JOIN pg_catalog.pg_proc p ON p.oid = ca.castfunc
                     WHERE ca.casttarget = t.oid AND p.provolatile = 'v'))),
  'volatile_operators', (
    SELECT coalesce(json_agg(DISTINCT o.oprname), '[]'::json)
      FROM pg_catalog.pg_operator o
      JOIN pg_catalog.pg_namespace n ON n.oid = o.oprnamespace
      JOIN pg_catalog.pg_proc p ON p.oid = o.oprcode
     WHERE n.nspname <> 'pg_catalog' AND p.provolatile = 'v')
) AS probe
""".replace("{max_source}", str(_MAX_SOURCE_CHARS))


@dataclass(slots=True)
class Vetting:
    """What the catalog told us about the names in a statement."""

    relations: dict[int, dict[str, Any]] = field(default_factory=dict)  # by oid
    functions: dict[int, dict[str, Any]] = field(default_factory=dict)  # by oid
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

    for round_number in range(1, _MAX_ROUNDS + 1):
        vetting.rounds = round_number
        ask_functions = sorted((calls | attrs) - checked_calls)
        ask_names = sorted(names - checked_names)
        if not ask_functions and not ask_names:
            return vetting
        probe = await db.value(_PROBE_SQL, [ask_functions, ask_names])
        checked_calls |= set(ask_functions)
        checked_names |= set(ask_names)

        if round_number == 1 and not permissive:
            _check_operators_and_types(probe, analysis)

        new_calls: set[str] = set()
        new_names: set[str] = set()

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
            body = _body_to_scan(function, profile, read_only=read_only, where=where)
            if body is not None:
                label = f"function {function['schema']}.{name}()"
                found_calls, found_names = _scan_text(
                    body, where=label, read_only=read_only, profile=profile
                )
                for item in found_calls | found_names:
                    contexts.setdefault(item, label)
                new_calls |= found_calls
                new_names |= found_names

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
                if not text:
                    continue
                found_calls, found_names = _scan_text(
                    text, where=label, read_only=read_only, profile=profile
                )
                for item in found_calls | found_names:
                    contexts.setdefault(item, label)
                new_calls |= found_calls
                new_names |= found_names

        calls |= new_calls
        names |= new_names

    raise Rejected(
        "the statement reaches views and functions nested too deeply to verify",
        rule="verification-depth",
        hint="Query the underlying tables directly, or add the trusted functions to "
        "allow_functions in the connection profile.",
    )


def _refuse_by_name(name: str, *, read_only: bool, where: str) -> None:
    if is_always_denied(name):
        raise Rejected(
            f"{name}() is not allowed ({where}): it can act outside the database or change "
            "the session, even inside a read-only transaction",
            rule="function-denied",
            hint="pgharbor has dedicated tools for server information; see pg_server_info.",
        )
    if read_only and is_denied_when_read_only(name):
        raise Rejected(
            f"{name}() has a side effect and this connection is read-only ({where})",
            rule="function-side-effect",
        )


def _judge_function(
    function: dict[str, Any], profile: ConnectionConfig, *, read_only: bool, where: str
) -> None:
    schema, name, volatility = function["schema"], function["name"], function["vol"]
    qualified = f"{schema}.{name}"

    if _matches(profile.deny_functions, schema, name):
        raise Rejected(
            f"{qualified}() is listed in deny_functions for this connection ({where})",
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
                f"{name}() is a VOLATILE built-in function that is not on the safe list ({where})",
                rule="function-volatile",
            )
        return

    if profile.function_policy is FunctionPolicy.STRICT:
        raise Rejected(
            f'{qualified}() is not a built-in function and function_policy is "strict" ({where})',
            rule="function-not-allowed",
            hint=f'Add "{qualified}" to allow_functions in the connection profile if it is safe.',
        )
    if volatility == "v" and not is_safe_volatile(name, builtin=False):
        raise Rejected(
            f"{qualified}() is declared VOLATILE, so it may change data or state, and this "
            f"connection is read-only ({where})",
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
            f"{qualified}() is written in {function['lang']}, which pgharbor cannot inspect "
            f"({where})",
            rule="function-unverifiable",
            hint=hint,
        )
    if (function.get("srclen") or 0) > _MAX_SOURCE_CHARS:
        raise Rejected(
            f"{qualified}() is too long to inspect ({where})",
            rule="function-unverifiable",
            hint=hint,
        )
    return function.get("src") or ""


def _scan_text(
    text: str, *, where: str, read_only: bool, profile: ConnectionConfig
) -> tuple[set[str], set[str]]:
    """Scan a view definition, policy expression or function body for what it can reach."""
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
    return calls, names


def _check_operators_and_types(probe: dict[str, Any], analysis: Analysis) -> None:
    for operator in probe["volatile_operators"]:
        if any(operator in used for used in analysis.operators):
            raise Rejected(
                f"the operator {operator} is implemented by a VOLATILE function",
                rule="operator-volatile",
            )
    if probe["volatile_types"]:
        raise Rejected(
            "the type "
            + ", ".join(sorted(probe["volatile_types"]))
            + " converts values through a VOLATILE function",
            rule="type-volatile",
        )
