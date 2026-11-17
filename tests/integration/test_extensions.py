"""Extension tools on every PostgreSQL version of the matrix, plus the extras server."""

import contextlib
import re
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from pgharbor.tools.extensions import pg_extension_report, pg_extensions, pg_integrity_check

from ..conftest import Server, call, rows_as_dicts, superuser

pytestmark = pytest.mark.integration

SCHEMA = "pgh_c_ext"
INTEGRITY = "pgh_c_int"
SERVER_NAME = "pgh_c_ext_srv"
MAPPING_PASSWORD = "pgh-c-ext-mapping-secret"


def _no_secrets(text: str) -> None:
    assert "SCRAM-SHA-256$" not in text
    assert not re.search(r"md5[0-9a-f]{32}", text)
    assert "password=" not in text
    assert MAPPING_PASSWORD not in text


def _installed(conn: psycopg.Connection[Any]) -> dict[str, tuple[str, str]]:
    rows = conn.execute(
        "SELECT e.extname, e.extversion, n.nspname FROM pg_extension e "
        "JOIN pg_namespace n ON n.oid = e.extnamespace"
    ).fetchall()
    return {name: (version, schema) for name, version, schema in rows}


# ------------------------------------------------------------------------ pg_extensions


async def test_extensions_lists_installed_and_available(app: Any, ready: Server) -> None:
    with superuser(ready) as conn:
        installed = _installed(conn)
    result = await call(pg_extensions, connection="ro")
    listed = {r["name"]: r for r in rows_as_dicts(result["installed"])}
    assert set(installed) <= set(listed)
    assert listed["plpgsql"]["schema"] == "pg_catalog"
    assert listed["pg_trgm"]["version"] == installed["pg_trgm"][0]
    assert listed["pg_trgm"]["comment"]

    available = {r["name"]: r for r in rows_as_dicts(result["available"])}
    assert not set(available) & set(listed)
    assert "citext" in available
    assert available["citext"]["install"] == (
        "role with CREATE on database" if ready.at_least(13) else "superuser"
    )
    assert available["citext"]["default_version"]
    assert "shared_preload_libraries" in result  # a superuser may read it

    only = await call(pg_extensions, connection="ro", only_installed=True)
    assert "available" not in only
    matched = await call(pg_extensions, connection="ro", pattern="trgm", only_installed=True)
    assert [r["name"] for r in rows_as_dicts(matched["installed"])] == ["pg_trgm"]
    nothing = await call(pg_extensions, connection="ro", pattern="zz_none", only_installed=True)
    assert nothing["installed"]["rows"] == []
    assert "hint" in nothing


async def test_extensions_report_an_available_update(app: Any, ready: Server) -> None:
    with superuser(ready) as conn:
        versions = conn.execute(
            "SELECT version, (SELECT default_version FROM pg_available_extensions "
            "WHERE name = 'citext') FROM pg_available_extension_versions "
            "WHERE name = 'citext' ORDER BY version"
        ).fetchall()
        older = [v for v, default in versions if v != default]
        if not older:
            pytest.skip("this server ships only one citext version")
        conn.execute("DROP EXTENSION IF EXISTS citext")
        try:
            conn.execute(f"CREATE EXTENSION citext VERSION '{older[0]}'")
            result = await call(pg_extensions, connection="ro", pattern="citext")
            row = rows_as_dicts(result["installed"])[0]
            assert row["version"] == older[0]
            assert row["update_to"] == versions[0][1]
            assert any("ALTER EXTENSION" in n for n in result["notes"])
        finally:
            conn.execute("DROP EXTENSION IF EXISTS citext")


async def test_extensions_as_unprivileged_role(app: Any, ready: Server) -> None:
    result = await call(pg_extensions, connection="limited", only_installed=True)
    assert "pg_trgm" in {r["name"] for r in rows_as_dicts(result["installed"])}
    assert "shared_preload_libraries" not in result


# ------------------------------------------------------------------- pg_extension_report


