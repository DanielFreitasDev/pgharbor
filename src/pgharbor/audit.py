"""Append-only audit log: one JSON line per tool call.

The log answers "what did the agent run, where, and what happened". It records the SQL
text (configurable), never result rows, and never credentials: SQL is scrubbed and bound
parameters are left out unless ``log_params`` is turned on.

Writing the log must never break a tool call, so failures are swallowed after being
reported once on stderr.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import AuditConfig
from .render import scrub

__all__ = ["Audit"]

_MAX_SQL_CHARS = 20_000


class Audit:
    def __init__(self, config: AuditConfig, session: str | None = None) -> None:
        self.config = config
        self.path: Path = config.resolved_path()
        # Which server process wrote the entry: the same id as its file under sessions/.
        self.session = session
        self._warned = False

    def record(
        self,
        *,
        tool: str,
        connection: str | None,
        outcome: str,
        sql: str | None = None,
        params: Any = None,
        rule: str | None = None,
        rows: int | None = None,
        elapsed_ms: float | None = None,
        detail: str | None = None,
        **extra: Any,
    ) -> None:
        if not self.config.enabled:
            return
        entry: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "tool": tool,
            "connection": connection,
            "outcome": outcome,
        }
        if self.session:
            entry["session"] = self.session
        if rule:
            entry["rule"] = rule
        if sql is not None and self.config.log_sql:
            entry["sql"] = scrub(sql[:_MAX_SQL_CHARS])
        if params and self.config.log_params:
            entry["params"] = params
        if rows is not None:
            entry["rows"] = rows
        if elapsed_ms is not None:
            entry["ms"] = round(elapsed_ms, 1)
        if detail:
            entry["detail"] = scrub(detail[:2000])
        for key, value in extra.items():
            if value not in (None, "", [], {}):
                entry[key] = value
        self._append(entry)

    def _append(self, entry: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"), default=str)
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
            fd = os.open(self.path, flags, 0o600)
            try:
                os.write(fd, (line + "\n").encode("utf-8"))
            finally:
                os.close(fd)
        except OSError as exc:
            if not self._warned:
                self._warned = True
                print(f"pgharbor: cannot write the audit log ({exc})", file=sys.stderr)

    def tail(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the newest ``limit`` entries, oldest first."""
        try:
            with self.path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                block = min(size, max(64_000, limit * 2_000))
                handle.seek(size - block)
                data = handle.read().decode("utf-8", errors="replace")
        except OSError:
            return []
        lines = data.splitlines()
        if block < size:
            lines = lines[1:]  # the first line may be cut in half
        entries: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
        return entries


class Stopwatch:
    def __init__(self) -> None:
        self._start = time.perf_counter()

    @property
    def ms(self) -> float:
        return (time.perf_counter() - self._start) * 1000
