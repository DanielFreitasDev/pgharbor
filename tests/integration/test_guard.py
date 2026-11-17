"""The read path against real servers: what must work and what must be refused.

Every case runs on every PostgreSQL version of the matrix. The last test proves that,
after all the attacks, nothing in the database changed.
"""

from __future__ import annotations

from typing import Any

import pytest

from pgharbor.errors import PgHarborError, Rejected
from pgharbor.runner import explain_statement, run_query, validate_statement
from pgharbor.session import Sessions

pytestmark = pytest.mark.integration


async def query(sessions: Sessions, sql: str, params: list[Any] | None = None, *,
                connection: str = "ro", **options: Any):
    async with sessions.transaction(connection) as db:
        return await run_query(db, sql, params or [], **options)


# ------------------------------------------------------------------------------ must work

ALLOWED = [
    "SELECT 1 AS one",
    "SELECT 1 AS one;",
    "  SELECT 1 -- trailing comment",
    "SELECT 1 /* block /* nested */ comment */ AS one",
    "VALUES (1, 'a'), (2, 'b')",
    "TABLE shop.no_pk",
    "(SELECT 1) UNION ALL (SELECT 2)",
    "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
    "WITH RECURSIVE t(n) AS (VALUES (1) UNION ALL SELECT n + 1 FROM t WHERE n < 5) SELECT sum(n) FROM t",
    "SELECT count(*), min(total), max(total), avg(total)::numeric(12,2) FROM shop.orders",
    "SELECT status, count(*) FROM shop.orders GROUP BY status ORDER BY 2 DESC",
    "SELECT id, total, rank() OVER (PARTITION BY status ORDER BY total DESC) FROM shop.orders LIMIT 5",
    "SELECT count(*) FILTER (WHERE status = 'paid') FROM shop.orders",
    "SELECT * FROM shop.order_summary ORDER BY id LIMIT 3",
    "SELECT * FROM analytics.daily_sales ORDER BY day LIMIT 3",
    "SELECT name, shop.fmt_price(price) FROM shop.products ORDER BY id LIMIT 3",
    "SELECT id, shop.order_total(id) FROM shop.orders ORDER BY id LIMIT 3",
    "SELECT shop.customer_label(c) FROM shop.customers c ORDER BY id LIMIT 2",
    "SELECT trap.honest_stable(1)",
    "SELECT attrs->>'color' AS color, tags[1] FROM shop.products WHERE attrs @> '{\"size\": 3}' LIMIT 3",
    "SELECT jsonb_agg(sku ORDER BY sku) FROM (SELECT sku FROM shop.products LIMIT 3) s",
    "SELECT (address).city, count(*) FROM shop.customers GROUP BY 1 ORDER BY 2 DESC LIMIT 3",
    "SELECT generate_series(1, 3) AS n",
    "SELECT now(), current_date, current_user, clock_timestamp(), random() < 2 AS r",
    "SELECT timezone('UTC', now()), now() AT TIME ZONE 'UTC'",
    "SELECT pg_relation_size('shop.orders'::regclass) > 0, pg_size_pretty(pg_database_size(current_database()))",
    "SELECT relname, relkind FROM pg_catalog.pg_class WHERE relnamespace = 'shop'::regnamespace ORDER BY 1 LIMIT 5",
    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'shop' ORDER BY 1",
    "SELECT pid, state FROM pg_stat_activity WHERE pid = pg_backend_pid()",
    "SELECT name, setting FROM pg_settings WHERE name = 'max_connections'",
    "SELECT count(*) FROM pg_locks",
    "SELECT rolname, rolsuper FROM pg_roles ORDER BY 1 LIMIT 3",
    "SELECT usename FROM pg_user LIMIT 1",
    "SELECT 'it''s', E'tab\\there', $$dollar 'quoted'$$, $tag$ a $$ b $tag$",
    "SELECT 'a\\'' , pg_sleep(1) -- still inside the string '",
    "SELECT lower('ÁÉ'), 'ção' AS \"Descrição\"",
    "SELECT id FROM shop.orders WHERE placed_at >= date '2026-01-02' AND total BETWEEN 10 AND 20 ORDER BY id LIMIT 2",
    "SELECT o.id FROM shop.orders o WHERE EXISTS (SELECT 1 FROM shop.order_items i WHERE i.order_id = o.id) LIMIT 2",
    "SELECT * FROM (SELECT 1 AS a, 2 AS a) dup",
    "SELECT last_value FROM shop.invoice_seq",
    "SELECT kind, count(*) FROM events.log GROUP BY kind ORDER BY 1",
    "SELECT NULL::int AS n, ARRAY[1,2,3] AS arr, '{\"a\": [1, 2]}'::jsonb AS j, ROW(1, 'x') AS r",
    "SHOW server_version",
    "show TIME ZONE",
    "SHOW ALL",
]


