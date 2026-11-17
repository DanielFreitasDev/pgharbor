"""Asking the human: approval to use a connection and confirmation of writes.

Both use MCP *elicitation*: the server asks the client to show a dialog, and the answer
comes from the person at the keyboard, not from the model. That is the only kind of
confirmation that still means something if the model has been talked into misbehaving.

The dialog is requested through the SDK's resolver mechanism (``Resolve`` + ``Elicit``),
which works on every protocol revision: older clients receive a request in the middle of
the call, newer ones get an "input required" result and call again with the answer.

When the client cannot show dialogs (headless runs), the profile decides what happens:
refuse, or accept an explicit statement by the agent that the user asked for it
(``approval_fallback`` / ``write_confirmation = "conversation"``). That second mode is a
convention, not a control, and the documentation says so.
"""

from typing import Any

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.resolve import Elicit
from pydantic import BaseModel, Field

from .config import ConnectionConfig, WriteConfirmation
from .errors import PgHarborError
from .render import clip, scrub

__all__ = [
    "Decision",
    "NoDialog",
    "NotNeeded",
    "ask_access",
    "ask_write",
    "client_has_dialog",
]


class Decision(BaseModel):
    approve: bool = Field(
        default=False,
        title="Approve",
        description="Tick to allow exactly what is described above.",
    )


class NoDialog:
    """The client cannot show a dialog: the tool falls back to the profile's rule."""


class NotNeeded:
    """Nothing to ask (dry run, or the profile asks for no confirmation)."""


def client_has_dialog(ctx: Context[Any, Any] | None) -> bool:
    if ctx is None:
        return False
    try:
        capabilities = ctx.client_capabilities
    except Exception:
        return False
    elicitation = getattr(capabilities, "elicitation", None) if capabilities else None
    if elicitation is None:
        return False
    # A bare capability means form support; a url-only client cannot show a form.
    return (
        getattr(elicitation, "form", None) is not None or getattr(elicitation, "url", None) is None
    )


def _profile(connection: str | None) -> ConnectionConfig | None:
    from .app import get_app  # imported late: app imports this module's users

    try:
        return get_app().profile(connection)
    except PgHarborError:
        return None


def ask_access(
    connection: str, reason: str, ctx: Context[Any, Any]
) -> Elicit[Decision] | NoDialog | NotNeeded:
    """Resolver for ``pg_request_access``."""
    from .app import get_app

    profile = _profile(connection)
    if profile is None or get_app().is_approved(profile):
        return NotNeeded()
    if not client_has_dialog(ctx):
        return NoDialog()
    why, _ = clip(scrub(reason or "(no reason given)"), 600)
    message = (
        f"Claude is asking to use the database connection '{profile.name}'.\n\n"
        f"Environment: {profile.environment}\n"
        f"Server: {profile.target()}\n"
        f"Access mode: {profile.access.value}\n\n"
        f"Reason given: {why}\n\n"
        "Approving allows this connection for the rest of this session"
        + (f" (or {profile.approval_ttl // 60000} min)." if profile.approval_ttl else ".")
    )
    return Elicit(message, Decision)


def ask_write(
    connection: str, sql: str, dry_run: bool, ctx: Context[Any, Any]
) -> Elicit[Decision] | NoDialog | NotNeeded:
    """Resolver for ``pg_execute``: confirm one statement that changes the database."""
    profile = _profile(connection)
    if profile is None or dry_run or profile.write_confirmation is WriteConfirmation.NONE:
        return NotNeeded()
    if profile.write_confirmation is WriteConfirmation.CONVERSATION or not client_has_dialog(ctx):
        return NoDialog()
    statement, _ = clip(scrub(sql.strip()), 3000)
    return Elicit(
        f"Claude wants to CHANGE the database on connection '{profile.name}' "
        f"({profile.environment}: {profile.target()}).\n\n{statement}\n\n"
        "This will be committed if you approve.",
        Decision,
    )


def ask_action(
    connection: str, action: str, target: str, ctx: Context[Any, Any]
) -> Elicit[Decision] | NoDialog | NotNeeded:
    """Resolver for structured administrative tools (maintenance, signalling a backend)."""
    profile = _profile(connection)
    if profile is None or profile.write_confirmation is WriteConfirmation.NONE:
        return NotNeeded()
    if profile.write_confirmation is WriteConfirmation.CONVERSATION or not client_has_dialog(ctx):
        return NoDialog()
    return Elicit(
        f"Claude wants to run an administrative action on connection '{profile.name}' "
        f"({profile.environment}: {profile.target()}).\n\nAction: {action}\nTarget: {target}",
        Decision,
    )


def settle(outcome: Any, *, fallback_allowed: bool, stated_by_agent: bool, what: str) -> str:
    """Turn a resolver outcome into "how it was approved", or raise ``Rejected``.

    ``fallback_allowed``: the profile accepts the agent's statement when no dialog exists.
    ``stated_by_agent``: the agent passed the flag saying the user asked for this.
    """
    from .errors import Rejected

    if getattr(outcome, "action", None) != "accept":
        raise Rejected(
            f"the user did not approve {what}",
            rule="not-approved",
            hint="Do not retry unless the user asks again.",
        )
    data = getattr(outcome, "data", None)
    if isinstance(data, NotNeeded):
        return "not required"
    if isinstance(data, Decision):
        if data.approve:
            return "dialog"
        raise Rejected(f"the approval box was not ticked for {what}", rule="not-approved")
    # NoDialog: this client cannot ask the user directly.
    if not fallback_allowed:
        raise Rejected(
            f"{what} needs a confirmation dialog and this client cannot show one",
            rule="no-dialog",
            hint="Use an interactive client, or change the confirmation mode in the "
            "pgharbor configuration file.",
        )
    if not stated_by_agent:
        raise Rejected(
            f"{what} needs the user's explicit go-ahead",
            rule="needs-user-request",
            hint="Tell the user exactly what will happen, wait for a clear yes, and call "
            "again with the confirmation flag set to true.",
        )
    return "stated by the agent"
