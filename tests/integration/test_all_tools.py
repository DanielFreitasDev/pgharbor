"""Every read tool, on every PostgreSQL version, as a superuser and as a plain role.

The per-tool tests check content. This one checks a property of the whole set: no tool
may crash. A tool may refuse (missing privilege, feature not in this version) with a
clear message, but "internal error" is never acceptable, the answer must stay within the
size the clients accept, and nothing that looks like a credential may come out.
"""

import inspect
import json
import re
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from pgharbor.app import MAX_OUTPUT_CHARS, REGISTRY
from pgharbor.server import load_tool_modules

from ..conftest import Server

pytestmark = pytest.mark.integration

load_tool_modules()

# Arguments for the tools that need more than a connection. Several entries exercise
# several shapes of the same tool.
CALLS: dict[str, list[dict[str, Any]]] = {
    "pg_query": [{"sql": "SELECT count(*) FROM shop.orders"}],
    "pg_explain": [{"sql": "SELECT * FROM shop.orders WHERE customer_id = 1"}],
    "pg_validate_sql": [{"sql": "SELECT id FROM shop.orders"}],
    "pg_audit_log": [],  # needs an enabled audit log; covered in test_core
    "pg_request_access": [],  # needs the dialog; covered in test_server
    "pg_describe_table": [
        {"table": "shop.orders"}, {"table": "shop.order_summary"},
        {"table": "analytics.daily_sales"}, {"table": "events.log"},
    ],
    "pg_list_tables": [{}, {"schema": "shop", "kinds": ["table", "view"], "order_by": "size"}],
    "pg_list_objects": [
        {"kind": kind}
        for kind in (
            "view", "materialized_view", "sequence", "function", "procedure", "aggregate",
            "trigger", "event_trigger", "index", "type", "enum", "domain", "extension",
            "foreign_table", "foreign_server", "foreign_data_wrapper", "operator", "cast",
            "collation", "language", "tablespace", "rule", "policy", "statistics",
            "publication", "subscription", "text_search_config", "access_method", "database",
        )
    ],
    "pg_describe_object": [
        {"name": "shop.order_total", "kind": "function"},
        {"name": "shop.invoice_seq", "kind": "sequence"},
        {"name": "shop.order_status", "kind": "type"},
        {"name": "shop.orders_customer_idx", "kind": "index"},
        {"name": "shop", "kind": "schema"},
        {"name": "pg_stat_statements", "kind": "extension"},
    ],
    "pg_get_ddl": [
        {"name": "shop.orders"}, {"name": "shop.order_summary", "kind": "view"},
        {"name": "shop.order_total", "kind": "function"}, {"name": "events.log"},
    ],
    "pg_search": [{"query": "order"}],
    "pg_search_source": [{"pattern": "order_items"}],
    "pg_dependencies": [{"name": "shop.orders"}, {"name": "shop.customers"}],
    "pg_relationships": [{"schema": "shop"}, {"schema": "shop", "format": "mermaid"}],
    "pg_schema_overview": [{"schema": "shop"}],
    "pg_compare_schemas": [
        {"source_connection": "ro", "target_connection": "ro", "schema": "shop",
         "target_schema": "vault"}
    ],
    "pg_list_partitions": [{"table": "events.log"}],
    "pg_sample_rows": [{"table": "shop.orders"}, {"table": "shop.orders", "random": True}],
    "pg_count_rows": [{"table": "shop.orders"}, {"table": "shop.no_pk", "exact": True}],
    "pg_column_stats": [{"table": "shop.orders"}],
    "pg_export_query": [],  # writes a file; covered in test_data
    "pg_stat_view": [{"view": "pg_stat_database"}, {"view": "pg_stat_user_tables"}],
    "pg_index_stats": [{}, {"show": "unused"}, {"show": "duplicate"}, {"show": "redundant"},
                       {"show": "invalid"}],
    "pg_bloat": [{}, {"exact_table": "shop.orders"}],
    "pg_sizes": [{"scope": scope} for scope in ("databases", "schemas", "tables", "indexes",
                                                "tablespaces")],
    "pg_settings": [{}, {"pattern": "autovacuum*"}, {"changed_only": True}],
    "pg_index_advisor": [{}, {"table": "shop.reviews"}],
    "pg_privileges": [{"object": "shop.customers"}, {"role": "harbor_ro"}],
    "pg_extension_report": [{"extension": "pg_stat_statements"}],
    "pg_integrity_check": [{}, {"schema": "shop"}],
}  # fmt: skip

