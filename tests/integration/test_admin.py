"""Tools that change the database: access modes, confirmation, dry runs."""

import warnings
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from mcp import Client
from mcp.server.elicitation import AcceptedElicitation
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ElicitResult

from pgharbor.app import App, set_app
from pgharbor.approval import NotNeeded
from pgharbor.config import Config
from pgharbor.server import build_server, enabled_tools, load_tool_modules
from pgharbor.tools.admin import pg_execute, pg_import_csv, pg_maintenance, pg_signal_backend
from pgharbor.tools.core import pg_query

from ..conftest import Server, build_config, call, profile, superuser

pytestmark = pytest.mark.integration

# What the framework injects when no confirmation is needed (the test profiles use
# write_confirmation = "none").
OK: Any = AcceptedElicitation[Any].model_construct(data=NotNeeded())
SCHEMA = "scratch_admin"


@pytest.fixture
def scratch(ready: Server) -> Iterator[str]:
    """A private schema with one small table, dropped afterwards."""
    with superuser(ready) as db:
        db.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        db.execute(f"CREATE SCHEMA {SCHEMA}")
        db.execute(f"CREATE TABLE {SCHEMA}.items (id serial PRIMARY KEY, name text, qty int)")
        db.execute(f"INSERT INTO {SCHEMA}.items (name, qty) VALUES ('a', 1), ('b', 2), ('c', 3)")
        db.execute(f"CREATE SEQUENCE {SCHEMA}.seq")
        db.execute(
            f"CREATE MATERIALIZED VIEW {SCHEMA}.totals AS SELECT sum(qty) AS total FROM {SCHEMA}.items"
        )
    try:
        yield SCHEMA
    finally:
        with superuser(ready) as db:
            db.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


async def count(sql: str) -> Any:
    return (await call(pg_query, connection="ro", sql=sql))["rows"][0][0]


async def test_read_only_connection_cannot_write(app: Any, scratch: str) -> None:
    with pytest.raises(ToolError) as caught:
        await call(pg_execute, sql=f"DELETE FROM {scratch}.items", connection="ro", decision=OK)
    assert "access-mode" in str(caught.value)
    assert await count(f"SELECT count(*) FROM {scratch}.items") == 3


async def test_insert_update_delete_with_parameters(app: Any, scratch: str) -> None:
    inserted = await call(
        pg_execute,
        sql=f"INSERT INTO {scratch}.items (name, qty) VALUES ($1, $2) RETURNING id, name",
        params=["d", 4],
        connection="rw",
        decision=OK,
    )
    assert inserted["status"] == "INSERT 0 1"
    assert inserted["committed"] is True
    assert inserted["returning"]["rows"] == [[4, "d"]]
    updated = await call(
        pg_execute,
        sql=f"UPDATE {scratch}.items SET qty = qty * 10 WHERE qty >= $1",
        params=[3],
        connection="rw",
        decision=OK,
    )
    assert updated["status"] == "UPDATE 2"
    assert await count(f"SELECT sum(qty) FROM {scratch}.items") == 1 + 2 + 30 + 40
    cte = await call(
        pg_execute,
        sql=f"WITH gone AS (DELETE FROM {scratch}.items WHERE name = 'a' RETURNING id) "
        f"INSERT INTO {scratch}.items (name, qty) SELECT 'again', id FROM gone",
        connection="rw",
        decision=OK,
    )
    assert cte["status"] == "INSERT 0 1"
    sequence = await call(
        pg_execute,
        sql=f"INSERT INTO {scratch}.items (name, qty) VALUES ('seq', nextval('{scratch}.seq')::int)",
        connection="rw",
        decision=OK,
    )
    assert sequence["status"] == "INSERT 0 1"


async def test_dry_run_changes_nothing(app: Any, scratch: str) -> None:
    result = await call(
        pg_execute,
        sql=f"DELETE FROM {scratch}.items WHERE qty > 1",
        connection="rw",
        decision=OK,
        dry_run=True,
    )
    assert result["status"] == "DELETE 2"
    assert result["committed"] is False
    assert "rolled back" in result["note"]
    assert await count(f"SELECT count(*) FROM {scratch}.items") == 3