@pytest.fixture
def trigram_index(ready: Server) -> Iterator[None]:
    with superuser(ready) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        try:
            conn.execute(f"CREATE SCHEMA {SCHEMA}")
            conn.execute(f"CREATE TABLE {SCHEMA}.words (w text)")
            conn.execute(f"CREATE INDEX words_trgm ON {SCHEMA}.words USING gin (w gin_trgm_ops)")
            yield
        finally:
            conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


async def test_report_generic_part(app: Any, ready: Server, trigram_index: None) -> None:
    result = await call(pg_extension_report, extension="pg_trgm", connection="ro")
    head = result["extension"]
    assert head["name"] == "pg_trgm"
    assert head["schema"] == "public"
    assert head["owner"] == "postgres"
    assert head["comment"]
    objects = result["objects"]
    assert "similarity" in objects["functions"]["names"]
    assert objects["functions"]["count"] >= 10
    assert "%" in objects["operators"]["names"]
    assert any(n.endswith("gtrgm") for n in objects["types"]["names"])
    # Settings of a library that was not loaded in the session are not registered yet.
    assert all(name.startswith("pg_trgm.") for name in result.get("settings", {}))

    trigram = rows_as_dicts(result["details"]["trigram_indexes"])
    entry = next(t for t in trigram if t["index"] == f"{SCHEMA}.words_trgm")
    assert entry["table"] == f"{SCHEMA}.words"
    assert entry["method"] == "gin"
    assert entry["valid"] is True
    assert entry["size"]
    assert "gin_trgm_ops" in entry["definition"]


async def test_report_pg_buffercache_summary(app: Any, ready: Server) -> None:
    result = await call(pg_extension_report, extension="pg_buffercache", connection="ro")
    details = result["details"]
    assert details["buffers"] > 0
    assert 0 < details["used"] <= details["buffers"]
    assert 0 <= details["used_percent"] <= 100
    assert details["block_size"] == 8192
    top = rows_as_dicts(details["top_relations"])
    assert top
    assert top[0]["buffers"] >= top[-1]["buffers"]
    assert "pg_buffercache_pages" in result["objects"]["functions"]["names"]


async def test_report_pg_stat_statements_tells_whether_it_is_active(
    app: Any, ready: Server
) -> None:
    result = await call(pg_extension_report, extension="pg_stat_statements", connection="ro")
    details = result["details"]
    with superuser(ready) as conn:
        libraries = conn.execute("SHOW shared_preload_libraries").fetchone()
    assert libraries is not None
    loaded = "pg_stat_statements" in libraries[0]
    assert details["active"] is loaded
    if loaded:
        assert details["entries"] >= 1
        assert details["max_entries"] == int(result["settings"]["pg_stat_statements.max"])
    else:
        notes = " ".join(result["notes"])
        assert "shared_preload_libraries" in notes
        assert "entries" not in details


async def test_report_postgres_fdw_lists_servers_and_hides_mapping_options(
    app: Any, ready: Server
) -> None:
    with superuser(ready) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.execute(f"DROP SERVER IF EXISTS {SERVER_NAME} CASCADE")
        try:
            conn.execute(f"CREATE SCHEMA {SCHEMA}")
            conn.execute(
                f"CREATE SERVER {SERVER_NAME} FOREIGN DATA WRAPPER postgres_fdw "
                "OPTIONS (host 'remote.invalid', dbname 'remote', sslrootcert '/etc/ca.pem')"
            )
            conn.execute(
                f"CREATE USER MAPPING FOR postgres SERVER {SERVER_NAME} "
                f"OPTIONS (user 'remote_user', password '{MAPPING_PASSWORD}')"
            )
            conn.execute(
                f"CREATE FOREIGN TABLE {SCHEMA}.remote_t (id int) SERVER {SERVER_NAME} "
                "OPTIONS (table_name 'orders')"
            )
            for extension in ("postgres_fdw", "dblink"):
                text = await pg_extension_report(extension=extension, connection="ro")
                _no_secrets(text)
                assert "remote_user" not in text
                assert "/etc/ca.pem" not in text
            details = (await call(pg_extension_report, extension="postgres_fdw", connection="ro"))[
                "details"
            ]
        finally:
            conn.execute(f"DROP SERVER IF EXISTS {SERVER_NAME} CASCADE")
            conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    server = next(s for s in rows_as_dicts(details["servers"]) if s["server"] == SERVER_NAME)
    assert server["wrapper"] == "postgres_fdw"
    assert server["options"] == ["host=remote.invalid", "dbname=remote"]
    assert server["other_options"] == 1
    assert server["user_mappings"] == 1
    assert server["mapped_users"] == ["postgres"]
    assert server["foreign_tables"] == 1
    assert {"table": f"{SCHEMA}.remote_t", "server": SERVER_NAME} in rows_as_dicts(
        details["foreign_tables"]
    )


