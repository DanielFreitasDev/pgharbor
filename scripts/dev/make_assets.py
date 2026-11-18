#!/usr/bin/env python3
"""Generate the images used by the README (assets/*.svg), in a light and a dark variant.

    python scripts/dev/make_assets.py

The drawings are plain SVG built from a handful of primitives, so that sizes and
alignment are exact and the two themes never drift apart. Text uses system fonts: GitHub
serves SVG images without external resources.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[2] / "assets"
SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace"


@dataclass(frozen=True)
class Theme:
    name: str
    canvas_top: str
    canvas_bottom: str
    border: str
    card: str
    card_border: str
    text: str
    muted: str
    faint: str
    accent: str  # harbor teal
    accent_soft: str
    blue: str  # PostgreSQL blue
    blue_soft: str
    good: str
    good_soft: str
    bad: str
    bad_soft: str
    warn: str
    code_bg: str


DARK = Theme(
    name="dark",
    canvas_top="#0b1220",
    canvas_bottom="#0f1b2d",
    border="#22304a",
    card="#131f33",
    card_border="#263750",
    text="#e6edf7",
    muted="#9fb0c8",
    faint="#6b7d99",
    accent="#2dd4bf",
    accent_soft="#0f3b3a",
    blue="#6ea8e0",
    blue_soft="#16304f",
    good="#4ade80",
    good_soft="#12351f",
    bad="#f87171",
    bad_soft="#3d1a1c",
    warn="#fbbf24",
    code_bg="#0a111d",
)
LIGHT = Theme(
    name="light",
    canvas_top="#f7fafc",
    canvas_bottom="#eef3f9",
    border="#d5deea",
    card="#ffffff",
    card_border="#d5deea",
    text="#0f1c2e",
    muted="#4a5b73",
    faint="#7a8aa1",
    accent="#0f8f83",
    accent_soft="#d9f3ef",
    blue="#2f6aa3",
    blue_soft="#e1ecf8",
    good="#15803d",
    good_soft="#dcf5e4",
    bad="#c02626",
    bad_soft="#fde5e5",
    warn="#b45309",
    code_bg="#f3f6fa",
)


class Svg:
    """A tiny SVG builder: every helper appends one element."""

    def __init__(self, width: int, height: int, theme: Theme, title: str) -> None:
        self.w, self.h, self.t = width, height, theme
        self.parts: list[str] = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">',
            f"<title>{escape(title)}</title>",
            "<defs>"
            f'<linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0" stop-color="{theme.canvas_top}"/>'
            f'<stop offset="1" stop-color="{theme.canvas_bottom}"/></linearGradient>'
            f'<linearGradient id="mark" x1="0" y1="0" x2="1" y2="1">'
            f'<stop offset="0" stop-color="{theme.accent}"/>'
            f'<stop offset="1" stop-color="{theme.blue}"/></linearGradient>'
            "</defs>",
            f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="18" '
            f'fill="url(#bg)" stroke="{theme.border}"/>',
        ]

    def rect(self, x: float, y: float, w: float, h: float, *, fill: str, stroke: str = "none",
             rx: float = 10, width: float = 1) -> None:  # fmt: skip
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="{width}"/>'
        )

    def text(self, x: float, y: float, content: str, *, size: int = 16, fill: str | None = None,
             weight: int = 400, anchor: str = "start", mono: bool = False,
             spacing: float = 0) -> None:  # fmt: skip
        family = MONO if mono else SANS
        extra = f' letter-spacing="{spacing}"' if spacing else ""
        self.parts.append(
            f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" '
            f'font-weight="{weight}" fill="{fill or self.t.text}" text-anchor="{anchor}"{extra} '
            f'xml:space="preserve">{escape(content)}</text>'
        )

    def line(self, x1: float, y1: float, x2: float, y2: float, *, stroke: str,
             width: float = 2, dash: str = "") -> None:  # fmt: skip
        extra = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" '
            f'stroke-width="{width}" stroke-linecap="round"{extra}/>'
        )

    def arrow(self, x1: float, y: float, x2: float, *, stroke: str) -> None:
        """A horizontal arrow from x1 to x2 (either direction)."""
        back = 9 if x2 > x1 else -9
        self.line(x1, y, x2 - back * 0.6, y, stroke=stroke)
        self.parts.append(
            f'<path d="M{x2 - back} {y - 6} L{x2} {y} L{x2 - back} {y + 6}" fill="none" '
            f'stroke="{stroke}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
        )

    def chip(
        self, x: float, y: float, label: str, *, fill: str, color: str, size: int = 15
    ) -> float:
        """A rounded label; returns its width so chips can be laid out in a row."""
        width = round(len(label) * size * 0.57 + 26)
        self.rect(x, y, width, size + 16, fill=fill, rx=(size + 16) / 2)
        self.text(x + width / 2, y + size + 3, label, size=size, fill=color, weight=600,
                  anchor="middle")  # fmt: skip
        return width

    def mark(self, x: float, y: float, size: float) -> None:
        """The logo: a database held inside the two arms of a harbor."""
        s = size / 100
        cx, cy = x + size / 2, y + size / 2
        self.rect(x, y, size, size, fill="url(#mark)", rx=22 * s)
        ink = "#06121f"
        # harbor walls: two arcs open at the top
        self.parts.append(
            f'<path d="M{cx - 30 * s} {cy - 22 * s} A {34 * s} {34 * s} 0 1 0 {cx + 30 * s} '
            f'{cy - 22 * s}" fill="none" stroke="{ink}" stroke-width="{7 * s}" '
            f'stroke-linecap="round"/>'
        )
        # database cylinder
        top, bottom, rx, ry = cy - 16 * s, cy + 14 * s, 15 * s, 5.5 * s
        self.parts.append(
            f'<path d="M{cx - rx} {top} V{bottom} A {rx} {ry} 0 0 0 {cx + rx} {bottom} V{top}" '
            f'fill="{ink}"/>'
            f'<ellipse cx="{cx}" cy="{top}" rx="{rx}" ry="{ry}" fill="{ink}"/>'
            f'<path d="M{cx - rx} {cy - 1 * s} A {rx} {ry} 0 0 0 {cx + rx} {cy - 1 * s}" '
            f'fill="none" stroke="url(#mark)" stroke-width="{2.6 * s}"/>'
            f'<ellipse cx="{cx}" cy="{top}" rx="{rx - 3 * s}" ry="{ry - 2 * s}" fill="url(#mark)" '
            f'opacity="0.55"/>'
        )

    def save(self, name: str) -> Path:
        ASSETS.mkdir(exist_ok=True)
        path = ASSETS / f"{name}-{self.t.name}.svg"
        path.write_text("\n".join([*self.parts, "</svg>"]) + "\n", encoding="utf-8")
        return path


# ------------------------------------------------------------------------------------ banner


def banner(theme: Theme, tools: int) -> Path:
    svg = Svg(1200, 300, theme, "pgharbor: a safe harbor between AI agents and PostgreSQL")
    svg.mark(64, 74, 152)
    svg.text(256, 138, "pgharbor", size=76, weight=800, spacing=-2)
    svg.text(258, 186, "A safe harbor between AI agents and PostgreSQL.", size=27,
             fill=theme.muted, weight=500)  # fmt: skip
    x = 258.0
    for label, fill, color in (
        ("PostgreSQL 9.6 → 19", theme.blue_soft, theme.blue),
        (f"{tools} tools", theme.accent_soft, theme.accent),
        ("read-only by default", theme.good_soft, theme.good),
        ("MCP 2026-07-28", theme.blue_soft, theme.blue),
    ):
        x += svg.chip(x, 214, label, fill=fill, color=color, size=16) + 12
    return svg.save("banner")


# ------------------------------------------------------------------------------------ layers

LAYERS = (
    ("1", "Scan", "PostgreSQL-faithful lexer", ("two statements", "unterminated text", "U& escapes")),
    ("2", "Vet", "names checked in the catalog", ("pg_read_file, dblink", "pg_sleep, set_config", "views and functions that hide them")),
    ("3", "Server decides", "sub-select, extended protocol", ("INSERT, UPDATE, DDL", "SET, COPY, DO, LOCK", "COMMIT; DROP ...")),
    ("4", "Pre-flight", "EXPLAIN before running", ("full scans of protected tables", "denied relations", "plans above the cost ceiling")),
    ("5", "Run, bounded", "limits applied on the server", ("unbounded result sets", "500 MB values", "runaway queries")),
    ("6", "Rollback", "then DISCARD ALL", ("leftover locks", "changed settings", "session state")),
)  # fmt: skip


def layers(theme: Theme) -> Path:
    svg = Svg(1200, 560, theme, "The six layers every query goes through")
    svg.text(48, 62, "Every query goes through six layers", size=28, weight=700)
    svg.text(48, 94, "Each one can refuse. The ones that decide what a statement is are "
             "enforced by PostgreSQL itself.", size=17, fill=theme.muted)  # fmt: skip
    gap, card_w, top = 14, 172, 132
    x0 = 48
    for i, (number, name, how, stops) in enumerate(LAYERS):
        x = x0 + i * (card_w + gap)
        server_side = number in ("3", "4", "5", "6")
        svg.rect(x, top, card_w, 150, fill=theme.card, stroke=theme.card_border, rx=14)
        svg.rect(x, top, card_w, 6, fill=theme.accent if server_side else theme.blue, rx=3)
        svg.parts.append(
            f'<circle cx="{x + 30}" cy="{top + 42}" r="16" fill="'
            f'{theme.accent_soft if server_side else theme.blue_soft}"/>'
        )
        svg.text(x + 30, top + 48, number, size=17, weight=700, anchor="middle",
                 fill=theme.accent if server_side else theme.blue)  # fmt: skip
        svg.text(x + 16, top + 92, name, size=19, weight=700)
        for j, piece in enumerate(_wrap(how, 21)):
            svg.text(x + 16, top + 116 + j * 19, piece, size=14, fill=theme.muted)
        # what it stops
        svg.line(x + card_w / 2, top + 150, x + card_w / 2, top + 178, stroke=theme.border,
                 dash="3 5")  # fmt: skip
        svg.rect(x, top + 178, card_w, 182, fill=theme.bad_soft, rx=14)
        svg.text(x + 16, top + 206, "STOPS", size=12, weight=700, fill=theme.bad, spacing=1.2)
        line_y = top + 232
        for stop in stops:
            for piece in _wrap(stop, 20):
                svg.text(x + 16, line_y, piece, size=14, fill=theme.text)
                line_y += 19
            line_y += 9
    legend_y = 528
    svg.rect(48, legend_y - 12, 14, 14, fill=theme.blue, rx=4)
    svg.text(70, legend_y, "checked by pgharbor before anything reaches the server", size=14,
             fill=theme.muted)  # fmt: skip
    svg.rect(520, legend_y - 12, 14, 14, fill=theme.accent, rx=4)
    svg.text(542, legend_y, "enforced by the PostgreSQL server", size=14, fill=theme.muted)
    return svg.save("layers")


def _wrap(text: str, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


# ------------------------------------------------------------------------------ architecture


def architecture(theme: Theme) -> Path:
    svg = Svg(1200, 622, theme, "How pgharbor sits between the agent and the databases")
    svg.text(48, 62, "Where pgharbor sits", size=28, weight=700)
    svg.text(48, 94, "One local process, started by your MCP client. It listens on stdio only: "
             "there is no network port.", size=17, fill=theme.muted)  # fmt: skip
    top, height = 134, 248
    # agent
    svg.rect(48, top, 210, height, fill=theme.card, stroke=theme.card_border, rx=16)
    svg.text(70, top + 40, "AI agent", size=21, weight=700)
    for i, item in enumerate(
        ("Claude Code", "Claude Desktop", "Codex, Cursor,", "VS Code, Gemini CLI")
    ):
        svg.text(70, top + 76 + i * 25, item, size=16, fill=theme.muted)
    svg.rect(70, top + 190, 166, 34, fill=theme.blue_soft, rx=8)
    svg.text(153, top + 212, "plain language", size=14, fill=theme.blue, weight=600,
             anchor="middle")  # fmt: skip
    # link
    svg.arrow(268, top + 104, 334, stroke=theme.faint)
    svg.arrow(334, top + 144, 268, stroke=theme.faint)
    svg.text(301, top + 90, "MCP", size=14, fill=theme.faint, weight=700, anchor="middle")
    svg.text(301, top + 170, "stdio", size=13, fill=theme.faint, anchor="middle")
    # pgharbor
    svg.rect(344, top, 476, height, fill=theme.card, stroke=theme.accent, rx=16, width=2)
    svg.mark(368, top + 22, 40)
    svg.text(420, top + 50, "pgharbor", size=23, weight=800)
    boxes = (
        ("Connection profiles", "access mode, limits, protected tables"),
        ("Safety layers", "scan, vet, pre-flight, bounded run"),
        ("Tools", "schema, queries, performance, security"),
        ("Approval and audit", "human dialog, append-only log"),
    )
    for i, (name, detail) in enumerate(boxes):
        y = top + 74 + i * 42
        svg.rect(368, y, 428, 34, fill=theme.code_bg, stroke=theme.card_border, rx=8)
        svg.text(382, y + 22, name, size=15, weight=700)
        svg.text(782, y + 22, detail, size=13, fill=theme.muted, anchor="end")
    # databases
    svg.arrow(828, top + 124, 908, stroke=theme.faint)
    svg.text(868, top + 110, "SQL", size=14, fill=theme.faint, weight=700, anchor="middle")
    databases = (
        ("dev", "read-write", theme.good, theme.good_soft),
        ("staging", "read-only", theme.blue, theme.blue_soft),
        ("prod", "needs approval", theme.warn, theme.card),
    )
    for i, (name, mode, color, fill) in enumerate(databases):
        y = top + i * 86
        svg.rect(916, y, 236, 76, fill=theme.card, stroke=theme.card_border, rx=14)
        _cylinder(svg, 940, y + 20, color)
        svg.text(990, y + 34, name, size=19, weight=700)
        svg.rect(990, y + 44, round(len(mode) * 7.4 + 18), 22, fill=fill, rx=11,
                 stroke=color if fill == theme.card else "none")  # fmt: skip
        svg.text(999, y + 60, mode, size=13, fill=color, weight=600)
    _panel_row(svg, theme, top + height)
    return svg.save("architecture")


def _panel_row(svg: Svg, theme: Theme, above: float) -> None:
    """The owner's side: the management panel, a separate program that meets the server
    only through three files."""
    files = ("config.toml", "audit log · sessions", "pause")
    widths = [round(len(label) * 15 * 0.57 + 26) for label in files]
    gap = 18
    x = 344 + (476 - sum(widths) - gap * (len(files) - 1)) / 2
    chips_y, box_y = above + 30, above + 96
    for label, width in zip(files, widths, strict=True):
        middle = x + width / 2
        svg.line(middle, above + 4, middle, chips_y - 4, stroke=theme.faint, dash="2 6")
        svg.chip(x, chips_y, label, fill=theme.code_bg, color=theme.muted)
        svg.line(middle, chips_y + 35, middle, box_y - 4, stroke=theme.faint, dash="2 6")
        x += width + gap
    # the owner
    svg.rect(48, box_y, 210, 98, fill=theme.card, stroke=theme.card_border, rx=16)
    svg.text(70, box_y + 40, "You", size=21, weight=700)
    svg.text(70, box_y + 68, "in your browser", size=16, fill=theme.muted)
    svg.arrow(268, box_y + 40, 334, stroke=theme.faint)
    svg.arrow(334, box_y + 64, 268, stroke=theme.faint)
    svg.text(301, box_y + 26, "key", size=14, fill=theme.faint, weight=700, anchor="middle")
    svg.text(301, box_y + 88, "127.0.0.1", size=13, fill=theme.faint, anchor="middle")
    # the panel
    svg.rect(344, box_y, 476, 98, fill=theme.card, stroke=theme.card_border, rx=16)
    svg.mark(368, box_y + 22, 40)
    svg.text(420, box_y + 42, "Management panel", size=21, weight=800)
    svg.text(420, box_y + 66, "pgharbor ui: a separate program, started by you", size=14,
             fill=theme.muted)  # fmt: skip
    svg.text(420, box_y + 85, "and only while you want it", size=14, fill=theme.muted)
    # what it is for
    svg.rect(916, box_y, 236, 98, fill=theme.card, stroke=theme.card_border, rx=14)
    notes = (
        ("See", "calls, sessions, refusals"),
        ("Change", "connections, rules, tools"),
        ("Stop", "all access, at once"),
    )
    for i, (verb, what) in enumerate(notes):
        y = box_y + 30 + i * 26
        svg.text(934, y, verb, size=13, weight=700)
        svg.text(990, y, what, size=12, fill=theme.muted)


def _cylinder(svg: Svg, x: float, y: float, color: str) -> None:
    svg.parts.append(
        f'<path d="M{x} {y + 6} V{y + 30} A 16 6 0 0 0 {x + 32} {y + 30} V{y + 6}" fill="none" '
        f'stroke="{color}" stroke-width="2.2"/>'
        f'<ellipse cx="{x + 16}" cy="{y + 6}" rx="16" ry="6" fill="none" stroke="{color}" '
        f'stroke-width="2.2"/>'
        f'<path d="M{x} {y + 18} A 16 6 0 0 0 {x + 32} {y + 18}" fill="none" stroke="{color}" '
        f'stroke-width="2.2"/>'
    )


# -------------------------------------------------------------------------------------- demo

Line = tuple[str, str]  # (style, text)


def terminal(theme: Theme, name: str, title: str, lines: list[Line]) -> Path:
    line_height = 25
    height = 86 + len(lines) * line_height + 44
    svg = Svg(1200, height, theme, title)
    svg.rect(24, 24, 1152, height - 48, fill=theme.code_bg, stroke=theme.card_border, rx=14)
    for i, color in enumerate(("#f87171", "#fbbf24", "#4ade80")):
        svg.parts.append(f'<circle cx="{52 + i * 22}" cy="50" r="6.5" fill="{color}"/>')
    svg.text(600, 55, title, size=14, fill=theme.faint, anchor="middle", mono=True)
    svg.line(24, 74, 1176, 74, stroke=theme.card_border, width=1)
    colors = {
        "user": theme.text,
        "tool": theme.accent,
        "out": theme.muted,
        "ok": theme.good,
        "no": theme.bad,
        "say": theme.text,
        "dim": theme.faint,
        "": theme.muted,
    }
    y = 106
    for style, content in lines:
        weight = 700 if style in ("user", "tool") else 400
        svg.text(52, y, content, size=15.5, fill=colors[style], mono=True, weight=weight)  # type: ignore[arg-type]
        y += line_height
    return svg.save(name)


DEMO_QUERY: list[Line] = [
    ("user", "> Which products sold the most last week on dev? Show revenue too."),
    ("", ""),
    ("tool", '● pg_list_tables(connection="dev", schema="shop")'),
    (
        "out",
        "  ⎿ customers 400 rows · orders 6,000 rows · order_items 12,000 rows · products 120 rows",
    ),
    ("tool", '● pg_describe_table(table="shop.order_items", detail="summary")'),
    ("out", "  ⎿ order_id bigint FK→orders.id · product_id int FK→products.id · qty int"),
    ("tool", '● pg_query(connection="dev", params=["2026-01-04"], sql='),
    ("out", '    "SELECT p.name, sum(i.qty) AS units, sum(i.qty * p.price) AS revenue'),
    ("out", "       FROM shop.order_items i JOIN shop.products p ON p.id = i.product_id"),
    ("out", "       JOIN shop.orders o ON o.id = i.order_id"),
    (
        "out",
        '      WHERE o.placed_at >= $1::date - 7 GROUP BY p.name ORDER BY units DESC LIMIT 5")',
    ),
    ("ok", "  ⎿ 5 rows · 14 ms · read-only transaction, rolled back"),
    ("", ""),
    (
        "say",
        "  The best seller was Product 7 (412 units, R$ 10,094.00), followed by Product 14 ...",
    ),
]

DEMO_REFUSED: list[Line] = [
    ("user", "> The report is slow. Just kill whatever is blocking it on prod."),
    ("", ""),
    (
        "tool",
        '● pg_query(connection="prod", sql="SELECT pg_terminate_backend(pid) FROM pg_stat_activity ...")',
    ),
    (
        "no",
        "  ⎿ Connection 'prod' (production: db.internal:5432/app) needs the user's approval before it",
    ),
    (
        "no",
        '    is used in this session. Call pg_request_access(connection="prod", reason="...") first.',
    ),
    ("tool", '● pg_request_access(connection="prod", reason="find what blocks the weekly report")'),
    ("ok", '  ⎿ {"connection": "prod", "approved": true, "how": "dialog", "access": "read-only"}'),
    (
        "tool",
        '● pg_query(connection="prod", sql="SELECT pg_terminate_backend(pid) FROM pg_stat_activity ...")',
    ),
    (
        "no",
        "  ⎿ Refused (function-denied): pg_terminate_backend() is not allowed: it can act outside the",
    ),
    (
        "no",
        "    database or change the session, even inside a read-only transaction. Hint: pgharbor has",
    ),
    (
        "no",
        "    dedicated tools for what the server knows: pg_activity and pg_locks for sessions, ...",
    ),
    ("tool", '● pg_locks(connection="prod")'),
    (
        "out",
        "  ⎿ pid 48213 (reporting, waiting 4 min) ← blocked by pid 47102 (app, idle in transaction 31 min)",
    ),
    ("", ""),
    (
        "say",
        "  I can't end sessions on prod: this connection is read-only. What is blocking the report is",
    ),
    (
        "say",
        "  pid 47102, an application session left idle in a transaction for 31 minutes. To free it, a",
    ),
    ("say", "  DBA can run:  SELECT pg_terminate_backend(47102);   (not executed)"),
]


def main() -> None:
    import sys

    tools = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    written = []
    for theme in (DARK, LIGHT):
        written += [
            banner(theme, tools),
            layers(theme),
            architecture(theme),
            terminal(theme, "demo-query", "claude — exploring and querying", DEMO_QUERY),
            terminal(
                theme,
                "demo-refused",
                "claude — what happens when the agent overreaches",
                DEMO_REFUSED,
            ),  # fmt: skip
        ]
    for path in written:
        print(path.relative_to(ASSETS.parent))


if __name__ == "__main__":
    main()
