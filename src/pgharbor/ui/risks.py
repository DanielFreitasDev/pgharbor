"""What a change to the configuration does to the protection, and where it stands today.

Two questions, both answered from validated configurations and never from text:

- :func:`compare` looks at the configuration before and after a change and lists every
  point where the new one protects less. The panel shows that list before saving, and
  the server refuses a save that carries such a point without an explicit acknowledgement.
- :func:`posture` looks at one configuration and lists what a careful owner would want to
  know about it: the hardening checklist of ``docs/security.md``, applied.

Findings are codes with data. The page translates them; nothing here is prose.
"""

from __future__ import annotations

import fnmatch
from typing import Any

from ..config import (
    Access,
    Approval,
    Config,
    ConnectionConfig,
    FunctionPolicy,
    ProtectedTable,
    WriteConfirmation,
)

__all__ = ["compare", "needs_acknowledgement", "posture"]

Finding = dict[str, Any]

_POLICY_RANK = {FunctionPolicy.STRICT: 0, FunctionPolicy.STANDARD: 1, FunctionPolicy.PERMISSIVE: 2}
_CONFIRM_RANK = {
    WriteConfirmation.DIALOG: 0,
    WriteConfirmation.CONVERSATION: 1,
    WriteConfirmation.NONE: 2,
}
_SSL_RANK = {"disable": 0, "allow": 1, "prefer": 2, "require": 3, "verify-ca": 4, "verify-full": 5}
# Limits where a higher number lets more through. For the last two time limits zero
# means "no limit at all", as it does in PostgreSQL.
_TIMEOUTS = ("statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout")
_SIZES = ("max_rows", "max_rows_limit", "max_cell_chars", "max_result_chars", "max_sql_chars")
# What decides which server, which database and which identity a connection reaches.
_TARGET_KEYS = ("host", "port", "dbname", "user", "user_env", "dsn", "dsn_env")
_TARGET_PARAMS = ("hostaddr", "service")
# libpq parameters that only tune the transport. Any other one (certificates, required
# authentication, the kind of server accepted...) changes what the connection trusts.
_TUNING_PARAMS = frozenset(
    {
        "connect_timeout",
        "keepalives",
        "keepalives_idle",
        "keepalives_interval",
        "keepalives_count",
        "tcp_user_timeout",
        "client_encoding",
    }
)


def _finding(code: str, level: str, connection: str | None = None, **data: Any) -> Finding:
    out: Finding = {"code": code, "level": level}
    if connection is not None:
        out["connection"] = connection
    if data:
        out["data"] = data
    return out


def needs_acknowledgement(findings: list[Finding]) -> bool:
    return any(f["level"] in ("danger", "warn") for f in findings)


# ------------------------------------------------------------------------------- compare


def compare(before: Config | None, after: Config) -> list[Finding]:
    """Every way ``after`` protects less than ``before``, most serious first."""
    if before is None:
        # Nothing valid to compare with (no file yet, or a file that did not load):
        # the whole configuration is new, and the owner is told to read it as such.
        fresh = [_finding("no-baseline", "warn")]
        for name, profile in after.connections.items():
            fresh += _new_connection(name, profile)
        return _sorted(fresh)

    found: list[Finding] = []
    gone = {name: old for name, old in before.connections.items() if name not in after.connections}
    renamed: dict[str, str] = {}  # old name -> new name
    for name, profile in after.connections.items():
        old = before.connections.get(name)
        if old is not None:
            found += _connection(name, old, profile)
            continue
        # A connection under a new name that still reaches the same place is the old one
        # renamed: it is compared with what it was, so nothing is lost on the way.
        twin = next((n for n, o in gone.items() if _target(o) == _target(profile)), None)
        if twin is None:
            found += _new_connection(name, profile)
        else:
            found.append(_finding("connection-renamed", "note", name, before=twin))
            found += _connection(name, gone.pop(twin), profile)
            renamed[twin] = name

    if before.audit.enabled and not after.audit.enabled:
        found.append(_finding("audit-disabled", "danger"))
    if before.audit.log_sql and not after.audit.log_sql:
        found.append(_finding("audit-sql-off", "warn"))
    if not before.audit.log_params and after.audit.log_params:
        found.append(_finding("audit-params-on", "warn"))
    if after.audit.enabled and before.audit.resolved_path() != after.audit.resolved_path():
        # A log sent somewhere else (to /dev/null, say) is a log turned off.
        found.append(_finding("audit-path-changed", "warn"))

    for enabled in sorted(set(before.tools.disabled) - set(after.tools.disabled)):
        found.append(_finding("tool-enabled", "warn", tool=enabled))
    for group in sorted(set(before.tools.disabled_groups) - set(after.tools.disabled_groups)):
        level = "danger" if group == "admin" else "warn"
        found.append(_finding("tool-group-enabled", level, group=group))

    for name, tool in after.custom_tools.items():
        old_tool = before.custom_tools.get(name)
        if old_tool is None:
            found.append(_finding("custom-tool-added", "warn", tool=name))
        elif old_tool.sql != tool.sql:
            found.append(_finding("custom-tool-sql-changed", "warn", tool=name))
        elif _wider(tuple(renamed.get(c, c) for c in old_tool.connections), tool.connections):
            # Compared under the new names: a connection that was only renamed is not
            # a wider scope.
            found.append(_finding("custom-tool-scope-widened", "warn", tool=name))
        elif old_tool.max_rows is not None and (
            tool.max_rows is None or tool.max_rows > old_tool.max_rows
        ):
            found.append(_finding("custom-tool-rows-raised", "note", tool=name))

    if after.default_connection != before.default_connection and after.default_connection:
        target = after.connections[after.default_connection]
        if target.is_production:
            found.append(_finding("default-is-production", "warn", after.default_connection))
    return _sorted(found)