@pytest.mark.parametrize("sql", ALLOWED)
async def test_allowed_queries_run(sessions: Sessions, sql: str) -> None:
    result = await query(sessions, sql)
    assert result.rows, sql
    assert all(len(row) == len(result.columns) for row in result.rows)


# ------------------------------------------------------------------------- must be refused

# (sql, expected rule or None when any refusal is fine)
REFUSED: list[tuple[str, str | None]] = [
    # --- more than one statement, in every disguise
    ("SELECT 1; SELECT 2", "multiple-statements"),
    ("COMMIT; DROP SCHEMA shop CASCADE", "multiple-statements"),
    ("SELECT 1; COMMIT; CREATE TABLE trap.zz (a int)", "multiple-statements"),
    ("SELECT 1 /* ; */ ; DELETE FROM shop.no_pk", "multiple-statements"),
    ("SELECT 1) AS a; DROP TABLE shop.no_pk; SELECT (2", None),
    # --- statements that are not read queries
    ("INSERT INTO shop.no_pk VALUES ('x', 'y')", "not-a-read-query"),
    ("UPDATE shop.no_pk SET label = 'x'", "not-a-read-query"),
    ("DELETE FROM shop.no_pk", "not-a-read-query"),
    ("TRUNCATE shop.no_pk", "not-a-read-query"),
    ("CREATE TABLE trap.zz (a int)", "not-a-read-query"),
    ("CREATE TEMP TABLE zz (a int)", "not-a-read-query"),
    ("DROP TABLE shop.no_pk", "not-a-read-query"),
    ("ALTER TABLE shop.no_pk ADD COLUMN z int", "not-a-read-query"),
    ("GRANT ALL ON shop.no_pk TO public", "not-a-read-query"),
    ("COMMENT ON TABLE shop.no_pk IS 'x'", "not-a-read-query"),
    ("VACUUM shop.no_pk", "not-a-read-query"),
    ("ANALYZE shop.no_pk", "not-a-read-query"),
    ("REINDEX TABLE shop.no_pk", "not-a-read-query"),
    ("REFRESH MATERIALIZED VIEW analytics.daily_sales", "not-a-read-query"),
    ("CHECKPOINT", "not-a-read-query"),
    ("SET statement_timeout = 0", "not-a-read-query"),
    ("SET transaction_read_only = off", "not-a-read-query"),
    ("SET TRANSACTION READ WRITE", "not-a-read-query"),
    ("SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE", "not-a-read-query"),
    ("SET ROLE postgres", "not-a-read-query"),
    ("RESET ALL", "not-a-read-query"),
    ("BEGIN", "not-a-read-query"),
    ("COMMIT", "not-a-read-query"),
    ("ROLLBACK", "not-a-read-query"),
    ("END", "not-a-read-query"),
    ("ABORT", "not-a-read-query"),
    ("SAVEPOINT s", "not-a-read-query"),
    ("PREPARE p AS SELECT 1", "not-a-read-query"),
    ("EXECUTE p", "not-a-read-query"),
    ("DEALLOCATE ALL", "not-a-read-query"),
    ("DECLARE c CURSOR FOR SELECT 1", "not-a-read-query"),
    ("LOCK TABLE shop.no_pk IN ACCESS EXCLUSIVE MODE", "not-a-read-query"),
    ("LISTEN chan", "not-a-read-query"),
    ("NOTIFY chan, 'x'", "not-a-read-query"),
    ("DISCARD ALL", "not-a-read-query"),
    ("LOAD 'auto_explain'", "not-a-read-query"),
    ("DO $$ BEGIN PERFORM 1; END $$", "not-a-read-query"),
    ("CALL shop.archive_orders('2020-01-01')", "not-a-read-query"),
    ("COPY (SELECT 1) TO PROGRAM 'id'", "not-a-read-query"),
    ("COPY (SELECT 1) TO '/tmp/pgharbor-should-not-exist'", "not-a-read-query"),
    ("COPY shop.no_pk FROM PROGRAM 'echo a'", "not-a-read-query"),
    ("EXPLAIN ANALYZE SELECT 1", "use-pg-explain"),
    # --- writes hidden inside a query (the server is the judge here)
    ("SELECT * INTO trap.zz FROM shop.no_pk", None),
    ("SELECT code FROM shop.no_pk FOR UPDATE", "read-only-transaction"),
    ("SELECT code FROM shop.no_pk FOR SHARE", "read-only-transaction"),
    ("WITH d AS (DELETE FROM shop.no_pk RETURNING code) SELECT * FROM d", None),
    ("WITH i AS (INSERT INTO shop.no_pk VALUES ('q', 'q') RETURNING code) SELECT * FROM i", None),
    # --- functions that act outside the database or change the session
    ("SELECT pg_sleep(1)", "function-denied"),
    ("SELECT PG_SLEEP(1)", "function-denied"),
    ('SELECT "pg_sleep"(1)', "function-denied"),
    ("SELECT pg_catalog.pg_sleep(1)", "function-denied"),
    ("SELECT pg_sleep /* hidden */ (1)", "function-denied"),
    ("SELECT pg_sleep\n\t(1)", "function-denied"),
    ("SELECT 1 FROM pg_sleep(1)", "function-denied"),
    ("SELECT 1 WHERE pg_sleep(1) IS NULL", "function-denied"),
    ("SELECT (SELECT pg_sleep(1))", "function-denied"),
    ("WITH s AS (SELECT pg_sleep(1)) SELECT 1 FROM s", "function-denied"),
    ("SELECT 1 FROM shop.no_pk, LATERAL pg_sleep(1) s", "function-denied"),
    ("VALUES (pg_sleep(1))", "function-denied"),
    ("SELECT 1 ORDER BY pg_sleep(1)", "function-denied"),
    ("SELECT pg_sleep_for('1 second')", "function-denied"),
    ("SELECT pg_terminate_backend(pg_backend_pid())", "function-denied"),
    ("SELECT pg_cancel_backend(1)", "function-denied"),
    ("SELECT pg_read_file('postgresql.conf')", "function-denied"),
    ("SELECT pg_read_binary_file('postgresql.conf')", "function-denied"),
    ("SELECT pg_ls_dir('.')", "function-denied"),
    ("SELECT pg_stat_file('postgresql.conf')", "function-denied"),
    ("SELECT lo_import('/etc/passwd')", "function-denied"),
    ("SELECT lo_export(1, '/tmp/x')", "function-denied"),
    ("SELECT lo_create(0)", "function-denied"),
    ("SELECT lo_get(1)", "function-denied"),
    ("SELECT set_config('statement_timeout', '0', false)", "function-denied"),
    ("SELECT set_config('transaction_read_only', 'off', false)", "function-denied"),
    ("SELECT pg_advisory_lock(1)", "function-denied"),
    ("SELECT pg_try_advisory_lock(1)", "function-denied"),
    ("SELECT pg_advisory_xact_lock(1)", "function-side-effect"),
    ("SELECT nextval('shop.invoice_seq')", "function-side-effect"),
    ("SELECT setval('shop.invoice_seq', 1)", "function-side-effect"),
    ("SELECT pg_notify('chan', 'x')", "function-side-effect"),
    ("SELECT txid_current()", "function-side-effect"),
    ("SELECT query_to_xml('select 1', true, false, '')", "function-denied"),
    ("SELECT table_to_xml('shop.no_pk'::regclass, true, false, '')", "function-denied"),
    ("SELECT database_to_xml(true, false, '')", "function-denied"),
    ("SELECT * FROM ts_stat('select to_tsvector(label) from shop.no_pk')", "function-denied"),
    ("SELECT * FROM dblink('dbname=postgres', 'select 1') AS t(a int)", "function-denied"),
    ("SELECT dblink_exec('dbname=postgres', 'create table zz(a int)')", "function-denied"),
    ("SELECT dblink_connect('c', 'dbname=postgres')", "function-denied"),
    ("SELECT * FROM crosstab('select 1, 2, 3') AS ct(a int, b int)", "function-denied"),
    ("SELECT pg_stat_reset()", "function-denied"),
    ("SELECT pg_stat_statements_reset()", "function-denied"),
    ("SELECT pg_reload_conf()", "function-denied"),
    ("SELECT pg_rotate_logfile()", "function-denied"),
    ("SELECT pg_switch_wal()", "function-denied"),
    ("SELECT pg_switch_xlog()", "function-denied"),
    ("SELECT pg_create_restore_point('x')", "function-denied"),
    ("SELECT pg_create_physical_replication_slot('s')", "function-denied"),
    ("SELECT pg_drop_replication_slot('s')", "function-denied"),
    ("SELECT pg_logical_emit_message(false, 'p', 'x')", "function-denied"),
    ("SELECT pg_prewarm('shop.orders')", "function-denied"),
    ("SELECT pg_export_snapshot()", "function-denied"),
    ("SELECT gin_clean_pending_list('shop.products_attrs_idx'::regclass)", "function-denied"),
    # --- user functions with side effects, however they are declared or reached
    ("SELECT shop.touch_order(1)", "function-volatile"),
    ("SELECT trap.lying_stable()", "function-denied"),
    ("SELECT trap.lying_immutable()", "function-denied"),
    ("SELECT trap.calls_liar()", "function-denied"),
    ("SELECT * FROM trap.view_with_sleep", "function-denied"),
    ("SELECT * FROM trap.view_with_liar", "function-denied"),
    ("SELECT * FROM trap.view_over_view", "function-denied"),
    ("SELECT locked FROM (SELECT * FROM trap.view_over_view) s", "function-denied"),
    ("SELECT 1 OPERATOR(trap.@@@) 2", "operator-volatile"),
    # --- credentials and raw statistics
    ("SELECT rolname, rolpassword FROM pg_authid", "relation-denied"),
    ("SELECT * FROM pg_catalog.pg_authid", "relation-denied"),
    ("SELECT passwd FROM pg_shadow", "relation-denied"),
    ("TABLE pg_shadow", "relation-denied"),
    ("SELECT a.rolpassword FROM pg_authid AS a", "relation-denied"),
    ("SELECT * FROM pg_user_mapping", "relation-denied"),
    ("SELECT * FROM pg_largeobject", "relation-denied"),
    ("SELECT * FROM pg_statistic", "relation-denied"),
    # --- text the scanner refuses to guess about
    ('SELECT U&"pg\\005fsleep"(1)', "lexical"),
    ("SELECT U&'\\0041'", "lexical"),
    ("SELECT 'unterminated", "lexical"),
    ("SELECT 1 /* unterminated", "lexical"),
    ("SELECT $a$ unterminated", "lexical"),
    ("SELECT 1 \\gexec", "lexical"),
    ("SELECT (1", "unbalanced-parentheses"),
    ("SELECT 1)", "unbalanced-parentheses"),
    ("", "empty-statement"),
    (";;", "empty-statement"),
]


