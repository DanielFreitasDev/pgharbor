# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

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
