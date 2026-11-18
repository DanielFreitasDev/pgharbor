"""A minimal client that talks to the panel's ASGI application in memory, the way a
browser or any other program would: every test decides its own headers, so the gate is
exercised with exactly what an attacker could send."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from pgharbor.ui.gate import Gate
from pgharbor.ui.server import Panel, build_app

PORT = 7432
HOST = f"127.0.0.1:{PORT}"
ORIGIN = f"http://{HOST}"


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")

    def json(self) -> Any:
        return json.loads(self.body)

    @property
    def error(self) -> str | None:
        try:
            return self.json()["error"]["code"]
        except (ValueError, KeyError, TypeError):
            return None


class PanelClient:
    def __init__(self, *, read_only: bool = False) -> None:
        self.gate = Gate(PORT)
        self.panel = Panel(self.gate, read_only=read_only)
        self.app = build_app(self.panel)

    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: list[tuple[str, str]] | None = None,
        body: bytes = b"",
    ) -> Reply:
        """Send exactly these headers: nothing is added, not even ``Host``."""
        target, _, query = path.partition("?")
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": target,
            "raw_path": target.encode(),
            "query_string": query.encode(),
            "root_path": "",
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or [])],
            "server": ("127.0.0.1", PORT),
            "client": ("127.0.0.1", 50_000),
        }
        sent = False
        status = 0
        got_headers: dict[str, str] = {}
        chunks: list[bytes] = []

        async def receive() -> dict[str, Any]:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                for name, value in message.get("headers", ()):
                    got_headers[name.decode().lower()] = value.decode()
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        await self.app(scope, receive, send)
        return Reply(status, got_headers, b"".join(chunks))

    def _as_browser(self, token: str | None) -> list[tuple[str, str]]:
        headers = [("host", HOST), ("sec-fetch-site", "same-origin")]
        if token is not None:
            headers.append(("authorization", f"Bearer {token}"))
        return headers

    async def get(self, path: str) -> Reply:
        """A request from the panel's own page."""
        return await self.request("GET", path, headers=self._as_browser(self.gate.token))

    async def post(self, path: str, data: Any = None) -> Reply:
        body = json.dumps({} if data is None else data).encode()
        headers = [
            *self._as_browser(self.gate.token),
            ("origin", ORIGIN),
            ("content-type", "application/json"),
            ("content-length", str(len(body))),
        ]
        return await self.request("POST", path, headers=headers, body=body)


CONFIG = """\
# The owner's notes stay in the file.
default_connection = "dev"

[defaults]
statement_timeout = "30s"  # short on purpose

# Development
[connections.dev]
description = "Development"
host = "127.0.0.1"
port = 1
dbname = "app"
user = "agent"
password = "hunter2-inline-secret"
connect_timeout = "1s"

[connections.dev.params]
sslmode = "disable"
sslpassword = "key-file-passphrase"

# Production
[connections.prod]
description = "Production"
environment = "production"
dsn = "postgresql://agent:dsn-secret-value@db.internal/app"
max_plan_cost = 500000

[[connections.prod.protect]]
table = "billing.cards"
deny_columns = ["card_number", "cvv"]
require_index_filter = true

[audit]
enabled = true
"""

SECRETS = ("hunter2-inline-secret", "key-file-passphrase", "dsn-secret-value")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated installation: one configuration file and an empty state directory."""
    for variable in ("XDG_STATE_HOME", "LOCALAPPDATA", "XDG_CONFIG_HOME", "APPDATA"):
        monkeypatch.setenv(variable, str(tmp_path / "home"))
    file = tmp_path / "etc" / "config.toml"
    file.parent.mkdir()
    file.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("PGHARBOR_CONFIG", str(file))
    return tmp_path


@pytest.fixture
def config_file(home: Path) -> Path:
    return home / "etc" / "config.toml"


@pytest.fixture
def panel(home: Path) -> PanelClient:
    return PanelClient()