def _wider(before: tuple[str, ...], after: tuple[str, ...]) -> bool:
    """An empty list means every connection, so emptying it widens the scope."""
    if not before:
        return False
    return not after or bool(set(after) - set(before))


def _new_connection(name: str, profile: ConnectionConfig) -> list[Finding]:
    # A new door for the agents, wherever it leads: the owner says yes to it.
    found = [_finding("connection-added", "warn", name, access=profile.access.value)]
    if profile.access is not Access.READ_ONLY:
        level = "danger" if profile.is_production else "warn"
        found.append(_finding("new-connection-writes", level, name, access=profile.access.value))
    if profile.is_production and profile.effective_approval is Approval.NEVER:
        found.append(_finding("production-without-approval", "danger", name))
    if profile.function_policy is FunctionPolicy.PERMISSIVE:
        found.append(_finding("functions-permissive", "danger", name))
    if profile.expose_secret_catalogs:
        found.append(_finding("secret-catalogs-exposed", "danger", name))
    return found


def _connection(name: str, old: ConnectionConfig, new: ConnectionConfig) -> list[Finding]:
    found: list[Finding] = []

    def add(code: str, level: str, **data: Any) -> None:
        found.append(_finding(code, level, name, **data))

    if new.access.rank > old.access.rank:
        add("access-raised", "danger", before=old.access.value, after=new.access.value)
    if old.effective_approval is Approval.SESSION and new.effective_approval is Approval.NEVER:
        add("approval-removed", "danger")
    if old.is_production and not new.is_production:
        add("environment-demoted", "warn", before=old.environment, after=new.environment)
    if old.approval_fallback == "deny" and new.approval_fallback == "conversation":
        add("approval-fallback-weakened", "warn")
    if new.effective_approval is Approval.SESSION and _longer(
        old.approval_ttl, new.approval_ttl, zero_is_unlimited=True
    ):
        add("approval-lasts-longer", "warn", before=old.approval_ttl, after=new.approval_ttl)
    if _CONFIRM_RANK[new.write_confirmation] > _CONFIRM_RANK[old.write_confirmation]:
        level = "danger" if new.write_confirmation is WriteConfirmation.NONE else "warn"
        add(
            "write-confirmation-weakened",
            level,
            before=old.write_confirmation.value,
            after=new.write_confirmation.value,
        )
    if _POLICY_RANK[new.function_policy] > _POLICY_RANK[old.function_policy]:
        level = "danger" if new.function_policy is FunctionPolicy.PERMISSIVE else "warn"
        add(
            "function-policy-weakened",
            level,
            before=old.function_policy.value,
            after=new.function_policy.value,
        )
    if added := sorted(set(new.allow_functions) - set(old.allow_functions)):
        add("functions-allowed", "warn", added=added)
    if removed := sorted(set(old.deny_functions) - set(new.deny_functions)):
        add("functions-no-longer-denied", "warn", removed=removed)
    if removed := sorted(set(old.deny_relations) - set(new.deny_relations)):
        add("relations-no-longer-denied", "warn", removed=removed)
    if new.expose_secret_catalogs and not old.expose_secret_catalogs:
        add("secret-catalogs-exposed", "danger")
    if removed := sorted(set(old.redact_columns) - set(new.redact_columns)):
        add("redaction-reduced", "warn", removed=removed, off=not new.redact_columns)
    if new.allow_explain_analyze and not old.allow_explain_analyze:
        add("explain-analyze-allowed", "warn")
    if new.allow_export and not old.allow_export:
        add("export-allowed", "warn")
    if new.export_dir != old.export_dir:
        add("export-dir-changed", "warn")
    if new.import_dir != old.import_dir:
        add("import-dir-changed", "warn")
    found += _protection(name, old, new)

    for key in _TIMEOUTS:
        before, after = getattr(old, key), getattr(new, key)
        if _longer(before, after, zero_is_unlimited=key != "statement_timeout"):
            level = "warn" if after == 0 or new.is_production else "note"
            add("limit-raised", level, key=key, before=before, after=after)
    for key in _SIZES:
        before, after = getattr(old, key), getattr(new, key)
        if after > before:
            level = "warn" if new.is_production else "note"
            add("limit-raised", level, key=key, before=before, after=after)
    if old.max_plan_cost is not None and (
        new.max_plan_cost is None or new.max_plan_cost > old.max_plan_cost
    ):
        add("cost-ceiling-raised", "warn", before=old.max_plan_cost, after=new.max_plan_cost)

    if new.role != old.role:
        # Also when a role is added: SET ROLE may be to a stronger role than the login.
        add("role-changed", "warn", before=old.role, after=new.role)
    changed = [key for key in _TARGET_KEYS if getattr(old, key) != getattr(new, key)]
    changed += [
        f"params.{key}" for key in _TARGET_PARAMS if old.params.get(key) != new.params.get(key)
    ]
    if changed:
        add("target-changed", "warn", keys=changed)
    # No `sslmode` is libpq's "prefer", on both sides of the comparison.
    old_ssl, new_ssl = old.params.get("sslmode"), new.params.get("sslmode")
    if _SSL_RANK.get(new_ssl or "prefer", 2) < _SSL_RANK.get(old_ssl or "prefer", 2):
        add("ssl-weakened", "warn", before=old_ssl or "prefer", after=new_ssl or "prefer")
    others = sorted(
        key
        for key in set(old.params) | set(new.params)
        if key not in (*_TARGET_PARAMS, "sslmode", "sslpassword")
        and old.params.get(key) != new.params.get(key)
    )
    trusted = [key for key in others if key not in _TUNING_PARAMS]
    if trusted:
        add("params-changed", "warn", keys=trusted)
    return found


