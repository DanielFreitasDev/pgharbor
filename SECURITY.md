# Security policy

pgharbor stands between AI agents and real databases, so reports about ways around its
protections are taken seriously.

## Reporting a vulnerability

Please do **not** open a public issue for a security problem.

Use GitHub's private reporting: **Security → Report a vulnerability** on
<https://github.com/DanielFreitasDev/pgharbor>.

Include, if you can:

- the pgharbor version and the PostgreSQL version;
- the connection profile settings that matter (`access`, `function_policy`, `protect`);
- the exact tool call or SQL, and what happened that should not have.

You will get an answer within a few days. Once a fix is released the report is credited
in the changelog, unless you prefer otherwise.

## What counts

- A statement that changes data, structure or session state through a `read-only`
  connection.
- Reading a host file, running a program, or signalling another session through any tool
  that is not explicitly an administrative one.
- Reading a column listed in `deny_columns`, or a table protected by
  `require_index_filter` without an index condition.
- A credential (password, hash, connection string) returned by a tool or written to the
  audit log.
- Bypassing the approval of a protected connection or the confirmation of a write.
- A difference between pgharbor's SQL scanner and PostgreSQL's that hides a function call.

The design and its known limits are described in [docs/security.md](docs/security.md).
Something listed there under "Known limits" is not a vulnerability, but ideas to narrow
those limits are welcome as ordinary issues.

## Supported versions

Fixes are released for the latest version only.
