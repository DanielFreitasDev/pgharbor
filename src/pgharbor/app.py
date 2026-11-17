"""Application state and the tool registry.

Tools are plain async functions decorated with :func:`tool`. The decorator records them
in :data:`REGISTRY` and wraps them so that every call, whatever the tool, gets the same
treatment: human-readable errors, an audit entry, scrubbed and size-bounded output.

A tool reaches the database only through :meth:`App.read` or :meth:`App.write`, which
enforce the connection profile (approval, access mode) before opening a transaction.
"""

import contextlib
import functools
import sys
import time
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import psycopg
from mcp.server.mcpserver.exceptions import ToolError

from .audit import Audit, Stopwatch
from .config import Access, Approval, Config, ConnectionConfig, load_config
from .errors import ApprovalRequired, PgHarborError, Rejected
from .render import scrub, scrub_deep, to_json
from .session import Db, Sessions, describe_db_error

__all__ = ["REGISTRY", "App", "ToolSpec", "get_app", "set_app", "tool"]

# Hard ceiling for one tool answer. Claude Code moves text beyond ~50,000 characters to a
# file, so staying under it keeps answers in the conversation.
MAX_OUTPUT_CHARS = 48_000


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    group: str
    title: str
    function: Callable[..., Awaitable[str]]
    writes: bool
    destructive: bool
    minimum_access: Access
    always_load: bool

    @property
    def description(self) -> str:
        return (self.function.__doc__ or "").strip()


REGISTRY: dict[str, ToolSpec] = {}