async def test_ddl_needs_admin(app: Any, scratch: str, ready: Server) -> None:
    with pytest.raises(ToolError) as caught:
        await call(
            pg_execute, sql=f"CREATE TABLE {scratch}.t2 (a int)", connection="rw", decision=OK
        )
    assert "access-mode" in str(caught.value)
    created = await call(
        pg_execute, sql=f"CREATE TABLE {scratch}.t2 (a int)", connection="admin", decision=OK
    )
    assert created["status"] == "CREATE TABLE"
    # Commands that refuse a transaction block take the autocommit path.
    index = await call(
        pg_execute,
        sql=f"CREATE INDEX CONCURRENTLY t2_a_idx ON {scratch}.t2 (a)",
        connection="admin",
        decision=OK,
    )
    assert index["status"] == "CREATE INDEX"
    vacuum = await call(pg_execute, sql=f"VACUUM {scratch}.t2", connection="admin", decision=OK)
    assert vacuum["status"] == "VACUUM"
    with pytest.raises(ToolError) as dry:
        await call(
            pg_execute, sql=f"VACUUM {scratch}.t2", connection="admin", decision=OK, dry_run=True
        )
    assert "no-dry-run" in str(dry.value)
    assert (
        await count(
            f"SELECT count(*) FROM pg_indexes WHERE schemaname = '{scratch}' AND indexname = 't2_a_idx'"
        )
        == 1
    )
    # The session is back to read-only by default after the autocommit path.
    assert await count("SELECT current_setting('default_transaction_read_only')") == "on"


NEVER = [
    ("BEGIN", "session-command"),
    ("COMMIT", "session-command"),
    ("ROLLBACK", "session-command"),
    ("SET statement_timeout = 0", "session-command"),
    ("RESET ALL", "session-command"),
    ("LOCK TABLE shop.no_pk", "session-command"),
    ("COPY shop.no_pk TO PROGRAM 'id'", "session-command"),
    ("COPY shop.no_pk FROM '/etc/passwd'", "session-command"),
    ("LISTEN x", "session-command"),
    ("DISCARD ALL", "session-command"),
    ("LOAD 'auto_explain'", "session-command"),
    ("SELECT 1", "use-pg-query"),
    ("DELETE FROM shop.no_pk; DROP TABLE shop.no_pk", "multiple-statements"),
    ("INSERT INTO shop.no_pk SELECT pg_read_file('postgresql.conf'), 'x'", "function-denied"),
    ("UPDATE shop.no_pk SET label = pg_sleep(1)::text", "function-denied"),
    (
        "INSERT INTO shop.no_pk SELECT * FROM dblink('x', 'select 1') AS t(a text, b text)",
        "function-denied",
    ),
    ("CREATE TABLE zz AS SELECT lo_export(1, '/tmp/x')", "function-denied"),
    ("INSERT INTO shop.no_pk SELECT rolname, rolpassword FROM pg_authid", "relation-denied"),
]


@pytest.mark.parametrize(("sql", "rule"), NEVER, ids=[s[:50] for s, _ in NEVER])
async def test_admin_still_has_limits(app: Any, sql: str, rule: str) -> None:
    with pytest.raises(ToolError) as caught:
        await call(pg_execute, sql=sql, connection="admin", decision=OK)
    assert rule in str(caught.value)


async def test_parameter_count_is_checked(app: Any, scratch: str) -> None:
    with pytest.raises(ToolError) as caught:
        await call(
            pg_execute,
            sql=f"DELETE FROM {scratch}.items WHERE id = $1",
            connection="rw",
            decision=OK,
        )
    assert "parameter-count" in str(caught.value)


