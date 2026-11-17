"""Looking at the data: samples, row counts, column statistics and file exports."""

import asyncio
import contextlib
import csv
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Literal, TextIO

from ..app import get_app, tool
from ..errors import PgHarborError, Rejected
from ..guard import quote_ident, quote_qualified
from ..render import compile_column_patterns
from ..runner import QueryResult, run_query
from ..session import Db
from ..vetting import relation_denied
from ._util import clamp, grid, relation_kind_label, resolve_relation

__all__ = ["pg_column_stats", "pg_count_rows", "pg_export_query", "pg_sample_rows"]

# Tables above this many (estimated) rows are sampled with a thin slice; below it a full random sort
# is cheap and gives an exact random sample.
_SORT_SAMPLE_MAX_ROWS = 20_000
_SAMPLE_OVERSHOOT = 20  # the thin slice asks for this many times the rows wanted
_EXPORT_ROW_CAP = 100_000
_EXPORT_CHAR_BUDGET = 64_000_000
_EXPORT_CELL_CHARS = 20_000


async def _table_columns(db: Db, oid: int) -> list[dict[str, Any]]:
    return await db.rows(
        """
        SELECT a.attname AS name, pg_catalog.format_type(a.atttypid, a.atttypmod) AS type
          FROM pg_catalog.pg_attribute a
         WHERE a.attrelid = $1 AND a.attnum > 0 AND NOT a.attisdropped
         ORDER BY a.attnum
        """,
        [oid],
    )


def _check_columns(requested: list[str], available: list[str], table: str) -> None:
    unknown = [c for c in requested if c not in available]
    if unknown:
        raise PgHarborError(
            f"{table} has no column named {', '.join(repr(c) for c in unknown)}. "
            f"Its columns are: {', '.join(available)}."
        )


async def _thin_sample(
    db: Db, projection: str, source: str, limit: int, estimate: float
) -> tuple[QueryResult, str]:
    """A random sample of a big table. Page sampling first, a row filter if it is refused."""
    percent = min(100.0, max(0.0001, limit * _SAMPLE_OVERSHOOT * 100.0 / estimate))
    method = "SYSTEM"
    while True:
        if method == "SYSTEM":
            inner = f"SELECT {projection} FROM {source} TABLESAMPLE SYSTEM ({percent:.4f})"
        else:
            inner = f"SELECT {projection} FROM {source} WHERE random() < {percent / 100:.8f}"
        sql = f"SELECT {projection} FROM ({inner}) AS sample ORDER BY random() LIMIT {limit}"
        try:
            result = await run_query(db, sql, max_rows=limit)
        except Rejected as exc:
            # The safety checks do not know the TABLESAMPLE methods (SYSTEM, BERNOULLI) and
            # refuse them as unknown volatile functions; a row filter does the same job.
            if method == "SYSTEM" and exc.rule == "function-volatile":
                method = "FILTER"
                continue
            raise
        if len(result.rows) >= limit or percent >= 100:
            break
        percent = min(100.0, percent * 10)  # an unlucky draw: try a wider slice
    label = (
        f"random pages (TABLESAMPLE SYSTEM {percent:.2f}%)"
        if method == "SYSTEM"
        else f"random rows (filter on random(), {percent:.2f}% of the table, whole table read)"
    )
    return result, label


# ------------------------------------------------------------------------------ pg_sample_rows


