"""The guarded path for SQL that comes from outside.

A read query goes through these steps, in this order, inside one read-only transaction:

1. **Scan** (:mod:`guard`). One statement, balanced, of a readable kind.
2. **Vet** (:mod:`vetting`). Every function, view, operator and type the text can reach
   is looked up in the catalog; anything with side effects stops here. This comes before
   any planning because the planner already runs IMMUTABLE and STABLE functions.
3. **Describe.** The query is wrapped as ``SELECT * FROM ( <query> ) AS _pgharbor LIMIT 0``
   and sent through the extended protocol. The wrapper is what makes the *server's*
   parser the judge: ``INSERT``, ``SET``, ``COPY``, ``DO``, ``LOCK``, ``COMMIT`` and friends
   are syntax errors inside a sub-select, and a second statement is refused by the
   protocol itself.
4. **Pre-flight.** ``EXPLAIN (VERBOSE, FORMAT JSON)`` of the statement that will run. The
   plan names the real base tables, so relation rules, "no sequential scan" rules and the
   cost ceiling are checked against what the server will actually do.
5. **Run** with a row limit and every value clipped on the server, so neither a huge table
   nor a huge value can flood the server, the network or the model.
6. **Rollback**, always.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg

from .audit import Stopwatch
from .config import ConnectionConfig, ProtectedTable
from .errors import PgHarborError, Rejected
from .functions import is_always_denied
from .guard import Analysis, analyze, name_variants, parse_explain, parse_show
from .lexer import Kind, LexError, tokenize
from .plan import PlanSummary, summarize_plan
from .render import REDACTED, compile_column_patterns, scrub
from .session import Db, describe_db_error
from .vetting import Vetting, relation_denied, vet_statement

__all__ = [
    "Column",
    "QueryResult",
    "estimate_plan",
    "explain_statement",
    "run_query",
    "validate_statement",
]

_WRAP_PREFIX = "SELECT * FROM (\n"
_ALIAS = "_pgharbor"

# Type OIDs that are converted to native JSON values.
_BOOL = 16
_INET = 869
_INTEGERS = frozenset({20, 21, 23, 26})
_FLOATS = frozenset({700, 701})
_NUMERIC = 1700
_JSON = frozenset({114, 3802})
_PLAIN_NUMBER = re.compile(r"-?\d+(\.\d+)?")


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    type: str
    type_oid: int
    origin: str | None = None  # schema.table.column when the value is a plain column

    def public(self) -> dict[str, str]:
        return {"name": self.name, "type": self.type}


@dataclass(slots=True)
class QueryResult:
    columns: list[Column]
    rows: list[list[Any]]
    has_more: bool = False
    notes: list[str] = field(default_factory=list)
    relations: list[str] = field(default_factory=list)
    total_cost: float | None = None
    elapsed_ms: float = 0.0

    def payload(self, connection: str) -> dict[str, Any]:
        out: dict[str, Any] = {
            "connection": connection,
            "columns": [c.public() for c in self.columns],
            "rows": self.rows,
            "row_count": len(self.rows),
            "has_more": self.has_more,
        }
        if self.notes:
            out["notes"] = self.notes
        out["elapsed_ms"] = round(self.elapsed_ms, 1)
        return out


# ----------------------------------------------------------------------------- entry points


async def run_query(
    db: Db,
    sql: str,
    params: Sequence[Any] = (),
    *,
    max_rows: int | None = None,
    offset: int = 0,
    max_cell_chars: int | None = None,
    row_cap: int | None = None,
    char_budget: int | None = None,
) -> QueryResult:
    """Run one read query through every check. ``db`` must be a read-only transaction.

    ``row_cap`` and ``char_budget`` replace the profile's ``max_rows_limit`` and
    ``max_result_chars``. They exist for tools that write the result to a file instead of
    sending it to the model; nothing an agent passes reaches them directly.
    """
    if not db.read_only:
        raise AssertionError("run_query needs a read-only transaction")
    profile = db.profile
    watch = Stopwatch()
    analysis = analyze(sql, max_chars=profile.max_sql_chars)

    if analysis.kind == "show":
        result = await _run_show(db, analysis)
        result.elapsed_ms = watch.ms
        return result
    _require_query(analysis, profile)
    _check_params(analysis, params)

    vetting = await vet_statement(db, analysis, read_only=True)
    await _refuse_tainted_views(db, vetting)

    columns = await _describe(db, analysis, params)
    limit = _row_limit(profile, max_rows, row_cap)
    cell = _cell_limit(profile, max_cell_chars)
    statement = _final_statement(analysis.sql, len(columns), cell, limit + 1, max(0, int(offset)))

    summary = await _preflight(db, statement, params, analysis)
    rule_limit = _enforce_plan_rules(profile, analysis, vetting, summary)
    if rule_limit is not None and rule_limit < limit:
        limit = rule_limit
        statement = _final_statement(
            analysis.sql, len(columns), cell, limit + 1, max(0, int(offset))
        )

    try:
        result = await db.raw(statement, params)
    except psycopg.errors.CannotCoerce:
        # A column type with no text cast: fetch as is and clip on this side instead.
        raise PgHarborError(
            "A result column has a type that cannot be shown as text. Cast it explicitly "
            "in the query (for example column::text)."
        ) from None
    except psycopg.Error as exc:
        raise _translate(exc, profile, analysis.sql) from exc

    query_result = _build_result(
        result,
        columns,
        profile,
        limit=limit,
        cell=cell,
        budget=char_budget or profile.max_result_chars,
    )
    query_result.relations = summary.relations()
    query_result.total_cost = summary.total_cost
    query_result.elapsed_ms = watch.ms
    if query_result.has_more:
        query_result.notes.append(
            f"Only the first {len(query_result.rows)} rows are shown. Narrow the WHERE clause, "
            f"aggregate, or page with offset={offset + len(query_result.rows)} "
            f"(max_rows up to {profile.max_rows_limit})."
        )
    return query_result


async def estimate_plan(db: Db, sql: str, params: Sequence[Any] = ()) -> PlanSummary:
    """Vet a read query and return the summary of its plan, without running it.

    Every rule of the connection applies, exactly as in :func:`run_query`. Tools that
    compare plans (an index advisor, for instance) call this instead of running EXPLAIN on
    their own.
    """
    profile = db.profile
    analysis = analyze(sql, max_chars=profile.max_sql_chars)
    _require_query(analysis, profile)
    _check_params(analysis, params)
    vetting = await vet_statement(db, analysis, read_only=True)
    await _refuse_tainted_views(db, vetting)
    summary = await _preflight(db, f"{_WRAP_PREFIX}{analysis.sql}\n) AS {_ALIAS}", params, analysis)
    _enforce_plan_rules(profile, analysis, vetting, summary)
    return summary


async def validate_statement(db: Db, sql: str, params: Sequence[Any] = ()) -> dict[str, Any]:
    """Check a read query completely, without running it."""
    profile = db.profile
    analysis = analyze(sql, max_chars=profile.max_sql_chars)
    if analysis.kind == "show":
        return {"valid": True, "kind": "show", "setting": parse_show(analysis)}
    _require_query(analysis, profile)
    _check_params(analysis, params)
    vetting = await vet_statement(db, analysis, read_only=True)
    await _refuse_tainted_views(db, vetting)
    columns = await _describe(db, analysis, params)
    statement = _final_statement(analysis.sql, len(columns), profile.max_cell_chars, 1, 0)
    summary = await _preflight(db, statement, params, analysis)
    _enforce_plan_rules(profile, analysis, vetting, summary)
    return {
        "valid": True,
        "kind": "query",
        "columns": [c.public() for c in columns],
        "parameters": analysis.param_count,
        "relations": summary.relations(),
        "estimated_cost": summary.total_cost,
    }


async def explain_statement(
    db: Db,
    sql: str,
    params: Sequence[Any] = (),
    *,
    analyze_execution: bool = False,
    verbose: bool = False,
    buffers: bool = False,
    settings: bool = False,
    costs: bool = True,
    output_format: str = "text",
) -> dict[str, Any]:
    """Return the plan of a read query. ``analyze_execution`` really runs it."""
    profile = db.profile
    analysis = analyze(sql, max_chars=profile.max_sql_chars)
    if analysis.kind == "explain":
        request = parse_explain(analysis)
        analyze_execution = analyze_execution or bool(request.options.get("analyze"))
        verbose = verbose or bool(request.options.get("verbose"))
        buffers = buffers or bool(request.options.get("buffers"))
        analysis = analyze(request.statement, max_chars=profile.max_sql_chars)
    _require_query(analysis, profile)
    _check_params(analysis, params)
    if analyze_execution and not profile.allow_explain_analyze:
        raise Rejected(
            "EXPLAIN ANALYZE runs the query, and this connection does not allow it",
            rule="explain-analyze-disabled",
            hint="Use a plain EXPLAIN, or set allow_explain_analyze = true in the "
            "connection profile.",
        )
    if output_format not in ("text", "json", "yaml", "xml"):
        raise Rejected("format must be text, json, yaml or xml", rule="invalid-explain")

    vetting = await vet_statement(db, analysis, read_only=True)
    await _refuse_tainted_views(db, vetting)
    wrapped = f"{_WRAP_PREFIX}{analysis.sql}\n) AS {_ALIAS}"

    # The rules are always checked on a plain plan first, even when ANALYZE was asked.
    summary = await _preflight(db, wrapped, params, analysis)
    _enforce_plan_rules(profile, analysis, vetting, summary)

    options = [f"FORMAT {output_format.upper()}"]
    if analyze_execution:
        options.append("ANALYZE")
    if verbose:
        options.append("VERBOSE")
    if not costs:
        options.append("COSTS OFF")
    if buffers and (analyze_execution or db.v >= 130000):
        options.append("BUFFERS")
    if settings and db.v >= 120000:
        options.append("SETTINGS")
    try:
        result = await db.raw(f"EXPLAIN ({', '.join(options)}) {wrapped}", params)
    except psycopg.Error as exc:
        raise _translate(exc, profile, analysis.sql) from exc

    lines = [_text(result.get_value(row, 0)) or "" for row in range(result.ntuples)]
    plan: Any = "\n".join(lines)
    if output_format == "json":
        plan = json.loads(plan)
    return {
        "executed": analyze_execution,
        "estimated_cost": summary.total_cost,
        "estimated_rows": summary.plan_rows,
        "relations": summary.relations(),
        "sequential_scans": sorted({s.qualified for s in summary.scans if s.sequential}),
        "plan": plan,
    }


# ------------------------------------------------------------------------------------ helpers


def _require_query(analysis: Analysis, profile: ConnectionConfig) -> None:
    if analysis.kind == "query":
        return
    if analysis.kind in ("dml", "ddl", "maintenance"):
        hint = (
            "Read tools only accept SELECT, WITH ... SELECT, VALUES, TABLE and SHOW."
            if profile.read_only
            else "Use pg_execute for statements that write."
        )
        raise Rejected(
            f"{analysis.verb} changes the database; this tool only reads",
            rule="not-a-read-query",
            hint=hint,
        )
    if analysis.kind == "explain":
        raise Rejected("EXPLAIN is handled by the pg_explain tool", rule="use-pg-explain")
    raise Rejected(
        f"{analysis.verb} is not a read query",
        rule="not-a-read-query",
        hint="Only SELECT, WITH ... SELECT, VALUES, TABLE and SHOW are accepted.",
    )


def _check_params(analysis: Analysis, params: Sequence[Any]) -> None:
    if len(params) != analysis.param_count:
        raise Rejected(
            f"the statement uses {analysis.param_count} parameter(s) ($1..${analysis.param_count}) "
            f"but {len(params)} value(s) were given"
            if analysis.param_count
            else f"the statement has no $n parameters but {len(params)} value(s) were given",
            rule="parameter-count",
        )


def _row_limit(profile: ConnectionConfig, requested: int | None, cap: int | None = None) -> int:
    if requested is None:
        return profile.max_rows
    return max(1, min(int(requested), cap or profile.max_rows_limit))


def _cell_limit(profile: ConnectionConfig, requested: int | None) -> int:
    if requested is None:
        return profile.max_cell_chars
    return max(16, min(int(requested), max(profile.max_cell_chars, 20_000)))


def _final_statement(sql: str, column_count: int, cell: int, limit: int, offset: int) -> str:
    """Wrap the query so that rows are limited and every value is clipped on the server."""
    tail = f"LIMIT {int(limit)}" + (f" OFFSET {int(offset)}" if offset else "")
    if column_count == 0:
        return f"SELECT FROM (\n{sql}\n) AS {_ALIAS} {tail}"
    aliases = ", ".join(f"c{i}" for i in range(column_count))
    projection = ", ".join(
        f"left({_ALIAS}.c{i}::text, {int(cell) + 1})" for i in range(column_count)
    )
    return f"SELECT {projection} FROM (\n{sql}\n) AS {_ALIAS}({aliases}) {tail}"


_DESCRIBE_TYPES_SQL = """
SELECT pg_catalog.format_type(u.type_oid, u.type_mod) AS type,
       n.nspname AS origin_schema, c.relname AS origin_table, a.attname AS origin_column
  FROM unnest($1::oid[], $2::int4[], $3::oid[], $4::int4[])
       WITH ORDINALITY AS u(type_oid, type_mod, rel, att, ord)
  LEFT JOIN pg_catalog.pg_class c ON c.oid = u.rel
  LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
  LEFT JOIN pg_catalog.pg_attribute a ON a.attrelid = u.rel AND a.attnum = u.att
 ORDER BY u.ord