def _target(profile: ConnectionConfig) -> tuple[Any, ...]:
    """Where a connection leads, whatever it is called."""
    return (
        *(getattr(profile, key) for key in _TARGET_KEYS),
        *(profile.params.get(key) for key in _TARGET_PARAMS),
    )


def _longer(before: int, after: int, *, zero_is_unlimited: bool) -> bool:
    if zero_is_unlimited:
        if before == 0:
            return False
        if after == 0:
            return True
    return after > before


def _first(rules: tuple[ProtectedTable, ...], table: str) -> ProtectedTable | None:
    """The rule in force for ``table``: the first one that matches, as in the server."""
    for rule in rules:
        if fnmatch.fnmatchcase(table, rule.table):
            return rule
    return None


def _weaker(old: ProtectedTable, new: ProtectedTable | None) -> list[tuple[str, dict[str, Any]]]:
    """Every way ``new`` protects less than ``old`` did."""
    if new is None:
        return [("protection-removed", {})]
    out: list[tuple[str, dict[str, Any]]] = []
    if lost := sorted(set(old.deny_columns) - set(new.deny_columns)):
        out.append(("columns-no-longer-denied", {"columns": lost}))
    if old.require_index_filter and not new.require_index_filter:
        out.append(("index-filter-dropped", {}))
    if old.max_rows is not None and (new.max_rows is None or new.max_rows > old.max_rows):
        out.append(("protected-rows-raised", {"before": old.max_rows, "after": new.max_rows}))
    return out


