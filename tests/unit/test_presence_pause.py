"""What a running server tells its owner (session files, the session id in the audit log)
and the one thing the owner can tell it at once: the emergency pause."""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from mcp import Client, StdioServerParameters

from pgharbor.app import App
from pgharbor.audit import Audit
from pgharbor.config import AuditConfig, Config, ConnectionConfig
from pgharbor.errors import ApprovalRequired, Rejected
from pgharbor.pause import clear_pause, pause_path, read_pause, write_pause
from pgharbor.presence import STALE_AFTER_SECONDS, Presence, read_sessions, sessions_dir


@pytest.fixture
def state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty state directory, on every platform."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    return tmp_path / "pgharbor"


def _config(**audit: Any) -> Config:
    return Config(
        connections={
            "dev": ConnectionConfig(name="dev"),
            "prod": ConnectionConfig(name="prod", environment="production", approval_ttl="10min"),
        },
        audit=AuditConfig(**audit),
        source="/somewhere/config.toml",
        digest="abc123",
    )


# ------------------------------------------------------------------------------- audit


def test_audit_entries_carry_the_session(tmp_path: Path) -> None:
    audit = Audit(AuditConfig(path=str(tmp_path / "audit.jsonl")), session="a1b2c3d4")
    audit.record(tool="pg_query", connection="dev", outcome="ok")
    assert audit.tail(1)[0]["session"] == "a1b2c3d4"

    anonymous = Audit(AuditConfig(path=str(tmp_path / "plain.jsonl")))
    anonymous.record(tool="pg_query", connection="dev", outcome="ok")
    assert "session" not in anonymous.tail(1)[0]


def test_an_application_marks_its_audit_entries_with_its_own_id(state_home: Path) -> None:
    app = App(_config())
    assert len(app.session_id) == 8
    assert app.audit.session == app.session_id
    assert app.presence.path == sessions_dir() / f"{app.session_id}.json"


# ---------------------------------------------------------------------------- presence


def test_presence_is_inert_until_started(state_home: Path) -> None:
    presence = Presence(_config(), "feedc0de")
    presence.beat()
    presence.note_client("claude-code", "2.1.0", "2026-07-28")
    presence.stop()
    assert not sessions_dir().exists()
    assert read_sessions() == []


def test_presence_lifecycle(state_home: Path) -> None:
    presence = Presence(_config(), "feedc0de")
    presence.start()
    [session] = read_sessions()
    assert session["state"] == "active"
    assert session["id"] == "feedc0de"
    assert session["pid"] == os.getpid()
    assert session["config"] == {"source": "/somewhere/config.toml", "digest": "abc123"}
    assert session["connections"] == ["dev", "prod"]
    assert session["client"] is None

    presence.note_client("claude-code", "2.1.0", "2026-07-28")
    presence.note_approvals({"prod": {"granted": "2026-11-18T10:00:00+00:00", "expires": None}})
    [session] = read_sessions()
    assert session["client"] == {"name": "claude-code", "version": "2.1.0"}
    assert session["protocol"] == "2026-07-28"
    assert list(session["approvals"]) == ["prod"]

    # No heartbeat for a while: the process died without saying goodbye.
    [session] = read_sessions(now=time.time() + STALE_AFTER_SECONDS + 5)
    assert session["state"] == "lost"

    presence.stop()
    [session] = read_sessions()
    assert session["state"] == "ended"
    assert session["approvals"] == {}  # nothing stays approved after the end


def test_what_a_client_says_about_itself_is_bounded(state_home: Path) -> None:
    presence = Presence(_config(), "feedc0de")
    presence.start()
    presence.note_client("x" * 5000, {"not": "text"}, None)
    [session] = read_sessions()
    assert len(session["client"]["name"]) == 120
    assert session["client"]["version"] is None


def test_presence_follows_the_audit_switch(state_home: Path) -> None:
    presence = Presence(_config(enabled=False), "feedc0de")
    presence.start()
    assert read_sessions() == []