@tool(group="data", title="Sample rows of a table")
async def pg_sample_rows(
    table: str,
    connection: str | None = None,
    columns: list[str] | None = None,
    limit: int = 10,
    random: bool = False,
) -> dict[str, Any]:
    """Show a few rows of a table or view to see what the data looks like.

    Pick `columns` to keep the answer small; by default every column the connection allows
    is listed (columns protected by the connection's rules are left out, and values of
    credential-like columns are redacted). `limit` is at most 100. By default the rows are
    simply the first ones the server finds, in no particular order; `random=true` draws a
    random sample instead (on a big table this reads the whole table once, so it can take a
    while; the time limit of the connection applies). For a filtered read use pg_query.
    """
    limit = clamp(limit, 10, 100)
    async with get_app().read(connection) as db:
        relation = await resolve_relation(db, table)
        qualified = f"{relation['schema']}.{relation['name']}"
        attributes = [c["name"] for c in await _table_columns(db, relation["oid"])]
        rule = db.profile.protection_for(relation["schema"], relation["name"])
        denied = set(rule.deny_columns) if rule else set()
        notes: list[str] = []
        if columns:
            _check_columns(columns, attributes, qualified)
            blocked = [c for c in columns if c in denied]
            if blocked:
                raise Rejected(
                    f"column(s) {', '.join(blocked)} of {qualified} are protected on connection "
                    f"'{db.profile.name}' and can never be read",
                    rule="protected-column",
                    hint="Leave them out of `columns`.",
                )
            selected = list(dict.fromkeys(columns))
        else:
            selected = [c for c in attributes if c not in denied]
            left_out = [c for c in attributes if c in denied]
            if left_out:
                notes.append(
                    "Protected column(s) left out: "
                    + ", ".join(left_out)
                    + "."
                    + (f" {rule.note}" if rule and rule.note else "")
                )
        if not selected:
            raise PgHarborError(
                f"Every column of {qualified} is protected on this connection; nothing can be "
                "sampled."
            )
        head = await db.row(
            "SELECT c.reltuples::float8 AS estimate, c.relkind::text AS relkind "
            "FROM pg_catalog.pg_class c WHERE c.oid = $1",
            [relation["oid"]],
        )
        assert head is not None
        projection = ", ".join(quote_ident(c) for c in selected)
        source = quote_qualified(relation["schema"], relation["name"])
        sampling = "first rows found by the server, in no particular order"
        if not random:
            sql = f"SELECT {projection} FROM {source} LIMIT {limit}"
            result = await run_query(db, sql, max_rows=limit)
        else:
            estimate = head["estimate"]
            thin = (
                head["relkind"] in ("r", "m", "p")
                and estimate is not None
                and estimate >= _SORT_SAMPLE_MAX_ROWS
            )
            if thin:
                result, sampling = await _thin_sample(db, projection, source, limit, estimate)
            else:
                sql = f"SELECT {projection} FROM {source} ORDER BY random() LIMIT {limit}"
                sampling = "random order of the whole table"
                if head["relkind"] in ("v", "f"):
                    notes.append(
                        "Views and foreign tables cannot be sampled, so the whole result "
                        "is read and shuffled."
                    )
                elif estimate is None or estimate < 0 or (estimate == 0 and head["relkind"] == "r"):
                    notes.append(
                        "The table has no statistics yet (never analyzed), so its size is "
                        "unknown; it was shuffled completely."
                    )
            if not thin:
                result = await run_query(db, sql, max_rows=limit)
    out: dict[str, Any] = {
        "connection": db.profile.name,
        "table": qualified,
        "kind": relation_kind_label(relation["kind"]),
        "sampling": sampling,
        "columns": [c.name for c in result.columns],
        "rows": result.rows,
        "row_count": len(result.rows),
    }
    notes += result.notes
    if not result.rows:
        notes.append("The table has no rows.")
    if notes:
        out["notes"] = notes
    return out


# ------------------------------------------------------------------------------ pg_count_rows