async def test_report_pointers_for_plain_extensions(app: Any, ready: Server) -> None:
    stattuple = await call(pg_extension_report, extension="pgstattuple", connection="ro")
    assert "whole relation" in stattuple["details"]["tip"]
    crypto = await call(pg_extension_report, extension="pgcrypto", connection="ro")
    assert "crypt" in crypto["details"]["tip"]
    assert "digest" in crypto["objects"]["functions"]["names"]
    unknown_detail = await call(pg_extension_report, extension="hstore", connection="limited")
    assert "details" not in unknown_detail  # generic part only
    assert unknown_detail["objects"]["types"]["count"] >= 1


async def test_report_explains_how_to_install(app: Any, ready: Server) -> None:
    with pytest.raises(ToolError) as available:
        await call(pg_extension_report, extension="citext", connection="ro")
    message = str(available.value)
    assert "CREATE EXTENSION citext" in message
    assert "not installed" in message
    with pytest.raises(ToolError) as missing:
        await call(pg_extension_report, extension="timescaledb", connection="ro")
    assert "not available on this server" in str(missing.value)
    with pytest.raises(ToolError) as similar:
        await call(pg_extension_report, extension="trgm", connection="ro")
    assert "not available" in str(similar.value)
    assert "pg_trgm" in str(similar.value)


# ---------------------------------------------------------------------- extras (PostgreSQL 18)


async def test_extras_vector(extras_app: Any) -> None:
    result = await call(pg_extension_report, extension="pgvector", connection="ro")
    assert result["extension"]["name"] == "vector"
    columns = rows_as_dicts(result["details"]["vector_columns"])
    assert columns == [
        {
            "table": "shop.embeddings",
            "column": "embedding",
            "type": "vector(3)",
            "dimensions": 3,
            "indexed": True,
        }
    ]
    index = rows_as_dicts(result["details"]["indexes"])[0]
    assert index["index"] == "shop.embeddings_hnsw_idx"
    assert index["method"] == "hnsw"
    assert index["options"] == "defaults"
    assert index["valid"] is True
    assert "hnswhandler" in result["objects"]["functions"]["names"]
    assert result["objects"]["types"]["count"] >= 2


async def test_extras_vector_hint_for_unindexed_column(extras: Server, extras_app: Any) -> None:
    with superuser(extras) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        try:
            conn.execute(f"CREATE SCHEMA {SCHEMA}")
            conn.execute(f"CREATE TABLE {SCHEMA}.v (id int, e halfvec(8), f vector(4), g vector)")
            conn.execute(
                f"CREATE INDEX ON {SCHEMA}.v USING ivfflat (f vector_l2_ops) WITH (lists = 7)"
            )
            result = await call(pg_extension_report, extension="vector", connection="ro")
        finally:
            conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    columns = {c["column"]: c for c in rows_as_dicts(result["details"]["vector_columns"])}
    assert columns["e"]["dimensions"] == 8
    assert columns["e"]["indexed"] is False
    assert columns["f"]["dimensions"] == 4
    assert columns["f"]["indexed"] is True
    assert columns["g"]["dimensions"] is None
    assert columns["g"]["indexed"] is False
    assert "sequentially" in result["details"]["hint"]
    ivfflat = next(
        i for i in rows_as_dicts(result["details"]["indexes"]) if i["method"] == "ivfflat"
    )
    assert ivfflat["options"] == ["lists=7"]


