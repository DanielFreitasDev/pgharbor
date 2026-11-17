"""Error types. Every message here is written for the agent and the user to read."""

from __future__ import annotations

__all__ = [
    "ApprovalRequired",
    "ConfigError",
    "NotAvailable",
    "PgHarborError",
    "Rejected",
]


class PgHarborError(Exception):
    """Base class. ``str(error)`` is safe to show: it never contains credentials."""


class ConfigError(PgHarborError):
    """The configuration file or environment is invalid."""


class Rejected(PgHarborError):
    """A statement or request was refused by a safety rule before it could run.

    ``rule`` is a short stable identifier (shown in the audit log and in the docs);
    ``hint`` tells the caller what to do instead.
    """

    def __init__(self, reason: str, *, rule: str, hint: str | None = None) -> None:
        self.reason = reason
        self.rule = rule
        self.hint = hint
        message = f"Refused ({rule}): {reason}"
        if hint:
            if not reason.endswith((".", "!", "?")):
                message += "."
            message += f" Hint: {hint}"
        super().__init__(message)


class ApprovalRequired(PgHarborError):
    """The connection needs a human approval that has not been given yet."""


class NotAvailable(PgHarborError):
    """The server cannot do this (version too old, extension missing, no privilege)."""