_COUNT_ESTIMATE_SQL = """
WITH RECURSIVE tree(oid) AS (
    SELECT $1::oid
    UNION ALL
    SELECT i.inhrelid FROM pg_catalog.pg_inherits i JOIN tree t ON i.inhparent = t.oid
)
SELECT (sum(greatest(c.reltuples, 0)) FILTER (WHERE c.relkind <> 'p'))::bigint AS rows,
       count(*) FILTER (WHERE c.relkind <> 'p') AS tables,
       count(*) FILTER (WHERE c.relkind <> 'p' AND coalesce(
           greatest(s.last_analyze, s.last_autoanalyze, s.last_vacuum, s.last_autovacuum)
           IS NOT NULL, false)) AS analyzed,
       max(greatest(s.last_analyze, s.last_autoanalyze))::text AS last_analyze
  FROM tree t
  JOIN pg_catalog.pg_class c ON c.oid = t.oid
  LEFT JOIN pg_catalog.pg_stat_all_tables s ON s.relid = c.oid
"""


@tool(group="data", title="Count rows")
async def pg_count_rows(
    table: str,
    connection: str | None = None,
    exact: bool = False,
) -> dict[str, Any]:
    """Count the rows of a table: an instant estimate from the planner statistics, and with
    `exact=true` the real count(*).

    The estimate costs nothing and is usually within a few percent; it adds up the
    partitions or child tables of a partitioned table, and says when the table was last
    analyzed (a table never analyzed has no useful estimate). The exact count reads the
    whole table, so on a big table prefer the estimate, or filter with pg_query.
    """
    async with get_app().read(connection) as db:
        relation = await resolve_relation(db, table)
        qualified = f"{relation['schema']}.{relation['name']}"
        out: dict[str, Any] = {
            "connection": db.profile.name,
            "table": qualified,
            "kind": relation_kind_label(relation["kind"]),
        }
        notes: list[str] = []
        estimate: int | None = None
        if relation["kind"] in ("v",):
            notes.append(
                "A view has no row estimate. Pass exact=true to count it (this runs the view's "
                "query), or count its base table."
            )
        else:
            info = await db.row(_COUNT_ESTIMATE_SQL, [relation["oid"]])
            assert info is not None
            # pg_class.reltuples is what the planner uses. The timestamps in pg_stat_* can be
            # empty on a server whose statistics counters were reset or that was restored
            # from a backup, while the estimate itself is still good.
            if info["analyzed"] == 0 and not info["rows"] and relation["kind"] != "m":
                notes.append(
                    "The table has never been analyzed, so there is no usable estimate "
                    "(an administrator can run ANALYZE). Pass exact=true for the real count."
                )
            else:
                estimate = info["rows"]
                out["estimated_rows"] = estimate
                if info["analyzed"] == 0 and relation["kind"] != "m":
                    notes.append(
                        "The server has no record of when this table was last analyzed (its "
                        "statistics counters were reset), so the age of the estimate is unknown."
                    )
                if info["last_analyze"]:
                    out["statistics_from"] = info["last_analyze"]
                    notes.append(
                        "The estimate is as old as the last ANALYZE; a table that changed a "
                        "lot since may be off."
                    )
        if exact:
            result = await run_query(
                db,
                f"SELECT count(*) FROM {quote_qualified(relation['schema'], relation['name'])}",
                max_rows=1,
            )
            count = result.rows[0][0]
            out["exact_rows"] = count
            if estimate and count:
                out["estimate_error_percent"] = round(100.0 * (estimate - count) / count, 1)
            notes.extend(result.notes)
        elif estimate is not None and estimate < _SORT_SAMPLE_MAX_ROWS:
            notes.append("The table is small: exact=true is cheap here.")
    if notes:
        out["notes"] = notes
    return out


# ----------------------------------------------------------------------------- pg_column_stats