async def test_maintenance(app: Any, scratch: str, ready: Server) -> None:
    analyzed = await call(
        pg_maintenance, action="analyze", target=f"{scratch}.items", connection="admin", decision=OK
    )
    assert analyzed["executed"] == f'ANALYZE "{scratch}"."items"'
    vacuumed = await call(
        pg_maintenance,
        action="vacuum_analyze",
        target=f"{scratch}.items",
        connection="admin",
        decision=OK,
    )
    assert vacuumed["executed"].startswith("VACUUM (ANALYZE)")
    reindexed = await call(
        pg_maintenance,
        action="reindex_index",
        target=f"{scratch}.items_pkey",
        connection="admin",
        decision=OK,
        concurrently=ready.at_least(12),
    )
    assert "REINDEX INDEX" in reindexed["executed"]
    with superuser(ready) as db:
        db.execute(f"INSERT INTO {scratch}.items (name, qty) VALUES ('z', 100)")
    refreshed = await call(
        pg_maintenance,
        action="refresh_materialized_view",
        target=f"{scratch}.totals",
        connection="admin",
        decision=OK,
    )
    assert "REFRESH MATERIALIZED VIEW" in refreshed["executed"]
    assert await count(f"SELECT total FROM {scratch}.totals") == 106
    reset = await call(
        pg_maintenance,
        action="reset_statistics",
        target=f"{scratch}.items",
        connection="admin",
        decision=OK,
    )
    assert "pg_stat_reset_single_table_counters" in reset["executed"]
    with pytest.raises(ToolError) as missing:
        await call(
            pg_maintenance,
            action="vacuum",
            target=f"{scratch}.nope",
            connection="admin",
            decision=OK,
        )
    assert "No table named" in str(missing.value)
    with pytest.raises(ToolError):
        await call(
            pg_maintenance,
            action="analyze",
            target=f"{scratch}.items",
            connection="rw",
            decision=OK,
        )


async def test_signal_backend(app: Any, ready: Server) -> None:
    victim = superuser(ready)
    try:
        pid = victim.info.backend_pid
        result = await call(
            pg_signal_backend, pid=pid, action="terminate", connection="admin", decision=OK
        )
        assert result["signal_sent"] is True
        assert result["session"]["pid"] == pid
        with pytest.raises(psycopg.Error):
            victim.execute("SELECT 1")
    finally:
        victim.close()
    with pytest.raises(ToolError) as caught:
        await call(pg_signal_backend, pid=2_000_000_000, connection="admin", decision=OK)
    assert "no session" in str(caught.value)


async def test_import_csv(ready: Server, scratch: str, tmp_path: Any) -> None:
    connections = {
        "rw": profile(
            ready, "rw", access="read-write", write_confirmation="none", import_dir=str(tmp_path)
        ),
        "ro": profile(ready, "ro"),
    }
    application = App(
        Config.model_validate({"connections": connections, "audit": {"enabled": False}})
    )
    set_app(application)
    try:
        (tmp_path / "items.csv").write_text('name,qty\nx,10\ny,20\n"with, comma",30\n')
        dry = await call(
            pg_import_csv,
            table=f"{scratch}.items",
            filename="items.csv",
            connection="rw",
            columns=["name", "qty"],
            decision=OK,
            dry_run=True,
        )
        assert dry["rows_loaded"] == 3
        assert dry["committed"] is False
        assert await count(f"SELECT count(*) FROM {scratch}.items") == 3
        real = await call(
            pg_import_csv,
            table=f"{scratch}.items",
            filename="items.csv",
            connection="rw",
            columns=["name", "qty"],
            decision=OK,
        )
        assert real["rows_loaded"] == 3
        assert await count(f"SELECT qty FROM {scratch}.items WHERE name = 'with, comma'") == 30
        for bad in ("../items.csv", "/etc/passwd", "missing.csv", ""):
            with pytest.raises(ToolError):
                await call(
                    pg_import_csv,
                    table=f"{scratch}.items",
                    filename=bad,
                    connection="rw",
                    decision=OK,
                )
        with pytest.raises(ToolError) as unknown:
            await call(
                pg_import_csv,
                table=f"{scratch}.items",
                filename="items.csv",
                connection="rw",
                columns=["name", "nope"],
                decision=OK,
            )
        assert "Unknown column" in str(unknown.value)
    finally:
        await application.close()
        set_app(None)