class App:
    """Everything a tool needs: configuration, connections, approvals and the audit log."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.sessions = Sessions(config)
        self.audit = Audit(config.audit)
        self._approved: dict[str, float] = {}

    # -------------------------------------------------------------------- profiles

    def profile(self, connection: str | None) -> ConnectionConfig:
        return self.config.connection(connection)

    # ------------------------------------------------------------------- approvals

    def is_approved(self, profile: ConnectionConfig) -> bool:
        if profile.effective_approval is Approval.NEVER:
            return True
        granted = self._approved.get(profile.name)
        if granted is None:
            return False
        if profile.approval_ttl and (time.monotonic() - granted) * 1000 > profile.approval_ttl:
            del self._approved[profile.name]
            return False
        return True

    def approve(self, profile: ConnectionConfig) -> None:
        self._approved[profile.name] = time.monotonic()

    def revoke(self, profile: ConnectionConfig) -> None:
        self._approved.pop(profile.name, None)

    def require_approval(self, profile: ConnectionConfig) -> None:
        if self.is_approved(profile):
            return
        raise ApprovalRequired(
            f"Connection '{profile.name}' ({profile.environment}: {profile.target()}) needs "
            "the user's approval before it is used in this session. Call "
            f'pg_request_access(connection="{profile.name}", reason="...") first. Do that '
            "only if the user explicitly asked for this environment; never use it as a "
            "fallback for another connection."
        )

    # ---------------------------------------------------------------- transactions

    @contextlib.asynccontextmanager
    async def read(
        self, connection: str | None, *, timeout_ms: int | None = None
    ) -> AsyncIterator[Db]:
        """A verified read-only transaction on ``connection``."""
        profile = self.profile(connection)
        self.require_approval(profile)
        async with self.sessions.transaction(profile.name, timeout_ms=timeout_ms) as db:
            yield db

    @contextlib.asynccontextmanager
    async def write(
        self, connection: str | None, *, minimum: Access = Access.READ_WRITE
    ) -> AsyncIterator[Db]:
        """A read-write transaction. Commits on success, rolls back on any exception."""
        profile = self.profile(connection)
        self.require_access(profile, minimum)
        self.require_approval(profile)
        async with self.sessions.transaction(profile.name, write=True) as db:
            yield db

    def require_access(self, profile: ConnectionConfig, minimum: Access) -> None:
        if profile.access.rank >= minimum.rank:
            return
        raise Rejected(
            f"connection '{profile.name}' has access \"{profile.access.value}\"; this needs "
            f'"{minimum.value}"',
            rule="access-mode",
            hint="The access mode is set in the pgharbor configuration file and cannot be "
            "changed from a conversation.",
        )

    async def close(self) -> None:
        await self.sessions.close()


_app: App | None = None


def get_app() -> App:
    """The running application. Created from the configuration on first use."""
    global _app
    if _app is None:
        _app = App(load_config())
    return _app


def set_app(app: App | None) -> None:
    global _app
    _app = app


# ----------------------------------------------------------------------------------- registry


def tool(
    *,
    group: str,
    title: str,
    writes: bool = False,
    destructive: bool = False,
    minimum_access: Access = Access.READ_ONLY,
    always_load: bool = False,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[str]]]:
    """Register an async function as an MCP tool.

    The function may return a string (sent as is) or any JSON-serializable value. Its
    docstring becomes the tool description: put the words an agent would search for in
    the first sentence.
    """

    def decorate(function: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[str]]:
        name = function.__name__

        @functools.wraps(function)
        async def run(*args: Any, **kwargs: Any) -> str:
            app = get_app()
            connection = kwargs.get("connection")
            if connection is None:
                # The log names the connection that was actually used, default included.
                resolved = _profile_or_none(app, None)
                connection = resolved.name if resolved else None
            watch = Stopwatch()
            sql = kwargs.get("sql") if isinstance(kwargs.get("sql"), str) else None
            if sql is None:
                sql = getattr(function, "__pgharbor_sql__", None)
            params = _audit_params(function, kwargs)
            try:
                result = await function(*args, **kwargs)
            except Rejected as exc:
                app.audit.record(
                    tool=name,
                    connection=connection,
                    outcome="refused",
                    rule=exc.rule,
                    sql=sql,
                    params=params,
                    elapsed_ms=watch.ms,
                    detail=exc.reason,
                )
                raise ToolError(str(exc)) from None
            except ApprovalRequired as exc:
                app.audit.record(
                    tool=name,
                    connection=connection,
                    outcome="needs-approval",
                    sql=sql,
                    params=params,
                    elapsed_ms=watch.ms,
                )
                raise ToolError(str(exc)) from None
            except PgHarborError as exc:
                app.audit.record(
                    tool=name,
                    connection=connection,
                    outcome="error",
                    sql=sql,
                    params=params,
                    elapsed_ms=watch.ms,
                    detail=str(exc),
                )
                raise ToolError(scrub(str(exc))) from None
            except psycopg.Error as exc:
                message = scrub(describe_db_error(exc, _profile_or_none(app, connection)))
                app.audit.record(
                    tool=name,
                    connection=connection,
                    outcome="error",
                    sql=sql,
                    params=params,
                    elapsed_ms=watch.ms,
                    detail=message,
                )
                raise ToolError(message) from None
            except ToolError:
                raise
            except Exception as exc:
                traceback.print_exc(file=sys.stderr)
                app.audit.record(
                    tool=name,
                    connection=connection,
                    outcome="bug",
                    sql=sql,
                    params=params,
                    elapsed_ms=watch.ms,
                    detail=repr(exc),
                )
                raise ToolError(
                    f"pgharbor hit an internal error in {name} ({type(exc).__name__}: {exc}). "
                    "Nothing was changed. Please report it."
                ) from None
            text = result if isinstance(result, str) else to_json(scrub_deep(result))
            if len(text) > MAX_OUTPUT_CHARS:
                text = (
                    text[:MAX_OUTPUT_CHARS]
                    + f"\n… [output cut: {len(text):,} characters in total. Ask for fewer "
                    "rows, a narrower filter or a smaller limit.]"
                )
            app.audit.record(
                tool=name,
                connection=connection,
                outcome="ok",
                sql=sql,
                params=params,
                elapsed_ms=watch.ms,
            )
            return text

        run.__annotations__ = {**function.__annotations__, "return": str}
        REGISTRY[name] = ToolSpec(
            name=name,
            group=group,
            title=title,
            function=run,
            writes=writes,
            destructive=destructive,
            minimum_access=minimum_access,
            always_load=always_load,
        )
        return run

    return decorate


def _audit_params(function: Callable[..., Any], kwargs: dict[str, Any]) -> Any:
    """What the audit log calls the parameters of a call (written only with ``log_params``):
    the bound values of a statement or, for a custom tool, its arguments."""
    if getattr(function, "__pgharbor_sql__", None) is not None:
        return {k: v for k, v in kwargs.items() if k != "connection"} or None
    params = kwargs.get("params")
    return params if isinstance(params, (list, tuple)) else None


def _profile_or_none(app: App, connection: str | None) -> ConnectionConfig | None:
    try:
        return app.profile(connection)
    except PgHarborError:
        return None