async def test_extras_postgis(extras_app: Any) -> None:
    result = await call(pg_extension_report, extension="postgis", connection="ro")
    details = result["details"]
    assert re.match(r"^3\.\d+", details["version"])
    assert "GEOS" in details["full_version"]
    stores = next(
        c for c in rows_as_dicts(details["spatial_columns"]) if c["table"] == "shop.stores"
    )
    assert stores == {
        "table": "shop.stores",
        "column": "geom",
        "type": "POINT",
        "srid": 4326,
        "dims": 2,
        "indexed": True,
    }
    index = next(
        i for i in rows_as_dicts(details["spatial_indexes"]) if i["index"] == "shop.stores_geom_idx"
    )
    assert index["method"] == "gist"
    assert index["table"] == "shop.stores"
    assert "st_distance" in result["objects"]["functions"]["names"] or (
        result["objects"]["functions"]["count"] > 100
    )
    assert "public.spatial_ref_sys" in result["objects"]["tables"]["names"]


async def test_extras_pg_cron_jobs(extras_app: Any) -> None:
    result = await call(pg_extension_report, extension="pg_cron", connection="ro")
    jobs = rows_as_dicts(result["details"]["jobs"])
    job = next(j for j in jobs if j["name"] == "nightly-analyze")
    assert job["schedule"] == "0 3 * * *"
    assert job["command"] == "ANALYZE shop.orders"
    assert job["active"] is True
    assert job["database"] == "pgharbor_test"
    assert "last_status" in job  # the run history is part of the answer
    assert result["details"]["failed_runs_last_24h"] >= 0
    assert result["settings"]["cron.database_name"]
    assert "schedule" in result["objects"]["functions"]["names"]


async def test_extras_pg_partman_without_tables(extras_app: Any) -> None:
    result = await call(pg_extension_report, extension="pg_partman", connection="ro")
    details = result["details"]
    assert details["version"] == result["extension"]["version"]
    assert details["configured_tables"]["rows"] == []
    assert "create_parent" in details["hint"]
    assert "parent_table" in details["configured_tables"]["columns"]
    assert result["extension"]["schema"] == "partman"
    assert "create_parent" in result["objects"]["functions"]["names"]


async def test_extras_pg_partman_with_a_managed_table(extras: Server, extras_app: Any) -> None:
    with superuser(extras) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        try:
            conn.execute(f"CREATE SCHEMA {SCHEMA}")
            conn.execute(
                f"CREATE TABLE {SCHEMA}.events (id bigint, at timestamptz NOT NULL) "
                "PARTITION BY RANGE (at)"
            )
            conn.execute(
                f"SELECT partman.create_parent(p_parent_table := '{SCHEMA}.events', "
                "p_control := 'at', p_interval := '1 day', p_premake := 2)"
            )
            result = await call(pg_extension_report, extension="pg_partman", connection="ro")
        finally:
            conn.execute(f"DELETE FROM partman.part_config WHERE parent_table = '{SCHEMA}.events'")
            conn.execute(f"DROP TABLE IF EXISTS partman.template_{SCHEMA}_events")
            conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    tables = rows_as_dicts(result["details"]["configured_tables"])
    entry = next(t for t in tables if t["parent_table"] == f"{SCHEMA}.events")
    assert entry["control"] == "at"
    assert entry["partition_interval"] == "1 day"
    assert entry["premake"] == 2
    assert entry["partitions"] >= 3


async def test_extras_pgaudit_and_hypopg_and_others(extras_app: Any) -> None:
    audit = await call(pg_extension_report, extension="pgaudit", connection="ro")
    assert audit["details"]["active"] is False
    assert audit["details"]["session_audit_classes"] == "none"
    assert audit["settings"]["pgaudit.log"] == "none"
    assert "audits nothing" in audit["details"]["hint"]

    hypopg = await call(pg_extension_report, extension="hypopg", connection="ro")
    assert hypopg["details"]["hypothetical_indexes"]["rows"] == []
    assert "session" in hypopg["details"]["note"]
    assert "hypopg_create_index" in hypopg["objects"]["functions"]["names"]

    statements = await call(pg_extension_report, extension="pg_stat_statements", connection="ro")
    assert statements["details"]["active"] is True
    assert statements["details"]["entries"] >= 1
    assert "notes" not in statements  # the library is preloaded on this server

    unknown = await call(pg_extension_report, extension="pg_wait_sampling", connection="ro")
    assert "details" not in unknown
    assert unknown["objects"]["functions"]["count"] >= 1