# What a leaked credential would look like.
SECRETS = re.compile(r"SCRAM-SHA-256\$|\bmd5[0-9a-f]{32}\b|password\s*=\s*(?!\[redacted\])\S")


def read_tools() -> list[str]:
    return [name for name, spec in REGISTRY.items() if not spec.writes and spec.group != "custom"]


def calls_for(name: str) -> list[dict[str, Any]]:
    return CALLS.get(name, [{}])


def test_every_read_tool_is_exercised() -> None:
    """A new tool that needs arguments must be added to CALLS, or it is not smoke-tested."""
    missing = []
    for name in read_tools():
        signature = inspect.signature(REGISTRY[name].function)
        required = [
            p for p, v in signature.parameters.items() if v.default is inspect.Parameter.empty
        ]
        if required and name not in CALLS:
            missing.append(name)
    assert not missing, f"add arguments for {missing} to CALLS in this file"


async def exercise(connection: str) -> list[str]:
    """Call every read tool; return descriptions of the calls that misbehaved."""
    problems: list[str] = []
    for name in read_tools():
        for arguments in calls_for(name):
            label = f"{name}({arguments})"
            try:
                if "connection" in inspect.signature(REGISTRY[name].function).parameters:
                    text = await REGISTRY[name].function(connection=connection, **arguments)
                else:
                    text = await REGISTRY[name].function(**arguments)
            except ToolError as refusal:
                message = str(refusal)
                if "internal error" in message:
                    problems.append(f"{label}: {message}")
                elif len(message) < 20:
                    problems.append(f"{label}: unhelpful refusal {message!r}")
                continue
            if not isinstance(text, str) or not text:
                problems.append(f"{label}: empty answer")
                continue
            if len(text) > MAX_OUTPUT_CHARS + 200:
                problems.append(f"{label}: answer of {len(text)} characters")
            if text.lstrip().startswith(("{", "[")):
                try:
                    json.loads(text)
                except ValueError:
                    if "output cut" not in text:
                        problems.append(f"{label}: invalid JSON")
            leak = SECRETS.search(text)
            if leak:
                problems.append(f"{label}: looks like a credential: {leak.group(0)!r}")
    return problems


async def test_no_tool_crashes_as_superuser(app: Any, ready: Server) -> None:
    problems = await exercise("ro")
    assert not problems, "\n".join(problems)


async def test_no_tool_crashes_as_a_plain_role(app: Any, ready: Server) -> None:
    problems = await exercise("limited")
    assert not problems, "\n".join(problems)


async def test_no_tool_crashes_with_protection_rules(app: Any, ready: Server) -> None:
    problems = await exercise("guarded")
    assert not problems, "\n".join(problems)


async def test_nothing_reads_the_protected_column(app: Any) -> None:
    """With the vault rules on, no tool may hand out a value of the protected column."""
    for name, arguments in (
        ("pg_sample_rows", {"table": "vault.files"}),
        ("pg_sample_rows", {"table": "vault.files", "columns": ["id", "access_key"]}),
        ("pg_column_stats", {"table": "vault.files"}),
        ("pg_column_stats", {"table": "vault.files", "columns": ["access_key"]}),
        ("pg_describe_table", {"table": "vault.files"}),
        ("pg_get_ddl", {"name": "vault.files"}),
        ("pg_stat_view", {"view": "pg_stats"}),
        ("pg_query", {"sql": "SELECT * FROM vault.file_keys"}),
        ("pg_count_rows", {"table": "vault.files", "exact": True}),
    ):
        try:
            text = await REGISTRY[name].function(connection="guarded", **arguments)
        except ToolError as refusal:
            text = str(refusal)
        assert "KEY-" not in text, f"{name}({arguments}) leaked a protected value"
