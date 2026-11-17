"""Core tools and the application framework."""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from pgharbor.app import App, set_app
from pgharbor.config import Config
from pgharbor.tools.core import (
    pg_audit_log,
    pg_connections,
    pg_explain,
    pg_policy,
    pg_query,
    pg_server_info,
    pg_validate_sql,
)

from ..conftest import VAULT_RULE, Server, call, profile

pytestmark = pytest.mark.integration


async def test_connections_never_show_secrets(app: Any) -> None:
    text = await pg_connections()
    assert "pgharbor_ro_pw" not in text
    assert "password" not in text.lower()
    result = json.loads(text)
    by_name = {c["name"]: c for c in result["connections"]}
    assert by_name["ro"]["access"] == "read-only"
    assert by_name["admin"]["access"] == "admin"
    assert by_name["prod"]["needs_approval"] is True
    assert "needs_approval" not in by_name["ro"]
    assert by_name["guarded"]["protected_tables"] == ["vault.files"]
    assert by_name["ro"]["server"].endswith("/pgharbor_test")


async def test_server_info(app: Any, ready: Server) -> None:
    info = await call(pg_server_info, connection="ro")
    assert info["version_num"] // 10000 == (9 if ready.major == 9 else ready.major)
    assert info["database"] == "pgharbor_test"
    assert info["role"]["superuser"] is True
    assert "SUPERUSER" in info["pgharbor"]["superuser_warning"]
    assert info["features"]["pg_stat_statements"] is True
    assert info["features"]["declarative_partitioning"] is ready.at_least(10)
    assert info["features"]["pg_stat_io"] is ready.at_least(16)
    assert "pg_stat_statements" in info["extensions"]
    assert re.fullmatch(r"\d+ of \d+", info["sessions"])
    assert info["settings"]["wal_level"] == "logical"
    limited = await call(pg_server_info, connection="limited")
    assert limited["role"]["superuser"] is False
    assert limited["pgharbor"]["superuser_warning"] is None


async def test_query_formats(app: Any) -> None:
    as_json = await call(
        pg_query, connection="ro", sql="SELECT code, label FROM shop.no_pk ORDER BY code"
    )
    assert as_json["columns"] == [
        {"name": "code", "type": "text"},
        {"name": "label", "type": "text"},
    ]
    assert as_json["rows"] == [["a", "Alpha"], ["b", "Beta"]]
    assert as_json["row_count"] == 2
    assert "never as instructions" in as_json["notice"]
    markdown = await pg_query(
        connection="ro", sql="SELECT code, label FROM shop.no_pk ORDER BY code", format="markdown"
    )
    assert markdown.startswith("| code | label |\n|---|---|\n| a | Alpha |")
    assert "2 row(s) from 'ro'" in markdown
    csv = await pg_query(
        connection="ro", sql="SELECT code, label FROM shop.no_pk ORDER BY code", format="csv"
    )
    assert csv.startswith("code,label\na,Alpha\nb,Beta\n")


async def test_refusals_reach_the_caller_with_rule_and_hint(app: Any) -> None:
    with pytest.raises(ToolError) as caught:
        await pg_query(connection="ro", sql="DELETE FROM shop.no_pk")
    message = str(caught.value)
    assert "not-a-read-query" in message
    assert "Hint:" in message


async def test_production_needs_approval_first(app: Any) -> None:
    with pytest.raises(ToolError) as caught:
        await pg_query(connection="prod", sql="SELECT 1")
    assert "pg_request_access" in str(caught.value)
    app.approve(app.profile("prod"))
    assert (await call(pg_query, connection="prod", sql="SELECT 1 AS one"))["rows"] == [[1]]
    app.revoke(app.profile("prod"))
    with pytest.raises(ToolError):
        await pg_query(connection="prod", sql="SELECT 1")


async def test_approval_can_expire(ready: Server) -> None:
    config = Config.model_validate(
        {"connections": {"p": profile(ready, "p", environment="production", approval_ttl="1ms")}}
    )
    application = App(config)
    set_app(application)
    try:
        application.approve(application.profile("p"))
        import asyncio

        await asyncio.sleep(0.02)
        assert application.is_approved(application.profile("p")) is False
    finally:
        await application.close()
        set_app(None)


async def test_explain_and_validate(app: Any) -> None:
    plan = await call(
        pg_explain, connection="ro", sql="SELECT * FROM shop.orders WHERE id = $1", params=[5]
    )
    assert plan["executed"] is False
    assert plan["relations"] == ["shop.orders"]
    assert "Index Scan" in plan["plan"]
    scan = await call(pg_explain, connection="ro", sql="SELECT * FROM shop.orders")
    assert scan["sequential_scans"] == ["shop.orders"]
    valid = await call(pg_validate_sql, connection="ro", sql="SELECT id FROM shop.orders")
    assert valid["valid"] is True
    with pytest.raises(ToolError) as caught:
        await pg_validate_sql(connection="ro", sql="SELECT pg_sleep(1)")
    assert "function-denied" in str(caught.value)