# --------------------------------------------------------------------- pg_integrity_check


@pytest.fixture
def broken_schema(ready: Server) -> Iterator[None]:
    """Objects with known integrity problems, in a schema of their own."""
    with superuser(ready) as conn:
        v = conn.info.server_version
        conn.execute(f"DROP SCHEMA IF EXISTS {INTEGRITY} CASCADE")
        try:
            conn.execute(f"CREATE SCHEMA {INTEGRITY}")
            conn.execute(f"CREATE TABLE {INTEGRITY}.t (id int, v int)")
            conn.execute(
                f"ALTER TABLE {INTEGRITY}.t ADD CONSTRAINT v_positive CHECK (v > 0) NOT VALID"
            )
            conn.execute(
                f"CREATE FUNCTION {INTEGRITY}.noop() RETURNS trigger LANGUAGE plpgsql "
                "AS $$ BEGIN RETURN NEW; END $$"
            )
            conn.execute(
                f"CREATE TRIGGER quiet BEFORE INSERT ON {INTEGRITY}.t "
                f"FOR EACH ROW EXECUTE PROCEDURE {INTEGRITY}.noop()"
            )
            conn.execute(f"ALTER TABLE {INTEGRITY}.t DISABLE TRIGGER quiet")
            conn.execute(f"CREATE TABLE {INTEGRITY}.parent (id int PRIMARY KEY)")
            conn.execute(
                f"CREATE TABLE {INTEGRITY}.child (id int PRIMARY KEY, "
                f"parent_id int REFERENCES {INTEGRITY}.parent (id))"
            )
            conn.execute(f"CREATE TABLE {INTEGRITY}.dups (k int)")
            conn.execute(f"INSERT INTO {INTEGRITY}.dups VALUES (1), (1)")
            # The failed build leaves an invalid index behind, which is the point.
            with contextlib.suppress(psycopg.errors.UniqueViolation):
                conn.execute(f"CREATE UNIQUE INDEX CONCURRENTLY dups_k ON {INTEGRITY}.dups (k)")
            conn.execute(f"CREATE SEQUENCE {INTEGRITY}.lonely_seq")
            conn.execute(f"CREATE SEQUENCE {INTEGRITY}.used_seq")
            conn.execute(
                f"CREATE TABLE {INTEGRITY}.uses (id int DEFAULT nextval('{INTEGRITY}.used_seq'))"
            )
            conn.execute(f"CREATE TABLE {INTEGRITY}.serial_t (id serial PRIMARY KEY)")
            if v >= 100000:
                conn.execute(
                    f"CREATE TABLE {INTEGRITY}.ident (id int GENERATED ALWAYS AS IDENTITY)"
                )
            yield
        finally:
            conn.execute(f"DROP SCHEMA IF EXISTS {INTEGRITY} CASCADE")


def _problems(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["check"]: p for p in result.get("problems", [])}