@pytest.mark.parametrize(("sql", "rule"), REFUSED, ids=[s[:60] for s, _ in REFUSED])
async def test_dangerous_statements_are_refused(
    sessions: Sessions, sql: str, rule: str | None
) -> None:
    with pytest.raises(PgHarborError) as caught:
        await query(sessions, sql)
    if rule is not None:
        assert isinstance(caught.value, Rejected), caught.value
        assert caught.value.rule == rule, str(caught.value)


async def test_nothing_changed_after_the_attacks(sessions: Sessions, ready: Any) -> None:
    """Run the whole refused list again, then prove the database is untouched."""
    before = await _snapshot(sessions)
    for sql, _rule in REFUSED:
        try:
            await query(sessions, sql)
        except PgHarborError:
            continue
        pytest.fail(f"not refused: {sql}")
    assert await _snapshot(sessions) == before


async def _snapshot(sessions: Sessions) -> dict[str, Any]:
    async with sessions.transaction("ro") as db:
        return {
            "tables": await db.rows(
                "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n "
                "ON n.oid = c.relnamespace WHERE n.nspname IN ('shop', 'trap', 'vault', 'public') "
                "AND c.relkind = 'r' ORDER BY 1, 2"
            ),
            "no_pk": await db.value("SELECT count(*) FROM shop.no_pk"),
            "side_effects": await db.value("SELECT count(*) FROM trap.side_effects"),
            "invoice_seq": await db.row("SELECT last_value, is_called FROM shop.invoice_seq"),
            "counter": await db.row("SELECT last_value, is_called FROM trap.counter"),
            "advisory_locks": await db.value(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
            ),
            "large_objects": await db.value("SELECT count(*) FROM pg_largeobject_metadata"),
            "slots": await db.value("SELECT count(*) FROM pg_replication_slots"),
            "timeout": await db.value("SELECT current_setting('statement_timeout')"),
            "orders_touched": await db.value(
                "SELECT count(*) FROM shop.orders WHERE updated_at IS NOT NULL"
            ),
        }


