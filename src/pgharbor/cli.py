"""Command line entry point.

pgharbor                 run the MCP server on stdio (what an MCP client launches)
pgharbor init            write a starter configuration file
pgharbor check           validate the configuration and try every connection
pgharbor tools           list the tools this configuration exposes
pgharbor config-path     print where the configuration file is expected
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .config import Config, default_config_path, load_config
from .errors import PgHarborError

EXAMPLE_CONFIG = """\
# pgharbor configuration. Reference: https://github.com/DanielFreitasDev/pgharbor/blob/main/docs/configuration.md
#
# Secrets do not belong in this file: point to environment variables instead.

# Applied to every connection unless the connection says otherwise.
[defaults]
statement_timeout = "30s"
lock_timeout = "3s"
max_rows = 200

# One table per database. The name is what the agent passes as `connection`.
[connections.dev]
description = "Development database"
environment = "development"
host = "localhost"
port = 5432
dbname = "app"
user_env = "PGHARBOR_DEV_USER"          # name of the variable that holds the user
password_env = "PGHARBOR_DEV_PASSWORD"  # name of the variable that holds the password
access = "read-only"                    # read-only | read-write | admin

# A production database: read-only, and the user must approve its use in every session.
# [connections.prod]
# description = "Production (read-only)"
# environment = "production"
# host = "db.internal"
# dbname = "app"
# user_env = "PGHARBOR_PROD_USER"
# password_env = "PGHARBOR_PROD_PASSWORD"
# access = "read-only"
# max_plan_cost = 500000
#
# [[connections.prod.protect]]
# table = "billing.cards"
# deny_columns = ["card_number", "cvv"]
# require_index_filter = true
# note = "Filter by id or customer_id."

[audit]
enabled = true
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pgharbor",
        description="A safe harbor between AI agents and PostgreSQL (MCP server).",
    )
    parser.add_argument("--version", action="version", version=f"pgharbor {__version__}")
    parser.add_argument("--config", metavar="PATH", help="configuration file to use")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the MCP server on stdio (default)")
    init = sub.add_parser("init", help="write a starter configuration file")
    init.add_argument("--force", action="store_true", help="overwrite an existing file")
    sub.add_parser("check", help="validate the configuration and test every connection")
    sub.add_parser("tools", help="list the tools this configuration exposes")
    sub.add_parser("config-path", help="print the default configuration file path")
    args = parser.parse_args(argv)

    if args.config:
        os.environ["PGHARBOR_CONFIG"] = args.config
    try:
        if args.command in (None, "serve"):
            return _serve()
        if args.command == "init":
            return _init(args.force)
        if args.command == "check":
            return asyncio.run(_check())
        if args.command == "tools":
            return _tools()
        if args.command == "config-path":
            print(default_config_path())
            return 0
    except PgHarborError as exc:
        print(f"pgharbor: {exc}", file=sys.stderr)
        return 2
    return 0


def _serve() -> int:
    # On stdio the protocol owns stdout: every log line goes to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="pgharbor: %(message)s")
    from .server import build_server

    try:
        config = load_config()
    except PgHarborError as exc:
        # Start anyway so the client shows the tools; they explain what is wrong.
        print(f"pgharbor: {exc}", file=sys.stderr)
        config = Config(source=f"invalid configuration: {exc}", warnings=(str(exc),))
    build_server(config).run()
    return 0


def _init(force: bool) -> int:
    path = Path(os.environ.get("PGHARBOR_CONFIG") or default_config_path()).expanduser()
    if path.exists() and not force:
        print(f"{path} already exists. Use --force to overwrite it.", file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    if sys.platform != "win32":
        path.chmod(0o600)
    print(f"Wrote {path}\nEdit it, then run: pgharbor check")
    return 0


async def _check() -> int:
    from .session import Sessions, describe_db_error

    config = load_config()
    print(f"Configuration: {config.source}")
    for warning in config.warnings:
        print(f"  warning: {warning}")
    if not config.connections:
        print("  no connection is configured (run `pgharbor init`)")
        return 1
    sessions = Sessions(config)
    failures = 0
    try:
        for name, profile in config.connections.items():
            try:
                async with sessions.transaction(name) as db:
                    facts = db.facts
                    role = "SUPERUSER" if facts.is_superuser else "regular role"
                    print(
                        f"  ok    {name:<14} {profile.access.value:<10} "
                        f"PostgreSQL {facts.label}  {facts.database}  as {facts.user} ({role})"
                    )
                    if facts.is_superuser and profile.is_production:
                        print(
                            f"        note: '{name}' is production and connects as a "
                            "superuser; a dedicated read-only role is safer."
                        )
            except Exception as exc:
                failures += 1
                print(f"  FAIL  {name:<14} {describe_db_error(exc, profile)}")
    finally:
        await sessions.close()
    return 1 if failures else 0


def _tools() -> int:
    from .server import _register_custom_tools, enabled_tools, load_tool_modules

    config = load_config()
    load_tool_modules()
    _register_custom_tools(config)
    specs = enabled_tools(config)
    group = None
    for spec in sorted(specs, key=lambda s: (s.group, s.name)):
        if spec.group != group:
            group = spec.group
            print(f"\n[{group}]")
        first_line = spec.description.splitlines()[0] if spec.description else ""
        flag = " (writes)" if spec.writes else ""
        print(f"  {spec.name:<28}{flag} {first_line}")
    print(f"\n{len(specs)} tools")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
