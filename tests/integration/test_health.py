"""Health tools on every PostgreSQL version of the matrix."""

import asyncio
import re
import time
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from pgharbor.tools import health
from pgharbor.tools.health import (
    checkpoint_reading,
    pg_health_check,
    pg_index_advisor,
    pg_sequences_health,
    pg_settings,
    pg_wal_checkpoints,
)

from ..conftest import Server, call, rows_as_dicts, superuser
from .test_performance import WORK, blocked_pair, by_name, scratch_schema, session
from .test_performance import bare_app as bare_db_app  # noqa: F401  (fixture)
from .test_performance import workload as perf_workload  # noqa: F401  (fixture)

pytestmark = pytest.mark.integration

CHECKS = [
    "autovacuum",
    "blocked_sessions",
    "cache_hit_ratio",
    "checkpoints",
    "connections",
    "dead_tuples",
    "deadlocks",
    "duplicate_indexes",
    "durability_settings",
    "idle_in_transaction",
    "invalid_indexes",
    "long_transactions",
    "never_analyzed",
    "prepared_transactions",
    "replication_slots",
    "rollback_ratio",
    "sequences",
    "settings_pending_restart",
    "tables_without_primary_key",
    "temporary_files",
    "wal_archiving",
    "xid_wraparound",
]
SEVERITIES = {"critical", "warning", "info", "ok", "not_available"}


