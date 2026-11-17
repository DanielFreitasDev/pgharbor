"""Proof of the premise in docs/security.md: a read-only transaction is not a boundary.

Every test here talks to PostgreSQL directly, without pgharbor, the way a tool that only
wraps the agent's SQL in ``BEGIN TRANSACTION READ ONLY`` would. Each one shows something
that such a tool lets through. The matching refusals by pgharbor are in ``test_guard.py``.
"""

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from ..conftest import Server, superuser

BEGIN = "BEGIN TRANSACTION READ ONLY"


@pytest.fixture
def direct(ready: Server) -> Iterator[psycopg.Connection[Any]]:
    connection = superuser(ready)
    try:
        yield connection
    finally:
        connection.close()


def one(connection: psycopg.Connection[Any], sql: str) -> Any:
    row = connection.execute(sql).fetchone()
    assert row is not None
    return row[0]


def test_the_transaction_can_be_made_read_write(direct: psycopg.Connection[Any]) -> None:
    direct.execute(BEGIN)
    assert one(direct, "SHOW transaction_read_only") == "on"
    direct.execute("ROLLBACK")

    direct.execute(BEGIN)
    direct.execute("SET transaction_read_only = off")  # allowed before the first query
    direct.execute("CREATE TABLE public.zz_not_read_only (a int)")  # DDL, accepted
    direct.execute("INSERT INTO public.zz_not_read_only VALUES (1)")
    assert one(direct, "SELECT count(*) FROM public.zz_not_read_only") == 1
    direct.execute("ROLLBACK")  # only this rollback keeps the database clean


def test_several_statements_in_one_string_leave_the_transaction(
    direct: psycopg.Connection[Any],
) -> None:
    direct.execute("DROP TABLE IF EXISTS public.zz_escaped")
    direct.execute(BEGIN)
    # No bound parameters: the driver uses the simple protocol, which accepts a script.
    direct.execute("COMMIT; CREATE TABLE public.zz_escaped (a int)")
    try:
        assert one(direct, "SELECT to_regclass('public.zz_escaped') IS NOT NULL") is True
    finally:
        direct.execute("DROP TABLE IF EXISTS public.zz_escaped")


def test_the_time_limit_can_be_removed(direct: psycopg.Connection[Any]) -> None:
    direct.execute(BEGIN)
    direct.execute("SET LOCAL statement_timeout = 1000")
    direct.execute("SELECT set_config('statement_timeout', '0', false)")
    assert one(direct, "SHOW statement_timeout") == "0"
    direct.execute("ROLLBACK")


def test_an_advisory_lock_survives_the_rollback(direct: psycopg.Connection[Any]) -> None:
    held = (
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
        "AND pid = pg_backend_pid() AND objid = 424242"
    )
    direct.execute(BEGIN)
    direct.execute("SELECT pg_advisory_lock(424242)")
    direct.execute("ROLLBACK")
    try:
        assert one(direct, held) == 1
    finally:
        direct.execute("SELECT pg_advisory_unlock_all()")
    assert one(direct, held) == 0


def test_a_table_can_be_locked_against_everyone(direct: psycopg.Connection[Any]) -> None:
    direct.execute(BEGIN)
    direct.execute("LOCK TABLE trap.side_effects IN ACCESS EXCLUSIVE MODE")
    mode = one(
        direct,
        "SELECT mode FROM pg_locks WHERE pid = pg_backend_pid() AND locktype = 'relation' "
        "AND relation = 'trap.side_effects'::regclass AND granted",
    )
    direct.execute("ROLLBACK")
    assert mode == "AccessExclusiveLock"


def test_another_session_can_be_killed(direct: psycopg.Connection[Any], ready: Server) -> None:
    victim = superuser(ready)
    try:
        pid = victim.info.backend_pid
        direct.execute(BEGIN)
        assert one(direct, f"SELECT pg_terminate_backend({pid})") is True
        direct.execute("ROLLBACK")
        with pytest.raises(psycopg.OperationalError):
            victim.execute("SELECT pg_sleep(5)")  # dies as soon as the signal arrives
    finally:
        victim.close()


def test_files_of_the_host_can_be_read(direct: psycopg.Connection[Any], ready: Server) -> None:
    direct.execute(BEGIN)
    content = one(direct, "SELECT pg_read_file('PG_VERSION')")
    direct.execute("ROLLBACK")
    assert content.strip() == ready.label.split(".")[0] or content.strip() == ready.label


def test_a_program_can_be_run_on_the_host(direct: psycopg.Connection[Any]) -> None:
    direct.execute(BEGIN)
    # A harmless program; the point is that the server ran it at all.
    direct.execute("COPY (SELECT 1) TO PROGRAM 'cat > /dev/null'")
    direct.execute("ROLLBACK")
