"""The audit log, read for the panel: pages, filters and numbers.

The log is append-only and may be large, so it is read incrementally: each refresh reads
only the bytes written since the previous one. Everything in it is data that an agent
helped to write (statements, error messages); this module returns it as it is and the
page shows it as text, never as markup.
"""

from __future__ import annotations

import bisect
import json
import os
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = ["RANGES", "AuditReader"]

# range name -> (seconds covered, seconds per bucket)
RANGES: dict[str, tuple[int, int]] = {
    "1h": (3_600, 60),
    "24h": (86_400, 3_600),
    "7d": (7 * 86_400, 86_400),
    "30d": (30 * 86_400, 86_400),
}
_MAX_ENTRIES = 300_000
_HEAD_BYTES = 256
_MAX_PAGE = 500
_FAILED = ("error", "bug")


def _epoch(stamp: Any) -> float:
    try:
        moment = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp()


def _percentile(ordered: list[float], fraction: float) -> float | None:
    if not ordered:
        return None
    return ordered[min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))]


class AuditReader:
    """An in-memory view of one audit file, kept up to date by :meth:`refresh`."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0
        self._identity: tuple[int, int] | None = None
        self._head = b""  # how the file began when it was first read
        self._next = 1
        self._entries: list[dict[str, Any]] = []
        self._times: list[float] = []
        self.size = 0

    # ------------------------------------------------------------------- reading

    def refresh(self) -> None:
        try:
            info = os.stat(self.path)
        except OSError:
            self._reset()
            return
        identity = (info.st_dev, info.st_ino)
        try:
            with self.path.open("rb") as handle:
                head = handle.read(_HEAD_BYTES)
                # Another file now, this one was cut, or it was rewritten in place (its
                # beginning is no longer what was read): start over.
                same_start = head[: len(self._head)] == self._head
                if identity != self._identity or info.st_size < self._offset or not same_start:
                    self._reset()
                    self._identity = identity
                if len(self._head) < _HEAD_BYTES:
                    self._head = head
                self.size = info.st_size
                if info.st_size == self._offset:
                    return
                handle.seek(self._offset)
                data = handle.read(info.st_size - self._offset)
        except OSError:
            return
        complete = data.rfind(b"\n") + 1  # a line still being written waits for next time
        self._offset += complete
        for line in data[:complete].splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            moment = _epoch(entry.get("ts"))
            # Several servers append to the same file; keep the list ordered by time.
            moment = max(moment, self._times[-1]) if self._times else moment
            entry["n"] = self._next
            self._next += 1
            self._entries.append(entry)
            self._times.append(moment)
        if len(self._entries) > _MAX_ENTRIES:
            drop = len(self._entries) - _MAX_ENTRIES
            del self._entries[:drop]
            del self._times[:drop]

    def _reset(self) -> None:
        self._offset = 0
        self._identity = None
        self._head = b""
        self._entries = []
        self._times = []
        self.size = 0

    @property
    def count(self) -> int:
        return len(self._entries)

    @property
    def last(self) -> int:
        return self._entries[-1]["n"] if self._entries else 0

    # --------------------------------------------------------------------- pages

    def page(
        self,
        *,
        limit: int = 100,
        before: int | None = None,
        after: int | None = None,
        connection: str | None = None,
        outcome: str | None = None,
        tool: str | None = None,
        rule: str | None = None,
        session: str | None = None,
        text: str | None = None,
        since: float | None = None,
        until: float | None = None,
    ) -> dict[str, Any]:
        """Entries that match, newest first. ``before`` continues an earlier page;
        ``after`` asks only for what is newer than an entry already shown."""
        limit = max(1, min(int(limit), _MAX_PAGE))
        needle = text.casefold() if text else None
        start = bisect.bisect_left(self._times, since) if since is not None else 0
        stop = bisect.bisect_right(self._times, until) if until is not None else len(self._entries)
        out: list[dict[str, Any]] = []
        more = False
        for index in range(stop - 1, start - 1, -1):
            entry = self._entries[index]
            if before is not None and entry["n"] >= before:
                continue
            if after is not None and entry["n"] <= after:
                break
            if connection and entry.get("connection") != connection:
                continue
            if outcome and entry.get("outcome") != outcome:
                continue
            if tool and entry.get("tool") != tool:
                continue
            if rule and entry.get("rule") != rule:
                continue
            if session and entry.get("session") != session:
                continue
            if needle and not any(
                needle in str(entry.get(key, "")).casefold()
                for key in ("sql", "detail", "tool", "rule", "connection")
            ):
                continue
            if len(out) == limit:
                more = True
                break
            out.append(entry)
        return {"entries": out, "has_more": more, "total": len(self._entries), "last": self.last}

    def facets(self) -> dict[str, list[str]]:
        """The values worth offering in the filters."""
        seen: dict[str, set[str]] = {key: set() for key in ("connection", "tool", "rule")}
        for entry in self._entries:
            for key, values in seen.items():
                value = entry.get(key)
                if isinstance(value, str) and value:
                    values.add(value)
        return {key: sorted(values) for key, values in seen.items()}

    # ------------------------------------------------------------------- numbers

    def stats(self, name: str, *, tz_minutes: int = 0, now: float | None = None) -> dict[str, Any]:
        """Totals and breakdowns for one of :data:`RANGES`. ``tz_minutes`` is the viewer's
        offset from UTC, so that a day in the chart is the viewer's day."""
        span, step = RANGES[name]
        moment = datetime.now(UTC).timestamp() if now is None else now
        shift = tz_minutes * 60
        last_bucket = int((moment + shift) // step)
        first_bucket = last_bucket - span // step + 1
        since = first_bucket * step - shift
        buckets = {
            b: {"t": b * step - shift, "ok": 0, "refused": 0, "error": 0, "other": 0}
            for b in range(first_bucket, last_bucket + 1)
        }
        totals: Counter[str] = Counter()
        previous: Counter[str] = Counter()
        tools: dict[str, dict[str, Any]] = defaultdict(_tally)
        connections: dict[str, dict[str, Any]] = defaultdict(_tally)
        rules: Counter[str] = Counter()
        hours = [0] * 24
        durations: list[float] = []
        slowest: list[dict[str, Any]] = []
        refusals: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []

        start = bisect.bisect_left(self._times, since - span)
        for index in range(start, len(self._entries)):
            entry, when = self._entries[index], self._times[index]
            outcome = entry.get("outcome")
            kind = _kind(outcome)
            if when < since:
                previous["calls"] += 1
                previous[kind] += 1
                continue
            totals["calls"] += 1
            totals[kind] += 1
            if isinstance(outcome, str):
                totals[f"outcome:{outcome}"] += 1
            bucket = buckets.get(int((when + shift) // step))
            if bucket is not None:
                bucket[kind] += 1
            hours[int(((when + shift) % 86_400) // 3_600)] += 1
            took = entry.get("ms")
            took = float(took) if isinstance(took, (int, float)) else None
            for table, key in ((tools, entry.get("tool")), (connections, entry.get("connection"))):
                if isinstance(key, str) and key:
                    row = table[key]
                    row["calls"] += 1
                    row[kind] += 1
                    if took is not None:
                        row["ms"] += took
                        row["timed"] += 1
            if took is not None:
                durations.append(took)
                slowest.append(entry)
            if kind == "refused":
                rules[str(entry.get("rule") or "?")] += 1
                refusals.append(entry)
            elif kind == "error":
                failures.append(entry)

        durations.sort()
        slowest.sort(key=lambda e: e["ms"], reverse=True)
        return {
            "range": name,
            "since": since,
            "until": moment,
            "bucket_seconds": step,
            "buckets": list(buckets.values()),
            "totals": dict(totals),
            "previous": dict(previous),
            "latency": {
                "p50": _percentile(durations, 0.50),
                "p95": _percentile(durations, 0.95),
                "max": durations[-1] if durations else None,
            },
            "by_tool": _ranked(tools),
            "by_connection": _ranked(connections),
            "by_rule": [{"name": name, "count": count} for name, count in rules.most_common(12)],
            "by_hour": hours,
            "slowest": slowest[:6],
            "recent_refusals": refusals[-8:][::-1],
            "recent_failures": failures[-8:][::-1],
        }

    def by_session(self) -> dict[str, dict[str, Any]]:
        """What each session did, for the list of sessions."""
        out: dict[str, dict[str, Any]] = {}
        for entry in self._entries:
            session = entry.get("session")
            if not isinstance(session, str):
                continue
            row = out.get(session)
            if row is None:
                row = out[session] = {
                    "calls": 0,
                    "refused": 0,
                    "error": 0,
                    "first": entry.get("ts"),
                    "last": None,
                    "connections": [],
                }
            row["calls"] += 1
            kind = _kind(entry.get("outcome"))
            if kind in ("refused", "error"):
                row[kind] += 1
            row["last"] = entry.get("ts")
            row["last_tool"] = entry.get("tool")
            connection = entry.get("connection")
            if isinstance(connection, str) and connection not in row["connections"]:
                row["connections"].append(connection)
        return out


def _kind(outcome: Any) -> str:
    if outcome == "ok":
        return "ok"
    if outcome == "refused":
        return "refused"
    if outcome in _FAILED:
        return "error"
    return "other"


def _tally() -> dict[str, Any]:
    return {"calls": 0, "ok": 0, "refused": 0, "error": 0, "other": 0, "ms": 0.0, "timed": 0}


def _ranked(table: dict[str, dict[str, Any]], limit: int = 15) -> list[dict[str, Any]]:
    rows = []
    for name, row in table.items():
        rows.append(
            {
                "name": name,
                "calls": row["calls"],
                "ok": row["ok"],
                "refused": row["refused"],
                "error": row["error"],
                "avg_ms": round(row["ms"] / row["timed"], 1) if row["timed"] else None,
            }
        )
    rows.sort(key=lambda r: (-r["calls"], r["name"]))
    return rows[:limit]