def _protection(name: str, old: ConnectionConfig, new: ConnectionConfig) -> list[Finding]:
    """What happens to the protected tables.

    The first rule that matches a table wins, so the question is never "is the rule
    still in the list" but "which rule is in force now". It is asked for every table
    either list names: a broad or an empty rule placed before another shows up as the
    loss it is.
    """
    found: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for table in dict.fromkeys([*(r.table for r in old.protect), *(r.table for r in new.protect)]):
        before = _first(old.protect, table)
        if before is None:
            continue
        for code, data in _weaker(before, _first(new.protect, table)):
            if (code, table) in seen:
                continue
            seen.add((code, table))
            softer = code in ("index-filter-dropped", "protected-rows-raised")
            found.append(_finding(code, "warn" if softer else "danger", name, table=table, **data))
    if not found and not _only_appended(old.protect, new.protect):
        # Patterns can overlap in ways the names above do not reveal: any other
        # rearrangement is shown to the owner rather than judged harmless.
        found.append(_finding("protection-rearranged", "warn", name))
    return found


def _only_appended(old: tuple[ProtectedTable, ...], new: tuple[ProtectedTable, ...]) -> bool:
    """True when every old rule is still in its place and at least as strong, and what
    is new comes after all of them, where it can take no precedence."""
    if len(new) < len(old):
        return False
    return all(n.table == o.table and not _weaker(o, n) for o, n in zip(old, new, strict=False))


def _sorted(found: list[Finding]) -> list[Finding]:
    order = {"danger": 0, "warn": 1, "note": 2}
    return sorted(found, key=lambda f: (order[f["level"]], f.get("connection") or "", f["code"]))


# ------------------------------------------------------------------------------- posture


def posture(
    config: Config, *, file_mode: str | None = None, secrets: tuple[str, ...] = ()
) -> list[Finding]:
    """The state of one configuration, as findings: ``danger``, ``warn``, ``note`` or ``ok``.

    ``file_mode`` is the octal mode of the file (``None`` where modes do not apply) and
    ``secrets`` the paths of the credentials written in it.
    """
    found: list[Finding] = []

    if secrets:
        found.append(_finding("secret-in-file", "warn", keys=sorted(secrets)))
        if file_mode is not None and int(file_mode, 8) & 0o077:
            found.append(_finding("file-readable-by-others", "danger", mode=file_mode))
    elif file_mode is not None and int(file_mode, 8) & 0o022:
        found.append(_finding("file-writable-by-others", "danger", mode=file_mode))

    if not config.audit.enabled:
        found.append(_finding("audit-is-off", "danger"))
    elif not config.audit.log_sql:
        found.append(_finding("audit-without-sql", "note"))
    if config.audit.log_params:
        found.append(_finding("audit-logs-values", "note"))

    for name, profile in config.connections.items():

        def add(code: str, level: str, _name: str = name, **data: Any) -> None:
            found.append(_finding(code, level, _name, **data))

        production = profile.is_production
        if production and profile.access is not Access.READ_ONLY:
            add("production-writes", "danger", access=profile.access.value)
        if production and profile.effective_approval is Approval.NEVER:
            add("production-without-approval", "danger")
        if production and profile.max_plan_cost is None:
            add("production-without-cost-ceiling", "warn")
        if production and profile.approval_fallback == "conversation":
            add("production-fallback-conversation", "note")
        if production and profile.allow_explain_analyze:
            add("production-explain-analyze", "warn")
        if profile.function_policy is FunctionPolicy.PERMISSIVE:
            add("functions-permissive", "danger" if production else "warn")
        if profile.expose_secret_catalogs:
            add("secret-catalogs-exposed", "danger" if production else "warn")
        if not profile.read_only and profile.write_confirmation is not WriteConfirmation.DIALOG:
            level = "danger" if profile.write_confirmation is WriteConfirmation.NONE else "warn"
            add("writes-without-dialog", level, mode=profile.write_confirmation.value)
        if not profile.redact_columns:
            add("redaction-off", "warn")
        if profile.allow_functions:
            add("functions-vouched", "note", count=len(profile.allow_functions))
        ssl = profile.params.get("sslmode")
        remote = profile.host not in (None, "localhost", "127.0.0.1", "::1")
        if remote and not profile.dsn and not profile.dsn_env and _SSL_RANK.get(ssl or "", 2) < 3:
            add("no-tls-required", "warn" if production else "note", sslmode=ssl)

    if not found:
        found.append(_finding("all-good", "ok"))
    order = {"danger": 0, "warn": 1, "note": 2, "ok": 3}
    return sorted(found, key=lambda f: (order[f["level"]], f.get("connection") or "", f["code"]))
