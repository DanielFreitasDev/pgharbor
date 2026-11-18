"""The panel's HTTP application: a JSON API behind the gate, and the page's own files.

Every route is reached only after :class:`~pgharbor.ui.gate.GateMiddleware` accepted the
request. Routes that change something (the configuration, the pause) are refused in
read-only mode, write an entry in the audit log, and never return a credential.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import os
import platform
import secrets
import socket
import sys
import time
import traceback
import webbrowser
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import __version__
from ..app import REGISTRY
from ..audit import Audit, Stopwatch
from ..config import _NAME_RE, Access, AuditConfig, Config, state_dir
from ..errors import PgHarborError
from ..pause import clear_pause, read_pause, write_pause
from ..presence import HEARTBEAT_SECONDS, STALE_AFTER_SECONDS, read_sessions, sessions_dir
from ..render import scrub
from ..server import load_tool_modules
from ..session import Sessions, describe_db_error
from . import DEFAULT_PORT
from .activity import RANGES, AuditReader
from .document import (
    Document,
    DocumentError,
    apply_ops,
    backup_text,
    check_text,
    create_starter,
    describe,
    list_backups,
    mask_text,
    read_document,
    save_text,
    unified_diff,
)
from .fields import schema
from .gate import Gate, GateMiddleware
from .risks import compare, needs_acknowledgement, posture

__all__ = ["Panel", "build_app", "serve"]

STATIC = Path(__file__).parent / "static"

_STATUS = {
    "conflict": 409,
    "invalid": 422,
    "invalid-op": 400,
    "toml": 422,
    "not-found": 404,
    "too-large": 413,
    "io": 500,
}
Handler = Callable[["Panel", Request], Awaitable[Any]]
Endpoint = Callable[[Request], Awaitable[Response]]


class _Refused(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        self.status = status
        self.body = {"error": {"code": code, "message": message, **extra}}
        super().__init__(message)


class Panel:
    """What one run of the panel holds: its gate, its mode and its view of the log."""

    def __init__(self, gate: Gate, *, read_only: bool = False, idle_seconds: float = 0) -> None:
        self.gate = gate
        self.read_only = read_only
        self.idle_seconds = idle_seconds
        self.started = time.time()
        self.saving = asyncio.Lock()
        self.stop: Callable[[], None] = lambda: None
        self._reader: AuditReader | None = None

    def reader(self, config: Config | None) -> AuditReader:
        path = (config.audit if config else AuditConfig()).resolved_path()
        if self._reader is None or self._reader.path != path:
            self._reader = AuditReader(path)
        self._reader.refresh()
        return self._reader

    def record(self, config: Config | None, action: str, outcome: str, **more: Any) -> None:
        """What the owner did in the panel goes to the same log as what agents do."""
        if config is not None:
            Audit(config.audit, session="panel").record(
                tool=f"panel.{action}", connection=more.pop("connection", None), outcome=outcome,
                **more,
            )  # fmt: skip


# ----------------------------------------------------------------------------- plumbing


def _route(*, changes: bool = False) -> Callable[[Handler], Endpoint]:
    """Turn a function returning plain data into a route with uniform errors."""

    def decorate(handler: Handler) -> Endpoint:
        @functools.wraps(handler)
        async def run(request: Request) -> Response:
            panel: Panel = request.app.state.panel
            try:
                if changes and panel.read_only:
                    raise _Refused(403, "read-only", "The panel was started in read-only mode.")
                return JSONResponse(await handler(panel, request))
            except _Refused as exc:
                return JSONResponse(exc.body, status_code=exc.status)
            except DocumentError as exc:
                body = {"error": {"code": exc.code, "message": scrub(str(exc))}}
                return JSONResponse(body, status_code=_STATUS.get(exc.code, 400))
            except PgHarborError as exc:
                body = {"error": {"code": "refused", "message": scrub(str(exc))}}
                return JSONResponse(body, status_code=400)
            except Exception:
                traceback.print_exc(file=sys.stderr)
                message = "The panel hit an internal error."
                body = {"error": {"code": "internal", "message": message}}
                return JSONResponse(body, status_code=500)

        return run

    return decorate


async def _body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except ValueError as exc:  # not JSON, or longer than it declared
        raise _Refused(400, "bad-request", "The request is not valid JSON.") from exc
    if not isinstance(data, dict):
        raise _Refused(400, "bad-request", "The request must be a JSON object.")
    return data


def _query_int(request: Request, name: str) -> int | None:
    value = request.query_params.get(name)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise _Refused(400, "bad-request", f"{name} must be a number.") from exc


def _query_float(request: Request, name: str) -> float | None:
    value = request.query_params.get(name)
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise _Refused(400, "bad-request", f"{name} must be a number.") from exc


def _document() -> Document:
    return read_document()


def _config_payload(document: Document) -> dict[str, Any]:
    out = describe(document)
    config = document.checked.config
    out["posture"] = (
        posture(config, file_mode=document.mode, secrets=document.secrets) if config else []
    )
    out["backups"] = len(list_backups(document.located))
    return out


def _pause_follows_renames(ops: Any) -> None:
    """A pause names connections. When a paused one is renamed, the new name is paused
    too (and the old one stays: running sessions still know the connection by it)."""
    pause = read_pause()
    if pause is None or not pause.connections:
        return
    names = list(pause.connections)
    for op in ops if isinstance(ops, list) else ():
        if (
            isinstance(op, dict)
            and op.get("op") == "rename"
            and isinstance(op.get("path"), list)
            and op["path"][:1] == ["connections"]
            and len(op["path"]) == 2
            and op["path"][1] in names
            and isinstance(op.get("to"), str)
            and op["to"] not in names
        ):
            names.append(op["to"])
    if len(names) != len(pause.connections):
        with contextlib.suppress(OSError):
            write_pause(pause.reason, tuple(names))


def _changed_paths(ops: Any) -> str:
    """Which keys a save touched, for the log: names only, never values."""
    paths = []
    for op in ops if isinstance(ops, list) else ():
        if isinstance(op, dict) and isinstance(op.get("path"), list):
            label = ".".join(str(part) for part in op["path"])
            paths.append(f"{label} -> {op.get('to')}" if op.get("op") == "rename" else label)
    return ", ".join(paths[:40])


# -------------------------------------------------------------------------------- routes


@_route()
async def meta(panel: Panel, request: Request) -> dict[str, Any]:
    document = _document()
    audit = document.checked.config.audit if document.checked.config else AuditConfig()
    return {
        "version": __version__,
        "read_only": panel.read_only,
        "idle_minutes": round(panel.idle_seconds / 60) if panel.idle_seconds else 0,
        "started": panel.started,
        "now": time.time(),
        "python": platform.python_version(),
        "platform": platform.system(),
        "paths": {
            "config": str(document.located.path),
            "state": str(state_dir()),
            "audit": str(audit.resolved_path()),
            "sessions": str(sessions_dir()),
        },
        "heartbeat_seconds": HEARTBEAT_SECONDS,
        "stale_after_seconds": STALE_AFTER_SECONDS,
        "schema": schema(),
    }  # fmt: skip


@_route()
async def get_config(panel: Panel, request: Request) -> dict[str, Any]:
    return _config_payload(_document())


@_route()
async def config_text(panel: Panel, request: Request) -> dict[str, Any]:
    document = _document()
    return {"text": mask_text(document.text), "digest": document.digest}


def _prepare(document: Document, data: dict[str, Any]) -> tuple[str, Any, list[dict[str, Any]]]:
    """The text a change would produce, the validator's verdict and what it costs."""
    if not document.located.exists:
        raise _Refused(409, "no-file", "There is no configuration file yet. Create it first.")
    if data.get("base") != document.digest:
        raise DocumentError(
            "conflict",
            "the configuration file changed on disk since it was read; reload and try again",
        )
    text = apply_ops(document.text, data.get("ops") or [])
    checked = check_text(text, document.located.path.resolve().parent)
    risks = compare(document.checked.config, checked.config) if checked.config else []
    return text, checked, risks


