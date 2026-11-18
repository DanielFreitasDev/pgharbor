"""Presence: one small file per running server, so the owner can see who is using pgharbor.

The transport is stdio, so there is no central server: every client session starts its own
pgharbor process. Each of them writes ``<state dir>/sessions/<id>.json`` and refreshes it
while it runs. The management panel (``pgharbor ui``) reads those files to list the
sessions, the client behind each one and the connections it has approved. The same id is
written in every audit entry, which is how a call is traced back to a session.

Nothing reads these files back into a server: they describe a session, they can never
change what it is allowed to do.

Liveness is a heartbeat, not a process probe: ``os.kill(pid, 0)`` terminates the process
on Windows, and a process id may be reused by an unrelated program.

Like the audit log, presence must never break a tool call: failures are swallowed after
being reported once on stderr. It follows the ``[audit] enabled`` switch, so turning the
log off also stops these files.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .config import Config, state_dir

__all__ = [
    "HEARTBEAT_SECONDS",
    "STALE_AFTER_SECONDS",
    "Presence",
    "read_sessions",
    "sessions_dir",
]

HEARTBEAT_SECONDS = 15.0
# A session whose file was not refreshed for this long is gone: its process was killed
# before it could say goodbye. A little over three heartbeats, to tolerate a busy machine.
STALE_AFTER_SECONDS = 50.0
_KEEP_DAYS = 14
_MAX_LISTED = 200
_MAX_TEXT = 120


def sessions_dir() -> Path:
    return state_dir() / "sessions"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _clip(value: Any) -> str | None:
    """Text the client sent about itself: kept short, and only if it is text."""
    return value[:_MAX_TEXT] if isinstance(value, str) and value else None


class Presence:
    """The file of this process. Inert until :meth:`start`, so tests and one-off commands
    that build an application never leave a session behind."""

    def __init__(self, config: Config, session_id: str) -> None:
        self.enabled = config.audit.enabled
        self.path = sessions_dir() / f"{session_id}.json"
        self._running = False
        self._warned = False
        try:
            cwd: str | None = os.getcwd()
        except OSError:
            cwd = None
        self._state: dict[str, Any] = {
            "id": session_id,
            "pid": os.getpid(),
            "version": __version__,
            "started": None,
            "heartbeat": None,
            "ended": None,
            "cwd": cwd,
            "client": None,
            "protocol": None,
            "config": {"source": config.source, "digest": config.digest},
            "connections": sorted(config.connections),
            "approvals": {},
        }

    def start(self) -> None:
        if not self.enabled or self._running:
            return
        self._running = True
        self._state["started"] = _now()
        _prune(self.path.parent)
        self.beat()

    def beat(self) -> None:
        """Refresh the file: this is what tells the panel the session is alive."""
        if not self._running:
            return
        self._state["heartbeat"] = _now()
        self._write()

    def stop(self) -> None:
        if not self._running:
            return
        self._state["ended"] = _now()
        self._state["approvals"] = {}
        self._write()
        self._running = False

    def note_client(self, name: Any, version: Any, protocol: Any) -> None:
        """Who is on the other end, as the client described itself (never trusted)."""
        client = {"name": _clip(name), "version": _clip(version)} if _clip(name) else None
        protocol = _clip(protocol)
        if client == self._state["client"] and protocol == self._state["protocol"]:
            return
        self._state["client"] = client
        self._state["protocol"] = protocol
        self.beat()

    def note_approvals(self, approvals: dict[str, dict[str, str | None]]) -> None:
        """The connections the user approved in this session, and until when."""
        if approvals == self._state["approvals"]:
            return
        self._state["approvals"] = approvals
        self.beat()

    def _write(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            data = json.dumps(self._state, ensure_ascii=False, separators=(",", ":"))
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, data.encode("utf-8"))
            finally:
                os.close(fd)
            # Replaced in one step, so a reader never sees half a file.
            os.replace(temporary, self.path)
        except OSError as exc:
            if not self._warned:
                self._warned = True
                print(f"pgharbor: cannot write the session file ({exc})", file=sys.stderr)


def _prune(directory: Path) -> None:
    """Forget sessions that ended long ago, so the directory does not grow for ever."""
    limit = time.time() - _KEEP_DAYS * 86_400
    with contextlib.suppress(OSError):
        for file in directory.glob("*.json"):
            with contextlib.suppress(OSError):
                if file.stat().st_mtime < limit:
                    file.unlink()


def read_sessions(now: float | None = None) -> list[dict[str, Any]]:
    """Every known session, most recently alive first, for the management panel.

    Each one gets a ``state``: ``active``, ``ended`` (it said goodbye) or ``lost`` (its
    file stopped being refreshed: the process was killed).
    """
    moment = time.time() if now is None else now
    found: list[tuple[float, dict[str, Any]]] = []
    try:
        files = list(sessions_dir().glob("*.json"))
    except OSError:
        return []
    for file in files:
        try:
            modified = file.stat().st_mtime
            data = json.loads(file.read_bytes())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            continue
        if data.get("ended"):
            data["state"] = "ended"
        elif moment - modified > STALE_AFTER_SECONDS:
            data["state"] = "lost"
        else:
            data["state"] = "active"
        found.append((modified, data))
    found.sort(key=lambda item: item[0], reverse=True)
    return [data for _, data in found[:_MAX_LISTED]]
