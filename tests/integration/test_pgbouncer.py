"""pgharbor behind PgBouncer in transaction pooling mode.

A pooler in transaction mode gives every transaction a possibly different server
connection and refuses the "options" start-up parameter. pgharbor must notice, apply its
settings inside each transaction instead, and keep every guarantee.

Skipped unless PgBouncer is running: scripts/pg-matrix.sh up bouncer
"""

import socket
from collections.abc import AsyncIterator

import psycopg
import pytest

from pgharbor.config import Config
from pgharbor.errors import Rejected
from pgharbor.runner import run_query
from pgharbor.session import Sessions

from .. import fixture
from ..conftest import HOST, PASSWORD, Server, ensure_fixture

pytestmark = pytest.mark.integration
PORT = 55432


@pytest.fixture
async def pooled() -> AsyncIterator[Sessions]:
    try:
        with socket.create_connection((HOST, PORT), timeout=0.5):
            pass
    except OSError:
        pytest.skip("PgBouncer is not running; run scripts/pg-matrix.sh up bouncer")
    ensure_fixture(Server("18", 55418))
    config = Config.model_validate(
        {
            "connections": {
                "pooled": {
                    "name": "pooled", "host": HOST, "port": PORT, "dbname": fixture.DATABASE,
                    "user": "postgres", "password": PASSWORD, "statement_timeout": "7s",
                }
            }
        }
    )  # fmt: skip
    sessions = Sessions(config)
    try:
        yield sessions
    finally:
        await sessions.close()


async def test_settings_apply_in_every_transaction(pooled: Sessions) -> None:
    for _ in range(5):
        async with pooled.transaction("pooled") as db:
            result = await run_query(
                db,
                "SELECT current_setting('transaction_read_only'),"
                " current_setting('statement_timeout'),"
                " current_setting('standard_conforming_strings'), count(*) FROM shop.no_pk",
            )
            assert result.rows == [["on", "7s", "on", 2]]
    # The pooler refused the start-up options, and pgharbor adapted.
    assert pooled._pools["pooled"].use_startup_options is False


@pytest.mark.parametrize(
    ("sql", "rule"),
    [
        ("SELECT 1; SELECT 2", "multiple-statements"),
        ("COMMIT; DROP TABLE shop.no_pk", "multiple-statements"),
        ("DELETE FROM shop.no_pk", "not-a-read-query"),
        ("SELECT pg_sleep(1)", "function-denied"),
        ("SET statement_timeout = 0", "not-a-read-query"),
        ("SELECT code FROM shop.no_pk FOR UPDATE", "read-only-transaction"),
    ],
)
async def test_refusals_hold_behind_the_pooler(pooled: Sessions, sql: str, rule: str) -> None:
    with pytest.raises(Rejected) as caught:
        async with pooled.transaction("pooled") as db:
            await run_query(db, sql)
    assert caught.value.rule == rule


async def test_write_through_the_pooler_is_refused_by_the_server(pooled: Sessions) -> None:
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        async with pooled.transaction("pooled") as db:
            await db.rows("CREATE TABLE shop.should_not_exist (a int)")
    async with pooled.transaction("pooled") as db:
        assert await db.value("SELECT to_regclass('shop.should_not_exist')") is None
