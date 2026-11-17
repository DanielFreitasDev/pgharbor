# Security model

pgharbor lets an AI agent work with real databases. This page explains exactly what
protects those databases, in which order, and where the protection ends.

The short version: **a read-only transaction is not a security boundary, so pgharbor
does not rely on one.** It stacks independent layers, and the layers that decide what a
statement *is* are enforced by the PostgreSQL server itself, not by pattern matching.

- [Threat model](#threat-model)
- [Why "BEGIN READ ONLY" is not enough](#why-begin-read-only-is-not-enough)
- [The read path, layer by layer](#the-read-path-layer-by-layer)
- [Functions](#functions)
- [Protected tables and columns](#protected-tables-and-columns)
- [Credentials](#credentials)
- [Approval and confirmation](#approval-and-confirmation)
- [The write path](#the-write-path)
- [Session hygiene](#session-hygiene)
- [Prompt injection](#prompt-injection)
- [Known limits](#known-limits)
- [Hardening checklist](#hardening-checklist)
- [How this is tested](#how-this-is-tested)

## Threat model

The agent is treated as **untrusted input**. It may be wrong, and it may have been
manipulated by text it read (a row, a comment, a web page). pgharbor assumes that any
SQL it receives could be hostile and that any argument could be an injection attempt.

What pgharbor defends:

- the **data and structure** of the database (no change through a read-only connection);
- the **database host** (no file access, no program execution);
- **other sessions and the server** (no cancelling, no locking, no reconfiguration);
- **secrets** (credentials are not returned, configured columns are never read);
- **availability** (time limits, row limits, size limits, optional cost ceiling).

What pgharbor trusts: its own configuration file, the PostgreSQL server, and objects that
already exist in the database only as far as described in [Functions](#functions).

## Why "BEGIN READ ONLY" is not enough

Most database tools for agents wrap the query in a read-only transaction and stop there.
That transaction stops `INSERT`, `UPDATE`, `DELETE` and DDL. It does **not** stop any of
the following. `tests/integration/test_read_only_is_not_enough.py` reproduces eight of
them against a bare `BEGIN TRANSACTION READ ONLY`, without pgharbor, on every supported
version:

| Inside `BEGIN TRANSACTION READ ONLY`, this still works | Effect |
|---|---|
| `SET transaction_read_only = off` as the first statement | the transaction becomes read-write |
| `COMMIT; DROP TABLE ...` sent as one string (simple protocol) | the rest runs outside the transaction |
| `SELECT pg_terminate_backend(pid)` | kills other sessions |
| `SELECT pg_read_file('pg_hba.conf')`, `pg_ls_dir(...)` | reads files on the host |
| `COPY (SELECT ...) TO PROGRAM 'cmd'` | runs a program on the host |
| `SELECT lo_export(oid, '/path')` | writes a file on the host |
| `SELECT dblink_exec(conn, 'DELETE ...')` | writes through a second connection |
| `SELECT set_config('statement_timeout', '0', false)` | removes the time limit |
| `SELECT pg_advisory_lock(1)` | takes a lock that survives the rollback |
| `LOCK TABLE t IN ACCESS EXCLUSIVE MODE` | blocks every other session |
| `SELECT query_to_xml('any sql', ...)` | runs SQL hidden inside a string |
| `SELECT f()` where `f` is declared `STABLE` but calls `dblink_exec` | writes; the label is only a promise |
| `EXPLAIN SELECT f()` with an `IMMUTABLE` `f` | the planner already ran `f` |

Several of these need a privileged role, and several do not (`pg_sleep`, advisory locks,
`set_config`, `query_to_xml`, `SET` are available to everyone). Real incidents followed
this pattern. The reference PostgreSQL MCP server accepted `COMMIT; DROP SCHEMA ...`
inside its read-only transaction
([Datadog Security Labs, 2025](https://securitylabs.datadoghq.com/articles/mcp-vulnerability-case-study-SQL-injection-in-the-postgresql-mcp-server/)),
and filters built on keyword lists have been bypassed through differences between the
filter's tokenizer and the server's.

## The read path, layer by layer

Every statement sent to `pg_query`, `pg_explain`, `pg_validate_sql`, the data tools and
the custom tools goes through these steps. Each step can refuse; refusals name the rule.

### 1. Scan

The SQL text is split into tokens by a scanner written to follow PostgreSQL's own lexer
(`scan.l`): nested comments, dollar quoting, `E''` escapes, quoted identifiers, the 63-byte
identifier limit, non-ASCII characters as identifier characters. Anything ambiguous is
refused (unterminated strings, stray characters, `U&` Unicode escapes).

The scanner is deliberately **not** a parser. It answers one question: *which names does
this text mention, and which of them are followed by an opening parenthesis?* It
over-reports on purpose.

Faithfulness is tested against the real scanner: on hundreds of thousands of random
inputs, every identifier and every call position that PostgreSQL sees is also seen by
pgharbor. (The oracle is `pglast`, used only in tests because of its GPL license.)

Refused here: more than one statement, empty statements, unbalanced parentheses,
statements longer than `max_sql_chars`.

### 2. Vet against the catalog

The names from step 1 are looked up in the server's catalog, **in every schema** (not
through `search_path`): if any function called `f` is unsafe, a call to `f` is refused.

Checked, one round trip at a time, until nothing new turns up:

- every function in call position (see [Functions](#functions));
- every **view** the statement mentions: its definition is scanned the same way, and so
  are the views it uses;
- **row-security policies** of the tables involved;
- the **source code** of user functions written in SQL or PL/pgSQL, and of everything
  they call in turn;
- the **support functions of user-defined aggregates** (an aggregate is only as safe as
  the state function it runs for every row);
- **custom operators**, wherever they are written: in the statement, in a view, in a
  function body, in a `CHECK`. An operator is a function call that does not look like
  one, so the function behind it is judged like a direct call;
- **code attached to user-defined types**: the function behind a cast, the `CHECK` of a
  domain, the functions of the type's operators and operator classes. The types
  followed are the ones the statement names, the column types of the tables it names,
  and the types in the signatures of the functions it calls (passing a value to a
  parameter of a domain type runs the domain's `CHECK`);
- relations that are denied (password catalogs by default, plus `deny_relations`).

A PL/pgSQL function that **builds SQL at run time** (`EXECUTE`, in any of its forms) is
refused as unverifiable: nothing in its source says which tables, columns or functions
it will reach, and no pattern over its strings can tell, because the text may be glued
from pieces or arrive in an argument. `SELECT count_rows('any.table')` is the classic
example. The owner of the configuration can vouch for such a function by name
(`allow_functions`).

All of this happens *before* anything is planned, because planning already executes
`IMMUTABLE` and `STABLE` functions.

Names are matched without their schema, on purpose: a call to `f` is judged against
every function named `f`. The price is an occasional refusal that a schema-aware parser
would not make (a column that happens to be named like a table in another schema drags
that table's types in). The refusal says which object caused it.

### 3. Let the server decide what the statement is

The query is sent as:

```sql
SELECT * FROM (
  <your statement>
) AS _pgharbor LIMIT 0
```

through the **extended query protocol**. Two things follow, and neither depends on
pgharbor's own analysis being right:

- inside a sub-select only a query is valid SQL. `INSERT`, `UPDATE`, `DELETE`, `SET`,
  `COPY`, `DO`, `CALL`, `LOCK`, `COMMIT`, `VACUUM`, `SELECT ... INTO` are syntax errors
  raised by the server's parser;
- in the extended protocol the server accepts exactly one statement per message. A
  second statement is refused by the protocol
  (`cannot insert multiple commands into a prepared statement`).

`SELECT ... FOR UPDATE` and data-modifying `WITH` clauses, which are syntactically
queries, are refused by the read-only transaction and by the server respectively.

`TABLESAMPLE SYSTEM (10)` is not a function call: the sampling method is resolved by the
server among C-language handlers only, so the name after `TABLESAMPLE` is not vetted.

> Drivers fall back to the simple protocol when a statement has no parameters. pgharbor
> always binds one extra, unused parameter, so the extended protocol is used for every
> statement that contains outside text.

### 4. Pre-flight on the plan

`EXPLAIN (VERBOSE, FORMAT JSON)` is run on the statement that will execute. The plan is
the server's own account of what will be touched: views are expanded to base tables and
every scan names its relation. Checked here:

- `deny_relations` of the profile;
- protected tables: no scan without an index condition, no whole-row reference;
- the optional cost ceiling (`max_plan_cost`);
- a second, independent search for forbidden function calls in the plan expressions.

### 5. Run, bounded

The statement that runs is:

```sql
SELECT left(c0::text, N), left(c1::text, N), ...
FROM ( <your statement> ) AS _pgharbor(c0, c1, ...)
LIMIT max_rows + 1
```

Rows are limited and **every value is cut on the server**, so neither a table of a billion
rows nor a single 500 MB value can flood the server's network buffers, pgharbor's memory
or the model's context. The whole answer is also capped (`max_result_chars`).

### 6. Roll back

Reads always end in `ROLLBACK`, then `DISCARD ALL` (see [Session hygiene](#session-hygiene)).

### The transaction itself

Every transaction opens with one message built from constants:

```sql
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = ...;  SET LOCAL lock_timeout = ...;
SET LOCAL idle_in_transaction_session_timeout = ...;
SET LOCAL standard_conforming_strings = on;
SET LOCAL default_transaction_read_only = on;
SELECT current_setting('transaction_read_only'), ...;
```

The final `SELECT` proves the mode and takes the transaction snapshot. After a snapshot
exists, PostgreSQL refuses to switch a transaction to read-write, which closes the
`SET transaction_read_only = off` door. `standard_conforming_strings` is forced because a
server running with it `off` reads `'\''` differently from the scanner, which is exactly
how tokenizer-based filters have been bypassed.

The same settings are also sent as connection start-up options, and for read-only
profiles libpq is asked to verify the session (`target_session_attrs=read-only`). A
pooler that refuses start-up options (PgBouncer) gets neither; there the settings inside
the transaction, which are verified on every call, are the whole mechanism.

On PostgreSQL 11 and later the transaction also starts with JIT compilation off:
pgharbor's catalog checks are short queries, and compiling each one costs more than
running it. A statement of yours whose estimated cost reaches the server's
`jit_above_cost` gets the server's setting back before it runs, so what you measure is
what the server does.

## Functions

| Class | Examples | Read-only connection | Writing connection |
|---|---|---|---|
| Always refused | `pg_read_file`, `pg_ls_dir`, `lo_*`, `dblink*`, `pg_terminate_backend`, `pg_cancel_backend`, `set_config`, `pg_sleep`, session advisory locks, `query_to_xml`, `table_to_xml`, `ts_stat`, `crosstab`, replication slot and WAL control, `pg_stat_reset*`, `pg_reload_conf` | refused | refused |
| Side effect | `nextval`, `setval`, `pg_notify`, `txid_current`, transaction advisory locks | refused | allowed |
| Volatile built-in on the safe list | `random`, `clock_timestamp`, `gen_random_uuid`, `pg_relation_size`, `pg_is_in_recovery`, `pg_blocking_pids` | allowed | allowed |
| Any other volatile built-in | trigger functions, handlers, new functions in future versions | refused | allowed |
| Immutable / stable built-in | almost everything else | allowed | allowed |

"Always refused" cannot be overridden by configuration. The lists were built from the
catalogs of PostgreSQL 9.6 through 19, and a test fails whenever a version adds a
volatile function that has not been classified.

For functions that are **not** built in (yours, or an extension's), `function_policy`
decides:

| `function_policy` | Meaning |
|---|---|
| `strict` | Only names listed in `allow_functions`. |
| `standard` (default) | `IMMUTABLE` / `STABLE` functions are allowed. If written in SQL or PL/pgSQL their source is inspected, recursively, and refused if it reaches a refused function. Refused as unverifiable, unless listed in `allow_functions`: functions that build SQL at run time (`EXECUTE`) and functions in languages pgharbor cannot read (PL/Python, PL/Perl...). `VOLATILE` functions are refused. |
| `permissive` | Everything except "always refused". Nothing is inspected. For disposable development databases. |

`allow_functions` and `deny_functions` accept `schema.name`, a bare `name` and wildcards
(`"postgis.st_*"`).

**Listing a function in `allow_functions` vouches for everything it does.** pgharbor
stops looking inside it: what it reads (protected tables included), what it calls, the
SQL it builds. List functions you have read, not functions you want to stop hearing
about. The "always refused" names stay refused when called directly, whatever the list
says.

Compiled functions (C, from extensions) cannot be read at all. They are judged by name
and by their volatility label; the ones attached to a type (PostGIS and pgvector
operators, for instance) are treated as part of the type.

## Protected tables and columns

```toml
[[connections.prod.protect]]
table = "billing.cards"
deny_columns = ["card_number", "cvv"]
require_index_filter = true
max_rows = 100
note = "Filter by id or customer_id."
```

`require_index_filter` refuses any plan that reads the table without an index condition:
sequential scans and full index scans alike. It is decided on the real plan, so it also
applies through views and joins.

`deny_columns` makes columns unreadable. Because no single check can prove a column is
not used, several are combined:

| Attempt | Stopped by |
|---|---|
| `SELECT card_number ...`, `WHERE card_number = ...`, `ORDER BY`, `md5(card_number)` | the column name anywhere in the statement |
| `SELECT *`, `t.*`, `TABLE billing.cards` | `*` is refused when the plan touches the table |
| `SELECT t`, `row_to_json(t)`, `t::text` | whole-row reference found in the plan |
| `FROM billing.cards AS t(a, b, c)` | column alias lists on the table are refused |
| a view that selects the column under another name, and views over that view | the catalog records which views depend on the column |
| a function, cast or domain `CHECK` whose code reads the table | the table is reached but not in the plan: refused |
| a function that builds its SQL at run time | refused as unverifiable, since it could read anything |
| `pg_stats` (sample values of every column) | denied when a profile protects columns |
| anything that still returns the column directly | the value is replaced by `[redacted]` |

This is defence in depth, and it is honest about being that. **The control that cannot be
bypassed is a database privilege**: connect with a role that has no `SELECT` on the
column. Use `deny_columns` when you cannot change privileges, or in addition to them.

## Credentials

- Passwords come from environment variables, a password file or libpq's own mechanisms.
  They are read when a connection is opened and are never logged, returned or included in
  error messages.
- Catalogs that hold credentials are denied to free SQL: `pg_authid`, `pg_shadow`,
  `pg_user_mapping(s)`, `pg_subscription`, plus `pg_largeobject` and raw `pg_statistic`.
  The dedicated tools read what is useful from them without the secret part.
- Result columns whose name looks like a credential (`password`, `senha`, `secret`,
  `token`, `api_key` ...; see `redact_columns`) come back as `[redacted]`, by the name in
  the result and by the name of the underlying table column.
- Any text leaving the server is scrubbed of embedded credentials: `password=...` in a
  connection string, `scheme://user:password@host`, `PASSWORD '...'` in a logged
  statement.

Redaction by name protects against accidents. It is not a boundary: an expression over
the column gets a different name. Use `protect` for columns that matter.

## Approval and confirmation

Two decisions belong to a person, not to the model:

- **Using a protected connection.** A connection whose `environment` is `production`
  (or that sets `approval = "session"`) must be unlocked with `pg_request_access`. The
  user sees a dialog naming the connection, the server and the reason.
- **Changing the database.** Every write is confirmed in a dialog showing the statement.

Both use MCP *elicitation*: the dialog is shown by the client and answered by the user,
so the confirmation still means something if the model has been manipulated.

When the client cannot show dialogs (headless runs), the profile decides:
`approval_fallback = "deny"` / `write_confirmation = "dialog"` refuse, while
`"conversation"` accepts a flag in which the agent states that the user asked for it. The
second mode is a convention, not a control, and should be chosen knowingly.

Write tools also carry the `anthropic/requiresUserInteraction` mark, which asks Claude
Code for a permission prompt on every call, even in its permissive modes. What a client
does with that mark is the client's decision; the confirmation above is pgharbor's own.

## The write path

Write tools are not even listed unless some connection has `access = "read-write"` or
`"admin"`. On such a connection:

- one statement per call, classified conservatively: `INSERT`, `UPDATE`, `DELETE`,
  `MERGE`, `CALL` need `read-write`; anything else needs `admin`;
- transaction control and session commands (`BEGIN`, `COMMIT`, `SET`, `RESET`, `LOCK`,
  `COPY`, `LISTEN`, `DISCARD`, `LOAD`, `PREPARE` ...) are never accepted;
- the always-refused functions stay refused;
- `dry_run` executes inside a transaction and rolls back, to show the effect first;
- the user confirms; the result and the confirmation method go to the audit log.

The access mode lives in the configuration file. No tool can raise it.

## Session hygiene

A connection that is reused carries state: settings, prepared statements, advisory
locks, temporary tables. After every call pgharbor runs `ROLLBACK` (or `COMMIT` for a
confirmed write) and then `DISCARD ALL`. A connection on which any of that fails, or
that was interrupted in the middle of a statement, is closed instead of reused.

## Prompt injection

Rows, comments, object names and error messages are **data**. pgharbor says so in its
server instructions and in query results, and it never executes anything found in data.

No wording makes a model immune. What limits the damage is what the agent *can* do:
read-only by default, no network or file access through the database, human confirmation
for writes and for production. Keep in mind the "lethal trifecta": private data,
untrusted content and a way to send data out. pgharbor provides the first, any database
may contain the second; be careful about which other tools the same agent has.

## Known limits

- **Existing database objects are trusted as far as their source can be read.** Views,
  policies, SQL and PL/pgSQL functions, domain checks and the code behind operators and
  casts are read. What cannot be read is refused (other languages, SQL built at run
  time) or trusted (compiled extension code). Not followed at all: an operator class
  defined for a built-in type and used by an index, index expressions, and code reached
  only through an implicit cast between two types the statement never names. All of
  these require someone with `CREATE` privilege to have set a trap in advance; none can
  be created through a read-only connection.
- **`allow_functions` and `function_policy = "permissive"` are statements of trust.**
  What they cover is not inspected, and the protections for tables and columns do not
  see through it.
- **Checks by name refuse too much rather than too little.** See the note at the end of
  [step 2](#2-vet-against-the-catalog).
- **Column protection is defence in depth**, as explained above.
- **Timing and estimates.** A read-only user can still learn about data it may read, and
  measure how long queries take. Row and time limits bound the cost, not the knowledge.
- **A superuser connection is the worst case.** pgharbor blocks the dangerous functions
  by name, which is exactly one layer. With an unprivileged role most of them do not
  exist as a risk at all.
- **Availability.** `statement_timeout` bounds time, not memory. A query can still use
  `work_mem` per plan node and write temporary files until the limit. Set
  `temp_file_limit` and a `CONNECTION LIMIT` on the role.
- **HTTP transports** are not offered; pgharbor speaks stdio only, so there is no network
  listener to protect.

## Hardening checklist

1. Create a role for the agent, not a superuser:
   ```sql
   CREATE ROLE agent LOGIN PASSWORD '...' CONNECTION LIMIT 3;
   GRANT pg_read_all_stats TO agent;                 -- PostgreSQL 10+, optional
   GRANT USAGE ON SCHEMA app TO agent;
   GRANT SELECT ON ALL TABLES IN SCHEMA app TO agent;
   REVOKE SELECT ON app.users FROM agent;
   GRANT SELECT (id, name, created_at) ON app.users TO agent;   -- column privileges
   ALTER ROLE agent SET default_transaction_read_only = on;
   ALTER ROLE agent SET statement_timeout = '30s';
   ALTER ROLE agent SET temp_file_limit = '1GB';     -- needs a superuser to set
   ```
2. Give production `environment = "production"` so its use needs approval.
3. Use `protect` for tables that are huge or hold sensitive columns.
4. Set `max_plan_cost` on production.
5. Keep `function_policy = "standard"` or `"strict"`.
6. Read the audit log (`pg_audit_log`) now and then.

## How this is tested

- `tests/integration/test_guard.py` runs about 250 cases (56 queries that must work,
  158 attacks that must be refused, 34 protected-table cases) on **every PostgreSQL
  version from 9.6 to 19**, then proves that nothing in the database changed: no table,
  no row, no sequence value, no lock, no large object, no replication slot.
- `tests/integration/test_read_only_is_not_enough.py` proves the premise of this
  document: the same attacks, sent to a bare read-only transaction, succeed.
- `tests/integration/test_all_tools.py` runs every read tool on every version, as a
  superuser and as an unprivileged role, and fails if any output looks like a credential.
- `tests/unit/test_lexer_oracle.py` compares the scanner with PostgreSQL's own.
- `tests/unit/test_functions.py` checks the function lists against the catalogs of all
  supported versions.
- `tests/integration/test_admin.py` checks access modes and the confirmation dialog on
  both MCP protocol generations.

Found a way through? See [SECURITY.md](../SECURITY.md).
