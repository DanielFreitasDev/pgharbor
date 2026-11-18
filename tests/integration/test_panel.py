"""The management panel against real servers: testing a connection, and the emergency
pause stopping a real query on every PostgreSQL version."""

import json
from pathlib import Path
from typing import Any

import pytest
from mcp.server.elicitation import AcceptedElicitation
from mcp.server.mcpserver.exceptions import ToolError

from pgharbor.app import REGISTRY, App
from pgharbor.approval import NotNeeded
from pgharbor.pause import clear_pause, read_pause
from pgharbor.server import load_tool_modules

from ..conftest import Server, write_config_file
from ..unit.panel_client import PanelClient

pytestmark = pytest.mark.integration

# What the write tool receives when no confirmation dialog is needed.
CONFIRMED: Any = AcceptedElicitation[Any].model_construct(data=NotNeeded())


@pytest.fixture
def installation(ready: Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A configuration file pointing at the server under test, and its own state."""
    for variable in ("XDG_STATE_HOME", "LOCALAPPDATA"):
        monkeypatch.setenv(variable, str(tmp_path / "state"))
    file = write_config_file(tmp_path / "config.toml", ready)
    monkeypatch.setenv("PGHARBOR_CONFIG", str(file))
    return file


async def test_the_panel_tests_a_connection(ready: Server, installation: Path) -> None:
    panel = PanelClient()
    tested = (await panel.post("/api/connections/ro/test")).json()
    assert tested["ok"] is True, tested
    assert tested["postgresql"].startswith(ready.label)
    assert tested["user"] == "postgres"
    assert tested["superuser"] is True
    assert "password" not in json.dumps(tested)

    wrong = installation.read_text().replace('user = "postgres"', 'user = "nobody_here"', 1)
    installation.write_text(wrong)
    failed = (await panel.post("/api/connections/ro/test")).json()
    assert failed["ok"] is False
    assert "nobody_here" in failed["error"] or "authentication" in failed["error"].lower()


async def test_a_pause_from_the_panel_stops_a_real_query(app: App, installation: Path) -> None:
    load_tool_modules()
    query = REGISTRY["pg_query"].function
    panel = PanelClient()
    try:
        assert '"rows"' in await query(sql="SELECT 1 AS one", connection="ro")

        await panel.post("/api/pause", {"paused": True, "reason": "drill", "connections": ["ro"]})
        with pytest.raises(ToolError, match=r"Refused \(paused\).*drill"):
            await query(sql="SELECT 1 AS one", connection="ro")
        # A connection the pause does not name keeps working, and listing is not access.
        assert '"rows"' in await query(sql="SELECT 1 AS one", connection="rw")
        assert '"ro"' in await REGISTRY["pg_connections"].function()

        await panel.post("/api/pause", {"paused": True})
        for connection in ("ro", "rw"):
            with pytest.raises(ToolError, match="paused"):
                await query(sql="SELECT 1 AS one", connection=connection)
        with pytest.raises(ToolError, match="paused"):  # a write is stopped as well
            await REGISTRY["pg_execute"].function(
                sql="DELETE FROM public._fixture WHERE version = 'no such version'",
                connection="rw",
                decision=CONFIRMED,
            )

        await panel.post("/api/pause", {"paused": False})
        assert read_pause() is None
        assert '"rows"' in await query(sql="SELECT 1 AS one", connection="ro")
    finally:
        clear_pause()
