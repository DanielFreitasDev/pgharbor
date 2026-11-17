"""pg_replication on every PostgreSQL version of the matrix."""

import re
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

from pgharbor.tools.replication import _summary, pg_replication

from ..conftest import Server, call, rows_as_dicts, superuser

pytestmark = pytest.mark.integration

SCHEMA = "pgh_c_rep"
PHYSICAL = "pgh_c_phys"
LOGICAL = "pgh_c_logical"
PUBLICATION = "pgh_c_pub"
SUBSCRIPTION = "pgh_c_sub"
# A password that must never come back, whatever the tool prints.
FAKE_PASSWORD = "hunter2-pgh-c"


def _cleanup(conn: psycopg.Connection[Any], v: int) -> None:
    if v >= 100000:
        left = conn.execute(
            "SELECT 1 FROM pg_subscription WHERE subname = %s AND subdbid = "
            "(SELECT oid FROM pg_database WHERE datname = current_database())",
            [SUBSCRIPTION],
        ).fetchone()
        if left:
            # Without this the DROP tries to remove a slot on the (unreachable) publisher.
            conn.execute(f"ALTER SUBSCRIPTION {SUBSCRIPTION} DISABLE")
            conn.execute(f"ALTER SUBSCRIPTION {SUBSCRIPTION} SET (slot_name = NONE)")
            conn.execute(f"DROP SUBSCRIPTION {SUBSCRIPTION}")
        conn.execute(f"DROP PUBLICATION IF EXISTS {PUBLICATION}")
    for slot in (PHYSICAL, LOGICAL):
        conn.execute(
            "SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots "
            "WHERE slot_name = %s AND NOT active",
            [slot],
        )
    conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


@pytest.fixture
def replication_objects(ready: Server) -> Iterator[dict[str, bool]]:
    """A physical slot, a logical slot, a publication and (10+) a disabled subscription."""
    created = {"logical": False}
    with superuser(ready) as conn:
        v = conn.info.server_version
        _cleanup(conn, v)
        try:
            conn.execute(f"CREATE SCHEMA {SCHEMA}")
            conn.execute(f"CREATE TABLE {SCHEMA}.no_pk (code text, label text)")
            conn.execute(f"CREATE TABLE {SCHEMA}.with_pk (id int PRIMARY KEY, label text)")
            conn.execute("SELECT pg_create_physical_replication_slot(%s, true)", [PHYSICAL])
            try:
                conn.execute(
                    "SELECT pg_create_logical_replication_slot(%s, 'test_decoding')", [LOGICAL]
                )
                created["logical"] = True
            except psycopg.Error:
                pass  # test_decoding is not part of every image
            if v >= 100000:
                conn.execute(
                    f"CREATE PUBLICATION {PUBLICATION} FOR TABLE {SCHEMA}.no_pk, {SCHEMA}.with_pk"
                )
                # connect = false records the subscription without reaching any server, so
                # the connection string (with a password) can be checked for leaks.
                conn.execute(
                    f"CREATE SUBSCRIPTION {SUBSCRIPTION} CONNECTION "
                    f"'host=127.0.0.1 port=5432 dbname=pgharbor_test user=postgres "
                    f"password={FAKE_PASSWORD}' PUBLICATION {PUBLICATION} WITH (connect = false, slot_name = NONE)"
                )
            yield created
        finally:
            _cleanup(conn, v)


def _by_name(table: dict[str, Any], key: str = "name") -> dict[str, dict[str, Any]]:
    return {row[key]: row for row in rows_as_dicts(table)}


async def test_primary_without_replication_objects_reports_role_and_settings(
    app: Any, ready: Server
) -> None:
    result = await call(pg_replication, connection="ro")
    assert result["role"] == "primary"
    assert result["summary"].startswith("Primary, ")
    assert result["settings"]["wal_level"] == "logical"
    assert result["settings"]["max_wal_senders"] == "4"
    assert result["settings"]["max_replication_slots"] == "4"
    assert "synchronous_standby_names" in result["settings"]
    assert "hot_standby" in result["settings"]
    # Settings that exist on one side of a version boundary only.
    if ready.at_least(13):
        assert "wal_keep_size" in result["settings"]
        assert "wal_keep_segments" not in result["settings"]
    else:
        assert "wal_keep_segments" in result["settings"]
    # Nothing is hidden behind an empty list: a standby section appears only when it has rows.
    assert "standby_of" not in result
    if not ready.at_least(10):
        assert any("PostgreSQL 10" in note for note in result["notes"])