"""


async def _describe(db: Db, analysis: Analysis, params: Sequence[Any]) -> list[Column]:
    """Ask the server what the query returns, without fetching a row."""
    try:
        result = await db.raw(f"{_WRAP_PREFIX}{analysis.sql}\n) AS {_ALIAS} LIMIT 0", params)
    except psycopg.Error as exc:
        raise _translate(exc, db.profile, analysis.sql) from exc
    count = result.nfields
    if count == 0:
        return []
    names = [_text(result.fname(i)) or "?column?" for i in range(count)]
    type_oids = [result.ftype(i) for i in range(count)]
    type_mods = [result.fmod(i) for i in range(count)]
    tables = [result.ftable(i) for i in range(count)]
    attributes = [result.ftablecol(i) for i in range(count)]

    def array(values: list[int]) -> str:
        return "{" + ",".join(str(v) for v in values) + "}"

    described = await db.rows(
        _DESCRIBE_TYPES_SQL,
        [array(type_oids), array(type_mods), array(tables), array(attributes)],
    )
    columns: list[Column] = []
    for i, info in enumerate(described):
        origin = None
        if info["origin_table"] and info["origin_column"]:
            origin = f"{info['origin_schema']}.{info['origin_table']}.{info['origin_column']}"
        columns.append(Column(names[i], info["type"] or "unknown", type_oids[i], origin))
    return columns


async def _preflight(
    db: Db, statement: str, params: Sequence[Any], analysis: Analysis
) -> PlanSummary:
    try:
        result = await db.raw(f"EXPLAIN (VERBOSE, FORMAT JSON) {statement}", params)
    except psycopg.Error as exc:
        raise _translate(exc, db.profile, analysis.sql) from exc
    text = "".join(_text(result.get_value(row, 0)) or "" for row in range(result.ntuples))
    return summarize_plan(text)


def _enforce_plan_rules(
    profile: ConnectionConfig, analysis: Analysis, vetting: Vetting, summary: PlanSummary
) -> int | None:
    """Apply the rules that need the plan. Returns a row limit imposed by a rule, if any."""
    for scan in summary.scans:
        reason = relation_denied(profile, scan.schema, scan.relation, configured_only=True)
        if reason:
            raise Rejected(reason, rule="relation-denied")

    for name in _denied_calls_in_plan(summary):
        raise Rejected(
            f"the query plan calls {name}(), which is never allowed", rule="function-denied"
        )

    row_limit: int | None = None
    touched: dict[str, ProtectedTable] = {}
    for scan in summary.scans:
        rule = profile.protection_for(scan.schema, scan.relation)
        if rule is None:
            continue
        touched[scan.qualified] = rule
        note = f" {rule.note}" if rule.note else ""
        if rule.require_index_filter and scan.reads_everything:
            raise Rejected(
                f"this query would read all of {scan.qualified} ({scan.node_type} with no "
                f"index condition), which is not allowed for this table.{note}",
                rule="full-scan-forbidden",
                hint="Filter on an indexed column with a selective condition.",
            )
        if rule.deny_columns:
            _enforce_protected_columns(analysis, scan.qualified, scan.relation, rule)
            if summary.uses_whole_row(scan):
                raise Rejected(
                    f"the query uses whole rows of {scan.qualified}, which has protected "
                    f"columns.{note}",
                    rule="protected-column",
                    hint="Name the columns you need.",
                )
        if rule.max_rows is not None:
            row_limit = rule.max_rows if row_limit is None else min(row_limit, rule.max_rows)

    # A protected table that the catalog lookup found but the plan does not scan is being
    # reached some other way (inside a function body, for instance).
    for relation in vetting.relations.values():
        rule = profile.protection_for(relation["schema"], relation["name"])
        qualified = f"{relation['schema']}.{relation['name']}"
        if (
            rule is not None
            and qualified not in touched
            and relation["kind"] != "v"
            and (rule.deny_columns or rule.require_index_filter)
        ):
            raise Rejected(
                f"{qualified} is a protected table and this query reaches it indirectly "
                "(through a function, or the name is used for something else)",
                rule="protected-table-indirect",
                hint="Query the table directly, naming the columns.",
            )

    if profile.max_plan_cost is not None and summary.total_cost > profile.max_plan_cost:
        raise Rejected(
            f"the estimated cost of this query ({summary.total_cost:,.0f}) is above the limit "
            f"for this connection ({profile.max_plan_cost:,.0f})",
            rule="plan-cost",
            hint="Add selective filters on indexed columns or reduce the number of joins. "
            "pg_explain shows where the cost comes from.",
        )
    return row_limit


def _denied_calls_in_plan(summary: PlanSummary) -> list[str]:
    """A second, independent look for forbidden calls, this time in the server's plan.

    Plan expressions are SQL text written by the server. They are scanned with the same
    lexer so that a function name inside a string constant is not mistaken for a call.
    """
    found: list[str] = []
    for expression in summary.expressions:
        if "(" not in expression:
            continue
        try:
            tokens = tokenize(expression)
        except LexError:
            continue
        for i, token in enumerate(tokens[:-1]):
            if token.is_name and tokens[i + 1].is_punct("(") and is_always_denied(token.value):
                found.append(token.value)
    return found


def _enforce_protected_columns(
    analysis: Analysis, qualified: str, relation: str, rule: ProtectedTable
) -> None:
    denied = set(rule.deny_columns)
    note = f" {rule.note}" if rule.note else ""
    tokens = analysis.tokens
    for i, token in enumerate(tokens):
        if not token.is_name:
            continue
        variants = name_variants(token.value, token.kind is Kind.QIDENT)
        if variants & denied:
            raise Rejected(
                f"column {token.value} of {qualified} is protected and cannot be read, "
                f"filtered on or used in any expression.{note}",
                rule="protected-column",
            )
        if relation in variants:
            # "table AS alias (a, b, c)" renames the columns and would hide a protected one.
            j = i + 1
            if j < len(tokens) and tokens[j].is_word("as"):
                j += 1
            if j + 1 < len(tokens) and tokens[j].is_name and tokens[j + 1].is_punct("("):
                raise Rejected(
                    f"a column alias list on {qualified} is not allowed: the table has "
                    f"protected columns.{note}",
                    rule="protected-column",
                )
            if i >= 1 and tokens[i - 1].is_word("table"):
                raise Rejected(
                    f"TABLE {qualified} returns every column and the table has protected "
                    f"columns.{note}",
                    rule="protected-column",
                    hint="Use SELECT with the columns you need.",
                )
    if analysis.stars:
        raise Rejected(
            f"this query uses * and reads {qualified}, which has protected columns.{note}",
            rule="protected-column",
            hint="Name the columns you need instead of *.",
        )


_PROTECTED_COLUMNS_SQL = """
SELECT a.attrelid::int8 AS oid, a.attnum::int4 AS attnum, n.nspname AS schema,
       c.relname AS name, a.attname AS column
  FROM pg_catalog.pg_attribute a
  JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
 WHERE a.attname = ANY($1::text[]) AND a.attnum > 0 AND NOT a.attisdropped
   AND c.relkind IN ('r', 'p', 'm', 'f')
