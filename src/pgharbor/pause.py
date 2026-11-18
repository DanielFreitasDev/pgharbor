"""The emergency pause: the owner's switch to stop database access at once.

A change to the configuration file reaches a session only when that session starts. The
pause is the one control with immediate effect, and it can only take access away: while
``<state dir>/pause.json`` exists, every running pgharbor refuses to reach the connections
it names (or all of them).

The file is written by the management panel (``pgharbor ui``) and read here before every
database access. A pause file that exists but cannot be understood is treated as a pause
of everything: when in doubt, the answer is no.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import state_dir

__all__ = ["Pause", "clear_pause", "pause_path", "read_pause", "write_pause"]

_MAX_REASON = 300


@dataclass(frozen=True, slots=True)
class Pause:
    since: str
    reason: str = ""
    connections: tuple[str, ...] = ()  # empty: every connection

    def covers(self, connection: str) -> bool:
        return not self.connections or connection in self.connections


def pause_path() -> Path:
    return state_dir() / "pause.json"


def read_pause() -> Pause | None:
    """The pause in force, or ``None``. Never raises."""
    try:
        raw = pause_path().read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        return Pause(since="", reason="the pause file cannot be read")
    try:
        data = json.loads(raw)
        connections = data.get("connections") or []
        if not isinstance(connections, list) or not all(isinstance(c, str) for c in connections):
            raise ValueError("connections")
        return Pause(
            since=str(data.get("since") or ""),
            reason=str(data.get("reason") or "")[:_MAX_REASON],
            connections=tuple(connections),
        )
    except (ValueError, AttributeError):
        return Pause(since="", reason="the pause file is damaged")


def write_pause(reason: str = "", connections: tuple[str, ...] = ()) -> Pause:
    """Start (or replace) the pause. Raises ``OSError`` when the file cannot be written:
    the person who pressed the button must know that nothing was paused."""
    pause = Pause(
        since=datetime.now(UTC).isoformat(timespec="seconds"),
        reason=reason.strip()[:_MAX_REASON],
        connections=tuple(dict.fromkeys(connections)),
    )
    path = pause_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    data = json.dumps(
        {"since": pause.since, "reason": pause.reason, "connections": list(pause.connections)},
        ensure_ascii=False,
    )
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(temporary, path)
    return pause


def clear_pause() -> None:
    pause_path().unlink(missing_ok=True)
