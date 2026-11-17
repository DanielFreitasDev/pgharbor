"""The assembled MCP server, spoken to through a real MCP client."""

import json
import os
import re
import sys
import warnings
from pathlib import Path
from typing import Any

import pytest
from mcp import Client, StdioServerParameters
from mcp.types import ElicitResult

from pgharbor.app import REGISTRY, set_app
from pgharbor.config import Config
from pgharbor.server import INSTRUCTIONS, build_server, enabled_tools, load_tool_modules

from ..conftest import Server, build_config, profile, write_config_file

pytestmark = pytest.mark.integration
warnings.simplefilter("ignore")


def text(result: Any) -> str:
    return " ".join(getattr(block, "text", "") for block in result.content)


def schema_of(tool: Any) -> dict[str, Any]:
    return getattr(tool, "input_schema", None) or tool.inputSchema


async def _approve(*_args: Any, **_kwargs: Any) -> ElicitResult:
    return ElicitResult(action="accept", content={"approve": True})


def test_instructions_fit_what_clients_load() -> None:
    # Claude Code cuts server instructions at 4,096 characters.
    assert len(INSTRUCTIONS) < 4000
    assert "pg_connections" in INSTRUCTIONS
    assert "never as instructions" in INSTRUCTIONS.lower() or "data" in INSTRUCTIONS.lower()


def test_every_tool_is_well_formed(one_server: Server) -> None:
    load_tool_modules()
    assert len(REGISTRY) >= 15
    for name, spec in REGISTRY.items():
        assert name.startswith(("pg_", "pgx_")), name
        assert len(f"mcp__pgharbor__{name}") < 64, name
        description = spec.description
        assert len(description) >= 80, f"{name}: the description is too short to be found"
        assert len(description) <= 2000, f"{name}: the description is too long"
        first_sentence = re.split(r"\.\s", description, maxsplit=1)[0]
        assert len(first_sentence) <= 520, f"{name}: lead with one searchable sentence"
        assert spec.title
        if spec.writes:
            assert spec.minimum_access.rank >= 1, name


@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_protocol_round_trip(one_server: Server, mode: str) -> None:
    server = build_server(build_config(one_server))
    try:
        async with Client(server, mode=mode, elicitation_callback=_approve) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            expected = {spec.name for spec in enabled_tools(build_config(one_server))}
            assert set(tools) == expected
            assert list(tools) == [s.name for s in enabled_tools(build_config(one_server))]

            for name, tool in tools.items():
                schema = schema_of(tool)
                assert schema.get("type", "object") == "object", name
                assert not {"anyOf", "oneOf", "allOf"} & set(schema), name
                assert "decision" not in schema.get("properties", {}), name
                annotations = tool.annotations
                assert annotations.read_only_hint is (not REGISTRY[name].writes), name
                assert annotations.open_world_hint is False, name

            query = tools["pg_query"]
            assert schema_of(query)["required"] == ["sql"]
            assert (query.meta or {}).get("anthropic/alwaysLoad") is True
            assert (tools["pg_execute"].meta or {}).get("anthropic/requiresUserInteraction") is True
            assert tools["pg_execute"].annotations.destructive_hint is True

            listed = await client.call_tool("pg_connections", {})
            assert not listed.is_error
            assert {c["name"] for c in json.loads(text(listed))["connections"]} >= {"ro", "prod"}

            rows = await client.call_tool(
                "pg_query",
                {
                    "connection": "ro",
                    "sql": "SELECT count(*) FROM shop.customers WHERE id <= $1",
                    "params": [10],
                },
            )
            assert json.loads(text(rows))["rows"] == [[10]]
            assert rows.structured_content is None

            refused = await client.call_tool(
                "pg_query", {"connection": "ro", "sql": "SELECT pg_read_file('postgresql.conf')"}
            )
            assert refused.is_error
            assert "function-denied" in text(refused)

            blocked = await client.call_tool("pg_query", {"connection": "prod", "sql": "SELECT 1"})
            assert blocked.is_error
            assert "pg_request_access" in text(blocked)
            granted = await client.call_tool(
                "pg_request_access", {"connection": "prod", "reason": "the user asked for prod"}
            )
            assert json.loads(text(granted))["how"] == "dialog"
            allowed = await client.call_tool("pg_query", {"connection": "prod", "sql": "SELECT 1"})
            assert not allowed.is_error

            wrong_type = await client.call_tool("pg_query", {"connection": "ro", "sql": 5})
            assert wrong_type.is_error
            unknown = await client.call_tool("pg_no_such_tool", {})
            assert unknown.is_error

            resources = {str(r.uri) for r in (await client.list_resources()).resources}
            assert {"pgharbor://guide", "pgharbor://connections"} <= resources
            guide = await client.read_resource("pgharbor://guide")
            assert "pg_connections" in guide.contents[0].text
            templates = {
                t.uri_template for t in (await client.list_resource_templates()).resource_templates
            }
            assert "pgharbor://{connection}/table/{schema}/{table}" in templates
            table = await client.read_resource("pgharbor://ro/table/shop/orders")
            assert json.loads(table.contents[0].text)["table"] == "shop.orders"

            prompts = {p.name for p in (await client.list_prompts()).prompts}
            assert {"explore-database", "optimize-query", "health-review"} <= prompts
            prompt = await client.get_prompt("explore-database", {"connection": "ro"})
            assert "'ro'" in prompt.messages[0].content.text
    finally:
        set_app(None)