def test_presence_never_raises(state_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state_home.mkdir(parents=True)
    (state_home / "sessions").write_text("a file where the directory should be")
    presence = Presence(_config(), "feedc0de")
    presence.start()
    presence.beat()
    presence.stop()
    assert capsys.readouterr().err.count("cannot write the session file") == 1


def test_session_files_that_are_not_ours_are_ignored(state_home: Path) -> None:
    directory = sessions_dir()
    directory.mkdir(parents=True)
    (directory / "broken.json").write_text("{not json")
    (directory / "list.json").write_text("[1, 2]")
    (directory / "noid.json").write_text('{"pid": 1}')
    assert read_sessions() == []


def test_an_approval_is_published_and_withdrawn(state_home: Path) -> None:
    app = App(_config())
    app.presence.start()
    prod = app.profile("prod")
    app.approve(prod)
    [session] = read_sessions()
    approval = session["approvals"]["prod"]
    assert approval["granted"] < approval["expires"]  # this connection has a time limit
    app.revoke(prod)
    assert read_sessions()[0]["approvals"] == {}


# ------------------------------------------------------------------------------- pause


def test_pause_round_trip(state_home: Path) -> None:
    assert read_pause() is None
    clear_pause()  # nothing to clear is not an error

    everything = write_pause("  incident 42  ")
    assert everything.reason == "incident 42"
    assert read_pause() == everything
    assert everything.covers("dev")
    assert everything.covers("prod")

    only_prod = write_pause("", ("prod", "prod"))
    assert only_prod.connections == ("prod",)
    assert read_pause() == only_prod
    assert only_prod.covers("prod")
    assert not only_prod.covers("dev")

    clear_pause()
    assert read_pause() is None


@pytest.mark.parametrize("content", ["{not json", "[]", '{"connections": "prod"}', ""])
def test_a_damaged_pause_file_pauses_everything(state_home: Path, content: str) -> None:
    state_home.mkdir(parents=True)
    pause_path().write_text(content)
    pause = read_pause()
    assert pause is not None
    assert pause.covers("anything")


def test_a_paused_connection_is_refused_before_approval(state_home: Path) -> None:
    app = App(_config())
    dev, prod = app.profile("dev"), app.profile("prod")
    app.require_approval(dev)  # nothing paused: a development connection just works
    with pytest.raises(ApprovalRequired):
        app.require_approval(prod)

    write_pause("maintenance window", ("prod",))
    app.require_approval(dev)  # not covered by the pause
    with pytest.raises(Rejected) as refused:
        app.require_approval(prod)  # refused as paused, not sent to ask for approval
    assert refused.value.rule == "paused"
    assert "maintenance window" in str(refused.value)

    write_pause()
    with pytest.raises(Rejected):
        app.require_approval(dev)

    clear_pause()
    app.require_approval(dev)


# ------------------------------------------------------------------- the real process


async def test_the_real_server_announces_itself_and_says_goodbye(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PG", "DATABASE_URL"))}
    for variable in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "APPDATA", "LOCALAPPDATA"):
        env[variable] = str(tmp_path)
    sessions = tmp_path / "pgharbor" / "sessions"
    parameters = StdioServerParameters(command=sys.executable, args=["-m", "pgharbor.cli"], env=env)
    async with Client(parameters) as client:
        await client.call_tool("pg_connections", {})
        [file] = sessions.glob("*.json")
        running = json.loads(file.read_text(encoding="utf-8"))
        assert running["ended"] is None
        assert running["client"]["name"]  # the client named itself in the handshake
        assert running["protocol"]
        # The same id marks the audit entry of the call that was just made.
        [entry] = [
            json.loads(line)
            for line in (tmp_path / "pgharbor" / "audit.jsonl").read_text().splitlines()
        ]
        assert entry["session"] == running["id"] == file.stem
    [file] = sessions.glob("*.json")
    if sys.platform != "win32":
        # Where the client can only end the process abruptly, the session is not marked:
        # the panel then shows it as "ended without notice" once the heartbeat stops.
        assert json.loads(file.read_text(encoding="utf-8"))["ended"]