@_route(changes=True)
async def config_preview(panel: Panel, request: Request) -> dict[str, Any]:
    document = _document()
    text, checked, risks = _prepare(document, await _body(request))
    return {
        "valid": checked.valid,
        "errors": list(checked.errors),
        "changed": text != document.text,
        "diff": unified_diff(document.text, text),
        "risks": risks,
        "needs_acknowledgement": needs_acknowledgement(risks),
    }


@_route(changes=True)
async def config_apply(panel: Panel, request: Request) -> dict[str, Any]:
    data = await _body(request)
    async with panel.saving:
        document = _document()
        text, checked, risks = _prepare(document, data)
        if not checked.valid:
            raise _Refused(
                422, "invalid", "The configuration would not be valid.", errors=list(checked.errors)
            )
        if needs_acknowledgement(risks) and data.get("acknowledged") is not True:
            raise _Refused(
                409,
                "acknowledge",
                "This change reduces the protection and was not acknowledged.",
                risks=risks,
            )
        saved = save_text(document.located, text, document.digest)
        _pause_follows_renames(data.get("ops"))
        detail = f"saved {_changed_paths(data.get('ops'))}"
        if risks:
            detail += "; reduces protection: " + ", ".join(sorted({r["code"] for r in risks}))
        if saved["backup"]:
            detail += f"; backup {saved['backup']}"
        # Written with the settings in force before the change, so that turning the
        # log off is itself the last thing logged.
        panel.record(document.checked.config or checked.config, "config", "ok", detail=detail)
        return {"saved": saved, **_config_payload(_document())}