def findings(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return by_name(result, "check")


# ------------------------------------------------------------------------- pg_health_check


async def test_health_check_runs_every_check(app: Any) -> None:
    result = await call(pg_health_check, connection="ro")
    assert result["database"] == "pgharbor_test"
    assert result["columns"] == ["check", "severity", "value", "threshold", "message", "see"]
    found = findings(result)
    assert sorted(found) == CHECKS
    assert {f["severity"] for f in found.values()} <= SEVERITIES
    assert sum(result["summary"].values()) == len(CHECKS)
    assert result["summary"]["not_available"] == 0, [
        f for f in found.values() if f["severity"] == "not_available"
    ]
    assert all(f["see"] and f["message"] for f in found.values())
    # Ranked: critical first, ok last.
    order = ["critical", "warning", "not_available", "info", "ok"]
    ranks = [order.index(f["severity"]) for f in rows_as_dicts(result)]
    assert ranks == sorted(ranks)
    assert result["status"] == "critical"


async def test_health_check_reports_the_sample_databases_problems(app: Any, ready: Server) -> None:
    found = findings(await call(pg_health_check, connection="ro"))

    durability = found["durability_settings"]
    assert durability["severity"] == "critical"  # the test servers run with fsync=off
    assert durability["value"] == ["fsync"]
    assert "fsync is off" in durability["message"]

    sequences = found["sequences"]
    assert sequences["severity"] == "critical"  # shop.tiny_seq is at 95 of 100
    assert sequences["value"] >= 90
    assert "shop.tiny_seq" in sequences["message"]
    assert sequences["see"] == "pg_sequences_health"

    duplicates = found["duplicate_indexes"]
    assert duplicates["severity"] == "warning"
    assert duplicates["value"] >= 1
    assert "shop.orders_customer" in duplicates["message"]

    no_pk = found["tables_without_primary_key"]
    assert no_pk["severity"] == "info"
    assert no_pk["value"] >= 3  # shop.no_pk, events.log and public._fixture at least
    assert "without a primary key" in no_pk["message"]

    assert found["xid_wraparound"]["severity"] == "ok"
    assert found["xid_wraparound"]["value"] < 1
    assert found["connections"]["severity"] == "ok"
    assert found["settings_pending_restart"]["severity"] == "ok"
    assert found["wal_archiving"]["severity"] == "ok"
    assert found["autovacuum"]["see"] == "pg_vacuum_status"
    for name, row in found.items():
        assert row["threshold"], name


@pytest.mark.usefixtures("perf_workload")
async def test_health_check_sees_problems_created_by_a_test(app: Any, ready: Server) -> None:
    found = findings(await call(pg_health_check, connection="ro"))
    assert found["invalid_indexes"]["severity"] == "warning"
    assert f"{WORK}.inv_bad_idx" in found["invalid_indexes"]["message"]
    assert found["dead_tuples"]["severity"] in ("warning", "critical")
    assert f"{WORK}.bloaty" in found["dead_tuples"]["message"]
    assert found["never_analyzed"]["severity"] in ("warning", "info")
    assert found["never_analyzed"]["value"] >= 1
    assert found["autovacuum"]["severity"] == "warning"  # autovacuum_enabled = false on a table
    assert f"{WORK}.bloaty" in found["autovacuum"]["message"]
    assert found["duplicate_indexes"]["value"] >= 2
    assert f"{WORK}.bloaty_id_pad_idx" in found["duplicate_indexes"]["message"]
    # A sequence that cycles wraps around instead of failing, so it is not a risk.
    assert "cyc" not in found["sequences"]["message"].replace("cycl", "")


async def test_health_check_flags_sessions_and_waits(
    app: Any, ready: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health, "_IDLE_IN_TRANSACTION_SECONDS", (0.5, 3600))
    monkeypatch.setattr(health, "_LONG_TRANSACTION_SECONDS", (0.5, 3600))
    monkeypatch.setattr(health, "_BLOCKED_SECONDS", (0.5, 3600))
    with session(ready, "pgh_health_idle") as idle:
        idle.execute("BEGIN")
        idle.execute("SELECT 1")
        quiet = findings(await call(pg_health_check, connection="ro"))
        assert quiet["idle_in_transaction"]["severity"] in ("info", "warning")
        assert quiet["idle_in_transaction"]["value"] >= 1
        await asyncio.sleep(1.0)
        found = findings(await call(pg_health_check, connection="ro"))
        assert found["idle_in_transaction"]["severity"] == "warning"
        assert str(idle.info.backend_pid) in found["idle_in_transaction"]["message"]
        assert found["long_transactions"]["severity"] == "warning"
        assert found["long_transactions"]["value"] >= 1
        idle.execute("ROLLBACK")

    with blocked_pair(ready, "pgh_health_lock"):
        await asyncio.sleep(1.0)
        found = findings(await call(pg_health_check, connection="ro"))
        assert found["blocked_sessions"]["severity"] == "warning"
        assert found["blocked_sessions"]["value"] >= 1
        assert found["blocked_sessions"]["see"] == "pg_locks"


async def test_health_check_flags_an_inactive_replication_slot(app: Any, ready: Server) -> None:
    with superuser(ready) as admin:
        admin.execute("SELECT pg_create_physical_replication_slot('pgh_health_slot')")
        try:
            found = findings(await call(pg_health_check, connection="ro"))
            slots = found["replication_slots"]
            assert slots["severity"] in ("warning", "critical")
            assert "pgh_health_slot" in slots["message"]
            assert slots["value"] >= 1
        finally:
            admin.execute("SELECT pg_drop_replication_slot('pgh_health_slot')")


async def test_one_failing_check_does_not_stop_the_others(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def breaks_in_sql(db: Any) -> dict[str, Any]:
        await db.rows("SELECT 1 / 0")
        return {}

    async def missing_relation(db: Any) -> dict[str, Any]:
        await db.rows("SELECT * FROM pg_catalog.pg_view_that_does_not_exist")
        return {}

    async def breaks_in_python(db: Any) -> dict[str, Any]:
        raise RuntimeError("check exploded")

    extra = (
        ("broken_sql", breaks_in_sql, "pg_activity"),
        ("broken_relation", missing_relation, "pg_stat_view"),
        ("broken_python", breaks_in_python, "pg_settings"),
    )
    monkeypatch.setattr(health, "_CHECKS", (*extra, *health._CHECKS))
    result = await call(pg_health_check, connection="ro")
    found = findings(result)
    for name in ("broken_sql", "broken_relation", "broken_python"):
        assert found[name]["severity"] == "not_available"
        assert found[name]["message"].startswith("Not available")
    assert "division by zero" in found["broken_sql"]["message"]
    assert "pg_view_that_does_not_exist" in found["broken_relation"]["message"]
    assert "check exploded" in found["broken_python"]["message"]
    assert found["broken_sql"]["see"] == "pg_activity"
    # Every real check still ran after the broken ones.
    assert {c for c in CHECKS if found[c]["severity"] != "not_available"} == set(CHECKS)
    assert result["summary"]["not_available"] == 3


async def test_health_check_with_an_unprivileged_role(app: Any) -> None:
    result = await call(pg_health_check, connection="limited")
    found = findings(result)
    assert sorted(found) == CHECKS
    assert found["connections"]["severity"] in ("ok", "warning", "critical")
    assert {f["severity"] for f in found.values()} <= SEVERITIES


@pytest.mark.usefixtures("bare_db_app")
async def test_health_check_on_an_empty_database(ready: Server) -> None:
    result = await call(pg_health_check, connection="bare")
    found = findings(result)
    assert found["duplicate_indexes"]["severity"] == "ok"
    assert found["invalid_indexes"]["severity"] == "ok"
    assert found["tables_without_primary_key"]["severity"] == "ok"
    assert found["sequences"]["severity"] == "ok"
    assert result["summary"]["not_available"] == 0

    # Known problems in a database of its own, so that the messages can be exact.
    with superuser(ready, "pgh_perf_bare") as conn:
        conn.execute("CREATE TABLE public.keyless (x int)")
        conn.execute("CREATE TABLE public.keyed (id int PRIMARY KEY, v int)")
        conn.execute("CREATE INDEX keyed_v_one ON public.keyed (v)")
        conn.execute("CREATE INDEX keyed_v_two ON public.keyed (v)")
    found = findings(await call(pg_health_check, connection="bare"))
    assert found["tables_without_primary_key"]["value"] == 1
    assert "public.keyless" in found["tables_without_primary_key"]["message"]
    assert "public.keyed" not in found["tables_without_primary_key"]["message"]
    assert found["duplicate_indexes"]["value"] == 1
    assert "public.keyed_v_one (same as keyed_v_two)" in found["duplicate_indexes"]["message"] or (
        "public.keyed_v_two (same as keyed_v_one)" in found["duplicate_indexes"]["message"]
    )


# ------------------------------------------------------------------------ pg_index_advisor


async def test_advisor_finds_foreign_keys_without_index(app: Any, ready: Server) -> None:
    result = await call(pg_index_advisor, connection="ro")
    keys = {
        (r["table"], r["constraint"]): r
        for r in rows_as_dicts(result["foreign_keys_without_index"])
    }
    reviews = keys[("reviews", "reviews_product_id_fkey")]
    assert reviews["schema"] == "shop"
    assert reviews["columns"] == "product_id"
    assert reviews["references"] == "shop.products"
    assert reviews["suggestion"] == "CREATE INDEX ON shop.reviews (product_id)"
    assert reviews["rows_estimate"] >= 500
    assert ("reviews", "reviews_customer_id_fkey") in keys
    assert (
        "order_items",
        "order_items_product_id_fkey",
    ) in keys  # the primary key starts with order_id
    assert ("orders", "orders_customer_id_fkey") not in keys  # orders_customer_idx supports it
    assert (
        "order_items",
        "order_items_order_id_fkey",
    ) not in keys  # first column of the primary key
    assert (("invoices", "invoices_order_id_fkey") in keys) == ready.at_least(10)
    sizes = [r["rows_estimate"] or 0 for r in rows_as_dicts(result["foreign_keys_without_index"])]
    assert sizes == sorted(sizes, reverse=True)

    only = await call(pg_index_advisor, connection="ro", table="shop.reviews")
    assert {r["table"] for r in rows_as_dicts(only["foreign_keys_without_index"])} == {"reviews"}
    pattern = await call(pg_index_advisor, connection="ro", table="rev*")
    assert len(pattern["foreign_keys_without_index"]["rows"]) == 2
    one = await call(pg_index_advisor, connection="ro", table="reviews", limit=1)
    assert len(one["foreign_keys_without_index"]["rows"]) == 1
    assert one["foreign_keys_without_index"]["has_more"] is True


async def test_advisor_understands_multi_column_and_quoted_keys(app: Any, ready: Server) -> None:
    schema = "pgh_health_fk"
    with scratch_schema(ready, schema), superuser(ready) as conn:
        conn.execute(f"CREATE TABLE {schema}.p (a int, b int, PRIMARY KEY (a, b))")
        conn.execute(
            f'CREATE TABLE {schema}."Odd Name" (a int, "Col X" int, '
            f'FOREIGN KEY (a, "Col X") REFERENCES {schema}.p (a, b))'
        )

        # A name with a space cannot be written bare in the filter: use a pattern.
        result = await call(pg_index_advisor, connection="ro", table=f"{schema}.Odd*")
        listed = rows_as_dicts(result["foreign_keys_without_index"])
        assert [r["suggestion"] for r in listed] == [
            f'CREATE INDEX ON {schema}."Odd Name" (a, "Col X")'
        ]
        assert listed[0]["columns"] == "a, Col X"

        conn.execute(f'CREATE INDEX ON {schema}."Odd Name" (a)')  # a prefix is not enough
        result = await call(pg_index_advisor, connection="ro", table=f"{schema}.Odd*")
        assert len(result["foreign_keys_without_index"]["rows"]) == 1

        conn.execute(f'CREATE INDEX ON {schema}."Odd Name" ("Col X", a)')  # same set, other order
        result = await call(pg_index_advisor, connection="ro", table=f"{schema}.Odd*")
        assert result["foreign_keys_without_index"]["rows"] == []


@pytest.mark.usefixtures("perf_workload")
async def test_advisor_finds_tables_read_by_sequential_scans(app: Any) -> None:
    result = await call(pg_index_advisor, connection="ro", table=f"{WORK}.scanme")
    scans = rows_as_dicts(result["large_tables_read_by_sequential_scans"])
    assert [(r["schema"], r["table"]) for r in scans] == [(WORK, "scanme")]
    scanme = scans[0]
    assert scanme["rows"] >= 12000
    assert scanme["seq_scan"] >= 25
    assert scanme["idx_scan"] == 0
    assert scanme["seq_scan_pct"] == 100
    assert scanme["rows_per_seq_scan"] >= 10000
    # A table that is mostly dead rows is not "large", and a table with an index is not listed.
    everything = await call(pg_index_advisor, connection="ro", table=f"{WORK}.*")
    names = {r["table"] for r in rows_as_dicts(everything["large_tables_read_by_sequential_scans"])}
    assert "bloaty" not in names


async def test_advisor_needs_hypopg(app: Any, ready: Server) -> None:
    with superuser(ready) as conn:
        installed = conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'hypopg'").fetchone()
    if installed:
        pytest.skip("hypopg is installed on this server")
    with pytest.raises(ToolError, match="CREATE EXTENSION hypopg"):
        await call(
            pg_index_advisor,
            connection="ro",
            sql="SELECT 1",
            hypothetical_indexes=["CREATE INDEX ON shop.reviews (id)"],
        )


# The hypopg tests run on the PostgreSQL 18 server with extensions ("extras").

REVIEWS_QUERY = "SELECT * FROM shop.reviews WHERE product_id = 5"
HYPOTHETICAL = ["CREATE INDEX ON shop.reviews (product_id)"]


async def test_advisor_compares_plans_with_hypothetical_indexes(
    extras_app: Any, extras: Server
) -> None:
    first = await call(
        pg_index_advisor, connection="ro", sql=REVIEWS_QUERY, hypothetical_indexes=HYPOTHETICAL
    )
    assert first["query"] == REVIEWS_QUERY
    assert first["before"]["scans"] == ["Seq Scan on shop.reviews"]
    assert first["after"]["scans"] != first["before"]["scans"]
    assert "shop.reviews" in first["after"]["scans"][0]
    assert first["after"]["cost"] < first["before"]["cost"]
    assert first["improvement_percent"] >= 10
    created = first["hypothetical_indexes"]
    assert len(created) == 1
    assert "reviews_product_id" in created[0]["name"]
    assert created[0]["definition"] == HYPOTHETICAL[0]
    assert created[0]["estimated_size"]
    assert first["verdict"].startswith("The planner would use")

    # Nothing is left behind: not in the catalog, and not in the connection that is reused.
    with superuser(extras) as conn:
        indexes = [
            r[0]
            for r in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'shop' AND tablename = 'reviews'"
            ).fetchall()
        ]
    assert indexes == ["reviews_pkey"]
    again = await call(
        pg_index_advisor, connection="ro", sql=REVIEWS_QUERY, hypothetical_indexes=HYPOTHETICAL
    )
    assert again["before"] == first["before"]
    assert again["after"] == first["after"]


async def test_advisor_reports_an_index_that_does_not_help(extras_app: Any) -> None:
    result = await call(
        pg_index_advisor,
        connection="ro",
        sql=REVIEWS_QUERY,
        hypothetical_indexes=["CREATE INDEX ON shop.reviews (body)"],
    )
    assert result["improvement_percent"] == 0
    assert result["after"] == result["before"]
    assert "does not benefit" in result["verdict"]


async def test_advisor_tests_several_indexes_at_once(extras_app: Any) -> None:
    result = await call(
        pg_index_advisor,
        connection="ro",
        sql="SELECT * FROM shop.reviews WHERE product_id = 5 AND customer_id = 7",
        hypothetical_indexes=[
            "CREATE INDEX ON shop.reviews (product_id, customer_id)",
            "CREATE UNIQUE INDEX ON shop.reviews (id, product_id)",
        ],
    )
    assert len(result["hypothetical_indexes"]) == 2
    assert result["improvement_percent"] > 0


async def test_advisor_refuses_bad_hypothetical_indexes(extras_app: Any) -> None:
    async def advise(*indexes: str, sql: str | None = REVIEWS_QUERY) -> Any:
        return await call(
            pg_index_advisor, connection="ro", sql=sql, hypothetical_indexes=list(indexes)
        )

    with pytest.raises(ToolError, match="is not a CREATE INDEX command"):
        await advise("DROP TABLE shop.reviews")
    with pytest.raises(ToolError, match="is not a CREATE INDEX command"):
        await advise("CREATE TABLE shop.x (a int)")
    with pytest.raises(ToolError, match="multiple-statements"):
        await advise("CREATE INDEX ON shop.reviews (id); DROP TABLE shop.reviews")
    with pytest.raises(ToolError, match=r"hypopg could not create.*nope"):
        await advise("CREATE INDEX ON shop.reviews (nope)")
    with pytest.raises(ToolError, match="hypopg could not create"):
        await advise("CREATE INDEX ON shop.table_that_does_not_exist (id)")
    with pytest.raises(ToolError, match="Give hypothetical_indexes"):
        await advise()
    with pytest.raises(ToolError, match="At most 10"):
        await advise(*["CREATE INDEX ON shop.reviews (id)"] * 11)
    with pytest.raises(ToolError, match="needs the `sql` query"):
        await call(
            pg_index_advisor,
            connection="ro",
            hypothetical_indexes=["CREATE INDEX ON shop.reviews (id)"],
        )
    with pytest.raises(ToolError, match="Give hypothetical_indexes"):
        await call(pg_index_advisor, connection="ro", sql=REVIEWS_QUERY)

    # After all those failures the planner still sees no hypothetical index.
    clean = await advise("CREATE INDEX ON shop.reviews (body)")
    assert clean["before"]["scans"] == ["Seq Scan on shop.reviews"]


async def test_advisor_applies_the_safety_rules_of_the_connection(extras_app: Any) -> None:
    with pytest.raises(ToolError, match="not-a-read-query"):
        await call(
            pg_index_advisor,
            connection="ro",
            sql="DELETE FROM shop.reviews",
            hypothetical_indexes=HYPOTHETICAL,
        )
    with pytest.raises(ToolError, match="protected-column"):
        await call(
            pg_index_advisor,
            connection="guarded",
            sql="SELECT access_key FROM vault.files WHERE id = 1",
            hypothetical_indexes=["CREATE INDEX ON vault.files (id)"],
        )
    with pytest.raises(ToolError, match="full-scan-forbidden"):
        await call(
            pg_index_advisor,
            connection="guarded",
            sql="SELECT name FROM vault.files WHERE size_bytes > 5",
            hypothetical_indexes=["CREATE INDEX ON vault.files (size_bytes)"],
        )
    with pytest.raises(ToolError, match="parameter"):
        await call(
            pg_index_advisor,
            connection="ro",
            sql="SELECT * FROM shop.reviews WHERE product_id = $1",
            hypothetical_indexes=HYPOTHETICAL,
        )


# ---------------------------------------------------------------------------- pg_settings


async def test_settings_search_by_name(app: Any, ready: Server) -> None:
    result = await call(pg_settings, connection="ro", pattern="shared_buffers")
    row = by_name(result, "name")["shared_buffers"]
    with superuser(ready) as conn:
        shown = conn.execute("SHOW shared_buffers").fetchone()
    assert shown is not None
    assert row["value"] == shown[0]
    assert row["default"]
    assert row["restart"] is True
    assert row["pending_restart"] is False
    assert row["category"].startswith("Resource Usage")
    assert "shared memory buffers" in row["description"]
    assert result["columns"][:3] == ["name", "value", "default"]

    work = by_name(await call(pg_settings, connection="ro", pattern="work_mem"), "name")
    assert work["work_mem"]["restart"] is False
    assert work["work_mem"]["default"] == "4MB"
    assert "maintenance_work_mem" in work

    wild = await call(pg_settings, connection="ro", pattern="autovacuum*scale_factor")
    assert {r["name"] for r in rows_as_dicts(wild)} >= {
        "autovacuum_vacuum_scale_factor",
        "autovacuum_analyze_scale_factor",
    }
    scale = by_name(wild, "name")["autovacuum_vacuum_scale_factor"]
    assert scale["value"] == "0.2"
    assert scale["source"] == "default"

    empty = await call(pg_settings, connection="ro", pattern="no_such_parameter_xyz")
    assert empty["rows"] == []
    assert empty["hint"]


async def test_settings_search_by_description_and_category(app: Any) -> None:
    by_description = await call(pg_settings, connection="ro", pattern="deadlock")
    assert "deadlock_timeout" in {r["name"] for r in rows_as_dicts(by_description)}
    # Names that match come before descriptions that match.
    assert rows_as_dicts(by_description)[0]["name"].startswith("deadlock")

    checkpoints = rows_as_dicts(await call(pg_settings, connection="ro", category="checkpoints"))
    names = {r["name"] for r in checkpoints}
    assert {"checkpoint_timeout", "max_wal_size"} <= names
    assert all("heckpoint" in r["category"] for r in checkpoints)
    assert next(r for r in checkpoints if r["name"] == "checkpoint_timeout")["value"] == "5min"


async def test_settings_changed_and_pending(app: Any, ready: Server) -> None:
    changed = rows_as_dicts(await call(pg_settings, connection="ro", changed_only=True, limit=500))
    names = {r["name"]: r for r in changed}
    assert names["fsync"]["value"] == "off"
    assert names["fsync"]["source"] != "default"
    assert all(r["source"] not in ("default", "override") for r in changed)
    # What pgharbor sets for its own session is not a change of the server.
    assert not {"statement_timeout", "lock_timeout", "datestyle"} & set(names)

    pending = await call(pg_settings, connection="ro", pending_restart_only=True)
    assert all(r["pending_restart"] for r in rows_as_dicts(pending))
    assert "database_and_role_settings" not in pending


async def test_settings_shows_what_pgharbor_sets_itself(app: Any) -> None:
    result = await call(pg_settings, connection="ro", pattern="statement_timeout")
    row = by_name(result, "name")["statement_timeout"]
    assert row["source"] == "pgharbor session"
    assert any("statement_timeout" in n and "pgharbor" in n for n in result["notes"])


async def test_settings_lists_database_and_role_overrides(app: Any, ready: Server) -> None:
    with superuser(ready) as admin:
        admin.execute("DROP ROLE IF EXISTS pgh_cfg_role")
        admin.execute("CREATE ROLE pgh_cfg_role")
        try:
            admin.execute("ALTER ROLE pgh_cfg_role IN DATABASE pgharbor_test SET work_mem = '13MB'")
            admin.execute("ALTER ROLE pgh_cfg_role SET random_page_cost = 1.1")
            result = await call(pg_settings, connection="ro", pattern="work_mem")
            overrides = {
                (r["database"], r["role"]): r["settings"]
                for r in rows_as_dicts(result["database_and_role_settings"])
            }
            assert overrides[("pgharbor_test", "pgh_cfg_role")] == ["work_mem=13MB"]
            assert (
                "(all databases)",
                "pgh_cfg_role",
            ) not in overrides  # does not match the pattern
            everything = await call(pg_settings, connection="ro")
            roles = {
                (r["database"], r["role"]): r["settings"]
                for r in rows_as_dicts(everything["database_and_role_settings"])
            }
            assert roles[("(all databases)", "pgh_cfg_role")] == ["random_page_cost=1.1"]
        finally:
            admin.execute("DROP ROLE pgh_cfg_role")


async def test_settings_for_an_unprivileged_role(app: Any) -> None:
    result = await call(pg_settings, connection="limited", pattern="work_mem")
    assert "work_mem" in {r["name"] for r in rows_as_dicts(result)}
    assert any("pg_file_settings" in n for n in result["notes"])


# ----------------------------------------------------------------------- pg_sequences_health


async def test_sequences_near_the_end(app: Any, ready: Server) -> None:
    result = await call(pg_sequences_health, connection="ro", threshold_percent=90)
    sequences = by_name(result, "sequence")
    tiny = sequences["tiny_seq"]
    assert tiny["schema"] == "shop"
    assert tiny["last_value"] == 95
    assert tiny["usable_max"] == 100
    assert tiny["used_pct"] == 94.9
    assert tiny["values_left"] == 5
    assert tiny["cycle"] is False
    assert tiny["column"] is None
    assert tiny["type"] == ("smallint" if ready.at_least(10) else "bigint")
    assert tiny["limit_source"] == "sequence"
    assert result["threshold_percent"] == 90
    assert result["sequences_checked"] >= 8
    assert "invoice_seq" not in sequences  # never used

    everything = await call(pg_sequences_health, connection="ro", threshold_percent=0)
    listed = rows_as_dicts(everything)
    used = [r["used_pct"] for r in listed]
    assert used == sorted(used, reverse=True)
    assert listed[0]["sequence"] == "tiny_seq"
    assert {"customers_id_seq", "orders_id_seq", "invoice_seq"} <= {r["sequence"] for r in listed}
    invoice = next(r for r in listed if r["sequence"] == "invoice_seq")
    assert invoice["used_pct"] == 0
    assert invoice["last_value"] is None

    none = await call(pg_sequences_health, connection="ro", threshold_percent=100)
    assert "tiny_seq" not in {r["sequence"] for r in rows_as_dicts(none)}
    assert none["message"].startswith("No sequence has used 100%")
    assert (await call(pg_sequences_health, connection="ro", threshold_percent=0, limit=2))[
        "has_more"
    ] is True


@pytest.mark.usefixtures("perf_workload")
async def test_sequences_measure_against_the_column_type(app: Any, ready: Server) -> None:
    result = await call(pg_sequences_health, connection="ro", threshold_percent=90)
    serial = by_name(result, "sequence")["serial_t_id_seq"]
    assert serial["schema"] == WORK
    assert serial["column"] == f"{WORK}.serial_t.id"
    assert serial["usable_max"] == 2147483647  # the integer column ends before the sequence does
    assert serial["limit_source"] == "column type (integer)"
    assert 93 <= serial["used_pct"] < 94
    assert serial["values_left"] == 2147483647 - 2000000000
    assert serial["type"] == ("bigint")

    mismatches = by_name(result["column_smaller_than_sequence"], "column")
    row = mismatches[f"{WORK}.serial_t.id"]
    assert row["column_type"] == "integer"
    assert row["sequence"] == f"{WORK}.serial_t_id_seq"
    assert row["sequence_max"] == 9223372036854775807
    assert any("column_smaller_than_sequence" in n for n in result["notes"])
    # PostgreSQL 10+ creates serial sequences as integer, so only 9.6 flags every serial column.
    everything = await call(pg_sequences_health, connection="ro", threshold_percent=0, limit=500)
    flagged = set(by_name(everything["column_smaller_than_sequence"], "column"))
    assert ("shop.customers.id" in flagged) == (not ready.at_least(10))


@pytest.mark.usefixtures("perf_workload")
async def test_sequences_that_cycle_are_flagged(app: Any) -> None:
    result = await call(pg_sequences_health, connection="ro", threshold_percent=80)
    cyc = by_name(result, "sequence")["cyc"]
    assert cyc["cycle"] is True
    assert cyc["used_pct"] == 88.9
    assert cyc["values_left"] == 1
    assert any("cycle=true" in n for n in result["notes"])


async def test_sequences_without_privilege(app: Any, ready: Server) -> None:
    result = await call(pg_sequences_health, connection="limited", threshold_percent=0)
    # The role has no USAGE or SELECT on the sequences, so it cannot see how far they are.
    assert any("lacks USAGE or SELECT" in n for n in result["notes"])
    assert (result["sequences_checked"] >= 1) == ready.at_least(10)


# ----------------------------------------------------------------------- pg_wal_checkpoints


def test_checkpoint_reading_branches() -> None:
    assert "too few to judge" in checkpoint_reading(3, 2)
    forced = checkpoint_reading(2, 18, "1GB")
    assert "forced too often" in forced
    assert "90%" in forced
    assert "raise max_wal_size (now 1GB)" in forced
    assert "raise max_wal_size so" in checkpoint_reading(1, 9)
    some = checkpoint_reading(16, 4)
    assert "20% of the checkpoints are forced" in some
    assert "mostly scheduled" in checkpoint_reading(95, 5)
    assert checkpoint_reading(100, 0).endswith("no change needed.")


async def test_wal_and_checkpoints(app: Any, ready: Server) -> None:
    result = await call(pg_wal_checkpoints, connection="ro")
    assert isinstance(result["reading"], str)
    assert result["reading"]
    checkpoints = result["checkpoints"]
    assert checkpoints["scheduled"] >= 0
    assert checkpoints["forced"] >= 0
    assert checkpoints["counters_since"]
    assert checkpoints["written_by_checkpoints"]
    assert ("written_by_backends" in checkpoints) == (not ready.at_least(17))
    assert ("wal" in result) == ready.at_least(14)
    if ready.at_least(14):
        assert result["wal"]["records"] > 0
        assert result["wal"]["generated"]
        assert "per_hour" in result["wal"]
    else:
        assert any("pg_stat_wal" in n for n in result["notes"])
    assert "archived_count" in result["archiver"]
    assert result["position"]["in_recovery"] is False
    assert re.fullmatch(r"[0-9A-F]+/[0-9A-F]+", result["position"]["current_lsn"])
    assert re.fullmatch(r"[0-9A-F]{24}", result["position"]["current_file"])

    settings = result["settings"]
    assert settings["max_wal_size"]
    assert settings["checkpoint_timeout"] == "5min"
    assert settings["fsync"] == "off"
    assert settings["wal_level"] in ("replica", "logical", "hot_standby")
    assert ("wal_keep_segments" in settings) == (not ready.at_least(13))
    assert ("wal_keep_size" in settings) == ready.at_least(13)
    assert ("max_slot_wal_keep_size" in settings) == ready.at_least(13)
    assert "archive_command" not in settings  # it may carry credentials


async def test_a_requested_checkpoint_is_counted_as_forced(app: Any, ready: Server) -> None:
    before = (await call(pg_wal_checkpoints, connection="ro"))["checkpoints"]["forced"]
    with superuser(ready) as admin:
        admin.execute("CHECKPOINT")
    deadline = time.monotonic() + 10
    after = before
    while time.monotonic() < deadline:
        after = (await call(pg_wal_checkpoints, connection="ro"))["checkpoints"]["forced"]
        if after > before:
            break
        await asyncio.sleep(0.3)
    assert after > before


async def test_wal_position_moves_forward(app: Any, ready: Server) -> None:
    def lsn_value(text: str) -> int:
        high, low = text.split("/")
        return (int(high, 16) << 32) + int(low, 16)

    first = (await call(pg_wal_checkpoints, connection="ro"))["position"]["current_lsn"]
    with superuser(ready) as admin:
        admin.execute("SELECT pg_switch_wal()" if ready.at_least(10) else "SELECT pg_switch_xlog()")
        try:
            admin.execute("CREATE TABLE public.pgh_health_wal (a int)")
            admin.execute(
                "INSERT INTO public.pgh_health_wal SELECT g FROM generate_series(1, 1000) g"
            )
        finally:
            admin.execute("DROP TABLE IF EXISTS public.pgh_health_wal")
    second = (await call(pg_wal_checkpoints, connection="ro"))["position"]["current_lsn"]
    assert lsn_value(second) > lsn_value(first)


async def test_wal_for_an_unprivileged_role(app: Any) -> None:
    result = await call(pg_wal_checkpoints, connection="limited")
    assert result["checkpoints"]["scheduled"] >= 0
    assert result["position"]["current_lsn"]