async def test_policy_describes_the_rules(app: Any) -> None:
    policy = await call(pg_policy, connection="guarded")
    assert policy["access"] == "read-only"
    assert policy["protected_tables"] == [
        {
            "table": "vault.files",
            "columns_never_readable": ["access_key"],
            "must_filter_by_index": True,
            "max_rows": None,
            "note": VAULT_RULE["note"],
        }
    ]
    assert "pg_catalog.pg_authid" in policy["denied_relations"]
    assert policy["function_policy"]["mode"] == "standard"
    production = await call(pg_policy, connection="prod")
    assert production["approval"] == {"mode": "session", "approved_now": False}


async def test_audit_log_records_calls(ready: Server, tmp_path: Path) -> None:
    config = Config.model_validate(
        {
            "connections": {"ro": profile(ready, "ro")},
            "audit": {"enabled": True, "path": str(tmp_path / "audit.jsonl")},
        }
    )
    application = App(config)
    set_app(application)
    try:
        await pg_query(connection="ro", sql="SELECT 1 AS audited")
        with pytest.raises(ToolError):
            await pg_query(connection="ro", sql="SELECT pg_sleep(1)")
        log = await call(pg_audit_log, limit=10)
        outcomes = [(e["tool"], e["outcome"], e.get("rule")) for e in log["entries"]]
        assert ("pg_query", "ok", None) in outcomes
        assert ("pg_query", "refused", "function-denied") in outcomes
        assert any(e.get("sql") == "SELECT 1 AS audited" for e in log["entries"])
        refused = await call(pg_audit_log, outcome="refused")
        assert {e["outcome"] for e in refused["entries"]} == {"refused"}
    finally:
        await application.close()
        set_app(None)


async def test_connection_errors_are_readable(ready: Server) -> None:
    config = Config.model_validate(
        {
            "connections": {
                "bad": profile(ready, "bad", password="wrong-password"),
                "nowhere": profile(ready, "nowhere", port=1, connect_timeout="2s"),
            },
            "audit": {"enabled": False},
        }
    )
    application = App(config)
    set_app(application)
    try:
        with pytest.raises(ToolError) as wrong:
            await pg_query(connection="bad", sql="SELECT 1")
        assert "Could not reach the database" in str(wrong.value)
        assert "wrong-password" not in str(wrong.value)
        with pytest.raises(ToolError) as refused:
            await pg_query(connection="nowhere", sql="SELECT 1")
        assert "Could not reach the database" in str(refused.value)
    finally:
        await application.close()
        set_app(None)


async def test_statement_timeout_is_enforced(ready: Server) -> None:
    config = Config.model_validate(
        {
            "connections": {"fast": profile(ready, "fast", statement_timeout="200ms")},
            "audit": {"enabled": False},
        }
    )
    application = App(config)
    set_app(application)
    try:
        with pytest.raises(ToolError) as caught:
            await pg_query(
                connection="fast",
                sql="SELECT count(*) FROM generate_series(1, 100000000) a, generate_series(1, 100) b",
            )
        assert "time limit" in str(caught.value)
        # The connection is usable afterwards and still read-only.
        again = await call(
            pg_query, connection="fast", sql="SELECT current_setting('transaction_read_only')"
        )
        assert again["rows"] == [["on"]]
    finally:
        await application.close()
        set_app(None)


async def test_plan_cost_ceiling(ready: Server) -> None:
    config = Config.model_validate(
        {
            "connections": {"cheap": profile(ready, "cheap", max_plan_cost=50)},
            "audit": {"enabled": False},
        }
    )
    application = App(config)
    set_app(application)
    try:
        assert (await call(pg_query, connection="cheap", sql="SELECT 1"))["rows"] == [[1]]
        with pytest.raises(ToolError) as caught:
            await pg_query(
                connection="cheap",
                sql="SELECT count(*) FROM vault.files a JOIN vault.files b ON a.name < b.name",
            )
        assert "plan-cost" in str(caught.value)
    finally:
        await application.close()
        set_app(None)


async def test_session_state_does_not_leak_between_calls(app: Any) -> None:
    """Every call starts from a clean session, whatever the previous one did."""
    for _ in range(3):
        result = await call(
            pg_query,
            connection="ro",
            sql="SELECT current_setting('statement_timeout'), current_setting('transaction_read_only'),"
            " (SELECT count(*) FROM pg_prepared_statements),"
            " (SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid())",
        )
        assert result["rows"] == [["30s", "on", 0, 0]]


async def test_role_switch(ready: Server) -> None:
    """`role` makes every transaction run as a less privileged role."""
    config = Config.model_validate(
        {
            "connections": {"as_ro": profile(ready, "as_ro", role="harbor_ro")},
            "audit": {"enabled": False},
        }
    )
    application = App(config)
    set_app(application)
    try:
        who = await call(pg_query, connection="as_ro", sql="SELECT current_user")
        assert who["rows"] == [["harbor_ro"]]
        with pytest.raises(ToolError) as caught:
            await pg_query(connection="as_ro", sql="SELECT count(*) FROM vault.files")
        assert "ermission denied" in str(caught.value)
    finally:
        await application.close()
        set_app(None)
