# Configuration

pgharbor reads one TOML file. It is the only place where connections and their rules are
defined: nothing the agent sends, and nothing read from a database, can change it.

- [Where the file lives](#where-the-file-lives)
- [Without a file](#without-a-file)
- [Structure](#structure)
- [Connections](#connections)
- [Protected tables](#protected-tables)
- [Audit log](#audit-log)
- [Turning tools off](#turning-tools-off)
- [Custom tools](#custom-tools)
- [Command line](#command-line)
- [Complete example](#complete-example)

## Where the file lives

First match wins:

1. `pgharbor --config /path/to/file.toml`
2. the `PGHARBOR_CONFIG` environment variable
3. `~/.config/pgharbor/config.toml` (`%APPDATA%\pgharbor\config.toml` on Windows;
   `$XDG_CONFIG_HOME` is honoured)

`pgharbor init` writes a starter file, `pgharbor config-path` prints the default path and
`pgharbor check` validates the file and tries the connections (see
[Command line](#command-line)).

Everything on this page can also be edited in a browser: `pgharbor ui` opens the
[management panel](ui.md), which shows every setting with an explanation, validates and
reviews each change, and keeps a backup of the file on every save.

A file in the current directory is never picked up by itself: opening a repository must
not be able to hand the agent a new set of database rules.

## Without a file

For a quick start, one connection named `default` is created from the environment:

| Variable | Meaning |
|---|---|
| `PGHARBOR_DSN` or `DATABASE_URL` | connection string or URI |
| `PGHOST`, `PGDATABASE`, `PGUSER`, `PGSERVICE` ... | libpq's own variables, used when no DSN is set |
| `PGHARBOR_ACCESS` | `read-only` (default), `read-write` or `admin` |
| `PGHARBOR_ENVIRONMENT` | label of the connection, `development` by default |

## Structure

```toml
default_connection = "dev"      # optional: used when a tool is called without `connection`

[defaults]                      # applied to every connection that does not override it
[connections.NAME]              # one table per database
[[connections.NAME.protect]]    # zero or more protected tables
[connections.NAME.params]       # extra libpq parameters
[audit]
[tools]
[custom_tools.NAME]
```

Durations accept a number of milliseconds or a string: `"500ms"`, `"30s"`, `"2min"`, `"1h"`.

The file is validated strictly, and a mistake stops the server from using it:

- an unknown key is an error, at any level (a typo never silently disables a rule);
- `[defaults]` takes the rule and limit keys only. What identifies one database (`host`,
  `port`, `dbname`, `user`, `user_env`, `password`, `password_env`, `password_file`,
  `dsn`, `dsn_env`, `description`, `protect`) belongs in the connection;
- numbers have ranges: `pool_size` 1 to 20, `max_cell_chars` at least 16,
  `connect_timeout` at least one second, `max_rows` not above `max_rows_limit`, time
  limits above zero.

## Connections

The table name is what the agent passes as `connection`: lowercase letters, digits, `-`
and `_`, up to 32 characters.

### Where to connect

| Key | Meaning |
|---|---|
| `host`, `port`, `dbname` | server address |
| `user` / `user_env` | user name, or the name of the environment variable holding it |
| `password_env` | name of the environment variable holding the password (recommended) |
| `password_file` | path of a file whose content is the password |
| `password` | the password itself. Avoid; `pgharbor check` warns if the file is readable by others |
| `dsn` / `dsn_env` | a full connection string or URI, or the variable holding one |
| `role` | `SET ROLE` to this role in every transaction: connect as one user, act as a weaker one |
| `[connections.NAME.params]` | other libpq parameters: `sslmode`, `sslrootcert`, `sslcert`, `sslkey`, `service`, `passfile`, `channel_binding`, `gssencmode`, `target_session_attrs`, `keepalives*`, `connect_timeout`, `hostaddr` ... Numbers may be written with or without quotes |

libpq's own mechanisms work as usual: `~/.pgpass`, `pg_service.conf`, `PG*` variables,
client certificates.

### What is allowed

| Key | Default | Meaning |
|---|---|---|
| `description` | | shown to the agent by `pg_connections` |
| `environment` | `"development"` | free label. `production` (or `prod`, `live`, `producao`) makes the connection require approval |
| `access` | `"read-only"` | `read-only`, `read-write` (adds INSERT/UPDATE/DELETE/MERGE/CALL) or `admin` (adds DDL and maintenance) |
| `approval` | by environment | `"never"` or `"session"`: the user must approve the connection once per session through `pg_request_access` |
| `approval_ttl` | `0` | how long an approval lasts; `0` means until the server stops |
| `approval_fallback` | `"conversation"` | for clients that cannot show a dialog: `"deny"`, or `"conversation"` to accept the agent's statement that the user asked for this environment |
| `write_confirmation` | `"dialog"` | `"dialog"` (required, refuse without one), `"conversation"` (accept the agent's statement that the user said yes) or `"none"` (automation and test databases only) |
| `function_policy` | `"standard"` | `strict`, `standard` or `permissive`. See [security](security.md#functions) |
| `allow_functions` | `[]` | functions to accept although they are `VOLATILE` or unknown: `"schema.name"` or a bare `"name"`, wildcards allowed. It cannot release the [always refused](security.md#functions) list |
| `deny_functions` | `[]` | functions never acceptable on this connection, same patterns |
| `deny_relations` | `[]` | tables and views that free SQL may not read: `"schema.table"`, wildcards allowed |
| `expose_secret_catalogs` | `false` | allow free SQL on `pg_authid`, `pg_shadow` and the other credential catalogs |
| `redact_columns` | credential-like names | regular expressions; result columns matching them come back as `[redacted]`. `[]` turns it off |
| `allow_explain_analyze` | `false` | let `pg_explain` really execute the query to time it |
| `allow_export` | `true` | let `pg_export_query` write results to files |
| `export_dir` | `~/pgharbor-exports` | the only directory `pg_export_query` writes to |
| `import_dir` | `~/pgharbor-imports` | the only directory `pg_import_csv` reads from |

### Limits

| Key | Default | Meaning |
|---|---|---|
| `statement_timeout` | `"30s"` | time limit of every statement |
| `lock_timeout` | `"3s"` | how long to wait for a lock |
| `idle_in_transaction_session_timeout` | `"10s"` | safety net for a stuck transaction |
| `max_rows` | `200` | rows returned when the agent does not ask for a number |
| `max_rows_limit` | `5000` | the most rows the agent can ask for |
| `max_cell_chars` | `2000` | values longer than this are cut, on the server. A call may ask for more for itself, up to 20,000 (or this value, if higher); `max_result_chars` still bounds the answer |
| `max_result_chars` | `40000` | size of the rows of one answer; when it is reached the answer says there is more. Whatever this is set to, no tool answer exceeds 48,000 characters: a longer table loses rows from the end and says so |
| `max_sql_chars` | `100000` | longest statement accepted |
| `max_plan_cost` | none | refuse queries whose estimated cost is higher (planner units) |

### Connection handling

| Key | Default | Meaning |
|---|---|---|
| `pool_size` | `2` | connections kept for this profile; calls beyond that wait |
| `idle_close` | `"5min"` | close an unused connection after this long; `0` closes after every call |
| `connect_timeout` | `"10s"` | how long to wait for the server; a `connect_timeout` under `params` (libpq, in seconds) wins over it |
| `application_name` | `"pgharbor"` | what `pg_stat_activity` shows |

Connections through **PgBouncer** work: when the pooler refuses start-up options, the same
settings are applied inside every transaction instead.

## Protected tables

```toml
[[connections.prod.protect]]
table = "billing.cards"            # schema.table; wildcards such as "audit.*" work
deny_columns = ["card_number", "cvv"]
require_index_filter = true
max_rows = 100
note = "Filter by id or customer_id."
```

| Key | Meaning |
|---|---|
| `table` | which table the rule is for |
| `deny_columns` | columns that cannot be read, filtered on or used in an expression |
| `require_index_filter` | refuse any query plan that reads the table without an index condition |
| `max_rows` | a lower row limit for queries touching the table |
| `note` | added to refusals: tell the agent how the table should be queried |

How each rule is enforced, and its limits, is in [security](security.md#protected-tables-and-columns).
For a partitioned table, use a pattern that also covers the partitions.

## Audit log

```toml
[audit]
enabled = true
path = "~/.local/state/pgharbor/audit.jsonl"   # default
log_sql = true
log_params = false
```

One JSON line per tool call: time (`ts`), `tool`, `connection` (by name, also when the
default one was used), `session` (which server process wrote the line), `outcome`, the
`rule` that refused it, the `sql` text (for a custom tool, its configured statement),
duration (`ms`) and a `detail` message on errors. Result rows are never logged. What the
owner does in the [management panel](ui.md) is logged too, as `panel.config`,
`panel.pause` and `panel.test`, with `session` set to `panel`. Bound parameters, and the arguments of custom tools, are logged
only with `log_params = true`. The file is created with mode `600`.

Without `path`, the log goes to `$XDG_STATE_HOME/pgharbor/audit.jsonl`
(`~/.local/state/pgharbor/` by default; `%LOCALAPPDATA%\pgharbor\` on Windows).

While `enabled` is true, each running server also keeps a small file under `sessions/` in
that same directory, refreshed every 15 seconds and named by its `session` identifier:
the client that started it, its folder, the configuration it loaded and the connections
the user approved. The panel uses those files to show who is connected; they are removed
two weeks after a session ends. `enabled = false` stops both the log and these files.

## Turning tools off

```toml
[tools]
disabled = ["pg_export_query"]
disabled_groups = ["admin", "extensions"]
```

Groups: `core`, `schema`, `data`, `performance`, `health`, `replication`, `security`,
`extensions`, `admin`, `custom`. `pgharbor tools` lists what a configuration exposes.

Tools that write are never listed when no connection could use them.

## Custom tools

A custom tool is a query you approve once and expose under its own name. It is the
safest way to give an agent access to production: no free SQL at all, only questions you
chose.

```toml
[custom_tools.orders_of_customer]
description = "List the most recent orders of one customer, newest first, with status and total."
sql = """
SELECT id, status, total, placed_at
  FROM shop.orders
 WHERE customer_id = $1 AND status = coalesce($2, status)
 ORDER BY placed_at DESC
"""
connections = ["prod"]        # optional: where the tool is available
max_rows = 50

[[custom_tools.orders_of_customer.params]]
name = "customer_id"
type = "integer"              # text | integer | number | boolean
description = "Customer id"

[[custom_tools.orders_of_customer.params]]
name = "status"
type = "text"
required = false              # optional parameters are NULL when not given...
# default = "paid"            # ...or take this value
```

Names are lowercase: a tool name is a letter followed by letters, digits or `_` (2 to 48
characters), a parameter name the same (up to 40).

The tool appears as `pgx_orders_of_customer`. Parameters are bound in the order they are
declared (`$1`, `$2`, ...), never pasted into the SQL. The query runs through the same
read path as `pg_query`, so limits, redaction and protected tables still apply. Combine
it with `[tools] disabled = ["pg_query"]` to remove free SQL from a setup entirely.

## Command line

```text
pgharbor [--config FILE] [serve]              run the MCP server on stdio (the default)
pgharbor [--config FILE] check [NAME ...]     validate the file and test connections
pgharbor [--config FILE] check --all          ... including those that need approval
pgharbor [--config FILE] tools                list the tools this configuration exposes
pgharbor [--config FILE] ui                   open the management panel in the browser
pgharbor init [--force]                       write a starter file (--force overwrites)
pgharbor config-path                          print the default file path
pgharbor --version
```

`check` prints one line per connection: the server version, the database and the role it
connected as, or the reason it could not. Connections that need approval (production) are
**skipped** unless you name them (`pgharbor check prod`) or pass `--all`, so that a
routine check never opens production. `--config` may come before or after the subcommand.

`ui` starts the [management panel](ui.md) on `127.0.0.1` and prints its address, which
carries a key that is new on every start. Options: `--port PORT` (default 7432; 0 picks a
free one), `--read-only` (monitor only), `--idle-timeout MINUTES` (default 30; 0 never
stops) and `--no-open` (do not open the browser).

## Complete example

```toml
default_connection = "dev"

[defaults]
statement_timeout = "30s"
max_rows = 200

[connections.dev]
description = "Development database"
environment = "development"
host = "dev-db.internal"
dbname = "app"
user_env = "APP_DB_USER"
password_env = "APP_DB_PASSWORD"
access = "read-write"

[connections.prod]
description = "Production, read-only"
environment = "production"       # needs approval once per session
host = "prod-db.internal"
dbname = "app"
user_env = "APP_DB_USER"
password_env = "APP_DB_PASSWORD"
access = "read-only"
statement_timeout = "15s"
max_plan_cost = 500000
pool_size = 1
idle_close = 0

[connections.prod.params]
sslmode = "verify-full"

[[connections.prod.protect]]
table = "billing.cards"
deny_columns = ["card_number", "cvv"]
require_index_filter = true
note = "Filter by id or customer_id."

[audit]
enabled = true
```
