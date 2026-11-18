"""The gate of the management panel: what a hostile page, another site, another local
program or a curious user can send, and what they get back.

Every case here is a request that must NOT work. The panel can change which databases an
agent reaches, so these are the tests that matter most in this directory.
"""

import http.client
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from .panel_client import HOST, ORIGIN, PORT, SECRETS, PanelClient

API = "/api/config"


def browser(token: str | None, **extra: str) -> list[tuple[str, str]]:
    headers = {"host": HOST, **extra}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    return list(headers.items())


async def test_the_key_opens_the_api_and_nothing_else_does(panel: PanelClient) -> None:
    assert (await panel.get(API)).status == 200

    no_key = await panel.request("GET", API, headers=browser(None))
    assert (no_key.status, no_key.error) == (401, "token")
    assert no_key.headers["www-authenticate"] == "Bearer"
    assert not any(secret in no_key.text for secret in SECRETS)

    for wrong in ("", "x", panel.gate.token[:-1], panel.gate.token + "x", "null"):
        refused = await panel.request("GET", API, headers=browser(wrong))
        assert refused.status == 401, wrong

    # Only the Authorization header carries the key: not the address, not a cookie.
    in_address = await panel.request(
        "GET", f"{API}?token={panel.gate.token}", headers=browser(None)
    )
    assert in_address.status == 401
    in_cookie = await panel.request(
        "GET", API, headers=browser(None, cookie=f"token={panel.gate.token}")
    )
    assert in_cookie.status == 401
    other_scheme = await panel.request(
        "GET", API, headers=[("host", HOST), ("authorization", f"Basic {panel.gate.token}")]
    )
    assert other_scheme.status == 401


async def test_every_run_has_its_own_key(panel: PanelClient) -> None:
    other = PanelClient()
    assert other.gate.token != panel.gate.token
    assert len(panel.gate.token) >= 43  # 256 bits, URL-safe
    stale = await panel.request("GET", API, headers=browser(other.gate.token))
    assert stale.status == 401
    # The key travels in the fragment, which browsers never send to a server.
    assert panel.gate.url == f"http://127.0.0.1:{PORT}/#token={panel.gate.token}"


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        f"evil.example:{PORT}",
        "127.0.0.1",  # the default port, not ours
        "127.0.0.1:80",
        f"127.0.0.1:{PORT + 1}",
        f"localhost.evil.example:{PORT}",
        f"127.0.0.1.evil.example:{PORT}",
        f"0.0.0.0:{PORT}",
        f"[::1]:{PORT}",
        f"127.0.0.1:{PORT}.evil.example",
        "",
    ],
)
async def test_dns_rebinding_a_foreign_name_is_refused_even_with_the_key(
    panel: PanelClient, host: str
) -> None:
    headers = [("host", host), ("authorization", f"Bearer {panel.gate.token}")]
    for path in (API, "/", "/js/app.js"):
        refused = await panel.request("GET", path, headers=headers)
        assert (refused.status, refused.error) == (403, "host"), (host, path)


async def test_a_request_must_name_exactly_one_host(panel: PanelClient) -> None:
    key = ("authorization", f"Bearer {panel.gate.token}")
    none = await panel.request("GET", API, headers=[key])
    two = await panel.request("GET", API, headers=[("host", HOST), ("host", "evil.example"), key])
    assert none.status == two.status == 403
    assert (
        await panel.request("GET", API, headers=[("host", f"LOCALHOST:{PORT}"), key])
    ).status == 200


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "http://evil.example",
        "null",
        "http://127.0.0.1",
        f"http://127.0.0.1:{PORT + 1}",
        f"https://127.0.0.1:{PORT}",
        f"http://127.0.0.1:{PORT}.evil.example",
        f"http://evil.example/http://127.0.0.1:{PORT}",
        "",
    ],
)
async def test_a_foreign_origin_is_refused_even_with_the_key(
    panel: PanelClient, origin: str
) -> None:
    refused = await panel.request("GET", API, headers=browser(panel.gate.token, origin=origin))
    assert (refused.status, refused.error) == (403, "origin"), origin
    also_static = await panel.request("GET", "/", headers=[("host", HOST), ("origin", origin)])
    assert also_static.status == 403