_COLUMN_STATS_SQL = """
SELECT DISTINCT ON (s.attname)
       s.attname::text AS name, s.inherited, s.null_frac::float8 AS null_fraction,
       s.avg_width, s.n_distinct::float8 AS n_distinct, s.correlation::float8 AS correlation,
       CASE WHEN s.attname::text = ANY($3::text[]) THEN NULL ELSE ARRAY(
           SELECT left(u.v, 60)
             FROM unnest(s.most_common_vals::text::text[]) WITH ORDINALITY AS u(v, i)
            ORDER BY u.i LIMIT 10) END AS common_values,
       CASE WHEN s.attname::text = ANY($3::text[]) THEN NULL
            ELSE (s.most_common_freqs::float8[])[1:10] END AS common_frequencies,
       CASE WHEN s.attname::text = ANY($3::text[]) THEN NULL
            ELSE left((s.histogram_bounds::text::text[])[1], 60) END AS histogram_first,
       CASE WHEN s.attname::text = ANY($3::text[]) THEN NULL
            ELSE left((s.histogram_bounds::text::text[])
                      [array_length(s.histogram_bounds::text::text[], 1)], 60) END
              AS histogram_last,
       array_length(s.histogram_bounds::text::text[], 1) - 1 AS histogram_buckets
  FROM pg_catalog.pg_stats s
 WHERE s.schemaname = $1 AND s.tablename = $2
 ORDER BY s.attname, s.inherited DESC
"""


def _hidden_reason(name: str, denied: set[str], pattern: re.Pattern[str] | None) -> str | None:
    if name in denied:
        return "protected column"
    if pattern is not None and pattern.search(name):
        return "name looks like a credential"
    return None


@tool(group="data", title="Column statistics")
async def pg_column_stats(
    table: str,
    connection: str | None = None,
    columns: list[str] | None = None,
) -> dict[str, Any]:
    """Describe the data of each column from the planner statistics, without reading the
    table: fraction of NULLs, estimated number of distinct values, average width in bytes,
    the most common values with their frequency (up to 10), the range of the histogram,
    and the correlation with the physical order (near 1 or -1 means sorted).

    Use it to see which columns are selective, skewed or mostly empty before writing a
    query or an index. The numbers come from the last ANALYZE; a table never analyzed
    has none, and the answer says so. For protected columns and columns whose name looks
    like a credential only the statistics are given, never sample values.
    """
    async with get_app().read(connection) as db:
        relation = await resolve_relation(db, table, kinds="rpmf")
        profile = db.profile
        reason = relation_denied(
            profile, relation["schema"], relation["name"], configured_only=True
        )
        if reason:
            raise Rejected(reason, rule="relation-denied")
        qualified = f"{relation['schema']}.{relation['name']}"
        attributes = await _table_columns(db, relation["oid"])
        names = [a["name"] for a in attributes]
        if columns:
            _check_columns(columns, names, qualified)
        wanted = list(dict.fromkeys(columns)) if columns else names
        rule = profile.protection_for(relation["schema"], relation["name"])
        denied = set(rule.deny_columns) if rule else set()
        pattern = compile_column_patterns(profile.redact_columns)
        hidden = {n: _hidden_reason(n, denied, pattern) for n in names}
        stats = {
            r["name"]: r
            for r in await db.rows(
                _COLUMN_STATS_SQL,
                [
                    relation["schema"],
                    relation["name"],
                    [n for n, why in hidden.items() if why],
                ],
            )
        }
        info = await db.row(
            """SELECT CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END AS rows,
                      greatest(s.last_analyze, s.last_autoanalyze)::text AS last_analyze
                 FROM pg_catalog.pg_class c
                 LEFT JOIN pg_catalog.pg_stat_all_tables s ON s.relid = c.oid
                WHERE c.oid = $1""",
            [relation["oid"]],
        )
    assert info is not None
    estimated_rows = info["rows"]
    types = {a["name"]: a["type"] for a in attributes}
    result: list[dict[str, Any]] = []
    missing: list[str] = []
    for name in wanted:
        row = stats.get(name)
        if row is None:
            missing.append(name)
            continue
        entry: dict[str, Any] = {
            "column": name,
            "type": types[name],
            "null_fraction": round(row["null_fraction"], 4),
            "avg_width": row["avg_width"],
        }
        n_distinct = row["n_distinct"]
        entry["n_distinct"] = n_distinct
        if n_distinct >= 0:
            entry["distinct_estimate"] = int(n_distinct)
        elif estimated_rows:
            entry["distinct_estimate"] = round(-n_distinct * estimated_rows)
        if row["correlation"] is not None:
            entry["correlation"] = round(row["correlation"], 3)
        if hidden[name]:
            entry["values_hidden"] = hidden[name]
        else:
            if row["common_values"]:
                entry["most_common"] = [
                    {"value": value, "frequency": round(freq, 4)}
                    for value, freq in zip(
                        row["common_values"], row["common_frequencies"] or [], strict=False
                    )
                ]
            if row["histogram_first"] is not None:
                entry["histogram"] = {
                    "first": row["histogram_first"],
                    "last": row["histogram_last"],
                    "buckets": row["histogram_buckets"],
                }
        result.append(entry)
    out: dict[str, Any] = {
        "connection": db.profile.name,
        "table": qualified,
        "estimated_rows": estimated_rows,
        "last_analyze": info["last_analyze"],
        "columns": result,
    }
    notes: list[str] = []
    withheld = [e["column"] for e in result if "values_hidden" in e]
    if withheld:
        notes.append(
            "Sample values are not shown for: "
            + ", ".join(withheld)
            + " (protected, or the name looks like a credential); their statistics are."
        )
    if missing:
        out["no_statistics"] = missing
        if not result:
            notes.append(
                "No statistics exist for this table: it was never analyzed (autovacuum has not "
                "reached it), or this role cannot read the columns. ANALYZE builds them; "
                "pg_count_rows still gives a row estimate."
            )
        else:
            notes.append(
                "No statistics for: " + ", ".join(missing) + " (all values NULL, a column added "
                "after the last ANALYZE, or no SELECT privilege on it)."
            )
    if not info["last_analyze"] and result:
        notes.append("The table has no recorded ANALYZE; the numbers may be stale.")
    if notes:
        out["notes"] = notes
    return out