async def test_integrity_finds_planted_problems(
    app: Any, ready: Server, broken_schema: None
) -> None:
    result = await call(pg_integrity_check, connection="ro", schema=INTEGRITY)
    problems = _problems(result)
    assert result["scope"] == f"schema {INTEGRITY}"

    constraints = rows_as_dicts(problems["not_valid_constraints"]["items"])
    assert constraints == [{"table": f"{INTEGRITY}.t", "constraint": "v_positive", "type": "check"}]
    assert "VALIDATE CONSTRAINT" in problems["not_valid_constraints"]["fix"]

    triggers = rows_as_dicts(problems["disabled_triggers"]["items"])
    assert {"table": f"{INTEGRITY}.t", "trigger": "quiet", "kind": "user trigger"} in triggers

    invalid = rows_as_dicts(problems["invalid_indexes"]["items"])
    assert len(invalid) == 1
    assert invalid[0]["index"] == f"{INTEGRITY}.dups_k"
    assert invalid[0]["valid"] is False
    assert invalid[0]["table"] == f"{INTEGRITY}.dups"

    no_pk = {r["table"] for r in rows_as_dicts(problems["no_primary_key"]["items"])}
    assert {f"{INTEGRITY}.t", f"{INTEGRITY}.dups", f"{INTEGRITY}.uses"} <= no_pk
    assert f"{INTEGRITY}.child" not in no_pk
    assert f"{INTEGRITY}.serial_t" not in no_pk
    if ready.at_least(10):
        assert f"{INTEGRITY}.ident" in no_pk

    sequences = {
        r["sequence"]: r["status"] for r in rows_as_dicts(problems["unowned_sequences"]["items"])
    }
    assert sequences[f"{INTEGRITY}.lonely_seq"] == "no column uses it"
    assert sequences[f"{INTEGRITY}.used_seq"] == (
        f"default of {INTEGRITY}.uses.id, but not owned by it"
    )
    assert f"{INTEGRITY}.serial_t_id_seq" not in sequences
    assert f"{INTEGRITY}.ident_id_seq" not in sequences

    assert problems["unowned_sequences"]["severity"] == "info"
    severities = [p["severity"] for p in result["problems"]]
    assert severities == sorted(severities, key=["critical", "warning", "info"].index)
    assert "prepared_transactions" in result["clean_checks"]
    assert "problem(s) found" in result["summary"]


async def test_integrity_disabled_foreign_key_triggers_are_critical(
    app: Any, ready: Server, broken_schema: None
) -> None:
    with superuser(ready) as conn:
        conn.execute(f"ALTER TABLE {INTEGRITY}.child DISABLE TRIGGER ALL")
    result = await call(pg_integrity_check, connection="ro", schema=INTEGRITY)
    problem = _problems(result)["disabled_triggers"]
    assert problem["severity"] == "critical"
    kinds = {r["kind"] for r in rows_as_dicts(problem["items"])}
    assert kinds == {"user trigger", "foreign key enforcement"}
    assert any(
        r["table"] == f"{INTEGRITY}.child" and r["kind"] == "foreign key enforcement"
        for r in rows_as_dicts(problem["items"])
    )


async def test_integrity_clean_schema_reports_clean(app: Any, ready: Server) -> None:
    with superuser(ready) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {INTEGRITY} CASCADE")
        try:
            conn.execute(f"CREATE SCHEMA {INTEGRITY}")
            conn.execute(f"CREATE TABLE {INTEGRITY}.fine (id serial PRIMARY KEY, v int)")
            result = await call(pg_integrity_check, connection="ro", schema=INTEGRITY)
        finally:
            conn.execute(f"DROP SCHEMA IF EXISTS {INTEGRITY} CASCADE")
    assert not _problems(result) or set(_problems(result)) <= {"data_checksums"}
    assert {"invalid_indexes", "not_valid_constraints", "no_primary_key"} <= set(
        result["clean_checks"]
    )
    assert "unowned_sequences" in result["clean_checks"]


async def test_integrity_version_dependent_checks(app: Any, ready: Server) -> None:
    result = await call(pg_integrity_check, connection="ro")
    with superuser(ready) as conn:
        checksums = conn.execute("SHOW data_checksums").fetchone()
    assert checksums is not None
    problems = _problems(result)
    assert ("data_checksums" in problems) == (checksums[0] == "off")
    notes = " ".join(result.get("notes", []))
    if ready.at_least(12):
        assert "checksum_failures" in result["clean_checks"] + list(problems)
    else:
        assert "Checksum failure counters need PostgreSQL 12" in notes
    if ready.at_least(10):
        assert "collation_versions" in result["clean_checks"] + list(problems)
    else:
        assert "Collation version tracking needs PostgreSQL 10" in notes
    # The sample database has tables without a primary key on purpose.
    assert "shop.no_pk" in {r["table"] for r in rows_as_dicts(problems["no_primary_key"]["items"])}