@_route(changes=True)
async def config_create(panel: Panel, request: Request) -> dict[str, Any]:
    from ..cli import EXAMPLE_CONFIG  # the same starter file as `pgharbor init`

    async with panel.saving:
        document = _document()
        create_starter(document.located, EXAMPLE_CONFIG)
        fresh = _document()
        panel.record(fresh.checked.config, "config", "ok", detail="created the starter file")
        return _config_payload(fresh)


@_route()
async def backups(panel: Panel, request: Request) -> dict[str, Any]:
    return {"backups": list_backups(_document().located)}


@_route()
async def backup_show(panel: Panel, request: Request) -> dict[str, Any]:
    document = _document()
    text = backup_text(document.located, request.path_params["name"])
    return {
        "name": request.path_params["name"],
        "text": mask_text(text),
        "diff": unified_diff(document.text, text),  # what restoring it would change
    }


def _prepare_restore(document: Document, data: dict[str, Any], name: str) -> tuple[str, Any, Any]:
    """The text of a backup, the validator's verdict on it and what restoring it costs."""
    if data.get("base") != document.digest:
        raise DocumentError(
            "conflict",
            "the configuration file changed on disk since it was read; reload and try again",
        )
    text = backup_text(document.located, name)
    checked = check_text(text, document.located.path.resolve().parent)
    risks = compare(document.checked.config, checked.config) if checked.config else []
    return text, checked, risks


@_route(changes=True)
async def backup_preview(panel: Panel, request: Request) -> dict[str, Any]:
    document = _document()
    text, checked, risks = _prepare_restore(
        document, await _body(request), request.path_params["name"]
    )
    return {
        "valid": checked.valid,
        "errors": list(checked.errors),
        "changed": text != document.text,
        "diff": unified_diff(document.text, text),
        "risks": risks,
        "needs_acknowledgement": needs_acknowledgement(risks),
    }


@_route(changes=True)
async def backup_restore(panel: Panel, request: Request) -> dict[str, Any]:
    data = await _body(request)
    name = request.path_params["name"]
    async with panel.saving:
        document = _document()
        text, checked, risks = _prepare_restore(document, data, name)
        if not checked.config:
            raise _Refused(
                422,
                "invalid",
                "This backup is not a valid configuration.",
                errors=list(checked.errors),
            )
        if needs_acknowledgement(risks) and data.get("acknowledged") is not True:
            raise _Refused(
                409, "acknowledge", "Restoring this backup reduces the protection.", risks=risks
            )
        saved = save_text(document.located, text, document.digest)
        panel.record(
            document.checked.config or checked.config,
            "config",
            "ok",
            detail=f"restored backup {name}; backup {saved['backup']}",
        )
        return {"saved": saved, **_config_payload(_document())}