"""

_TAINTED_VIEWS_SQL = """
WITH RECURSIVE tainted(oid) AS (
    SELECT DISTINCT w.ev_class
      FROM pg_catalog.pg_depend d
      JOIN pg_catalog.pg_rewrite w ON w.oid = d.objid
     WHERE d.classid = 'pg_catalog.pg_rewrite'::regclass
       AND d.refclassid = 'pg_catalog.pg_class'::regclass
       AND (d.refobjid, d.refobjsubid) IN (SELECT * FROM unnest($1::oid[], $2::int4[]))
    UNION
    SELECT w.ev_class
      FROM tainted t
      JOIN pg_catalog.pg_depend d ON d.refclassid = 'pg_catalog.pg_class'::regclass
           AND d.refobjid = t.oid AND d.classid = 'pg_catalog.pg_rewrite'::regclass
      JOIN pg_catalog.pg_rewrite w ON w.oid = d.objid
     WHERE w.ev_class <> t.oid
)
SELECT oid::int8 AS oid FROM tainted
"""


async def _refuse_tainted_views(db: Db, vetting: Vetting) -> None:
    """Refuse views that expose a protected column, however they rename it."""
    profile = db.profile
    denied = sorted({col for rule in profile.protect for col in rule.deny_columns})
    views = {oid: r for oid, r in vetting.relations.items() if r["kind"] == "v" and r.get("def")}
    if not denied or not views:
        return
    candidates = await db.rows(_PROTECTED_COLUMNS_SQL, [denied])
    oids: list[int] = []
    attnums: list[int] = []
    for row in candidates:
        rule = profile.protection_for(row["schema"], row["name"])
        if rule is not None and row["column"] in rule.deny_columns:
            oids.append(row["oid"])
            attnums.append(row["attnum"])
    if not oids:
        return

    def array(values: list[int]) -> str:
        return "{" + ",".join(str(v) for v in values) + "}"

    tainted = {
        row["oid"] for row in await db.rows(_TAINTED_VIEWS_SQL, [array(oids), array(attnums)])
    }
    for oid, relation in views.items():
        if oid in tainted:
            raise Rejected(
                f"the view {relation['schema']}.{relation['name']} exposes a protected column",
                rule="protected-column",
                hint="Query the base table and name the columns you need.",
            )


def _build_result(
    result: Any,
    columns: list[Column],
    profile: ConnectionConfig,
    *,
    limit: int,
    cell: int,
    budget: int,
) -> QueryResult:
    count = result.nfields
    total = result.ntuples
    notes: list[str] = []

    name_pattern = compile_column_patterns(profile.redact_columns)
    hidden: list[str | None] = []
    for column in columns:
        reason: str | None = None
        if column.origin:
            schema, table, attribute = column.origin.rsplit(".", 2)
            rule = profile.protection_for(schema, table)
            if rule is not None and attribute in rule.deny_columns:
                reason = "protected"
            elif name_pattern is not None and name_pattern.search(attribute):
                reason = "name"
        if reason is None and name_pattern is not None and name_pattern.search(column.name):
            reason = "name"
        hidden.append(reason)
    redacted_names = [c.name for c, h in zip(columns, hidden, strict=True) if h]
    if redacted_names:
        notes.append(
            "Redacted column(s): "
            + ", ".join(redacted_names)
            + ". Their names look like credentials or they are protected in this connection."
        )

    rows: list[list[Any]] = []
    used = 0
    clipped = 0
    scrubbed = 0
    has_more = total > limit
    for r in range(min(total, limit)):
        row: list[Any] = []
        for c in range(count):
            raw = result.get_value(r, c)
            if raw is None:
                row.append(None)
                continue
            if hidden[c]:
                row.append(REDACTED)
                used += len(REDACTED)
                continue
            text = raw.decode("utf-8", errors="replace")
            was_clipped = len(text) > cell
            if was_clipped:
                text = text[:cell] + "…"
                clipped += 1
            value = _convert(text, columns[c].type_oid, was_clipped)
            if isinstance(value, str):
                clean = scrub(value)
                if clean != value:
                    scrubbed += 1
                    value = clean
            used += len(text) + 3
            row.append(value)
        if used > budget and rows:
            has_more = True
            notes.append(
                f"The result was cut at {len(rows)} rows to stay within "
                f"{budget:,} characters. Select fewer columns or fewer rows."
            )
            break
        rows.append(row)

    if clipped:
        notes.append(
            f"{clipped} value(s) longer than {cell} characters were cut (marked with …). "
            "Raise max_cell_chars or use substr() to read a specific part."
        )
    if scrubbed:
        notes.append(f"{scrubbed} value(s) contained embedded credentials, replaced by {REDACTED}.")
    return QueryResult(columns=columns, rows=rows, has_more=has_more, notes=notes)


def _convert(text: str, type_oid: int, clipped: bool) -> Any:
    """Turn the server's text form into a native JSON value when that is lossless."""
    if clipped:
        return text
    if type_oid == _BOOL:
        return text in ("true", "t")
    if type_oid == _INET:
        # The text cast always prints the netmask; drop it for a plain host address.
        suffix = "/128" if ":" in text else "/32"
        return text[: -len(suffix)] if text.endswith(suffix) else text
    if type_oid in _INTEGERS:
        try:
            return int(text)
        except ValueError:
            return text
    if type_oid in _FLOATS:
        try:
            value = float(text)
        except ValueError:
            return text
        return value if value == value and abs(value) != float("inf") else text
    if type_oid == _NUMERIC and _PLAIN_NUMBER.fullmatch(text):
        if "." not in text:
            return int(text) if len(text) <= 18 else text
        # Keep the scale the server sent (1.50 stays "1.50") unless a float shows it exactly.
        return float(text) if repr(float(text)) == text else text
    if type_oid in _JSON:
        try:
            return json.loads(text)
        except ValueError:
            return text
    return text