# ------------------------------------------------------------------- function policies


async def test_strict_policy_refuses_user_functions(sessions: Sessions) -> None:
    with pytest.raises(Rejected) as caught:
        await query(sessions, "SELECT shop.fmt_price(1)", connection="strict")
    assert caught.value.rule == "function-not-allowed"
    assert (await query(sessions, "SELECT upper('a')", connection="strict")).rows == [["A"]]


async def test_permissive_policy_still_refuses_the_dangerous_ones(sessions: Sessions) -> None:
    result = await query(sessions, "SELECT trap.honest_stable(1)", connection="permissive")
    assert result.rows == [[3]]
    with pytest.raises(Rejected):
        await query(sessions, "SELECT pg_read_file('postgresql.conf')", connection="permissive")
    with pytest.raises(Rejected):
        await query(sessions, "SELECT pg_sleep(0)", connection="permissive")


# ------------------------------------------------------------------- protected tables

GUARDED_OK = [
    "SELECT id, name, checksum, size_bytes FROM vault.files WHERE id = 7",
    "SELECT id, name FROM vault.files WHERE name = 'file-5.pdf'",
    "SELECT id FROM vault.files WHERE checksum = md5('9')",
    "SELECT f.id, d.path FROM vault.files f JOIN vault.folders d ON d.id = f.folder_id WHERE f.id = 7",
    "SELECT id, name FROM vault.file_names WHERE id = 5",
    "SELECT count(*) FROM vault.folders",
    "SELECT * FROM vault.folders ORDER BY id LIMIT 2",
]
GUARDED_REFUSED = [
    ("SELECT access_key FROM vault.files WHERE id = 1", "protected-column"),
    ("SELECT id FROM vault.files WHERE access_key = 'x' AND id = 1", "protected-column"),
    ("SELECT id FROM vault.files WHERE id = 1 ORDER BY access_key", "protected-column"),
    ("SELECT md5(access_key) FROM vault.files WHERE id = 1", "protected-column"),
    ('SELECT "access_key" FROM vault.files WHERE id = 1', "protected-column"),
    ("SELECT * FROM vault.files WHERE id = 1", "protected-column"),
    ("SELECT f.* FROM vault.files f WHERE id = 1", "protected-column"),
    ("SELECT f FROM vault.files f WHERE id = 1", "protected-column"),
    ("SELECT row_to_json(f) FROM vault.files f WHERE id = 1", "protected-column"),
    ("SELECT to_jsonb(f) FROM vault.files f WHERE id = 1", "protected-column"),
    ("SELECT f::text FROM vault.files f WHERE id = 1", "protected-column"),
    ("SELECT d FROM vault.files AS t(a, b, c, d) WHERE a = 1", "protected-column"),
    ("TABLE vault.files", None),
    ("SELECT k FROM vault.file_keys WHERE id = 1", "protected-column"),
    ("SELECT kk FROM vault.file_keys_twice WHERE id = 1", "protected-column"),
    ("SELECT id, name FROM vault.files WHERE folder_id = 3", "full-scan-forbidden"),
    ("SELECT id, name FROM vault.files", "full-scan-forbidden"),
    ("SELECT count(*) FROM vault.files", "full-scan-forbidden"),
    ("SELECT id FROM vault.files WHERE size_bytes > 10 LIMIT 1", "full-scan-forbidden"),
    ("SELECT id FROM vault.files ORDER BY id LIMIT 3", "full-scan-forbidden"),
    ("SELECT trap.reads_vault()", "protected-table-indirect"),
    ("SELECT most_common_vals FROM pg_stats WHERE tablename = 'files'", "relation-denied"),
]