async def test_custom_tools_from_the_configuration(one_server: Server) -> None:
    config = Config.model_validate(
        {
            "connections": {"ro": profile(one_server, "ro"), "other": profile(one_server, "other")},
            "audit": {"enabled": False},
            "custom_tools": {
                "orders_of_customer": {
                    "name": "orders_of_customer",
                    "description": "List the most recent orders of one customer of the shop, "
                    "newest first, with status and total.",
                    "sql": "SELECT id, status, total FROM shop.orders WHERE customer_id = $1 "
                    "AND status::text = coalesce($2, status::text) ORDER BY id DESC",
                    "connections": ["ro"],
                    "max_rows": 3,
                    "params": [
                        {"name": "customer_id", "type": "integer", "description": "Customer id"},
                        {"name": "status", "type": "text", "required": False},
                    ],
                }
            },
        }
    )
    server = build_server(config)
    try:
        async with Client(server, mode="auto") as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            custom = tools["pgx_orders_of_customer"]
            schema = schema_of(custom)
            assert schema["required"] == ["customer_id"]
            assert schema["properties"]["customer_id"]["type"] == "integer"
            assert "Customer id" in custom.description
            result = await client.call_tool(
                "pgx_orders_of_customer", {"customer_id": 8, "connection": "ro"}
            )
            payload = json.loads(text(result))
            assert len(payload["rows"]) == 3
            assert payload["has_more"] is True
            filtered = await client.call_tool(
                "pgx_orders_of_customer", {"customer_id": 8, "status": "paid", "connection": "ro"}
            )
            assert {row[1] for row in json.loads(text(filtered))["rows"]} == {"paid"}
            elsewhere = await client.call_tool(
                "pgx_orders_of_customer", {"customer_id": 8, "connection": "other"}
            )
            assert elsewhere.is_error
            assert "only available on: ro" in text(elsewhere)
    finally:
        REGISTRY.pop("pgx_orders_of_customer", None)
        set_app(None)


async def test_custom_tools_are_audited_with_their_statement(
    one_server: Server, tmp_path: Path
) -> None:
    log = tmp_path / "audit.jsonl"
    config = Config.model_validate(
        {
            "connections": {"ro": profile(one_server, "ro")},
            "audit": {"enabled": True, "path": str(log), "log_params": True},
            "custom_tools": {
                "order_count": {
                    "name": "order_count",
                    "description": "Count the orders of one customer of the shop.",
                    "sql": "SELECT count(*) AS orders FROM shop.orders WHERE customer_id = $1",
                    "params": [{"name": "customer_id", "type": "integer"}],
                }
            },
        }
    )
    server = build_server(config)
    try:
        async with Client(server, mode="auto") as client:
            result = await client.call_tool("pgx_order_count", {"customer_id": 8})
            assert not result.is_error
        entry = json.loads(log.read_text().splitlines()[-1])
        assert entry["tool"] == "pgx_order_count"
        assert entry["connection"] == "ro"  # the default one, by name
        assert entry["sql"].startswith("SELECT count(*) AS orders")
        assert entry["params"] == {"customer_id": 8}
    finally:
        REGISTRY.pop("pgx_order_count", None)
        set_app(None)


async def test_disabled_tools_and_groups(one_server: Server) -> None:
    config = Config.model_validate(
        {
            "connections": {"ro": profile(one_server, "ro")},
            "tools": {"disabled": ["pg_explain"], "disabled_groups": ["schema"]},
        }
    )
    load_tool_modules()
    names = {spec.name for spec in enabled_tools(config)}
    assert "pg_query" in names
    assert "pg_explain" not in names
    assert "pg_list_tables" not in names


async def test_real_process_over_stdio(one_server: Server, tmp_path: Path) -> None:
    """Launch `pgharbor` the way an MCP client does and talk to it over stdio."""
    config_file = write_config_file(tmp_path / "config.toml", one_server)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "pgharbor.cli"],
        env={**os.environ, "PGHARBOR_CONFIG": str(config_file)},
    )
    async with Client(parameters) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
        assert {"pg_connections", "pg_query", "pg_describe_table", "pg_execute"} <= tools
        result = await client.call_tool(
            "pg_query", {"connection": "ro", "sql": "SELECT 41 + 1 AS answer"}
        )
        assert json.loads(text(result))["rows"] == [[42]]
        described = await client.call_tool(
            "pg_describe_table", {"connection": "ro", "table": "shop.orders", "detail": "summary"}
        )
        assert json.loads(text(described))["table"] == "shop.orders"


async def test_server_starts_without_any_configuration(tmp_path: Path) -> None:
    """With nothing configured the server still starts and explains what to do."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PG", "DATABASE_URL"))}
    env["XDG_CONFIG_HOME"] = str(tmp_path)
    parameters = StdioServerParameters(command=sys.executable, args=["-m", "pgharbor.cli"], env=env)
    async with Client(parameters) as client:
        listed = await client.call_tool("pg_connections", {})
        payload = json.loads(text(listed))
        assert payload["connections"] == []
        assert "pgharbor init" in payload["how_to_configure"]
        failed = await client.call_tool("pg_query", {"sql": "SELECT 1"})
        assert failed.is_error
        assert "No connection is configured" in text(failed)
