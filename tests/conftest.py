"""Shared test fixtures.

Integration tests run against real servers started by ``scripts/pg-matrix.sh up``. Each
test that takes the ``server`` fixture runs once per reachable PostgreSQL version; when
no server is reachable those tests are skipped and the unit tests still run.

Environment:
    PGHARBOR_TEST_HOST       default 127.0.0.1
    PGHARBOR_TEST_PASSWORD   superuser password, default "pgharbor"
    PGHARBOR_TEST_SERVERS    "label:port,label:port" to override the default matrix
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest

from pgharbor.app import App, set_app
from pgharbor.config import Config
from pgharbor.session import Sessions

from . import fixture

# The suite must never read the developer's own pgharbor configuration (which may point
# at real databases) nor pick up a database from the environment.
_ISOLATED_CONFIG_HOME = tempfile.mkdtemp(prefix="pgharbor-tests-")
os.environ["XDG_CONFIG_HOME"] = _ISOLATED_CONFIG_HOME
os.environ["XDG_STATE_HOME"] = _ISOLATED_CONFIG_HOME
for _name in ("PGHARBOR_CONFIG", "PGHARBOR_DSN", "DATABASE_URL", "PGHARBOR_ACCESS",
              "PGHARBOR_ENVIRONMENT"):  # fmt: skip
    os.environ.pop(_name, None)

HOST = os.environ.get("PGHARBOR_TEST_HOST", "127.0.0.1")
PASSWORD = os.environ.get("PGHARBOR_TEST_PASSWORD", "pgharbor")
_DEFAULT_MATRIX = {
    "9.6": 55496,
    "10": 55410,
    "11": 55411,
    "12": 55412,
    "13": 55413,
    "14": 55414,
    "15": 55415,
    "16": 55416,
    "17": 55417,
    "18": 55418,
    "19": 55419,
}


@dataclass(frozen=True)
class Server:
    label: str
    port: int

    @property
    def major(self) -> int:
        return 9 if self.label.startswith("9") else int(self.label)

    def at_least(self, major: int) -> bool:
        return self.major >= major


def _matrix() -> dict[str, int]:
    raw = os.environ.get("PGHARBOR_TEST_SERVERS")
    if not raw:
        return dict(_DEFAULT_MATRIX)
    out: dict[str, int] = {}
    for item in raw.split(","):
        label, _, port = item.strip().partition(":")
        out[label] = int(port)
    return out


def _reachable(port: int) -> bool:
    try:
        with socket.create_connection((HOST, port), timeout=0.5):
            return True
    except OSError:
        return False


SERVERS = [Server(label, port) for label, port in _matrix().items() if _reachable(port)]

# An optional PostgreSQL 18 with third-party extensions (hypopg, pgvector, PostGIS, pg_cron,
# pg_partman...). Started by `scripts/pg-matrix.sh up extras`. Tests that need it take the
# `extras` fixture and are skipped when it is not running.
EXTRAS_PORT = int(os.environ.get("PGHARBOR_TEST_EXTRAS_PORT", "55500"))
EXTRAS = Server("18", EXTRAS_PORT) if _reachable(EXTRAS_PORT) else None


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "server" in metafunc.fixturenames:
        if SERVERS:
            metafunc.parametrize(
                "server", SERVERS, ids=[f"pg{s.label}" for s in SERVERS], scope="session"
            )
        else:
            metafunc.parametrize(
                "server",
                [
                    pytest.param(
                        None,
                        marks=pytest.mark.skip(
                            reason="no test server; run scripts/pg-matrix.sh up"
                        ),
                    )
                ],
                scope="session",
            )


def superuser(server: Server, dbname: str = fixture.DATABASE) -> psycopg.Connection[Any]:
    """A plain superuser connection (autocommit) for tests that must prepare something.

    It bypasses pgharbor on purpose: use it to create what a test needs (a publication, a
    replication slot, a blocked session) and to clean up afterwards. Never change the
    shared sample database with it; create objects in a schema of your own and drop it.
    """
    return _connect(server, dbname)


def _connect(server: Server, dbname: str) -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=HOST,
        port=server.port,
        user="postgres",
        password=PASSWORD,
        dbname=dbname,
        autocommit=True,
        connect_timeout=10,
    )


_READY: set[str] = set()


def ensure_fixture(server: Server, key: str | None = None) -> None:
    """Build the sample database on ``server`` unless it is already up to date."""
    key = key or server.label
    if key in _READY:
        return
    with _connect(server, "postgres") as admin:
        exists = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", [fixture.DATABASE]
        ).fetchone()
        current = None
        if exists:
            with _connect(server, fixture.DATABASE) as db:
                try:
                    row = db.execute("SELECT version FROM public._fixture").fetchone()
                    current = row[0] if row else None
                except psycopg.Error:
                    current = None
        if current == fixture.FIXTURE_VERSION:
            _READY.add(key)
            return
        # Serialize concurrent test runs that want to rebuild the same server.
        admin.execute("SELECT pg_advisory_lock(770077)")
        try:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                [fixture.DATABASE],
            )
            force = " WITH (FORCE)" if admin.info.server_version >= 130000 else ""
            admin.execute(f"DROP DATABASE IF EXISTS {fixture.DATABASE}{force}")
            admin.execute(f"CREATE DATABASE {fixture.DATABASE}")
            role = admin.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", [fixture.LIMITED_ROLE]
            ).fetchone()
            if not role:
                admin.execute(
                    f"CREATE ROLE {fixture.LIMITED_ROLE} LOGIN PASSWORD "
                    f"'{fixture.LIMITED_PASSWORD}'"
                )
            with _connect(server, fixture.DATABASE) as db:
                version = db.info.server_version
                available = {
                    r[0] for r in db.execute("SELECT name FROM pg_available_extensions").fetchall()
                }
                for extension in (*fixture.EXTENSIONS, *fixture.EXTRA_EXTENSIONS):
                    if extension in available:
                        db.execute(f"CREATE EXTENSION IF NOT EXISTS {extension}")
                for statement in fixture.statements(version):
                    db.execute(statement)
                for statement in fixture.extras_statements(available):
                    db.execute(statement)
                for statement in fixture.grants():
                    db.execute(statement)
                db.execute("CREATE TABLE public._fixture (version text)")
                db.execute("INSERT INTO public._fixture VALUES (%s)", [fixture.FIXTURE_VERSION])
        finally:
            admin.execute("SELECT pg_advisory_unlock(770077)")
    _READY.add(key)


def profile(server: Server, name: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": name,
        "host": HOST,
        "port": server.port,
        "dbname": fixture.DATABASE,
        "user": "postgres",
        "password": PASSWORD,
    }
    base.update(overrides)
    return base


VAULT_RULE = {
    "table": "vault.files",
    "deny_columns": ["access_key"],
    "require_index_filter": True,
    "note": "Filter by id, name or checksum.",
}


def build_config(server: Server, **audit: Any) -> Config:
    """Profiles used across the suite: one per access mode and per rule set."""
    connections = {
        "ro": profile(server, "ro"),
        "rw": profile(server, "rw", access="read-write", write_confirmation="none"),
        "admin": profile(server, "admin", access="admin", write_confirmation="none"),
        "limited": profile(
            server, "limited", user=fixture.LIMITED_ROLE, password=fixture.LIMITED_PASSWORD
        ),
        "guarded": profile(server, "guarded", protect=[VAULT_RULE]),
        "strict": profile(server, "strict", function_policy="strict"),
        "permissive": profile(server, "permissive", function_policy="permissive"),
        "prod": profile(server, "prod", environment="production"),
    }
    return Config.model_validate(
        {"connections": connections, "audit": {"enabled": False, **audit}, "source": "tests"}
    )


@pytest.fixture(scope="session")
def ready(server: Server) -> Server:
    ensure_fixture(server)
    return server


@pytest.fixture
async def sessions(ready: Server) -> AsyncIterator[Sessions]:
    manager = Sessions(build_config(ready))
    try:
        yield manager
    finally:
        await manager.close()


@pytest.fixture(scope="session")
def one_server() -> Server:
    """One server for tests that do not depend on the PostgreSQL version."""
    if not SERVERS:
        pytest.skip("no test server; run scripts/pg-matrix.sh up")
    preferred = next((s for s in SERVERS if s.label == "18"), SERVERS[-1])
    ensure_fixture(preferred)
    return preferred


def write_config_file(path: Any, server: Server, **overrides: Any) -> Any:
    """Write a TOML configuration file with the suite's profiles; returns ``path``."""
    lines = ["[audit]", "enabled = false", ""]
    connections = {
        "ro": {},
        "rw": {"access": "read-write", "write_confirmation": "none"},
        "prod": {"environment": "production"},
    }
    connections.update(overrides)
    for name, extra in connections.items():
        body = {**profile(server, name), **extra}
        body.pop("name")
        lines.append(f"[connections.{name}]")
        for key, value in body.items():
            lines.append(f"{key} = {json.dumps(value)}")
        lines.append("")
    path.write_text("\n".join(lines))
    return path