async def test_the_panels_own_origins_are_accepted(panel: PanelClient) -> None:
    for origin in (ORIGIN, f"http://localhost:{PORT}"):
        accepted = await panel.request("GET", API, headers=browser(panel.gate.token, origin=origin))
        assert accepted.status == 200, origin


@pytest.mark.parametrize("site", ["cross-site", "same-site", "none", "SAME-ORIGIN", ""])
async def test_what_the_browser_says_about_the_request_is_believed(
    panel: PanelClient, site: str
) -> None:
    headers = browser(panel.gate.token, **{"sec-fetch-site": site})
    refused = await panel.request("GET", API, headers=headers)
    assert (refused.status, refused.error) == (403, "cross-site"), site


async def test_typing_the_address_reaches_the_page_but_not_the_api(panel: PanelClient) -> None:
    typed = [("host", HOST), ("sec-fetch-site", "none")]
    assert (await panel.request("GET", "/", headers=typed)).status == 200
    from_elsewhere = [("host", HOST), ("sec-fetch-site", "cross-site")]
    assert (await panel.request("GET", "/", headers=from_elsewhere)).status == 403


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"])
async def test_only_get_and_post_exist(panel: PanelClient, method: str) -> None:
    refused = await panel.request(method, API, headers=browser(panel.gate.token))
    assert refused.status == 405
    # In particular no CORS preflight is ever answered with permission.
    assert not any(name.startswith("access-control-") for name in refused.headers)


@pytest.mark.parametrize(
    "content_type",
    ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data", ""],
)
async def test_a_form_cannot_change_anything(panel: PanelClient, content_type: str) -> None:
    """The three types a page can send without asking permission are all refused."""
    body = json.dumps({"paused": True}).encode()
    headers = [
        *browser(panel.gate.token),
        ("content-type", content_type),
        ("content-length", str(len(body))),
    ]
    refused = await panel.request("POST", "/api/pause", headers=headers, body=body)
    assert (refused.status, refused.error) == (415, "content-type")
    assert (await panel.get("/api/pause")).json() == {"paused": False}


async def test_a_change_without_the_key_changes_nothing(panel: PanelClient) -> None:
    body = json.dumps({"paused": True}).encode()
    headers = [("host", HOST), ("content-type", "application/json")]
    assert (await panel.request("POST", "/api/pause", headers=headers, body=body)).status == 401
    assert (await panel.get("/api/pause")).json() == {"paused": False}
    # The page's files accept no POST at all.
    assert (await panel.request("POST", "/", headers=headers, body=body)).status == 405


async def test_a_request_too_large_is_refused(panel: PanelClient) -> None:
    headers = [*browser(panel.gate.token), ("content-type", "application/json")]
    declared = await panel.request(
        "POST", "/api/pause", headers=[*headers, ("content-length", "5000000")], body=b"{}"
    )
    assert (declared.status, declared.error) == (413, "too-large")
    garbage = await panel.request(
        "POST", "/api/pause", headers=[*headers, ("content-length", "-1")], body=b"{}"
    )
    assert garbage.status == 413


async def test_every_answer_carries_the_security_headers(panel: PanelClient) -> None:
    replies = [
        await panel.get(API),
        await panel.request("GET", "/", headers=[("host", HOST)]),
        await panel.request("GET", API, headers=[("host", "evil.example")]),
        await panel.request("GET", "/nothing-here", headers=[("host", HOST)]),
    ]
    for reply in replies:
        policy = reply.headers["content-security-policy"]
        assert "default-src 'none'" in policy
        assert "script-src 'self'" in policy
        assert "frame-ancestors 'none'" in policy
        assert "unsafe" not in policy
        assert "*" not in policy
        assert reply.headers["x-content-type-options"] == "nosniff"
        assert reply.headers["x-frame-options"] == "DENY"
        assert reply.headers["referrer-policy"] == "no-referrer"
        assert reply.headers["cache-control"] == "no-store"
        assert reply.headers["cross-origin-resource-policy"] == "same-origin"
        assert "set-cookie" not in reply.headers  # the panel keeps nothing in cookies
        assert not any(name.startswith("access-control-") for name in reply.headers)


