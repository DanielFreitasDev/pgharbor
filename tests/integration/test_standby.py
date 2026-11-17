"""pgharbor on a hot standby, and the replication tools with a real replica attached.

Skipped unless the replica is running: scripts/pg-matrix.sh up standby
(a streaming replica of the PostgreSQL 17 test server, on port 55517).
"""

import asyncio
import socket
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from pgharbor.app import App, set_app
from pgharbor.config import Config
from pgharbor.tools.core import pg_query, pg_server_info
from pgharbor.tools.replication import pg_replication

from ..conftest import HOST, Server, call, ensure_fixture, profile, superuser
from .test_all_tools import exercise

pytestmark = pytest.mark.integration
PRIMARY = Server("17", 55417)
STANDBY = Server("17", 55517)


@pytest.fixture
async def replica() -> AsyncIterator[App]:
    try:
        with socket.create_connection((HOST, STANDBY.port), timeout=0.5):
            pass
    except OSError:
        pytest.skip("standby not running; run scripts/pg-matrix.sh up standby")
    ensure_fixture(PRIMARY)
    # Wait until the replica has replayed the sample database.
    deadline = time.monotonic() + 30
    while True:
        try:
            with superuser(STANDBY) as db:
                if db.execute("SELECT to_regclass('shop.orders') IS NOT NULL").fetchone() == (
                    True,
                ):
                    break
        except Exception:  # noqa: S110 - the replica may still be starting
            pass
        if time.monotonic() > deadline:
            pytest.fail("the standby did not catch up with the primary")
        await asyncio.sleep(0.5)
    application = App(
        Config.model_validate(
            {
                "connections": {
                    "ro": profile(STANDBY, "ro"),
                    "limited": profile(STANDBY, "limited", user="harbor_ro",
                                       password="harbor_ro_pw"),
                    "guarded": profile(STANDBY, "guarded"),
                    "primary": profile(PRIMARY, "primary"),
                },
                "audit": {"enabled": False},
            }
        )
    )  # fmt: skip
    set_app(application)
    try:
        yield application
    finally:
        await application.close()
        set_app(None)


async def test_reads_work_on_a_standby(replica: Any) -> None:
    info = await call(pg_server_info, connection="ro")
    assert info["is_replica"] is True
    rows = await call(pg_query, connection="ro", sql="SELECT count(*) FROM shop.orders")
    assert rows["rows"] == [[6000]]
    state = await call(
        pg_query,
        connection="ro",
        sql="SELECT pg_is_in_recovery(), current_setting('transaction_read_only')",
    )
    assert state["rows"] == [[True, "on"]]


async def test_refusals_hold_on_a_standby(replica: Any) -> None:
    for sql in ("SELECT pg_sleep(1)", "DELETE FROM shop.no_pk", "SELECT 1; SELECT 2"):
        with pytest.raises(ToolError) as caught:
            await pg_query(connection="ro", sql=sql)
        assert "Refused" in str(caught.value)


async def test_every_read_tool_runs_on_a_standby(replica: Any) -> None:
    problems = await exercise("ro")
    assert not problems, "\n".join(problems)


async def test_replication_seen_from_both_sides(replica: Any) -> None:
    standby = await call(pg_replication, connection="ro")
    assert standby["role"] == "standby"
    text = str(standby)
    assert "password" not in text.lower()
    primary = await call(pg_replication, connection="primary")
    assert primary["role"] == "primary"
    assert "standbys" in primary or "replicas" in primary or "connected" in str(primary).lower()
    assert "streaming" in str(primary)