@_route()
async def tools(panel: Panel, request: Request) -> dict[str, Any]:
    config = _document().checked.config or Config()
    load_tool_modules()
    can_write = any(not c.read_only for c in config.connections.values())
    can_admin = any(c.access is Access.ADMIN for c in config.connections.values())
    disabled, disabled_groups = set(config.tools.disabled), set(config.tools.disabled_groups)

    def state(name: str, group: str, minimum: Access) -> tuple[str, str | None]:
        if name in disabled:
            return "off", "disabled"
        if group in disabled_groups:
            return "off", "group-disabled"
        if minimum is Access.READ_WRITE and not can_write:
            return "hidden", "no-writable-connection"
        if minimum is Access.ADMIN and not can_admin:
            return "hidden", "no-admin-connection"
        return "on", None

    out = []
    for spec in REGISTRY.values():
        if spec.group == "custom":
            continue  # listed from the configuration below, as it is now
        status, reason = state(spec.name, spec.group, spec.minimum_access)
        out.append(
            {
                "name": spec.name,
                "group": spec.group,
                "title": spec.title,
                "description": spec.description,
                "writes": spec.writes,
                "destructive": spec.destructive,
                "minimum_access": spec.minimum_access.value,
                "state": status,
                "reason": reason,
            }
        )
    for custom in config.custom_tools.values():
        name = f"pgx_{custom.name}"
        status, reason = state(name, "custom", Access.READ_ONLY)
        out.append(
            {
                "name": name,
                "group": "custom",
                "title": custom.name.replace("_", " ").capitalize(),
                "description": custom.description,
                "writes": False,
                "destructive": False,
                "minimum_access": Access.READ_ONLY.value,
                "state": status,
                "reason": reason,
                "custom": custom.name,
            }
        )
    return {"tools": out}


@_route()
async def audit_page(panel: Panel, request: Request) -> dict[str, Any]:
    document = _document()
    config = document.checked.config
    reader = panel.reader(config)
    query = request.query_params
    page = reader.page(
        limit=_query_int(request, "limit") or 100,
        before=_query_int(request, "before"),
        after=_query_int(request, "after"),
        connection=query.get("connection") or None,
        outcome=query.get("outcome") or None,
        tool=query.get("tool") or None,
        rule=query.get("rule") or None,
        session=query.get("session") or None,
        text=query.get("q") or None,
        since=_query_float(request, "since"),
        until=_query_float(request, "until"),
    )
    page["facets"] = reader.facets()
    page["file"] = {"path": str(reader.path), "size": reader.size}
    page["enabled"] = config.audit.enabled if config else True
    return page


@_route()
async def audit_stats(panel: Panel, request: Request) -> dict[str, Any]:
    name = request.query_params.get("range") or "24h"
    if name not in RANGES:
        raise _Refused(400, "bad-request", "Unknown range.")
    offset = max(-900, min(_query_int(request, "tz") or 0, 900))
    return panel.reader(_document().checked.config).stats(name, tz_minutes=offset)


@_route()
async def sessions(panel: Panel, request: Request) -> dict[str, Any]:
    document = _document()
    config = document.checked.config
    activity = panel.reader(config).by_session()
    listed = read_sessions()
    for session in listed:
        session["activity"] = activity.get(session["id"])
        known = (session.get("config") or {}).get("digest")
        session["outdated"] = bool(
            session["state"] == "active" and document.digest and known != document.digest
        )
    return {
        "sessions": listed,
        "now": time.time(),
        "recording": config.audit.enabled if config else True,
        "config_digest": document.digest,
    }


@_route()
async def connection_test(panel: Panel, request: Request) -> dict[str, Any]:
    name = request.path_params["name"]
    config = _document().checked.config
    if config is None:
        raise _Refused(409, "invalid", "The configuration is not valid: fix it first.")
    if name not in config.connections:
        raise _Refused(404, "not-found", "No such connection.")
    profile = config.connections[name]
    pool = Sessions(config)
    watch = Stopwatch()
    try:
        async with pool.transaction(name) as db:
            facts = db.facts
            result: dict[str, Any] = {
                "ok": True,
                "postgresql": facts.label,
                "database": facts.database,
                "user": facts.user,
                "superuser": facts.is_superuser,
                "in_recovery": facts.in_recovery,
            }
    except Exception as exc:
        result = {"ok": False, "error": scrub(describe_db_error(exc, profile))}
    finally:
        await pool.close()
    result["ms"] = round(watch.ms, 1)
    panel.record(
        config, "test", "ok" if result["ok"] else "error", connection=name,
        elapsed_ms=watch.ms, detail=result.get("error"),
    )  # fmt: skip
    return result


