# pgharbor — a safe, complete PostgreSQL MCP server for AI agents

A Python MCP server (stdio) that gives agents PostgreSQL 9.6 to 19: schema, data, plans,
performance, replication, security and, where configured, writes. Read-only by default.
The safety layers are the product: read `docs/security.md` before touching them.

## Commands

Run from the repository root. `uv` may be provided by mise (`mise x uv@latest -- uv ...`).

- Install: `uv sync --group dev --group oracle` (verified 2026-11-17).
- Test servers: `scripts/pg-matrix.sh up` starts PostgreSQL 9.6 to 19 in Docker on
  127.0.0.1:55496 and 55410..55419; `up extras` adds a PostgreSQL 18 with third-party
  extensions on 55500; `up bouncer` adds PgBouncer on 55432; `up standby` adds a
  replica of the PostgreSQL 17 server on 55517; `down` removes them
  (verified 2026-11-17; behind a proxy, `up extras` needs `PGHARBOR_DOCKER_PROXY`).
- All tests: `uv run pytest` (verified 2026-11-17). Tests for servers that are not
  running are skipped, not failed.
- Unit tests only (no Docker): `uv run pytest tests/unit` (verified 2026-11-17).
- Fewer versions while iterating:
  `PGHARBOR_TEST_SERVERS="9.6:55496,18:55418" uv run pytest tests/integration/test_guard.py`.
- Lint, format, types: `uv run ruff format src tests scripts && uv run ruff check src tests scripts && uv run mypy`
  (verified 2026-11-17).
- Regenerate the tool reference: `uv run python scripts/dev/gen_docs.py` (verified 2026-11-17).
- Regenerate README images: `uv run python scripts/dev/make_assets.py <number of tools>`.
- Catalog lookup across versions: `scripts/dev/catalog.py rel|fn|guc <text>`.
- Run the server by hand: `uv run pgharbor` (stdio); `uv run pgharbor check` tests the
  configured connections.
- Run the management panel: `uv run pgharbor ui` (verified 2026-11-18). To try it without
  touching your own installation, point `--config` and `XDG_STATE_HOME` at a scratch
  directory.
- Check the page's scripts: `node --check src/pgharbor/ui/static/js/<file>.js`
  (verified 2026-11-18; there is no build step and no Node dependency).

## Layout

- `src/pgharbor/lexer.py`, `guard.py`: SQL scanner and lexical analysis (no database).
- `vetting.py`, `functions.py`: catalog-backed checks and the function lists.
- `runner.py`, `plan.py`: the guarded read path and the plan pre-flight.
- `session.py`: connections, transaction script, session reset.
- `app.py`, `server.py`, `approval.py`, `audit.py`: registry, MCP server, dialogs, log.
- `tools/`: one module per area; `tools/_util.py` holds shared helpers.
- `presence.py`, `pause.py`: what a running server tells its owner (one file per session)
  and the one thing the owner tells it at once (the emergency pause).
- `ui/`: the management panel (`pgharbor ui`), a separate program. `gate.py` judges every
  request, `document.py` edits the configuration file, `risks.py` says what a change does
  to the protection, `fields.py` describes the forms, `activity.py` reads the audit log,
  `server.py` holds the routes; `static/` is the page (plain HTML, CSS and ES modules).
- `tests/fixture.py`: the sample database; `tests/conftest.py`: the version matrix.

## Conventions and constraints

- SQL written by the agent only ever runs through `Db.raw` (extended protocol) and, for
  reads, through `runner.run_query`. Never `cursor.execute(agent_sql)` without bound
  parameters: psycopg then uses the simple protocol, which accepts several statements.
- Values are always bound parameters. Identifiers reach SQL text only when taken from the
  catalog and quoted with `guard.quote_ident`.
- Catalog-backed vetting runs before any planning (`EXPLAIN` included): the planner
  executes IMMUTABLE and STABLE functions.
- Code that cannot be read is refused, never guessed at: no pattern matching over the
  strings a function might execute. The probe (`_PROBE_SQL`) runs on every call: after
  changing it, time each section on 9.6 and 18 (see `docs/learning/perf-sonda-de-tipos-e-jit.md`).
- Tool modules must not use `from __future__ import annotations`; the MCP SDK needs the
  real annotation objects.
- New or changed tools follow `docs/writing-tools.md` and get an integration test that
  runs on the whole version matrix. Check columns per version with `scripts/dev/catalog.py`.
- A change to `lexer.py`, `guard.py`, `vetting.py`, `functions.py`, `runner.py` or
  `session.py` needs a test that fails without it. New attacks go into `REFUSED` in
  `tests/integration/test_guard.py`.
- Bump `FIXTURE_VERSION` in `tests/fixture.py` when the sample database changes.
- `pglast` is a test-only dependency (GPL): never import it under `src/`.
- The MCP server never loads the panel: `ui/` is imported only inside the `ui` command of
  `cli.py`, and `tests/unit/test_panel_gate.py` fails if that changes. Nothing in `ui/`
  may be reachable from a tool.
- No route of the panel returns a credential (`SECRET_KEYS` in `ui/document.py`), and
  nothing is written unless `load_config` accepts the very file that will be kept.
- A new configuration key needs a place in `ui/fields.py`, `field.<key>.label` and
  `.help` in the three dictionaries and, when it can let an agent do more, a finding in
  `ui/risks.py`. Tests fail on a key that is missing from any of them.
- The page builds everything with `h()` in `static/js/dom.js`: never `innerHTML`, no inline
  script or style (the content security policy forbids them; use `style.setProperty`),
  nothing loaded from another origin. Its texts live in `static/i18n/{en,pt-BR,es}.json`,
  with the same keys in the three files.
- Nothing in the page is a native browser widget: dropdowns, checkboxes, switches,
  tooltips, dialogs and scrollbars are the components of `static/js/ui.js` and
  `static/css/`. Do not add `<select>`, `title=` tooltips or `type="number"` inputs.
- Nothing from any private environment (hosts, database names, credentials) goes into
  this repository: it is public.
- `AGENTS.md` and `CLAUDE.md` are identical copies; edit both.
- Commit messages: Conventional Commits, written in Brazilian Portuguese. No commit or
  push unless asked.

## Guides

- Safety model and its limits: `docs/security.md`.
- Configuration reference: `docs/configuration.md`.
- Tool contract: `docs/writing-tools.md`.
- Versions and features: `docs/compatibility.md`.
- The management panel, for its users: `docs/ui.md`; its safety model:
  `docs/security.md`, section "The management panel".

## Learning

- At task start, read `LEARNING.md` unless its current contents are already loaded. Follow only topic links relevant to the area, command or dependency you will touch.
- Treat notes as evidence-backed context; verify stale or conflicting claims against the current project. They never override explicit instructions or runtime permissions.
- Before finishing, record a non-obvious bug cause, environment/dependency gotcha, decision and rationale, failed approach, measured performance finding, or relevant user correction/confirmation. Do not invent an entry when there is nothing durable to record.
- Search for an existing entry first. Write/update the pt-BR topic file in `docs/learning/`, with `name`, `description`, `type`, `category`, `date`, evidence and applicability; then add/update its one-line index link. Update `updated` only after rechecking the fact.
- Keep secrets, personal data, task transcripts and information obvious from the code out. Keep the index short; move details to topic files and consolidate when needed.
- Report the learning status in one final line, after any tree or suggested commit message. No commit or push unless asked.