@pytest.mark.parametrize("sql", GUARDED_OK)
async def test_protected_table_allows_safe_access(sessions: Sessions, sql: str) -> None:
    result = await query(sessions, sql, connection="guarded")
    assert result.rows


@pytest.mark.parametrize(("sql", "rule"), GUARDED_REFUSED, ids=[s[:60] for s, _ in GUARDED_REFUSED])
async def test_protected_table_refuses_the_rest(
    sessions: Sessions, sql: str, rule: str | None
) -> None:
    with pytest.raises(Rejected) as caught:
        await query(sessions, sql, connection="guarded")
    if rule:
        assert caught.value.rule == rule, str(caught.value)
    assert "KEY-" not in str(caught.value)


async def test_unguarded_profile_sees_the_same_table(sessions: Sessions) -> None:
    """The rules belong to the profile: without them the table is an ordinary table."""
    result = await query(sessions, "SELECT count(*) FROM vault.files")
    assert result.rows == [[20000]]


# ----------------------------------------------------------------------- limits and shape


async def test_row_limit_and_paging(sessions: Sessions) -> None:
    first = await query(sessions, "SELECT id FROM shop.orders ORDER BY id", max_rows=5)
    assert [r[0] for r in first.rows] == [1, 2, 3, 4, 5]
    assert first.has_more
    assert any("offset=5" in note for note in first.notes)
    second = await query(sessions, "SELECT id FROM shop.orders ORDER BY id", max_rows=5, offset=5)
    assert [r[0] for r in second.rows] == [6, 7, 8, 9, 10]
    all_rows = await query(sessions, "SELECT code FROM shop.no_pk ORDER BY 1")
    assert not all_rows.has_more
    assert all_rows.rows == [["a"], ["b"]]


