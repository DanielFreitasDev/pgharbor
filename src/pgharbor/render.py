"""Turning results into text for the model: compact, bounded and scrubbed.

Three concerns live here:

* **Size.** Tool output is paid for in tokens, so JSON is written without padding and
  long values are clipped with an explicit marker.
* **Secrets.** Credentials embedded in text (connection strings, ``PASSWORD '...'`` in a
  logged statement) are replaced before anything leaves the server.
* **Shape.** Tabular data is ``{"columns": [...], "rows": [[...]]}``: column names appear
  once instead of once per row.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Iterable, Sequence
from typing import Any

__all__ = [
    "REDACTED",
    "clip",
    "compile_column_patterns",
    "render_csv",
    "render_markdown",
    "scrub",
    "scrub_deep",
    "to_json",
]

REDACTED = "[redacted]"

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # libpq keyword/value: password=secret  password='se cret'
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|sslpassword)\s*=\s*('(?:[^'\\]|\\.)*'|[^\s;,&'\"]+)"
        ),
        r"\1=" + REDACTED,
    ),
    # SQL: PASSWORD 'secret'   ENCRYPTED PASSWORD 'md5...'
    (re.compile(r"(?i)\b(password)\s+(E?'(?:[^']|'')*')"), r"\1 '" + REDACTED + "'"),
    # URI: scheme://user:secret@host
    (
        re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@]+):([^\s@/]+)@"),
        r"\1:" + REDACTED + "@",
    ),
)


def scrub(text: str) -> str:
    """Remove credentials embedded in ``text``."""
    lowered = text.lower()
    if "passw" not in lowered and "://" not in text and "pwd" not in lowered:
        return text
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def scrub_deep(value: Any) -> Any:
    """Apply :func:`scrub` to every string inside lists and dictionaries."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, list):
        return [scrub_deep(item) for item in value]
    if isinstance(value, tuple):
        return [scrub_deep(item) for item in value]
    if isinstance(value, dict):
        return {key: scrub_deep(item) for key, item in value.items()}
    return value


def compile_column_patterns(patterns: Iterable[str]) -> re.Pattern[str] | None:
    """One regular expression matching any of the configured column-name patterns."""
    parts = [p for p in patterns if p]
    if not parts:
        return None
    return re.compile("|".join(f"(?:{p})" for p in parts), re.IGNORECASE)


def clip(text: str, limit: int) -> tuple[str, bool]:
    """Cut ``text`` to ``limit`` characters. Returns the text and whether it was cut."""
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"… [+{len(text) - limit} chars]", True


def _default(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "\\x" + bytes(value).hex()
    return str(value)


def to_json(value: Any) -> str:
    """Compact JSON: no spaces, non-ASCII kept as is."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=_default)


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return to_json(value)
    return str(value)


def render_markdown(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    def escape(text: str) -> str:
        return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", "")

    lines = [
        "| " + " | ".join(escape(c) for c in columns) + " |",
        "|" + "|".join("---" for _ in columns) + "|",
    ]
    for row in rows:
        lines.append("| " + " | ".join(escape(_cell(v)) for v in row) + " |")
    return "\n".join(lines)


def render_csv(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_cell(v) for v in row])
    return buffer.getvalue()
