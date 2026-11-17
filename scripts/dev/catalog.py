#!/usr/bin/env python3
"""Look up system catalogs, views, functions and settings across PostgreSQL versions.

The data is a snapshot taken from real servers, 9.6 through 19 (see catalogs.json.gz in
this directory). Use it while writing version-aware SQL instead of trusting memory.

    scripts/dev/catalog.py rel pg_stat_activity        columns of a relation, per version
    scripts/dev/catalog.py rel pg_stat_progress        every relation matching the text
    scripts/dev/catalog.py fn pg_get_partkeydef        functions matching the text
    scripts/dev/catalog.py guc wal_                    settings matching the text
    scripts/dev/catalog.py list 17                     every relation of one version
"""

import gzip
import json
import sys
from pathlib import Path

with gzip.open(Path(__file__).with_name("catalogs.json.gz"), "rt") as _handle:
    DATA = json.load(_handle)
VERSIONS = sorted(DATA, key=float)


def ranges(present: list[str]) -> str:
    """Turn ['12', '13', '14'] into '12-14' against the full version list."""
    out, start, prev = [], None, None
    for v in VERSIONS:
        if v in present:
            start = start or v
            prev = v
        elif start:
            out.append(start if start == prev else f"{start}-{prev}")
            start = None
    if start:
        out.append(start if start == prev else f"{start}-{prev}")
    return ", ".join(out)


def relation(text: str) -> None:
    names = sorted({r for v in VERSIONS for r in DATA[v]["relations"] if text in r})
    exact = [n for n in names if n.split(".", 1)[1] == text]
    for name in exact or names:
        present = [v for v in VERSIONS if name in DATA[v]["relations"]]
        print(f"\n{name}  [{ranges(present)}]")
        if not exact and len(names) > 6:
            continue
        columns: dict[str, list[str]] = {}
        for v in present:
            for column in DATA[v]["relations"][name]["columns"]:
                columns.setdefault(column, []).append(v)
        for column, versions in columns.items():
            note = "" if versions == present else f"   <- only {ranges(versions)}"
            print(f"    {column}{note}")


def function(text: str) -> None:
    found: dict[str, list[str]] = {}
    for v in VERSIONS:
        for signature in DATA[v]["functions"]:
            if text in signature.split("(", 1)[0]:
                found.setdefault(signature, []).append(v)
    for signature, versions in sorted(found.items()):
        print(f"{signature}   [{ranges(versions)}]")


def setting(text: str) -> None:
    found: dict[str, list[str]] = {}
    for v in VERSIONS:
        for name in DATA[v]["settings"]:
            if text in name:
                found.setdefault(name, []).append(v)
    for name, versions in sorted(found.items()):
        print(f"{name}   [{ranges(versions)}]")


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in ("rel", "fn", "guc", "list"):
        print(__doc__)
        return 2
    kind, text = sys.argv[1:]
    if kind == "rel":
        relation(text)
    elif kind == "fn":
        function(text)
    elif kind == "guc":
        setting(text)
    else:
        for name in sorted(DATA[text]["relations"]):
            print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
