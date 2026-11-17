# Writing a tool

Every pgharbor tool is one async function in `src/pgharbor/tools/`. This page is the
contract those functions follow. `src/pgharbor/tools/schema.py` and
`tests/integration/test_schema.py` are the reference implementation: read them first.

## The shape of a tool

```python
from typing import Any, Literal

from ..app import get_app, tool
from ._util import clamp, grid, like_pattern, page, resolve_relation


@tool(group="performance", title="Table statistics")
async def pg_table_stats(
    connection: str | None = None,
    schema: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Show scan counts, row changes, dead rows and last vacuum/analyze per table.

    Use it to find tables that are read by sequential scans or that need vacuum. ...
    """
    limit = clamp(limit, 50, 500)
    async with get_app().read(connection) as db:
        rows = await db.rows("SELECT ... WHERE ($1::text IS NULL OR n.nspname = $1) LIMIT $2",
                             [schema, limit + 1])
        rows, has_more = page(rows, limit)
    return {"connection": db.profile.name, **grid(rows), "has_more": has_more}
```

Rules:

1. **No `from __future__ import annotations` in tool modules.** The MCP SDK reads the
   real annotation objects to build the input schema.
2. **Parameters** are keyword parameters with defaults. `connection: str | None = None`
   comes first among the optional ones. Use only `str`, `int`, `float`, `bool`,
   `list[...]`, `Literal[...]` and `| None`. Name them the way an agent would guess.
3. **The docstring is the tool description** that the model reads. First sentence: what
   it returns, using the words someone would search for. Then when to use it and what the
   important parameters do. Plain English, under about 900 characters. No implementation
   details.
4. **Name**: `pg_` + verb/noun, lowercase, unique. The function name is the tool name.
5. **Return** a `dict` (or a `str`). The framework serializes it compactly, scrubs
   credentials and caps the size. Never build JSON by hand.

## Talking to the database

`get_app().read(connection)` opens a verified read-only transaction and yields a `Db`:

| Call | Use |
|---|---|
| `await db.rows(sql, params)` | list of dictionaries |
| `await db.row(sql, params)` / `await db.value(sql, params)` | one row / one value |
| `db.v` | `server_version_num` (90624, 130023, 180006 ...) |
| `db.has("pg_stat_io")` | does `pg_catalog.pg_stat_io` exist on this server? |
| `db.ext("hypopg")` | `ExtensionInfo(name, version, schema)` or `None` |
| `db.need_ext("pg_stat_statements")` | same, or a clear `NotAvailable` error |
| `db.need_version(100000, "Declarative partitioning")` | clear error on older servers |
| `db.facts` | version text, database, user, `is_superuser`, `in_recovery` |
| `db.profile` | the connection profile (limits, protected tables) |

Rules:

6. **Values are always bound parameters** (`$1`, `$2`). Never format a value coming from
   a tool argument into SQL text, not even a name: pass names as parameters to a `WHERE`
   clause (`WHERE n.nspname = $1`) or resolve them to an OID first
   (`resolve_relation(db, "schema.table")`) and use the OID.
   When an identifier really must appear in SQL text (reading a user table), take the
   name **from the catalog**, quote it with `guard.quote_ident` / `guard.quote_qualified`,
   and run the statement through `runner.run_query` (next rule).
7. **Reading user data goes through `runner.run_query(db, sql, params, ...)`**, never
   through `db.rows`. That is the guarded path: protected tables and columns, credential
   redaction, row and size limits all apply. `db.rows` is for catalog and statistics
   queries written in this repository.
8. **Qualify catalog objects**: `pg_catalog.pg_class`, `pg_catalog.pg_get_indexdef(...)`.
9. **Version awareness.** PostgreSQL 9.6 through 19 are supported. Build the SQL
   according to `db.v` / `db.has(...)`. Do not trust memory for which column exists in
   which version; look it up:

   ```bash
   scripts/dev/catalog.py rel pg_stat_activity     # columns per version
   scripts/dev/catalog.py fn pg_get_partkeydef     # functions per version
   scripts/dev/catalog.py guc wal_                 # settings per version
   ```

   When a feature does not exist on the server, say so clearly (`db.need_version`,
   `db.need_ext`, or a `"not available on PostgreSQL X"` note in the result) instead of
   failing with a SQL error.
10. **Stay cheap.** Every query runs under the connection's statement timeout, but a tool
    must not be heavy by default: always `LIMIT`, never scan user tables unless the caller
    asked for an exact figure through an explicit parameter.
11. **Never return secrets.** No password hashes (`pg_authid.rolpassword`), no user
    mapping options, no subscription connection strings. Do not select them at all.
12. **Privileges.** Some views hide data from non-superusers (`pg_stat_activity.query`),
    some refuse (`pg_hba_file_rules`). When part of the answer is unavailable, return the
    rest with a `notes` entry explaining what is missing and why.

## Output conventions

- Top level: `{"connection": ..., <data>, "notes": [...]}`. Omit keys that would be
  null or empty when that keeps the answer readable.
- A list of similar rows is returned with `grid(rows, columns)`:
  `{"columns": [...], "rows": [[...], ...]}`. Column names appear once.
- Sizes: human readable (`pg_size_pretty`), plus raw bytes only if useful for sorting.
- Timestamps and intervals: cast to `text` in SQL.
- Lists are limited by a `limit` parameter (`clamp(limit, default, maximum)`); fetch
  `limit + 1` rows and report `has_more` with `page(rows, limit)`.
- Errors: raise `PgHarborError("what happened and what to do")`. Messages are read by a
  model: say which tool or parameter would help.

## Tests

One test module per tool module, in `tests/integration/`. Tests run against real
servers, once per PostgreSQL version (see `tests/conftest.py`):

```python
from ..conftest import Server, call, rows_as_dicts

async def test_table_stats(app: Any, ready: Server) -> None:
    result = await call(pg_table_stats, connection="ro", schema="shop")
    ...
    if ready.at_least(13):
        ...
```

- `app` points the tools at the server under test; `call(tool, **arguments)` calls a tool
  and decodes its JSON; a refusal raises `ToolError`.
- Profiles available: `ro` (superuser, read-only), `limited` (unprivileged role),
  `guarded` (with a protected table), `rw`, `admin`, `strict`, `permissive`, `prod`.
- The sample database is described in `tests/fixture.py`. Assert on its real content.
- A test that needs something the sample database lacks creates it with
  `superuser(ready)` in a schema of its own and drops it afterwards. Do not edit
  `tests/fixture.py` for one test.
- Other test runs may be using the same servers: assert on what must be there, not on
  exact global counters.
- Start the servers with `scripts/pg-matrix.sh up`. While developing you can limit the
  matrix: `PGHARBOR_TEST_SERVERS="9.6:55496,13:55413,18:55418" pytest tests/integration/test_x.py`.
  Before you are done, run the module on the whole matrix.
- `extras_app` / `extras` give a PostgreSQL 18 with third-party extensions (hypopg,
  pgvector, PostGIS, pg_cron, pg_partman, pg_stat_kcache, pg_qualstats,
  pg_wait_sampling, pgaudit); tests using them are skipped when that server is not up.
