"""The real process over stdio, with no database: it starts, lists its tools and explains
what is missing. Runs on every platform CI covers."""

import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters


def text(result: Any) -> str:
    return " ".join(getattr(block, "text", "") for block in result.content)


async def test_server_starts_and_answers_without_a_database(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PG", "DATABASE_URL"))}
    env["XDG_CONFIG_HOME"] = str(tmp_path)  # nothing configured, on any platform
    env["APPDATA"] = str(tmp_path)
    parameters = StdioServerParameters(command=sys.executable, args=["-m", "pgharbor.cli"], env=env)
    async with Client(parameters) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
        assert {"pg_connections", "pg_query", "pg_describe_table"} <= tools
        assert "pg_execute" not in tools  # nothing configured that could write
        listed = json.loads(text(await client.call_tool("pg_connections", {})))
        assert listed["connections"] == []
        assert "pgharbor init" in listed["how_to_configure"]
        failed = await client.call_tool("pg_query", {"sql": "SELECT 1"})
        assert failed.is_error
        assert "No connection is configured" in text(failed)