# ------------------------------------------------------------------------------ pg_export_query

_FILENAME = re.compile(r"\w[\w .-]*")
_EXTENSIONS = {"csv": ".csv", "json": ".json", "jsonl": ".jsonl"}


def _valid_filename(filename: str, fmt: str) -> str:
    name = filename.strip()
    if not name or len(name) > 200:
        raise PgHarborError("`filename` must be a file name of 1 to 200 characters.")
    if "/" in name or "\\" in name or ".." in name or not _FILENAME.fullmatch(name):
        raise Rejected(
            f"{filename!r} is not a plain file name",
            rule="export-filename",
            hint="Give only a name such as customers.csv: letters, digits, spaces, '_', '-' "
            "and '.', with no directory and no '..'. The folder is set in the connection "
            "profile (export_dir).",
        )
    if not Path(name).suffix:
        name += _EXTENSIONS[fmt]
    return name


def _export_directory(configured: str | None) -> Path:
    base = Path(configured).expanduser() if configured else Path.home() / "pgharbor-exports"
    return base


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _unique_names(columns: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for name in columns:
        seen[name] = seen.get(name, 0) + 1
        out.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    return out


def _dump(handle: TextIO, fmt: str, columns: list[str], rows: list[list[Any]]) -> None:
    names = _unique_names(columns)
    if fmt == "csv":
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(names)
        for row in rows:
            writer.writerow([_cell(v) for v in row])
    elif fmt == "jsonl":
        for row in rows:
            handle.write(json.dumps(dict(zip(names, row, strict=True)), ensure_ascii=False) + "\n")
    else:
        handle.write("[\n")
        for i, row in enumerate(rows):
            handle.write(
                ("," if i else "")
                + json.dumps(dict(zip(names, row, strict=True)), ensure_ascii=False)
                + "\n"
            )
        handle.write("]\n")


def _save(
    directory: Path,
    filename: str,
    fmt: str,
    columns: list[str],
    rows: list[list[Any]],
    overwrite: bool,
) -> tuple[Path, int]:
    """Write the file next to its final name, then move it into place."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = directory.resolve()
    final = directory / filename
    if final.parent != directory:
        raise Rejected(f"{filename!r} points outside the export folder", rule="export-filename")
    exists_message = (
        f"{final} already exists. Pass overwrite=true to replace it, or choose another filename."
    )
    if not overwrite and final.exists():
        raise PgHarborError(exists_message)
    if final.is_symlink():
        raise Rejected(f"{final} is a symbolic link", rule="export-filename")
    handle_fd, temporary = tempfile.mkstemp(prefix=".pgharbor-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="") as handle:
            _dump(handle, fmt, columns, rows)
        if overwrite:
            os.replace(temporary, final)
        else:
            try:
                os.link(temporary, final)
            except FileExistsError:
                raise PgHarborError(exists_message) from None
            except OSError:  # a file system without hard links
                if final.exists():
                    raise PgHarborError(exists_message) from None
                os.replace(temporary, final)
            with contextlib.suppress(OSError):
                os.unlink(temporary)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    return final, final.stat().st_size


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} bytes" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} bytes"


@tool(group="data", title="Export a query to a file")
async def pg_export_query(
    sql: str,
    filename: str,
    connection: str | None = None,
    params: list[Any] | None = None,
    format: Literal["csv", "json", "jsonl"] = "csv",
    max_rows: int = 10000,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Run a read-only query and save the result to a local file (CSV, JSON or JSON Lines)
    instead of sending the rows to the conversation. Use it for results too big to read:
    the answer holds only the path, the row count and the size.

    The query goes through the same safety checks as pg_query (one statement, read only,
    protected columns refused, credential-like columns redacted); use `params` ($1, $2, ...)
    for values. `filename` is a plain file name, written to the connection's export folder
    (default ~/pgharbor-exports). Up to `max_rows` rows, never more than 100,000; the file is
    not overwritten unless `overwrite=true`. Exports can be turned off per connection.
    """
    profile = get_app().profile(connection)
    if not profile.allow_export:
        raise Rejected(
            f"exporting to files is disabled for connection '{profile.name}'",
            rule="export-disabled",
            hint="Set allow_export = true in the connection profile, or read the rows with "
            "pg_query.",
        )
    name = _valid_filename(filename, format)
    directory = _export_directory(profile.export_dir)
    target = directory / name
    if not overwrite and await asyncio.to_thread(target.exists):
        raise PgHarborError(
            f"{target} already exists. Pass overwrite=true to replace it, or choose another "
            "filename."
        )
    limit = max(1, min(int(max_rows), _EXPORT_ROW_CAP))
    async with get_app().read(connection) as db:
        result = await run_query(
            db,
            sql,
            params or [],
            max_rows=limit,
            row_cap=_EXPORT_ROW_CAP,
            max_cell_chars=_EXPORT_CELL_CHARS,
            char_budget=_EXPORT_CHAR_BUDGET,
        )
    names = [c.name for c in result.columns]
    path, size = await asyncio.to_thread(
        _save, directory, name, format, names, result.rows, overwrite
    )
    out: dict[str, Any] = {
        "connection": db.profile.name,
        "path": str(path),
        "format": format,
        "rows": len(result.rows),
        "size": _human_size(size),
        "bytes": size,
        "columns": names,
    }
    notes = [n for n in result.notes if not n.startswith("Only the first")]
    if result.has_more:
        out["truncated"] = True
        notes.insert(
            0,
            f"The result has more rows than the {len(result.rows):,} exported "
            f"(max_rows={limit}, hard limit {_EXPORT_ROW_CAP:,}). Add a WHERE clause or LIMIT, or "
            "export in slices.",
        )
    if Path(name).suffix.lower() != _EXTENSIONS[format]:
        notes.append(
            f"The file is written as {format} although its name does not end in .{format}."
        )
    if notes:
        out["notes"] = notes
    return out


_ = grid  # re-exported helper kept for symmetry with catalog.py
