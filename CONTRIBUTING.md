# Contributing

Thank you for helping. This page gets you from a clone to a green test run.

## Set up

You need Python 3.11 or newer, [uv](https://docs.astral.sh/uv/) and Docker.

```bash
git clone https://github.com/DanielFreitasDev/pgharbor
cd pgharbor
uv sync --group dev --group oracle     # "oracle" installs pglast for the scanner tests
scripts/pg-matrix.sh up                # PostgreSQL 9.6 through 19, one container each
uv run pytest                          # the whole suite, on every version
```

`scripts/pg-matrix.sh up 13 18` starts only some versions; `down` removes the containers;
`up extras` adds a PostgreSQL 18 with third-party extensions (hypopg, pgvector, PostGIS,
pg_cron ...) used by the extension tests. Tests are skipped, not failed, for servers that
are not running. Unit tests need no server at all: `uv run pytest tests/unit`.

Limit the matrix while iterating:

```bash
PGHARBOR_TEST_SERVERS="9.6:55496,18:55418" uv run pytest tests/integration/test_schema.py
```

## Before opening a pull request

```bash
uv run ruff format src tests scripts
uv run ruff check src tests scripts
uv run mypy
uv run pytest
uv run python scripts/dev/gen_docs.py     # regenerates docs/tools.md and the README tables
```

## Where things are

| Path | What |
|---|---|
| `src/pgharbor/lexer.py`, `guard.py` | SQL scanner and lexical analysis |
| `src/pgharbor/vetting.py`, `functions.py` | catalog-backed checks and function lists |
| `src/pgharbor/runner.py`, `plan.py` | the guarded read path and plan pre-flight |
| `src/pgharbor/session.py` | connections, transactions, session reset |
| `src/pgharbor/app.py`, `server.py` | tool registry and MCP server |
| `src/pgharbor/tools/` | the tools, one module per area |
| `tests/fixture.py` | the sample database used by integration tests |
| `scripts/dev/catalog.py` | look up catalogs, functions and settings across versions |

## Adding or changing a tool

Read [docs/writing-tools.md](docs/writing-tools.md). In short: one async function with a
docstring written for the model, values always bound as parameters, version-aware SQL
checked with `scripts/dev/catalog.py`, and an integration test that runs on the whole
matrix.

## Changing the safety layers

Changes to `lexer.py`, `guard.py`, `vetting.py`, `functions.py`, `runner.py` or
`session.py` need a test that fails without the change. If you found a statement that
should be refused and is not, add it to `REFUSED` in `tests/integration/test_guard.py`
first. Security reports go through [SECURITY.md](SECURITY.md), not public issues.

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/). The history is written in
Brazilian Portuguese; English is welcome in pull requests.
