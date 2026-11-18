# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-11-18

### Added

- Management panel: `pgharbor ui` opens a local web page (English, Brazilian Portuguese
  and Spanish; light and dark themes) that shows calls, refusals, failures and sessions,
  searches the audit log, and edits everything in the configuration file. A change is
  validated by the server's own loader, reviewed as a difference, flagged when it reduces
  the protection, backed up and logged. See [docs/ui.md](docs/ui.md).
- Emergency pause: the owner can stop database access at once, for every connection or
  for some, in every running session. A paused call is refused with the rule `paused`.
- Session files: each running server records which client started it, from which folder,
  with which configuration and which connections were approved, so the owner can see who
  is using pgharbor.
- Audit entries carry a `session` identifier that ties a call to a session.
- `pgharbor ui --read-only`, `--port`, `--idle-timeout` and `--no-open`.
- The architecture picture now shows the panel, and the README has screenshots of it.

### Changed

- New runtime dependency `tomlkit` (used only by the panel, to edit the configuration file
  without losing comments); `starlette` and `uvicorn`, already installed with the MCP SDK,
  are now declared. The MCP server itself loads none of the panel's code.
- The server instructions tell agents that the configuration file and the panel belong to
  the user.

## [0.1.0] - 2026-11-17

First public release.

### Added

- Guarded read path: a PostgreSQL-faithful SQL scanner, catalog-backed vetting of
  functions, views, row-security policies, aggregates, operators, casts and domain
  checks, refusal of functions that build SQL at run time, server-side statement typing
  through the extended query protocol, a pre-flight check on the query plan, and
  server-side row and value limits.
- Connection profiles with access modes (`read-only`, `read-write`, `admin`), function
  policies, protected tables and columns, plan cost ceiling, credential redaction.
- Human approval for protected connections and human confirmation for writes, through
  MCP elicitation, on both protocol generations (2025-11-25 and 2026-07-28).
- Tools for schema discovery, DDL reconstruction, search, dependencies, data sampling,
  performance, health, replication, security, extensions and administration.
- Custom tools declared in the configuration file.
- Append-only audit log.
- Support for PostgreSQL 9.6 through 19, each tested in the integration suite.
