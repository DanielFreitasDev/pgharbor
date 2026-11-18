"""What the panel needs to know about every configuration key to draw a form for it.

The list of keys, their defaults, ranges and choices come from the configuration models
themselves, so the panel cannot drift from what the server accepts. What a model does not
say (which keys are durations, which hold a credential, how keys are grouped on screen)
is declared here, and a test fails when a key is added to a model and not placed here.

Labels and explanations are not here: they are translated, in the page's dictionaries,
under ``field.<key>``.
"""

from __future__ import annotations

import types
from enum import Enum
from typing import Any, Literal, Union, get_args, get_origin

from annotated_types import Ge, Gt, Le
from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from ..config import (
    _LIBPQ_PARAMS,
    _NAME_RE,
    _PRODUCTION_LABELS,
    _TOOL_NAME_RE,
    AuditConfig,
    ConnectionConfig,
    CustomTool,
    CustomToolParam,
    ProtectedTable,
    ToolsConfig,
)

__all__ = ["CONNECTION_GROUPS", "NOT_IN_DEFAULTS", "schema"]

# Sections of the connection form, in the order they are shown.
CONNECTION_GROUPS: dict[str, tuple[str, ...]] = {
    "identity": ("description", "environment"),
    "where": (
        "host",
        "port",
        "dbname",
        "user",
        "user_env",
        "password_env",
        "password_file",
        "password",
        "dsn_env",
        "dsn",
        "role",
        "params",
    ),
    "access": (
        "access",
        "approval",
        "approval_ttl",
        "approval_fallback",
        "write_confirmation",
    ),
    "functions": ("function_policy", "allow_functions", "deny_functions"),
    "data": (
        "protect",
        "deny_relations",
        "redact_columns",
        "expose_secret_catalogs",
        "allow_explain_analyze",
        "allow_export",
        "export_dir",
        "import_dir",
    ),
    "limits": (
        "statement_timeout",
        "lock_timeout",
        "idle_in_transaction_session_timeout",
        "max_rows",
        "max_rows_limit",
        "max_cell_chars",
        "max_result_chars",
        "max_sql_chars",
        "max_plan_cost",
    ),
    "pool": ("pool_size", "idle_close", "connect_timeout", "application_name"),
}

# Keys that identify one database and therefore cannot be set under ``[defaults]``
# (the same list the loader enforces; a test keeps the two equal).
NOT_IN_DEFAULTS = frozenset(
    {
        "name",
        "dsn",
        "dsn_env",
        "host",
        "port",
        "dbname",
        "user",
        "user_env",
        "password",
        "password_env",
        "password_file",
        "protect",
        "description",
    }
)

# How a key is edited, where its type alone does not say.
_KINDS: dict[str, dict[str, str]] = {
    "connection": {
        "password": "secret",
        "dsn": "secret",
        "user_env": "env",
        "password_env": "env",
        "dsn_env": "env",
        "password_file": "path",
        "export_dir": "path",
        "import_dir": "path",
        "protect": "protect",
        "params": "params",
        "environment": "environment",
        "approval_ttl": "duration",
        "statement_timeout": "duration",
        "lock_timeout": "duration",
        "idle_in_transaction_session_timeout": "duration",
        "idle_close": "duration",
        "connect_timeout": "duration",
    },
    "protect": {"note": "text"},
    "audit": {"path": "path"},
    "tools": {"disabled": "tools", "disabled_groups": "groups"},
    "custom_tool": {"sql": "sql", "connections": "connections", "params": "tool_params"},
    "custom_tool_param": {"default": "scalar"},
}

TOOL_GROUPS = (
    "core",
    "schema",
    "data",
    "performance",
    "health",
    "replication",
    "security",
    "extensions",
    "admin",
    "custom",
)


def _strip_optional(annotation: Any) -> tuple[Any, bool]:
    if get_origin(annotation) in (Union, types.UnionType):
        members = [a for a in get_args(annotation) if a is not type(None)]
        if len(members) == 1:
            return members[0], True
        return annotation, type(None) in get_args(annotation)
    return annotation, False


def _json(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (tuple, list)):
        return [_json(v) for v in value]
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    return value


def _describe(
    model: type[BaseModel], section: str, *, skip: frozenset[str]
) -> list[dict[str, Any]]:
    kinds = _KINDS[section]
    out: list[dict[str, Any]] = []
    for key, info in model.model_fields.items():
        if key in skip:
            continue
        annotation, optional = _strip_optional(info.annotation)
        entry: dict[str, Any] = {"key": key, "optional": optional}
        if key in kinds:
            entry["kind"] = kinds[key]
        elif isinstance(annotation, type) and issubclass(annotation, Enum):
            entry["kind"] = "choice"
            entry["options"] = [member.value for member in annotation]
        elif get_origin(annotation) is Literal:
            entry["kind"] = "choice"
            entry["options"] = list(get_args(annotation))
        elif annotation is bool:
            entry["kind"] = "bool"
        elif annotation is int:
            entry["kind"] = "int"
        elif annotation is float:
            entry["kind"] = "float"
        elif get_origin(annotation) in (tuple, list):
            entry["kind"] = "list"
        else:
            entry["kind"] = "text"
        if info.default is not PydanticUndefined:
            entry["default"] = _json(info.default)
        elif info.default_factory is not None:
            entry["default"] = _json(info.default_factory())  # type: ignore[call-arg]
        else:
            entry["required"] = True
        for rule in info.metadata:
            if isinstance(rule, Ge):
                entry["min"] = rule.ge
            elif isinstance(rule, Gt):
                entry["above"] = rule.gt
            elif isinstance(rule, Le):
                entry["max"] = rule.le
        out.append(entry)
    return out


def schema() -> dict[str, Any]:
    """Everything the page needs to build its forms, as JSON."""
    group_of = {key: group for group, keys in CONNECTION_GROUPS.items() for key in keys}
    connection = _describe(ConnectionConfig, "connection", skip=frozenset({"name"}))
    for entry in connection:
        entry["group"] = group_of[entry["key"]]
        entry["in_defaults"] = entry["key"] not in NOT_IN_DEFAULTS
    order = {key: index for index, key in enumerate(group_of)}
    connection.sort(key=lambda entry: order[entry["key"]])
    return {
        "connection": connection,
        "connection_groups": list(CONNECTION_GROUPS),
        "protect": _describe(ProtectedTable, "protect", skip=frozenset()),
        "audit": _describe(AuditConfig, "audit", skip=frozenset()),
        "tools": _describe(ToolsConfig, "tools", skip=frozenset()),
        "custom_tool": _describe(CustomTool, "custom_tool", skip=frozenset({"name"})),
        "custom_tool_param": _describe(CustomToolParam, "custom_tool_param", skip=frozenset()),
        "libpq_params": sorted(_LIBPQ_PARAMS),
        "production_labels": sorted(_PRODUCTION_LABELS),
        "tool_groups": list(TOOL_GROUPS),
        "connection_name_pattern": _NAME_RE.pattern,
        "tool_name_pattern": _TOOL_NAME_RE.pattern,
    }