async def test_integrity_detects_a_changed_collation_version(app: Any, ready: Server) -> None:
    if not ready.at_least(10):
        pytest.skip("collation versions are tracked from PostgreSQL 10")
    with superuser(ready) as conn:
        row = conn.execute(
            "SELECT c.oid, n.nspname || '.' || c.collname, c.collversion FROM pg_collation c "
            "JOIN pg_namespace n ON n.oid = c.collnamespace "
            "WHERE c.collversion IS NOT NULL ORDER BY c.oid LIMIT 1"
        ).fetchone()
        if row is None:
            pytest.skip("this image records no collation versions")
        oid, name, recorded = row
        # Only this database's own copy of the catalog row is touched, and it is restored.
        conn.execute("UPDATE pg_collation SET collversion = '0.0-pgh-c' WHERE oid = %s", [oid])
        try:
            result = await call(pg_integrity_check, connection="ro")
        finally:
            conn.execute("UPDATE pg_collation SET collversion = %s WHERE oid = %s", [recorded, oid])
    problem = _problems(result)["collation_versions"]
    entry = next(r for r in rows_as_dicts(problem["items"]) if r["object"] == name)
    assert entry["kind"] == "collation"
    assert entry["recorded"] == "0.0-pgh-c"
    assert entry["actual"] == recorded
    assert "REINDEX" in problem["fix"]


async def test_integrity_with_prepared_transactions(app: Any, ready: Server) -> None:
    with superuser(ready) as conn:
        allowed = conn.execute("SHOW max_prepared_transactions").fetchone()
        assert allowed is not None
        if int(allowed[0]) == 0:
            pytest.skip("max_prepared_transactions is 0 on this server")
        conn.execute("DROP SCHEMA IF EXISTS pgh_c_int_px CASCADE")
        conn.execute("CREATE SCHEMA pgh_c_int_px")
        conn.execute("CREATE TABLE pgh_c_int_px.x (id int PRIMARY KEY)")
        conn.autocommit = False
        conn.execute("INSERT INTO pgh_c_int_px.x VALUES (1)")
        conn.execute("PREPARE TRANSACTION 'pgh_c_forgotten'")
        conn.autocommit = True
        try:
            result = await call(pg_integrity_check, connection="ro")
        finally:
            conn.execute("ROLLBACK PREPARED 'pgh_c_forgotten'")
            conn.execute("DROP SCHEMA IF EXISTS pgh_c_int_px CASCADE")
    items = rows_as_dicts(_problems(result)["prepared_transactions"]["items"])
    assert any(i["gid"] == "pgh_c_forgotten" and i["age"] for i in items)


async def test_integrity_amcheck_on_one_index(app: Any, ready: Server) -> None:
    hint = await call(pg_integrity_check, connection="ro", schema="shop")
    if not ready.at_least(10):
        with pytest.raises(ToolError) as old:
            await call(pg_integrity_check, connection="ro", index="shop.orders_pkey")
        assert "needs PostgreSQL 10" in str(old.value)
        assert "amcheck_hint" not in hint
        return
    assert "amcheck_hint" in hint

    result = await call(pg_integrity_check, connection="ro", index="shop.orders_customer_idx")
    check = result["amcheck"]
    assert check["index"] == "shop.orders_customer_idx"
    assert check["status"] == "ok"
    assert check["size"]
    assert "amcheck_hint" not in result
    unqualified = await call(pg_integrity_check, connection="ro", index="orders_pkey")
    assert unqualified["amcheck"]["index"] == "shop.orders_pkey"

    with pytest.raises(ToolError) as gin:
        await call(pg_integrity_check, connection="ro", index="shop.products_attrs_idx")
    assert "b-tree indexes only" in str(gin.value)
    assert "gin" in str(gin.value)
    with pytest.raises(ToolError) as table:
        await call(pg_integrity_check, connection="ro", index="shop.orders")
    assert "No index named" in str(table.value)
    with pytest.raises(ToolError) as role:
        await call(pg_integrity_check, connection="limited", index="shop.orders_pkey")
    assert "superuser" in str(role.value)


async def test_integrity_as_an_unprivileged_role(
    app: Any, ready: Server, broken_schema: None
) -> None:
    result = await call(pg_integrity_check, connection="limited", schema=INTEGRITY)
    problems = _problems(result)
    assert {"not_valid_constraints", "disabled_triggers", "invalid_indexes"} <= set(problems)
    text = await pg_integrity_check(connection="limited")
    _no_secrets(text)