async def test_slots_publications_and_subscriptions_are_listed(
    app: Any, ready: Server, replication_objects: dict[str, bool]
) -> None:
    result = await call(pg_replication, connection="ro")
    assert "slot(s)" in result["summary"]

    slots = _by_name(result["slots"])
    physical = slots[PHYSICAL]
    assert physical["type"] == "physical"
    assert physical["active"] is False
    assert physical["restart_lsn"]
    assert physical["wal_retained"]
    assert any(PHYSICAL in w and "inactive" in w for w in result["warnings"])
    if ready.at_least(10):
        assert physical["temporary"] is False
    if ready.at_least(13):
        assert physical["wal_status"] == "reserved"
    else:
        assert "wal_status" not in result["slots"]["columns"]
    if ready.at_least(17):
        assert "inactive_since" in result["slots"]["columns"]
        assert physical["failover"] is False
    else:
        assert "inactive_since" not in result["slots"]["columns"]
    if replication_objects["logical"]:
        logical = slots[LOGICAL]
        assert logical["type"] == "logical"
        assert logical["plugin"] == "test_decoding"
        assert logical["database"] == "pgharbor_test"

    if not ready.at_least(10):
        assert "publications" not in result
        assert "subscriptions" not in result
        return

    publication = {p["name"]: p for p in result["publications"]}[PUBLICATION]
    assert publication["all_tables"] is False
    assert publication["table_count"] == 2
    assert publication["tables"] == [f"{SCHEMA}.no_pk", f"{SCHEMA}.with_pk"]
    assert {"insert", "update", "delete"} <= set(publication["publishes"].split(","))
    assert ("truncate" in publication["publishes"]) == ready.at_least(11)
    assert any(f"{SCHEMA}.no_pk" in w and "no primary key" in w for w in result["warnings"])
    assert not any(f"{SCHEMA}.with_pk" in w for w in result["warnings"])

    subscription = {s["name"]: s for s in result["subscriptions"]}[SUBSCRIPTION]
    assert subscription["enabled"] is False
    assert subscription["publications"] == [PUBLICATION]
    assert subscription["owner"] == "postgres"
    assert any(SUBSCRIPTION in w and "disabled" in w for w in result["warnings"])
    if ready.at_least(14):
        assert subscription["binary"] is False
        assert subscription["streaming"] == ("parallel" if ready.at_least(18) else "off")
    if ready.at_least(15):
        assert subscription["two_phase"] == "disabled"
        assert subscription["apply_errors"] == 0
        assert subscription["sync_errors"] == 0
    if ready.at_least(16):
        assert subscription["origin"] == "any"
        assert subscription["password_required"] is True
    if ready.at_least(17):
        assert subscription["failover"] is False
    if ready.at_least(18):
        assert subscription["conflicts"] == 0


async def test_connection_strings_and_hashes_never_leave(
    app: Any, ready: Server, replication_objects: dict[str, bool]
) -> None:
    for profile in ("ro", "limited"):
        text = await pg_replication(connection=profile)
        assert FAKE_PASSWORD not in text
        assert "password=" not in text
        assert "conninfo" not in text
        assert "SCRAM-SHA-256$" not in text
        assert not re.search(r"md5[0-9a-f]{32}", text)


async def test_unprivileged_role_gets_an_answer_not_an_error(
    app: Any, ready: Server, replication_objects: dict[str, bool]
) -> None:
    result = await call(pg_replication, connection="limited")
    assert result["role"] == "primary"
    assert PHYSICAL in _by_name(result["slots"])
    if ready.at_least(10):
        assert PUBLICATION in {p["name"] for p in result["publications"]}
        # pg_subscription is readable for the columns that carry no secret.
        assert SUBSCRIPTION in {s["name"] for s in result["subscriptions"]}


def _stub(v: int) -> Any:
    return SimpleNamespace(v=v)


def test_summary_says_plainly_when_nothing_replicates() -> None:
    text = _summary(_stub(170000), False, None, None, [], [], None, {"wal_level": "replica"})
    assert text.startswith("Primary, 0 standby(s) connected, 0 replication slot(s)")
    assert "Replication is not in use on this server." in text
    minimal = _summary(_stub(170000), False, None, None, [], [], None, {"wal_level": "minimal"})
    assert "wal_level=minimal" in minimal
    no_senders = _summary(_stub(90624), False, None, None, [], [], None, {"max_wal_senders": "0"})
    assert "max_wal_senders=0" in no_senders
    assert "publication" not in no_senders  # logical replication does not exist before 10


def test_summary_of_a_standby_without_receiver_explains_it() -> None:
    text = _summary(_stub(160000), True, None, None, [], [], None, {})
    assert text.startswith("Standby (in recovery)")
    assert "No WAL receiver is running" in text
    assert "not in use" not in text