def test_write_tools_are_hidden_from_read_only_setups(ready: Server) -> None:
    load_tool_modules()
    read_only = Config.model_validate({"connections": {"a": profile(ready, "a")}})
    names = {spec.name for spec in enabled_tools(read_only)}
    assert "pg_query" in names
    assert not {"pg_execute", "pg_maintenance", "pg_signal_backend", "pg_import_csv"} & names
    writer = Config.model_validate({"connections": {"a": profile(ready, "a", access="read-write")}})
    names = {spec.name for spec in enabled_tools(writer)}
    assert {"pg_execute", "pg_import_csv"} <= names
    assert not {"pg_maintenance", "pg_signal_backend"} & names


# ------------------------------------------------------------------ confirmation dialog


def _text(result: Any) -> str:
    return " ".join(getattr(block, "text", "") for block in result.content)


async def _accept(*_args: Any, **_kwargs: Any) -> ElicitResult:
    return ElicitResult(action="accept", content={"approve": True})


async def _accept_unticked(*_args: Any, **_kwargs: Any) -> ElicitResult:
    return ElicitResult(action="accept", content={"approve": False})


async def _decline(*_args: Any, **_kwargs: Any) -> ElicitResult:
    return ElicitResult(action="decline")


@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_writes_are_confirmed_by_the_user(ready: Server, scratch: str, mode: str) -> None:
    """Through a real MCP client, on both protocol generations."""
    warnings.simplefilter("ignore")
    config = build_config(ready)
    connections = {name: c.model_dump() for name, c in config.connections.items()}
    connections["rw"]["write_confirmation"] = "dialog"
    connections["admin"]["write_confirmation"] = "conversation"
    server = build_server(
        Config.model_validate({"connections": connections, "audit": {"enabled": False}})
    )
    delete = {"sql": f"DELETE FROM {scratch}.items WHERE name = 'a'", "connection": "rw"}
    try:
        async with Client(server, mode=mode, elicitation_callback=_decline) as client:
            refused = await client.call_tool("pg_execute", delete)
            assert refused.is_error
            assert "not-approved" in _text(refused)
        async with Client(server, mode=mode, elicitation_callback=_accept_unticked) as client:
            refused = await client.call_tool("pg_execute", delete)
            assert refused.is_error
        async with Client(server, mode=mode) as client:  # a client with no dialogs
            refused = await client.call_tool("pg_execute", delete)
            assert refused.is_error
            assert "no-dialog" in _text(refused)
            dry = await client.call_tool("pg_execute", {**delete, "dry_run": True})
            assert not dry.is_error
            assert "DELETE 1" in _text(dry)
            # "conversation" mode: the agent must state that the user said yes.
            create = {"sql": f"CREATE TABLE {scratch}.t3 (a int)", "connection": "admin"}
            refused = await client.call_tool("pg_execute", create)
            assert refused.is_error
            assert "needs-user-request" in _text(refused)
            done = await client.call_tool("pg_execute", {**create, "confirmed_by_user": True})
            assert not done.is_error
        with superuser(ready) as db:
            assert db.execute(f"SELECT count(*) FROM {scratch}.items").fetchone() == (3,)
        async with Client(server, mode=mode, elicitation_callback=_accept) as client:
            listed = {tool.name: tool for tool in (await client.list_tools()).tools}
            schema = listed["pg_execute"].input_schema
            assert "decision" not in schema["properties"]
            done = await client.call_tool("pg_execute", delete)
            assert not done.is_error
            assert '"confirmed":"dialog"' in _text(done)
        with superuser(ready) as db:
            assert db.execute(f"SELECT count(*) FROM {scratch}.items").fetchone() == (2,)
    finally:
        set_app(None)