@pytest.fixture(scope="session")
def extras() -> Server:
    """The server with third-party extensions, with the sample database loaded."""
    if EXTRAS is None:
        pytest.skip("extras server not running; run scripts/pg-matrix.sh up extras")
    ensure_fixture(EXTRAS, key="extras")
    return EXTRAS


@pytest.fixture
async def extras_app(extras: Server) -> AsyncIterator[App]:
    application = App(build_config(extras))
    set_app(application)
    try:
        yield application
    finally:
        await application.close()
        set_app(None)


@pytest.fixture
async def app(ready: Server) -> AsyncIterator[App]:
    """The application the tools use, pointed at the server under test."""
    application = App(build_config(ready))
    set_app(application)
    try:
        yield application
    finally:
        await application.close()
        set_app(None)


async def call(function: Any, /, **arguments: Any) -> Any:
    """Call a tool the way the MCP server does and decode its answer.

    Tools return text; when that text is JSON it is parsed so tests can look inside.
    A refusal raises ``mcp.server.mcpserver.exceptions.ToolError``.
    """
    text = await function(**arguments)
    try:
        return json.loads(text)
    except ValueError:
        return text


def rows_as_dicts(table: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn the compact ``{"columns": [...], "rows": [[...]]}`` form into dictionaries."""
    return [dict(zip(table["columns"], row, strict=True)) for row in table["rows"]]