def _pause_payload() -> dict[str, Any]:
    pause = read_pause()
    if pause is None:
        return {"paused": False}
    return {
        "paused": True,
        "since": pause.since,
        "reason": pause.reason,
        "connections": list(pause.connections),
    }


@_route()
async def get_pause(panel: Panel, request: Request) -> dict[str, Any]:
    return _pause_payload()


@_route(changes=True)
async def set_pause(panel: Panel, request: Request) -> dict[str, Any]:
    data = await _body(request)
    config = _document().checked.config
    if data.get("paused") is True:
        names = data.get("connections") or []
        if not isinstance(names, list) or not all(
            isinstance(n, str) and _NAME_RE.match(n) for n in names
        ):
            raise _Refused(400, "bad-request", "Invalid list of connections.")
        given = data.get("reason")
        reason = given if isinstance(given, str) else ""
        try:
            pause = write_pause(reason, tuple(names))
        except OSError as exc:
            raise _Refused(500, "io", f"The pause could not be written: {exc.strerror}") from exc
        scope = ", ".join(pause.connections) or "every connection"
        panel.record(config, "pause", "ok", detail=f"paused {scope}: {pause.reason}".rstrip(": "))
    elif data.get("paused") is False:
        try:
            clear_pause()
        except OSError as exc:
            raise _Refused(500, "io", f"The pause could not be lifted: {exc.strerror}") from exc
        panel.record(config, "pause", "ok", detail="pause lifted")
    else:
        raise _Refused(400, "bad-request", "Say whether to pause.")
    return _pause_payload()


@_route()
async def pulse(panel: Panel, request: Request) -> dict[str, Any]:
    """The few numbers the page asks for every few seconds, to know what to reload."""
    document = _document()
    reader = panel.reader(document.checked.config)
    idle_left = None
    if panel.idle_seconds:
        idle_left = max(0.0, panel.idle_seconds - (time.monotonic() - panel.gate.last_use))
    return {
        "now": time.time(),
        "config_digest": document.digest,
        "config_valid": document.checked.valid,
        "audit_last": reader.last,
        "active_sessions": sum(1 for s in read_sessions() if s["state"] == "active"),
        "pause": _pause_payload(),
        "idle_seconds_left": idle_left,
    }


@_route()
async def shutdown(panel: Panel, request: Request) -> dict[str, Any]:
    asyncio.get_running_loop().call_later(0.3, panel.stop)
    return {"stopping": True}


@_route()
async def not_found(panel: Panel, request: Request) -> dict[str, Any]:
    raise _Refused(404, "not-found", "No such route.")


def build_app(panel: Panel) -> GateMiddleware:
    """The whole application: the gate around the API and the page's files."""
    routes = [
        Route("/api/meta", meta),
        Route("/api/config", get_config),
        Route("/api/config/text", config_text),
        Route("/api/config/preview", config_preview, methods=["POST"]),
        Route("/api/config/apply", config_apply, methods=["POST"]),
        Route("/api/config/create", config_create, methods=["POST"]),
        Route("/api/backups", backups),
        Route("/api/backups/{name}", backup_show),
        Route("/api/backups/{name}/preview", backup_preview, methods=["POST"]),
        Route("/api/backups/{name}/restore", backup_restore, methods=["POST"]),
        Route("/api/tools", tools),
        Route("/api/audit", audit_page),
        Route("/api/audit/stats", audit_stats),
        Route("/api/sessions", sessions),
        Route("/api/connections/{name}/test", connection_test, methods=["POST"]),
        Route("/api/pause", get_pause),
        Route("/api/pause", set_pause, methods=["POST"]),
        Route("/api/pulse", pulse),
        Route("/api/shutdown", shutdown, methods=["POST"]),
        Route("/api/{rest:path}", not_found, methods=["GET", "POST"]),
        Mount("/", app=StaticFiles(directory=STATIC, html=True)),
    ]
    application = Starlette(routes=routes)
    application.state.panel = panel
    return GateMiddleware(application, panel.gate)


