"""Who may talk to the panel.

The panel can change which databases an agent reaches, so every request is checked
before any of its own code runs. The threats, and what answers each one:

- **A web page open in the owner's browser** can make that browser send requests to
  ``127.0.0.1``. It cannot read the key (a random token made at start-up, shown only in
  the terminal), and it cannot send it: the token travels in the ``Authorization``
  header, which a foreign page cannot set, and never in a cookie. Cookies are shared by
  every port of ``127.0.0.1``, so a cookie would hand the key to any other local server.
- **DNS rebinding** makes a foreign name point at ``127.0.0.1`` so that the page and the
  panel look like one site. The ``Host`` header then carries the foreign name, and only
  the panel's own address and port are accepted.
- **Cross-site requests** that need no key at all (a form, an image) are refused by what
  the browser itself says about them: ``Origin`` and ``Sec-Fetch-Site``. Both are
  compared for equality with known values, never matched as patterns.
- **Another user of the machine** can reach the port but not the key.

What this does not stop is a program running as the owner: it can read the configuration
file without any panel. ``docs/security.md`` says so plainly.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

__all__ = ["MAX_BODY_BYTES", "SECURITY_HEADERS", "Gate", "GateMiddleware"]

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
App = Callable[[Scope, Receive, Send], Awaitable[None]]

MAX_BODY_BYTES = 1_000_000

# The page is made of its own files only: no inline script or style, nothing from
# another origin, no embedding in a frame, and nothing kept by the browser.
SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (
        b"content-security-policy",
        b"default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
        b"font-src 'self'; connect-src 'self'; manifest-src 'self'; base-uri 'none'; "
        b"form-action 'none'; frame-ancestors 'none'",
    ),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"cache-control", b"no-store"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
)


class Gate:
    """The key of one run of the panel and the rules every request must meet."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.token = secrets.token_urlsafe(32)  # 256 bits, new on every start
        hosts = (f"127.0.0.1:{port}", f"localhost:{port}")
        self.hosts = frozenset(hosts)
        self.origins = frozenset(f"http://{host}" for host in hosts)
        self.last_use = time.monotonic()

    @property
    def url(self) -> str:
        # In the fragment: it is never sent to a server and never appears in a Referer.
        return f"http://127.0.0.1:{self.port}/#token={self.token}"

    def refusal(self, scope: Scope) -> tuple[int, str, str] | None:
        """``(status, code, message)`` when the request must not be served."""
        headers: dict[str, list[str]] = {}
        for raw_name, raw_value in scope.get("headers", ()):
            headers.setdefault(raw_name.decode("latin-1").lower(), []).append(
                raw_value.decode("latin-1")
            )
        method = scope.get("method", "")
        path = scope.get("path", "")

        hosts = headers.get("host", [])
        if len(hosts) != 1 or hosts[0].lower() not in self.hosts:
            return 403, "host", "This address is not the panel's own address."
        if method not in ("GET", "HEAD", "POST"):
            return 405, "method", "Method not allowed."
        is_api = path.startswith("/api/")
        site = headers.get("sec-fetch-site")
        if site is not None:
            allowed = ("same-origin",) if is_api else ("same-origin", "none")
            if len(site) != 1 or site[0] not in allowed:
                return 403, "cross-site", "Requests from other sites are refused."
        origin = headers.get("origin")
        if origin is not None and (len(origin) != 1 or origin[0] not in self.origins):
            return 403, "origin", "Requests from other origins are refused."
        if not is_api:
            if method == "POST":
                return 405, "method", "Method not allowed."
            return None

        authorization = headers.get("authorization", [])
        scheme, _, presented = (authorization[0] if len(authorization) == 1 else "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(
            presented.strip().encode("utf-8"), self.token.encode("utf-8")
        ):
            return 401, "token", "Open the address shown in the terminal by `pgharbor ui`."
        if method == "POST":
            kind = headers.get("content-type", [""])[0].partition(";")[0].strip().lower()
            if kind != "application/json":
                return 415, "content-type", "Send JSON."
            length = headers.get("content-length", ["0"])[0]
            if not length.isdigit() or int(length) > MAX_BODY_BYTES:
                return 413, "too-large", "The request is too large."
        self.last_use = time.monotonic()
        return None


class GateMiddleware:
    """The outermost layer of the panel: nothing is served that the gate refused, and
    every answer carries the security headers."""

    def __init__(self, app: App, gate: Gate) -> None:
        self.app = app
        self.gate = gate

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            return  # no WebSocket, nothing else: the panel speaks plain HTTP only

        async def guarded_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                names = {name for name, _ in SECURITY_HEADERS}
                kept = [(n, v) for n, v in message.get("headers", ()) if n.lower() not in names]
                message["headers"] = [*kept, *SECURITY_HEADERS]
            await send(message)

        refused = self.gate.refusal(scope)
        if refused is not None:
            status, code, text = refused
            body = json.dumps({"error": {"code": code, "message": text}}).encode("utf-8")
            headers = [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ]
            if status == 401:
                headers.append((b"www-authenticate", b"Bearer"))
            await guarded_send(
                {"type": "http.response.start", "status": status, "headers": headers}
            )
            await guarded_send({"type": "http.response.body", "body": body})
            return

        received = 0

        async def bounded_receive() -> Message:
            # The declared length was checked; this holds a client to what it declared.
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_BODY_BYTES:
                    raise ValueError("request body larger than declared")
            return message

        await self.app(scope, bounded_receive, guarded_send)