@pytest.mark.parametrize(
    "path",
    [
        "/../server.py",
        "/..%2fserver.py",
        "/%2e%2e/%2e%2e/config.py",
        "/js/../../gate.py",
        "/static/../../../../etc/passwd",
        "//etc/passwd",
        "/.git/config",
    ],
)
async def test_only_the_pages_own_files_are_served(panel: PanelClient, path: str) -> None:
    reply = await panel.request("GET", path, headers=[("host", HOST)])
    assert reply.status in (400, 404), path
    assert "def " not in reply.text
    assert "root:" not in reply.text


async def test_unknown_api_routes_answer_as_json(panel: PanelClient) -> None:
    missing = await panel.get("/api/does-not-exist")
    assert (missing.status, missing.error) == (404, "not-found")


async def test_using_the_api_counts_as_use_but_knocking_does_not(panel: PanelClient) -> None:
    panel.gate.last_use = 0.0
    await panel.request("GET", API, headers=browser("wrong"))
    await panel.request("GET", "/", headers=[("host", HOST)])
    assert panel.gate.last_use == 0.0  # a stranger cannot keep the panel awake
    await panel.get(API)
    assert panel.gate.last_use > 0.0


def test_the_mcp_server_never_loads_the_panel() -> None:
    """What the agent talks to carries no panel code and nothing that writes the
    configuration. (The MCP SDK imports a web server of its own; pgharbor never starts it.)"""
    code = (
        "import sys, pgharbor.cli, pgharbor.server\n"
        "pgharbor.server.build_server(pgharbor.server.Config())\n"
        "bad = [m for m in sys.modules if m.startswith(('pgharbor.ui.', 'tomlkit'))]\n"
        "print(bad)\n"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_the_real_command_serves_the_panel_and_stops(tmp_path: Path) -> None:
    """`pgharbor ui` as a person runs it: it prints the address, answers only with the
    key and only to its own address, and stops when asked."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PG", "DATABASE_URL"))}
    for variable in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "APPDATA", "LOCALAPPDATA"):
        env[variable] = str(tmp_path)
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        [sys.executable, "-m", "pgharbor.cli", "ui", "--port", "0", "--no-open"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        address = None
        assert process.stdout is not None
        for _ in range(40):
            line = process.stdout.readline()
            found = re.search(r"http://127\.0\.0\.1:(\d+)/#token=([A-Za-z0-9_-]+)", line)
            if found:
                address = found
                break
        assert address, "the command did not print its address"
        port, token = int(address.group(1)), address.group(2)

        def ask(path: str, *, key: str | None = None, host: str | None = None, post: bool = False):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            headers = {"Host": host or f"127.0.0.1:{port}"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            if post:
                headers["Content-Type"] = "application/json"
            connection.request(
                "POST" if post else "GET", path, body=b"{}" if post else None, headers=headers
            )
            reply = connection.getresponse()
            body = reply.read()
            connection.close()
            return reply.status, dict(reply.getheaders()), body

        status, headers, body = ask("/")
        assert status == 200
        assert b"pgharbor" in body
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert "server" not in {name.lower() for name in headers}  # it does not say what it runs on
        assert ask("/js/app.js")[0] == 200
        assert ask("/i18n/pt-BR.json")[0] == 200
        assert ask("/api/meta")[0] == 401
        assert ask("/api/meta", key=token, host="evil.example")[0] == 403
        status, _, body = ask("/api/meta", key=token)
        assert status == 200
        assert json.loads(body)["read_only"] is False
        assert json.loads(ask("/api/config", key=token)[2])["file"]["origin"] == "none"

        assert ask("/api/shutdown", key=token, post=True)[0] == 200
        assert process.wait(timeout=20) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