async def test_requested_rows_are_capped_by_the_profile(sessions: Sessions) -> None:
    result = await query(sessions, "SELECT id FROM vault.files ORDER BY id", max_rows=10**9)
    assert len(result.rows) == sessions.profile("ro").max_rows_limit
    assert result.has_more


async def test_long_values_are_clipped_on_the_server(sessions: Sessions) -> None:
    result = await query(
        sessions, "SELECT notes, length(notes) FROM shop.customers WHERE id = 50", max_cell_chars=100
    )
    text, length = result.rows[0]
    assert length == 6000
    assert len(text) == 101
    assert text.endswith("…")
    assert any("were cut" in note for note in result.notes)
    photo = await query(sessions, "SELECT photo FROM shop.products WHERE id = 40")
    assert photo.rows[0][0].startswith("\\xabab")
    assert len(photo.rows[0][0]) == sessions.profile("ro").max_cell_chars + 1


async def test_values_keep_their_types(sessions: Sessions) -> None:
    result = await query(
        sessions,
        "SELECT 7::int AS i, 9007199254740993::bigint AS big, true AS b, 1.50::numeric(10,2) AS n,"
        " 2.5::float8 AS f, 'NaN'::float8 AS nan, '{\"a\": [1, null]}'::jsonb AS j,"
        " ARRAY['x', 'y'] AS arr, NULL::text AS nothing, date '2026-11-17' AS d,"
        " timestamptz '2026-11-17 10:00:00+00' AS ts, interval '1 day 02:00' AS iv,"
        " 'a'::bytea AS raw, '10.0.0.1'::inet AS ip",
    )
    assert result.rows == [[
        7, 9007199254740993, True, "1.50", 2.5, "NaN", {"a": [1, None]}, "{x,y}", None,
        "2026-11-17", "2026-11-17 10:00:00+00", "1 day 02:00:00", "\\x61", "10.0.0.1",
    ]]
    types = [c.type for c in result.columns]
    assert types[:5] == ["integer", "bigint", "boolean", "numeric(10,2)", "double precision"]


async def test_parameters_are_bound(sessions: Sessions) -> None:
    result = await query(
        sessions,
        "SELECT id, name FROM shop.customers WHERE id = $1 OR name = $2 ORDER BY id",
        [3, "Customer 5"],
    )
    assert [r[0] for r in result.rows] == [3, 5]
    hostile = await query(
        sessions, "SELECT count(*) FROM shop.no_pk WHERE code = $1", ["a'; DROP TABLE shop.no_pk; --"]
    )
    assert hostile.rows == [[0]]
    with pytest.raises(Rejected) as caught:
        await query(sessions, "SELECT $1::int", [])
    assert caught.value.rule == "parameter-count"
    with pytest.raises(Rejected):
        await query(sessions, "SELECT 1", [5])