def _text(value: bytes | None) -> str | None:
    return None if value is None else value.decode("utf-8", errors="replace")


async def _run_show(db: Db, analysis: Analysis) -> QueryResult:
    setting = parse_show(analysis)
    if setting == "all":
        rows = await db.rows(
            "SELECT name, setting, unit, short_desc FROM pg_catalog.pg_settings ORDER BY name"
        )
        columns = [
            Column("name", "text", 25),
            Column("setting", "text", 25),
            Column("unit", "text", 25),
            Column("description", "text", 25),
        ]
        return QueryResult(
            columns=columns,
            rows=[[r["name"], scrub(r["setting"] or ""), r["unit"], r["short_desc"]] for r in rows],
        )
    try:
        value = await db.value("SELECT pg_catalog.current_setting($1)", [setting])
    except psycopg.Error as exc:
        raise _translate(exc, db.profile, analysis.sql) from exc
    return QueryResult(columns=[Column(setting, "text", 25)], rows=[[scrub(str(value))]])


def _translate(exc: psycopg.Error, profile: ConnectionConfig, sql: str) -> PgHarborError:
    """Explain a server error in terms of the SQL the caller wrote."""
    state = getattr(exc, "sqlstate", None) or ""
    message = exc.diag.message_primary or str(exc).splitlines()[0]

    if state == "42601" and "multiple commands" in message:
        return Rejected(
            "more than one statement was sent",
            rule="multiple-statements",
            hint="Send one statement per call.",
        )
    if isinstance(exc, psycopg.errors.ReadOnlySqlTransaction):
        return Rejected(
            f"the server refused it: {message}",
            rule="read-only-transaction",
            hint="This connection only reads.",
        )
    if state == "0A000" and "data-modifying" in message:
        return Rejected(
            "a WITH clause that changes data is not a read query", rule="not-a-read-query"
        )
    if (
        isinstance(
            exc,
            (
                psycopg.errors.QueryCanceled,
                psycopg.errors.LockNotAvailable,
                psycopg.errors.InsufficientPrivilege,
            ),
        )
        or not state
    ):
        return PgHarborError(describe_db_error(exc, profile))

    # The server reports positions inside the wrapped statement; translate them back.
    where = ""
    position = exc.diag.statement_position
    if position:
        offset = int(position) - len(_WRAP_PREFIX)
        if 0 < offset <= len(sql):
            line = sql.count("\n", 0, offset - 1) + 1
            column = offset - (sql.rfind("\n", 0, offset - 1) + 1)
            where = f" (line {line}, column {column} of your statement)"
        elif offset > len(sql):
            where = " (at the end of your statement: it looks incomplete)"
    parts = [f"{message}{where}."]
    if exc.diag.message_detail:
        parts.append(f"Detail: {exc.diag.message_detail}")
    if exc.diag.message_hint:
        parts.append(f"Hint: {exc.diag.message_hint}")
    if state == "42601":
        parts.append(
            "Only a single read query is accepted here: SELECT, WITH ... SELECT, VALUES or TABLE."
        )
        return PgHarborError("Syntax error: " + " ".join(parts))
    return PgHarborError(f"PostgreSQL error [{state}]: " + " ".join(parts))