# ------------------------------------------------------------------------------- running


def _listen(port: int) -> socket.socket:
    """A socket on the loopback address only: the panel is never reachable from the
    network, whatever the firewall says."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform != "win32":
        # Lets the panel restart at once on the same port. Not on Windows, where the
        # same option would let another program share the port.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
        # Listening from now on: the address is printed before the server starts
        # answering, and whoever opens it at once must wait, not be refused.
        sock.listen(64)
    except OSError as exc:
        sock.close()
        raise PgHarborError(
            f"cannot listen on 127.0.0.1:{port} ({exc.strerror}). Is the panel already "
            "running? Use --port to choose another port (0 picks any free one)."
        ) from exc
    return sock


def _handoff_page(gate: Gate) -> Path:
    """A private page that takes the browser to the panel.

    Handing the address itself to the browser would put the key on a command line, and
    command lines can be read by every user of the machine. The key is written instead
    to a file only the owner can read, and the browser is given the path of that file.
    """
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    page = directory / f"open-panel-{secrets.token_hex(8)}.html"
    html = (
        '<!doctype html><meta charset="utf-8"><title>pgharbor</title>'
        f'<meta http-equiv="refresh" content="0;url={gate.url}">'
        f'<p><a href="{gate.url}">pgharbor</a></p>\n'
    )
    fd = os.open(page, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, html.encode("utf-8"))
    finally:
        os.close(fd)
    return page


async def _open_browser(gate: Gate) -> None:
    """Open the panel in the browser, then remove the page that led there."""
    try:
        page = _handoff_page(gate)
    except OSError as exc:
        print(f"pgharbor: could not prepare the browser ({exc.strerror}).", file=sys.stderr)
        return
    try:
        await asyncio.to_thread(webbrowser.open, page.as_uri())
        await asyncio.sleep(20)  # long enough for a browser that is only starting
    finally:
        with contextlib.suppress(OSError):
            page.unlink()


async def _stop_when_idle(panel: Panel) -> None:
    while True:
        await asyncio.sleep(5)
        if time.monotonic() - panel.gate.last_use > panel.idle_seconds:
            print("pgharbor: the panel stopped after a period without use.", file=sys.stderr)
            panel.stop()
            return


async def serve(
    *, port: int = DEFAULT_PORT, open_browser: bool = True, read_only: bool = False,
    idle_minutes: float = 30,
) -> int:  # fmt: skip
    """Run the panel until it is stopped (Ctrl+C, the page's button, or idleness)."""
    sock = _listen(port)
    gate = Gate(sock.getsockname()[1])
    panel = Panel(gate, read_only=read_only, idle_seconds=max(0.0, idle_minutes) * 60)
    server = uvicorn.Server(
        uvicorn.Config(
            build_app(panel),
            log_level="warning",
            access_log=False,
            server_header=False,
            date_header=False,
            lifespan="off",
            ws="none",
            timeout_keep_alive=5,
            timeout_graceful_shutdown=3,
        )
    )
    panel.stop = lambda: setattr(server, "should_exit", True)

    mode = "read-only (nothing can be changed)" if read_only else "editing enabled"
    stops = f"after {idle_minutes:g} min without use, or with Ctrl+C" if panel.idle_seconds else (
        "with Ctrl+C"
    )  # fmt: skip
    print(
        "\npgharbor panel is running.\n\n"
        f"  Open:   {gate.url}\n\n"
        "  This address is the key to the panel: do not share it. If the browser does\n"
        "  not show the panel by itself, open the address above.\n"
        f"  Mode:   {mode}\n"
        f"  Stops:  {stops}\n",
        flush=True,
    )
    tasks: list[asyncio.Task[Any]] = []
    if open_browser:
        tasks.append(asyncio.create_task(_open_browser(gate)))
    if panel.idle_seconds:
        tasks.append(asyncio.create_task(_stop_when_idle(panel)))
    try:
        await server.serve(sockets=[sock])
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    return 0