async def test_columns_named_like_credentials_are_redacted(sessions: Sessions) -> None:
    plain = await query(sessions, "SELECT id, api_token FROM shop.customers WHERE id = 1")
    assert plain.rows == [[1, "[redacted]"]]
    renamed = await query(sessions, "SELECT id, api_token AS t FROM shop.customers WHERE id = 1")
    assert renamed.rows == [[1, "[redacted]"]]
    assert any("Redacted" in note for note in renamed.notes)


async def test_embedded_credentials_are_scrubbed(sessions: Sessions) -> None:
    result = await query(
        sessions,
        "SELECT 'host=db user=app password=hunter2 dbname=x' AS dsn,"
        " 'postgresql://app:hunter2@db/x' AS uri,"
        " 'ALTER ROLE app PASSWORD ''hunter2''' AS stmt",
    )
    flat = " ".join(result.rows[0])
    assert "hunter2" not in flat
    assert flat.count("[redacted]") == 3


async def test_syntax_errors_point_at_the_callers_sql(sessions: Sessions) -> None:
    with pytest.raises(PgHarborError) as caught:
        await query(sessions, "SELECT id\nFROM shop.orders\nWHERE")
    assert "end of your statement" in str(caught.value)
    with pytest.raises(PgHarborError) as caught:
        await query(sessions, "SELECT id\nFROM shop.orders\nWHERE id = = 3")
    assert "line 3, column 12" in str(caught.value)
    with pytest.raises(PgHarborError) as caught:
        await query(sessions, "SELECT id\nFROM shop.orders\nWHERE nope = 3")
    assert "line 3, column 7" in str(caught.value)


async def test_least_privilege_role(sessions: Sessions) -> None:
    result = await query(sessions, "SELECT count(*) FROM shop.orders", connection="limited")
    assert result.rows == [[6000]]
    with pytest.raises(PgHarborError) as caught:
        await query(sessions, "SELECT count(*) FROM vault.files", connection="limited")
    assert "ermission denied" in str(caught.value)
    with pytest.raises(PgHarborError):
        await query(sessions, "SELECT api_token FROM shop.customers", connection="limited")


# -------------------------------------------------------------------- explain and validate


async def test_explain_does_not_run_the_query(sessions: Sessions) -> None:
    async with sessions.transaction("ro") as db:
        plan = await explain_statement(db, "SELECT * FROM shop.orders WHERE customer_id = $1", [3])
    assert plan["executed"] is False
    assert "shop.orders" in plan["relations"]
    assert "Scan" in plan["plan"]
    async with sessions.transaction("ro") as db:
        as_json = await explain_statement(
            db, "EXPLAIN (VERBOSE) SELECT count(*) FROM shop.customers", output_format="json"
        )
    assert as_json["plan"][0]["Plan"]["Node Type"]


async def test_explain_analyze_needs_permission(sessions: Sessions) -> None:
    async with sessions.transaction("ro") as db:
        with pytest.raises(Rejected) as caught:
            await explain_statement(db, "SELECT 1", analyze_execution=True)
    assert caught.value.rule == "explain-analyze-disabled"
    async with sessions.transaction("ro") as db:
        with pytest.raises(Rejected):
            await explain_statement(db, "EXPLAIN ANALYZE SELECT pg_sleep(1)")
    async with sessions.transaction("ro") as db:
        with pytest.raises(Rejected):
            await explain_statement(db, "DELETE FROM shop.no_pk")


async def test_validate_reports_without_running(sessions: Sessions) -> None:
    async with sessions.transaction("ro") as db:
        report = await validate_statement(
            db, "SELECT id, total FROM shop.orders WHERE customer_id = $1", [1]
        )
    assert report["valid"] is True
    assert [c["name"] for c in report["columns"]] == ["id", "total"]
    assert report["relations"] == ["shop.orders"]
    async with sessions.transaction("ro") as db:
        with pytest.raises(PgHarborError):
            await validate_statement(db, "SELECT nope FROM shop.orders")
