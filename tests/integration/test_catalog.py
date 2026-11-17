"""Catalog tools on every PostgreSQL version of the matrix."""

import contextlib
import json
import re
from collections.abc import Iterator
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from pgharbor.tools.catalog import (
    pg_compare_schemas,
    pg_dependencies,
    pg_describe_object,
    pg_get_ddl,
    pg_list_objects,
    pg_list_partitions,
    pg_relationships,
    pg_schema_overview,
    pg_search,
    pg_search_source,
)

from ..conftest import Server, call, rows_as_dicts, superuser

pytestmark = pytest.mark.integration

DRAFT = "pgh_catalog_draft"


@pytest.fixture
def draft(ready: Server) -> Iterator[str]:
    """An empty schema of its own, dropped afterwards."""
    with superuser(ready) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {DRAFT} CASCADE")
        conn.execute(f"CREATE SCHEMA {DRAFT}")
    try:
        yield DRAFT
    finally:
        with superuser(ready) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {DRAFT} CASCADE")


@contextlib.contextmanager
def scratch(ready: Server, setup: list[str], teardown: list[str]) -> Iterator[None]:
    """Run ``setup`` with a superuser connection, and ``teardown`` even if the test fails."""
    with superuser(ready) as conn:
        for statement in teardown:
            with contextlib.suppress(Exception):
                conn.execute(statement)
        for statement in setup:
            conn.execute(statement)
    try:
        yield
    finally:
        with superuser(ready) as conn:
            for statement in teardown:
                with contextlib.suppress(Exception):
                    conn.execute(statement)


async def listing(**arguments: Any) -> list[dict[str, Any]]:
    return rows_as_dicts(await call(pg_list_objects, connection="ro", **arguments))


# ------------------------------------------------------------------------------- pg_list_objects


async def test_list_functions(app: Any) -> None:
    rows = {r["name"]: r for r in await listing(kind="function", schema="shop")}
    assert {"fmt_price", "order_total", "touch_order", "set_updated_at", "customer_label"} <= set(
        rows
    )
    assert rows["fmt_price"]["volatility"] == "immutable"
    assert rows["fmt_price"]["returns"] == "text"
    assert rows["order_total"]["volatility"] == "stable"
    assert rows["order_total"]["arguments"] == "bigint"
    assert rows["touch_order"]["volatility"] == "volatile"
    assert rows["set_updated_at"]["returns"] == "trigger"
    assert rows["set_updated_at"]["language"] == "plpgsql"
    assert rows["order_total"]["security_definer"] is False
    assert "extension" not in rows["order_total"]  # empty columns are dropped
    assert "procedure" not in {r["name"] for r in rows.values()}


async def test_list_filters_and_paging(app: Any) -> None:
    matched = await listing(kind="function", schema="shop", pattern="order*")
    assert [r["name"] for r in matched] == ["order_total"]
    contains = await listing(kind="function", schema="shop", pattern="updated")
    assert [r["name"] for r in contains] == ["set_updated_at"]

    first = await call(pg_list_objects, kind="function", connection="ro", schema="shop", limit=2)
    assert len(first["rows"]) == 2
    assert first["has_more"] is True
    assert first["next_offset"] == 2
    second = await call(
        pg_list_objects, kind="function", connection="ro", schema="shop", limit=2, offset=2
    )
    names = {r[1] for r in first["rows"]} | {r[1] for r in second["rows"]}
    assert len(names) == 4

    empty = await call(pg_list_objects, kind="view", connection="ro", schema="nope")
    assert empty["rows"] == []
    assert any("Nothing matched" in n for n in empty["notes"])
    # Extension code comes last, and is hidden when the schema holds none.
    everywhere = await listing(kind="function", limit=1000)
    assert any(r["schema"] == "shop" for r in everywhere)
    assert all(r["schema"] != "pg_catalog" for r in everywhere)


async def test_list_sequences(app: Any, ready: Server) -> None:
    rows = {r["name"]: r for r in await listing(kind="sequence", schema="shop")}
    assert rows["orders_id_seq"]["owned_by"] == "shop.orders.id"
    assert rows["invoice_seq"]["owned_by"] is None
    if ready.at_least(10):
        assert rows["tiny_seq"]["data_type"] == "smallint"
        assert rows["tiny_seq"]["last_value"] == 95
        assert rows["invoice_seq"]["increment"] == 5
        assert rows["invoice_seq"]["last_value"] is None
        assert rows["invoices_id_seq"]["owned_by"] == "shop.invoices.id"
    else:
        result = await call(pg_list_objects, kind="sequence", connection="ro", schema="shop")
        assert any("9.6" in n for n in result["notes"])


async def test_list_views_matviews_and_types(app: Any) -> None:
    views = {r["name"]: r for r in await listing(kind="view", schema="shop")}
    assert set(views) == {"order_summary", "customers_public"}
    assert views["order_summary"]["columns"] == 6
    assert views["order_summary"]["comment"] == "One row per order with customer name"
    matviews = await listing(kind="materialized_view", schema="analytics")
    assert [(r["name"], r["populated"]) for r in matviews] == [("daily_sales", True)]
    assert matviews[0]["size"]

    types = {r["name"]: r["kind"] for r in await listing(kind="type", schema="shop")}
    assert types == {"order_status": "enum", "email": "domain", "address": "composite"}
    enums = await listing(kind="enum", schema="shop")
    assert enums[0]["labels"] == ["new", "paid", "shipped", "cancelled"]
    domains = await listing(kind="domain", schema="shop")
    assert domains[0]["base_type"] == "text"
    assert "VALUE" in domains[0]["checks"][0]


async def test_list_indexes_and_triggers(app: Any) -> None:
    indexes = {r["name"]: r for r in await listing(kind="index", schema="shop", pattern="orders")}
    assert indexes["orders_pkey"]["unique"] is True
    assert indexes["orders_pkey"]["method"] == "btree"
    assert indexes["orders_paid_idx"]["partial"] is True
    assert indexes["orders_customer_idx"]["table"] == "orders"
    assert indexes["orders_customer_idx"]["columns"] == "customer_id"
    gin = {r["name"]: r for r in await listing(kind="index", schema="shop", pattern="attrs")}
    assert gin["products_attrs_idx"]["method"] == "gin"

    triggers = await listing(kind="trigger", schema="shop")
    trigger = next(r for r in triggers if r["name"] == "orders_updated_at")
    assert (trigger["table"], trigger["timing"], trigger["events"]) == (
        "orders",
        "BEFORE",
        "UPDATE",
    )
    assert trigger["level"] == "row"
    assert trigger["state"] == "enabled"
    assert trigger["function"] == "shop.set_updated_at"


async def test_list_policies_extensions_and_instance_objects(app: Any, ready: Server) -> None:
    policies = await listing(kind="policy", schema="shop")
    assert policies[0]["name"] == "tenant_isolation"
    assert policies[0]["command"] == "ALL"
    assert policies[0]["roles"] == ["public"]
    assert "app.tenant" in policies[0]["using"]

    assert "plpgsql" in {r["name"] for r in await listing(kind="extension")}
    assert "pgharbor_test" in {r["name"] for r in await listing(kind="database")}
    assert {"plpgsql", "sql"} <= {r["name"] for r in await listing(kind="language")}
    assert "pg_default" in {r["name"] for r in await listing(kind="tablespace")}
    methods = {r["name"]: r["type"] for r in await listing(kind="access_method")}
    assert methods["btree"] == "index"
    assert methods["gin"] == "index"
    if ready.at_least(12):
        assert methods["heap"] == "table"

    operators = await listing(kind="operator", schema="trap")
    assert [(r["name"], r["left"], r["right"]) for r in operators] == [
        ("===", "integer", "integer"),
        ("@@@", "integer", "integer"),
    ]
    assert operators[1]["function"] == "trap.volatile_op"

    casts = await listing(kind="cast", pattern="bigint")
    assert any(r["source"] == "bigint" and r["target"] == "integer" for r in casts)
    assert all(
        not (r["source"] == "bigint" and r["target"] == "integer")
        for r in await listing(kind="cast")
    )
    collations = await listing(kind="collation", schema="pg_catalog", pattern="posix")
    assert "POSIX" in {r["name"] for r in collations}
    assert all(r["provider"] in ("libc", "icu", "builtin", "default") for r in collations)


async def test_list_objects_created_by_the_test(app: Any, ready: Server, draft: str) -> None:
    setup = [
        f"CREATE TABLE {draft}.t (a int, b int)",
        f"CREATE RULE pgh_cat_rule AS ON INSERT TO {draft}.t DO INSTEAD NOTHING",
        f"CREATE TEXT SEARCH CONFIGURATION {draft}.cfg (COPY = pg_catalog.simple)",
        f"CREATE AGGREGATE {draft}.pgh_sum(int) (SFUNC = int4pl, STYPE = int, INITCOND = '0')",
        "CREATE FUNCTION pgh_cat_evt() RETURNS event_trigger LANGUAGE plpgsql AS $$ BEGIN END $$",
        "CREATE EVENT TRIGGER pgh_cat_evt ON ddl_command_end WHEN TAG IN ('CREATE TABLE') "
        "EXECUTE PROCEDURE pgh_cat_evt()",
        "CREATE FOREIGN DATA WRAPPER pgh_cat_fdw",
        "CREATE SERVER pgh_cat_srv FOREIGN DATA WRAPPER pgh_cat_fdw "
        "OPTIONS (host 'db.example', password 'sekret_pw_77')",
        f"CREATE FOREIGN TABLE {draft}.ft (a int) SERVER pgh_cat_srv",
    ]
    teardown = [
        "DROP EVENT TRIGGER IF EXISTS pgh_cat_evt",
        "DROP FUNCTION IF EXISTS pgh_cat_evt()",
        "DROP FOREIGN DATA WRAPPER IF EXISTS pgh_cat_fdw CASCADE",
    ]
    if ready.at_least(10):
        setup += [
            f"CREATE STATISTICS {draft}.pgh_stat (dependencies) ON a, b FROM {draft}.t",
            f"CREATE PUBLICATION pgh_cat_pub FOR TABLE {draft}.t",
            "CREATE SUBSCRIPTION pgh_cat_sub CONNECTION "
            "'host=127.0.0.1 port=1 dbname=x password=sekret_conn_55' PUBLICATION pgh_cat_pub "
            "WITH (connect = false)",
        ]
        teardown = [
            "ALTER SUBSCRIPTION pgh_cat_sub DISABLE",
            "ALTER SUBSCRIPTION pgh_cat_sub SET (slot_name = NONE)",
            "DROP SUBSCRIPTION IF EXISTS pgh_cat_sub",
            "DROP PUBLICATION IF EXISTS pgh_cat_pub",
            *teardown,
        ]
    with scratch(ready, setup, teardown):
        rules = await listing(kind="rule", schema=draft)
        assert [(r["table"], r["name"], r["event"], r["instead"]) for r in rules] == [
            ("t", "pgh_cat_rule", "INSERT", True)
        ]
        configs = await listing(kind="text_search_config", schema=draft)
        assert [(r["name"], r["parser"]) for r in configs] == [("cfg", "default")]
        aggregates = await listing(kind="aggregate", schema=draft)
        assert aggregates[0]["name"] == "pgh_sum"
        assert aggregates[0]["state_type"] == "integer"
        events = await listing(kind="event_trigger")
        assert any(r["name"] == "pgh_cat_evt" and r["event"] == "ddl_command_end" for r in events)

        servers = await listing(kind="foreign_server")
        server = next(r for r in servers if r["name"] == "pgh_cat_srv")
        assert server["wrapper"] == "pgh_cat_fdw"
        assert "host=db.example" in server["options"]
        assert "sekret_pw_77" not in json.dumps(servers)
        assert "pgh_cat_fdw" in {r["name"] for r in await listing(kind="foreign_data_wrapper")}
        tables = await listing(kind="foreign_table", schema=draft)
        assert [(r["name"], r["server"], r["wrapper"]) for r in tables] == [
            ("ft", "pgh_cat_srv", "pgh_cat_fdw")
        ]

        if ready.at_least(10):
            stats = await listing(kind="statistics", schema=draft)
            assert stats[0]["kinds"] == ["dependencies"]
            assert stats[0]["columns"] == ["a", "b"]
            publications = await listing(kind="publication")
            publication = next(r for r in publications if r["name"] == "pgh_cat_pub")
            assert "insert" in publication["operations"]
            assert publication["tables"] == 1
            result = await call(pg_list_objects, kind="subscription", connection="ro")
            subscriptions = rows_as_dicts(result)
            subscription = next(r for r in subscriptions if r["name"] == "pgh_cat_sub")
            assert subscription["publications"] == ["pgh_cat_pub"]
            assert subscription["enabled"] is False
            assert "sekret_conn_55" not in json.dumps(result)
            assert "conninfo" not in json.dumps(result)


async def test_list_kinds_missing_on_old_servers(app: Any, ready: Server) -> None:
    if ready.at_least(11):
        procedures = await listing(kind="procedure", schema="shop")
        assert [r["name"] for r in procedures] == ["archive_orders"]
        assert procedures[0]["arguments"].endswith("before date")
    else:
        result = await call(pg_list_objects, kind="procedure", connection="ro")
        assert result["rows"] == []
        assert "11 or newer" in result["notes"][0]
    if not ready.at_least(10):
        for kind in ("publication", "subscription", "statistics"):
            result = await call(pg_list_objects, kind=kind, connection="ro")
            assert result["rows"] == []
            assert "10 or newer" in result["notes"][0]


# -------------------------------------------------------------------------- pg_describe_object


async def test_describe_function_overloads_and_source(app: Any, ready: Server, draft: str) -> None:
    with superuser(ready) as conn:
        conn.execute(
            f"CREATE FUNCTION {draft}.fmt_price(numeric, text) RETURNS text "
            "LANGUAGE sql IMMUTABLE SECURITY DEFINER PARALLEL SAFE COST 7 "
            "SET search_path = pg_catalog AS $$ SELECT $2 || $1::text $$"
        )
        conn.execute(
            f"CREATE FUNCTION {draft}.fmt_price(numeric) RETURNS text LANGUAGE sql AS $$ SELECT 'x'::text $$"
        )
    result = await call(
        pg_describe_object, name=f"{draft}.fmt_price", kind="function", connection="ro"
    )
    signatures = {o["signature"] for o in result["overloads"]}
    assert signatures == {f"{draft}.fmt_price(numeric)", f"{draft}.fmt_price(numeric, text)"}
    two = next(o for o in result["overloads"] if o["signature"].endswith("text)"))
    assert two["security_definer"] is True
    assert two["parallel"] == "safe"
    assert two["cost"] == 7
    assert two["volatility"] == "immutable"
    assert two["config"] == ["search_path=pg_catalog"]
    assert "$2 || $1::text" in two["source"]

    one = await call(
        pg_describe_object, name=f"{draft}.fmt_price(numeric)", kind="function", connection="ro"
    )
    assert len(one["overloads"]) == 1

    order_total = await call(
        pg_describe_object,
        name="shop.order_total",
        kind="function",
        connection="ro",
        max_source_chars=30,
    )
    only = order_total["overloads"][0]
    assert only["comment"] == "Sum of the items of an order"
    assert only["returns"] == "numeric"
    assert len(only["source"]) == 30
    assert "30 of" not in only["source_truncated"]
    assert "characters in total" in only["source_truncated"]

    if ready.at_least(11):
        procedure = await call(
            pg_describe_object, name="shop.archive_orders", kind="procedure", connection="ro"
        )
        assert procedure["overloads"][0]["kind"] == "procedure"
        assert "DELETE FROM shop.orders" in procedure["overloads"][0]["source"]
    else:
        with pytest.raises(ToolError, match="11 or newer"):
            await call(pg_describe_object, name="x", kind="procedure", connection="ro")


async def test_describe_aggregate(app: Any, ready: Server, draft: str) -> None:
    with superuser(ready) as conn:
        conn.execute(
            f"CREATE AGGREGATE {draft}.pgh_sum(int) (SFUNC = int4pl, STYPE = int, INITCOND = '5')"
        )
    result = await call(
        pg_describe_object, name=f"{draft}.pgh_sum", kind="aggregate", connection="ro"
    )
    aggregate = result["overloads"][0]["aggregate"]
    assert aggregate["transition"] == "int4pl"
    assert aggregate["state_type"] == "integer"
    assert aggregate["initial_value"] == "5"
    assert "source" not in result["overloads"][0]


async def test_describe_object_errors(app: Any, ready: Server) -> None:
    with pytest.raises(ToolError) as missing:
        await call(pg_describe_object, name="shop.order_tot", kind="function", connection="ro")
    assert "shop.order_total" in str(missing.value)
    with pytest.raises(ToolError, match="signature"):
        await call(pg_describe_object, name="shop.order_total(", kind="function", connection="ro")
    with pytest.raises(ToolError, match="No function matches the signature"):
        await call(
            pg_describe_object, name="shop.order_total(text)", kind="function", connection="ro"
        )
    with pytest.raises(ToolError, match="pg_describe_table"):
        await call(pg_describe_object, name="shop.orders", kind="table", connection="ro")
    with pytest.raises(ToolError, match="pg_describe_table"):
        await call(pg_describe_object, name="shop.orders", kind="type", connection="ro")
    with pytest.raises(ToolError, match="No sequence named"):
        await call(pg_describe_object, name="shop.nope_seq", kind="sequence", connection="ro")
    with pytest.raises(ToolError, match="No schema named"):
        await call(pg_describe_object, name="shopp", kind="schema", connection="ro")
    with pytest.raises(ToolError, match="No trigger named"):
        await call(pg_describe_object, name="nope_trigger", kind="trigger", connection="ro")
    with pytest.raises(ToolError, match="No extension named"):
        await call(pg_describe_object, name="no_such_extension", kind="extension", connection="ro")
    with pytest.raises(ToolError, match="Invalid object name"):
        await call(pg_describe_object, name="a.b.c.d", kind="function", connection="ro")


async def test_describe_sequences(app: Any, ready: Server) -> None:
    tiny = await call(pg_describe_object, name="shop.tiny_seq", kind="sequence", connection="ro")
    assert tiny["maximum"] == 100
    assert tiny["last_value"] == 95
    assert tiny["percent_used"] == 95.0
    assert tiny["cycle"] is False
    assert any("80%" in n for n in tiny["notes"])
    assert tiny["data_type"] == ("smallint" if ready.at_least(10) else "bigint")

    invoice = await call(
        pg_describe_object, name="shop.invoice_seq", kind="sequence", connection="ro"
    )
    assert (invoice["start"], invoice["increment"], invoice["minimum"]) == (1000, 5, 1)
    assert invoice["percent_used"] == 0.0
    assert "last_value" not in invoice
    assert any("not been used" in n for n in invoice["notes"])
    assert "owned_by" not in invoice

    owned = await call(
        pg_describe_object, name="shop.orders_id_seq", kind="sequence", connection="ro"
    )
    assert owned["owned_by"] == "shop.orders.id"
    assert owned["last_value"] is None or owned["last_value"] >= 1
    if ready.at_least(10):
        identity = await call(
            pg_describe_object, name="shop.invoices_id_seq", kind="sequence", connection="ro"
        )
        assert identity["owned_by"] == "shop.invoices.id (identity column)"


async def test_describe_types(app: Any, ready: Server, draft: str) -> None:
    enum = await call(pg_describe_object, name="shop.order_status", kind="type", connection="ro")
    assert enum["kind"] == "enum"
    assert enum["labels"] == ["new", "paid", "shipped", "cancelled"]
    assert "shop.orders.status" in enum["used_by_columns"]
    assert enum["array_type"] == "_order_status"

    domain = await call(pg_describe_object, name="shop.email", kind="type", connection="ro")
    assert domain["kind"] == "domain"
    assert domain["base_type"] == "text"
    assert domain["checks"][0]["name"] == "email_check"
    assert "shop.customers.email" in domain["used_by_columns"]

    composite = await call(pg_describe_object, name="shop.address", kind="type", connection="ro")
    assert composite["kind"] == "composite"
    assert rows_as_dicts(composite["attributes"]) == [
        {"name": "street", "type": "text"},
        {"name": "city", "type": "text"},
        {"name": "zip", "type": "text"},
    ]

    with superuser(ready) as conn:
        conn.execute(f"CREATE TYPE {draft}.pgh_range AS RANGE (SUBTYPE = float8)")
    ranged = await call(pg_describe_object, name=f"{draft}.pgh_range", kind="type", connection="ro")
    assert ranged["kind"] == "range"
    assert ranged["range"]["subtype"] == "double precision"
    if ready.at_least(14):
        assert ranged["range"]["multirange"].endswith("pgh_multirange")

    base = await call(pg_describe_object, name="integer", kind="type", connection="ro")
    assert base["kind"] == "base"
    assert base["name"] == "pg_catalog.int4"


async def test_describe_index_trigger_extension_schema(app: Any, ready: Server) -> None:
    index = await call(
        pg_describe_object, name="shop.orders_paid_idx", kind="index", connection="ro"
    )
    assert index["table"] == "shop.orders"
    assert index["method"] == "btree"
    assert index["predicate"]
    assert "paid" in index["predicate"]
    assert index["valid"] is True
    assert index["columns"] == "placed_at"
    assert "WHERE" in index["definition"]
    assert "scans" in index["usage"]

    primary = await call(pg_describe_object, name="shop.orders_pkey", kind="index", connection="ro")
    assert primary["primary"] is True
    assert primary["constraint"] == "orders_pkey (primary key)"
    gin = await call(
        pg_describe_object, name="shop.products_attrs_idx", kind="index", connection="ro"
    )
    assert gin["method"] == "gin"

    trigger = await call(
        pg_describe_object, name="shop.orders.orders_updated_at", kind="trigger", connection="ro"
    )
    assert trigger["timing"] == "BEFORE"
    assert trigger["events"] == "UPDATE"
    assert trigger["level"] == "row"
    assert trigger["function"] == "shop.set_updated_at"
    assert "NEW.updated_at" in trigger["function_source"]
    assert trigger["function_language"] == "plpgsql"
    short = await call(
        pg_describe_object, name="orders_updated_at", kind="trigger", connection="ro"
    )
    assert short["table"] == "shop.orders"

    extension = await call(pg_describe_object, name="plpgsql", kind="extension", connection="ro")
    assert extension["schema"] == "pg_catalog"
    assert extension["objects"]["function"] >= 1
    if ready.at_least(10):
        with superuser(ready) as conn:
            available = {
                r[0] for r in conn.execute("SELECT name FROM pg_available_extensions").fetchall()
            }
            installed = {r[0] for r in conn.execute("SELECT extname FROM pg_extension").fetchall()}
        spare = sorted(available - installed)
        if spare:
            with pytest.raises(ToolError, match="CREATE EXTENSION"):
                await call(pg_describe_object, name=spare[0], kind="extension", connection="ro")

    schema = await call(pg_describe_object, name="shop", kind="schema", connection="ro")
    assert schema["comment"] == "Online shop: customers, products and orders"
    assert schema["objects"]["tables"] >= 7
    assert schema["objects"]["views"] == 2
    assert schema["objects"]["functions"] >= 5
    assert schema["objects"]["types"] == 3
    assert schema["owner"]


async def test_describe_trigger_name_that_exists_twice(app: Any, ready: Server, draft: str) -> None:
    with superuser(ready) as conn:
        conn.execute(f"CREATE TABLE {draft}.a (x int)")
        conn.execute(
            f"CREATE TRIGGER orders_updated_at BEFORE UPDATE ON {draft}.a "
            "FOR EACH ROW EXECUTE PROCEDURE shop.set_updated_at()"
        )
    with pytest.raises(ToolError) as caught:
        await call(pg_describe_object, name="orders_updated_at", kind="trigger", connection="ro")
    assert "several tables" in str(caught.value)
    assert f"{draft}.a.orders_updated_at" in str(caught.value)
    found = await call(
        pg_describe_object, name=f"{draft}.a.orders_updated_at", kind="trigger", connection="ro"
    )
    assert found["table"] == f"{draft}.a"


# ------------------------------------------------------------------------------------ pg_get_ddl


def _normalize(text: str | None, *schemas: str) -> str | None:
    if text is None:
        return None
    for schema in schemas:
        text = text.replace(f"{schema}.", "")
    return text


def _table_shape(conn: Any, schema: str, table: str, version: int, *schemas: str) -> dict[str, Any]:
    identity = "a.attidentity" if version >= 100000 else "''"
    generated = "a.attgenerated" if version >= 120000 else "''"
    columns = conn.execute(
        f"""SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
                   pg_get_expr(d.adbin, d.adrelid), {identity}, {generated}
              FROM pg_attribute a
              LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
             WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
             ORDER BY a.attnum""",
        [f"{schema}.{table}"],
    ).fetchall()
    constraints = conn.execute(
        """SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
            WHERE conrelid = %s::regclass AND contype <> 'n' ORDER BY conname""",
        [f"{schema}.{table}"],
    ).fetchall()
    indexes = conn.execute(
        """SELECT indexrelid::regclass::text, pg_get_indexdef(indexrelid) FROM pg_index
            WHERE indrelid = %s::regclass ORDER BY 1""",
        [f"{schema}.{table}"],
    ).fetchall()
    policies = conn.execute(
        """SELECT polname, polcmd, pg_get_expr(polqual, polrelid) FROM pg_policy
            WHERE polrelid = %s::regclass ORDER BY 1""",
        [f"{schema}.{table}"],
    ).fetchall()
    row_security = conn.execute(
        "SELECT relrowsecurity FROM pg_class WHERE oid = %s::regclass", [f"{schema}.{table}"]
    ).fetchone()
    return {
        "columns": [tuple(_normalize(str(v), *schemas) for v in row) for row in columns],
        "constraints": [tuple(_normalize(str(v), *schemas) for v in row) for row in constraints],
        "indexes": [tuple(_normalize(str(v), *schemas) for v in row) for row in indexes],
        "policies": [tuple(_normalize(str(v), *schemas) for v in row) for row in policies],
        "row_security": row_security[0] if row_security else None,
    }


@pytest.mark.parametrize(
    "table",
    ["customers", "products", "order_items", "orders", "tenant_docs", "reviews", "no_pk"],
)
async def test_ddl_of_a_table_rebuilds_an_equivalent_table(
    app: Any, ready: Server, draft: str, table: str
) -> None:
    result = await call(pg_get_ddl, name=f"shop.{table}", connection="ro")
    assert result["kind"] == "table"
    ddl = result["ddl"]
    assert ddl.startswith(f"CREATE TABLE shop.{table} (")
    rebuilt = ddl.replace(f"shop.{table}", f"{draft}.{table}")
    with superuser(ready) as conn:
        conn.execute(rebuilt)
        original = _table_shape(conn, "shop", table, conn.info.server_version, "shop", draft)
        copy = _table_shape(conn, draft, table, conn.info.server_version, "shop", draft)
    assert copy == original


async def test_ddl_of_orders_has_its_parts(app: Any) -> None:
    ddl = (await call(pg_get_ddl, name="shop.orders", connection="ro"))["ddl"]
    assert "id bigserial" in ddl
    assert "status shop.order_status DEFAULT 'new'::shop.order_status NOT NULL" in ddl
    assert "CONSTRAINT orders_pkey PRIMARY KEY (id)" in ddl
    assert "FOREIGN KEY (customer_id) REFERENCES shop.customers(id)" in ddl
    assert "CREATE INDEX orders_paid_idx ON shop.orders USING btree (placed_at) WHERE" in ddl
    assert "CREATE TRIGGER orders_updated_at BEFORE UPDATE ON shop.orders" in ddl
    assert "orders_pkey ON" not in ddl  # constraint indexes are not repeated

    customers = (await call(pg_get_ddl, name="shop.customers", connection="ro"))["ddl"]
    assert "COMMENT ON TABLE shop.customers IS 'People who buy from the shop';" in customers
    assert (
        "COMMENT ON COLUMN shop.customers.api_token IS 'Secret token of the customer API';"
        in customers
    )
    assert "email shop.email" in customers
    assert "customers_lower_name_idx" in customers

    secured = (await call(pg_get_ddl, name="shop.tenant_docs", connection="ro"))["ddl"]
    assert "ENABLE ROW LEVEL SECURITY;" in secured
    assert "CREATE POLICY tenant_isolation ON shop.tenant_docs" in secured


async def test_ddl_identity_generated_and_partitions(app: Any, ready: Server, draft: str) -> None:
    if ready.at_least(10):
        invoices = (await call(pg_get_ddl, name="shop.invoices", connection="ro"))["ddl"]
        assert "id bigint GENERATED ALWAYS AS IDENTITY" in invoices
    if ready.at_least(12):
        lines = (await call(pg_get_ddl, name="shop.lines", connection="ro"))["ddl"]
        assert "GENERATED ALWAYS AS ((net + tax)) STORED" in lines or (
            "GENERATED ALWAYS AS (net + tax) STORED" in lines
        )
    parent = (await call(pg_get_ddl, name="events.log", connection="ro"))["ddl"]
    child = (await call(pg_get_ddl, name="events.log_2025", connection="ro"))["ddl"]
    if ready.at_least(10):
        assert parent.rstrip().endswith("PARTITION BY RANGE (created_at);")
        assert child.startswith(
            "CREATE TABLE events.log_2025 PARTITION OF events.log FOR VALUES FROM"
        )
        assert "(\n" not in child
    else:
        assert "INHERITS (events.log)" in child
        assert "CONSTRAINT log_2025_created_at_check CHECK" in child
    with superuser(ready) as conn:
        conn.execute(parent.replace("events.", f"{draft}."))
        conn.execute(child.replace("events.", f"{draft}."))
        kinds = conn.execute(
            "SELECT relname FROM pg_class WHERE relnamespace = %s::regnamespace "
            "AND relname LIKE 'log%%' ORDER BY 1",
            [draft],
        ).fetchall()
    assert [k[0] for k in kinds] == ["log", "log_2025"]


async def test_ddl_quotes_only_what_needs_quotes(app: Any, ready: Server, draft: str) -> None:
    with superuser(ready) as conn:
        conn.execute(
            f'CREATE TABLE {draft}."order" ("user" int PRIMARY KEY, "MixedCase" text, plain int)'
        )
    ddl = (await call(pg_get_ddl, name=f'{draft}."order"', connection="ro"))["ddl"]
    assert f'CREATE TABLE {draft}."order" (' in ddl
    assert '"user" integer NOT NULL' in ddl
    assert '"MixedCase" text' in ddl
    assert "    plain integer" in ddl
    with superuser(ready) as conn:
        conn.execute(f'DROP TABLE {draft}."order"')
        conn.execute(ddl)


async def test_ddl_of_views_functions_sequences_types(app: Any, ready: Server, draft: str) -> None:
    view = (await call(pg_get_ddl, name="shop.order_summary", kind="view", connection="ro"))["ddl"]
    assert view.startswith("CREATE OR REPLACE VIEW shop.order_summary AS\nSELECT")
    assert "COMMENT ON VIEW shop.order_summary IS 'One row per order with customer name';" in view
    matview = (
        await call(
            pg_get_ddl, name="analytics.daily_sales", kind="materialized_view", connection="ro"
        )
    )["ddl"]
    assert matview.startswith("CREATE MATERIALIZED VIEW analytics.daily_sales AS")
    assert "WITH DATA;" in matview
    assert "CREATE UNIQUE INDEX daily_sales_day_idx ON analytics.daily_sales" in matview

    function = (await call(pg_get_ddl, name="shop.order_total", kind="function", connection="ro"))[
        "ddl"
    ]
    assert "CREATE OR REPLACE FUNCTION shop.order_total(bigint)" in function
    assert (
        "COMMENT ON FUNCTION shop.order_total(bigint) IS 'Sum of the items of an order';"
        in function
    )
    if ready.at_least(11):
        procedure = (
            await call(pg_get_ddl, name="shop.archive_orders", kind="procedure", connection="ro")
        )["ddl"]
        assert "CREATE OR REPLACE PROCEDURE shop.archive_orders" in procedure
    else:
        with pytest.raises(ToolError, match="11 or newer"):
            await call(pg_get_ddl, name="shop.archive_orders", kind="procedure", connection="ro")

    sequence = (await call(pg_get_ddl, name="shop.invoice_seq", kind="sequence", connection="ro"))[
        "ddl"
    ]
    assert "START WITH 1000 INCREMENT BY 5" in sequence
    owned = (await call(pg_get_ddl, name="shop.orders_id_seq", kind="sequence", connection="ro"))[
        "ddl"
    ]
    assert "ALTER SEQUENCE shop.orders_id_seq OWNED BY shop.orders.id;" in owned
    enum = (await call(pg_get_ddl, name="shop.order_status", kind="type", connection="ro"))["ddl"]
    assert enum == "CREATE TYPE shop.order_status AS ENUM ('new', 'paid', 'shipped', 'cancelled');"
    domain = (await call(pg_get_ddl, name="shop.email", kind="type", connection="ro"))["ddl"]
    assert domain.startswith("CREATE DOMAIN shop.email AS text")
    composite = (await call(pg_get_ddl, name="shop.address", kind="type", connection="ro"))["ddl"]
    assert composite == "CREATE TYPE shop.address AS (street text, city text, zip text);"
    index = (await call(pg_get_ddl, name="shop.orders_paid_idx", kind="index", connection="ro"))[
        "ddl"
    ]
    assert index.startswith("CREATE INDEX orders_paid_idx ON shop.orders USING btree")
    trigger = (
        await call(
            pg_get_ddl, name="shop.orders.orders_updated_at", kind="trigger", connection="ro"
        )
    )["ddl"]
    assert trigger.startswith("CREATE TRIGGER orders_updated_at BEFORE UPDATE ON shop.orders")
    schema = (await call(pg_get_ddl, name="shop", kind="schema", connection="ro"))["ddl"]
    assert schema.startswith("CREATE SCHEMA shop AUTHORIZATION")
    assert "COMMENT ON SCHEMA shop IS 'Online shop: customers, products and orders';" in schema

    # What the tool writes runs: rebuild a few objects in the draft schema and compare.
    with superuser(ready) as conn:
        for statement in (
            view.replace("shop.order_summary", f"{draft}.order_summary"),
            function.replace("shop.order_total", f"{draft}.order_total"),
            sequence.replace("shop.invoice_seq", f"{draft}.invoice_seq"),
            enum.replace("shop.order_status", f"{draft}.order_status"),
            domain.replace("shop.email", f"{draft}.email"),
            composite.replace("shop.address", f"{draft}.address"),
            matview.replace("analytics.daily_sales", f"{draft}.daily_sales"),
        ):
            conn.execute(statement)
        same = conn.execute(
            f"""SELECT pg_get_viewdef('{draft}.order_summary'::regclass)
                     = pg_get_viewdef('shop.order_summary'::regclass),
                       (SELECT prosrc FROM pg_proc WHERE oid = '{draft}.order_total(bigint)'::regprocedure)
                     = (SELECT prosrc FROM pg_proc WHERE oid = 'shop.order_total(bigint)'::regprocedure)"""
        ).fetchone()
    assert same == (True, True)


async def test_ddl_errors(app: Any) -> None:
    with pytest.raises(ToolError) as wrong_kind:
        await call(pg_get_ddl, name="shop.orders", kind="view", connection="ro")
    assert "is a table, not a view" in str(wrong_kind.value)
    assert "kind='table'" in str(wrong_kind.value)
    with pytest.raises(ToolError, match=r"[Ss]imilar"):
        await call(pg_get_ddl, name="shop.order", connection="ro")
    with pytest.raises(ToolError, match="row type of a table"):
        await call(pg_get_ddl, name="shop.orders", kind="type", connection="ro")
    with pytest.raises(ToolError, match="No schema named"):
        await call(pg_get_ddl, name="nope", kind="schema", connection="ro")
    with pytest.raises(ToolError, match="No function named"):
        await call(pg_get_ddl, name="shop.nope", kind="function", connection="ro")


async def test_ddl_refuses_foreign_tables(app: Any, ready: Server, draft: str) -> None:
    setup = [
        "CREATE FOREIGN DATA WRAPPER pgh_cat_fdw2",
        "CREATE SERVER pgh_cat_srv2 FOREIGN DATA WRAPPER pgh_cat_fdw2",
        f"CREATE FOREIGN TABLE {draft}.ft_ddl (a int) SERVER pgh_cat_srv2",
    ]
    teardown = ["DROP FOREIGN DATA WRAPPER IF EXISTS pgh_cat_fdw2 CASCADE"]
    with scratch(ready, setup, teardown), pytest.raises(ToolError, match="pg_describe_table"):
        await call(pg_get_ddl, name=f"{draft}.ft_ddl", connection="ro")


# ------------------------------------------------------------------------------------- pg_search


async def test_search_groups_by_kind_and_ranks(app: Any) -> None:
    result = await call(pg_search, query="customer", connection="ro")
    found = result["results"]
    assert found["table"][0].startswith("shop.customers")
    assert "shop.customers_public" in found["view"]
    assert "shop.orders.customer_id (integer)" in found["column"]
    assert "shop.customer_label(shop.customers) [function]" in found["function"]
    assert "shop.customers_pkey on shop.customers" in found["index"]
    assert "shop.customers_id_seq" in found["sequence"]
    assert "shop.orders.orders_customer_id_fkey (foreign key)" in found["constraint"]
    assert result["total"] == sum(len(v) for v in found.values())

    exact = await call(pg_search, query="orders", connection="ro", kinds=["table"])
    assert exact["results"]["table"][0] == "shop.orders"
    assert set(exact["results"]) == {"table"}


async def test_search_filters(app: Any, ready: Server) -> None:
    labels = await call(pg_search, query="shipped", connection="ro", kinds=["enum_label"])
    assert labels["results"]["enum_label"] == ["'shipped' in shop.order_status"]
    comments = await call(pg_search, query="Secret token", connection="ro", kinds=["comment"])
    assert any("api_token" in c for c in comments["results"]["comment"])
    in_schema = await call(pg_search, query="id", connection="ro", kinds=["table"], schema="vault")
    assert all(r.startswith("vault.") for r in in_schema["results"].get("table", []))
    wildcard = await call(pg_search, query="order_*", connection="ro", kinds=["table", "view"])
    assert "shop.order_items" in wildcard["results"]["table"]
    assert any(v.startswith("shop.order_summary") for v in wildcard["results"]["view"])
    system = await call(
        pg_search, query="pg_class", connection="ro", schema="pg_catalog", kinds=["table"]
    )
    assert "pg_catalog.pg_class" in system["results"]["table"]
    default = await call(pg_search, query="pg_class", connection="ro", kinds=["table"])
    assert "pg_catalog.pg_class" not in default["results"].get("table", [])
    ext = await call(pg_search, query="plpgsql", connection="ro", kinds=["extension"])
    assert any(e.startswith("plpgsql ") for e in ext["results"]["extension"])
    triggers = await call(pg_search, query="updated_at", connection="ro", kinds=["trigger"])
    assert "shop.orders.orders_updated_at" in triggers["results"]["trigger"]

    capped = await call(pg_search, query="a", connection="ro", kinds=["column"], limit=3)
    assert len(capped["results"]["column"]) == 3
    assert capped["truncated"] == ["column"]

    nothing = await call(pg_search, query="zzzz_no_such_thing", connection="ro")
    assert nothing["results"] == {}
    assert "hint" in nothing
    with pytest.raises(ToolError, match="Give a name"):
        await call(pg_search, query="  ", connection="ro")


# ------------------------------------------------------------------------------ pg_search_source


async def test_search_source_returns_lines(app: Any, ready: Server) -> None:
    result = await call(pg_search_source, pattern="shop.orders", connection="ro")
    by_object = {m["object"]: m for m in result["matches"]}
    touch = by_object["shop.touch_order(bigint)"]
    assert touch["kind"] == "function"
    assert touch["matching_lines"] == 1
    assert touch["lines"] == ["3: UPDATE shop.orders SET updated_at = now() WHERE id = $1;"]
    assert by_object["shop.order_summary"]["kind"] == "view"
    assert "FROM shop.orders o" in by_object["shop.order_summary"]["lines"][0]
    assert by_object["analytics.daily_sales"]["kind"] == "materialized_view"
    assert by_object["shop.orders.orders_updated_at"]["kind"] == "trigger"
    # Code of extensions (and the system catalog) is not searched.
    assert all(not m["object"].startswith("pg_catalog.") for m in result["matches"])

    only_views = await call(pg_search_source, pattern="orders", connection="ro", kinds=["view"])
    assert {m["kind"] for m in only_views["matches"]} == {"view"}
    in_schema = await call(pg_search_source, pattern="orders", connection="ro", schema="analytics")
    assert [m["object"] for m in in_schema["matches"]] == ["analytics.daily_sales"]
    insensitive = await call(
        pg_search_source, pattern="UPDATE SHOP", connection="ro", kinds=["function"]
    )
    assert insensitive["matches"][0]["object"] == "shop.touch_order(bigint)"

    pattern = r"UPDATE\s+shop\.orders\s+SET"
    regex = await call(pg_search_source, pattern=pattern, connection="ro", regex=True)
    assert [m["object"] for m in regex["matches"]] == ["shop.touch_order(bigint)"]
    plain = await call(pg_search_source, pattern=pattern, connection="ro")
    assert plain["matches"] == []
    assert "hint" in plain

    with pytest.raises(ToolError, match="not valid"):
        await call(pg_search_source, pattern="(unclosed", connection="ro", regex=True)
    with pytest.raises(ToolError, match="Give the text"):
        await call(pg_search_source, pattern="", connection="ro")

    capped = await call(pg_search_source, pattern="shop", connection="ro", limit=1)
    assert len(capped["matches"]) == 1
    assert capped["has_more"] is True
    if ready.at_least(11):
        procedures = await call(
            pg_search_source, pattern="DELETE FROM", connection="ro", kinds=["procedure"]
        )
        assert procedures["matches"][0]["object"].startswith("shop.archive_orders(")
    else:
        none = await call(pg_search_source, pattern="DELETE", connection="ro", kinds=["function"])
        assert [m["object"] for m in none["matches"]] == []


# ------------------------------------------------------------------------------- pg_dependencies


def _dependents(result: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(r["kind"], r["object"]): r for r in rows_as_dicts(result["dependents"])}


async def test_dependencies_of_a_table(app: Any) -> None:
    result = await call(pg_dependencies, name="shop.customers", connection="ro")
    dependents = _dependents(result)
    view = dependents[("view", "shop.order_summary")]
    assert view["effect"] == "needs CASCADE"
    assert view["detail"] == "uses id, name"
    assert dependents[("view", "shop.customers_public")]["effect"] == "needs CASCADE"
    foreign = dependents[("table constraint", "orders_customer_id_fkey on shop.orders")]
    assert foreign["effect"] == "needs CASCADE"
    assert ("table constraint", "reviews_customer_id_fkey on shop.reviews") in dependents
    # A function taking the table's row type depends on the table through that type.
    assert (
        dependents[("function", "shop.customer_label(shop.customers)")]["effect"] == "needs CASCADE"
    )
    assert dependents[("index", "shop.customers_lower_name_idx")]["effect"] == "dropped with it"
    assert dependents[("sequence", "shop.customers_id_seq")]["effect"] == "dropped with it"
    impact = result["drop_impact"]
    assert impact["blocks_drop_without_cascade"]["view"] == 2
    assert impact["blocks_drop_without_cascade"]["function"] == 1
    assert impact["dropped_with_it"]["index"] >= 1

    needs = {(r["kind"], r["object"]) for r in rows_as_dicts(result["dependencies"])}
    assert ("type", "shop.email") in needs
    assert ("sequence", "shop.customers_id_seq") in needs
    assert any("code inside function bodies" in n for n in result["notes"])


async def test_dependencies_follow_views_of_views(app: Any) -> None:
    deep = await call(pg_dependencies, name="vault.files", connection="ro", direction="dependents")
    rows = rows_as_dicts(deep["dependents"])
    by_object = {r["object"]: r for r in rows}
    assert by_object["vault.file_keys"]["depth"] == 1
    assert by_object["vault.file_keys_twice"]["depth"] == 2
    assert "through view vault.file_keys" in by_object["vault.file_keys_twice"]["detail"]
    assert "dependencies" not in deep

    shallow = await call(
        pg_dependencies, name="vault.files", connection="ro", direction="dependents", max_depth=1
    )
    assert "vault.file_keys_twice" not in {
        r["object"] for r in rows_as_dicts(shallow["dependents"])
    }

    orders = await call(pg_dependencies, name="shop.orders", connection="ro")
    names = {r["object"] for r in rows_as_dicts(orders["dependents"])}
    assert {"shop.order_summary", "analytics.daily_sales"} <= names
    up = {(r["kind"], r["object"]) for r in rows_as_dicts(orders["dependencies"])}
    assert ("table", "shop.customers") in up
    assert ("type", "shop.order_status") in up
    assert ("function", "shop.set_updated_at()") in up
    only_up = await call(
        pg_dependencies, name="shop.orders", connection="ro", direction="dependencies"
    )
    assert "dependents" not in only_up


async def test_dependencies_of_other_kinds(app: Any, ready: Server, draft: str) -> None:
    view = await call(
        pg_dependencies,
        name="shop.order_summary",
        kind="view",
        connection="ro",
        direction="dependencies",
    )
    base = {r["object"] for r in rows_as_dicts(view["dependencies"])}
    assert {"shop.orders", "shop.customers", "shop.order_items"} <= base

    enum = await call(pg_dependencies, name="shop.order_status", kind="type", connection="ro")
    assert "shop.orders" in {r["object"] for r in rows_as_dicts(enum["dependents"])}

    nothing = await call(
        pg_dependencies,
        name="shop.fmt_price",
        kind="function",
        connection="ro",
        direction="dependents",
    )
    assert nothing["dependents"]["rows"] == []
    assert any("without CASCADE" in n for n in nothing["notes"])

    sequence = await call(
        pg_dependencies, name="shop.orders_id_seq", kind="sequence", connection="ro"
    )
    assert any(r["kind"] == "default value" for r in rows_as_dicts(sequence["dependents"]))
    index = await call(pg_dependencies, name="shop.orders_pkey", kind="index", connection="ro")
    assert index["kind"] == "index"

    with superuser(ready) as conn:
        conn.execute(f"CREATE FUNCTION {draft}.twice(int) RETURNS int LANGUAGE sql AS 'SELECT 1'")
        conn.execute(f"CREATE FUNCTION {draft}.twice(text) RETURNS int LANGUAGE sql AS 'SELECT 2'")
    with pytest.raises(ToolError) as ambiguous:
        await call(pg_dependencies, name=f"{draft}.twice", kind="function", connection="ro")
    assert "several overloads" in str(ambiguous.value)
    assert f"{draft}.twice(text)" in str(ambiguous.value)
    chosen = await call(
        pg_dependencies, name=f"{draft}.twice(text)", kind="function", connection="ro"
    )
    assert chosen["object"] == f"{draft}.twice(pg_catalog.text)"

    with pytest.raises(ToolError, match="No table named"):
        await call(pg_dependencies, name="shop.nope", connection="ro")
    with pytest.raises(ToolError, match="is a view, not a table"):
        await call(pg_dependencies, name="shop.order_summary", connection="ro")


# ----------------------------------------------------------------------------- pg_relationships


async def test_relationships_json(app: Any, ready: Server) -> None:
    result = await call(pg_relationships, connection="ro", schema="shop")
    edges = {r["constraint"]: r for r in rows_as_dicts(result)}
    orders = edges["orders_customer_id_fkey"]
    assert (orders["from"], orders["from_columns"]) == ("shop.orders", ["customer_id"])
    assert (orders["to"], orders["to_columns"]) == ("shop.customers", ["id"])
    assert orders["on_delete"] == "NO ACTION"
    assert orders["required"] is True
    assert edges["order_items_order_id_fkey"]["on_delete"] == "CASCADE"
    assert edges["reviews_customer_id_fkey"]["required"] is False
    assert result["relationships"] == len(edges)
    assert result["tables"] >= 5
    assert result["has_more"] is False
    if ready.at_least(10):
        assert edges["invoices_order_id_fkey"]["to"] == "shop.orders"

    one = rows_as_dicts(await call(pg_relationships, connection="ro", table="shop.customers"))
    assert {r["constraint"] for r in one} == {
        "orders_customer_id_fkey",
        "reviews_customer_id_fkey",
    }
    leaf = rows_as_dicts(await call(pg_relationships, connection="ro", table="order_items"))
    assert {r["to"] for r in leaf} == {"shop.orders", "shop.products"}

    capped = await call(pg_relationships, connection="ro", schema="shop", limit=2)
    assert len(capped["rows"]) == 2
    assert capped["has_more"] is True
    assert "limit" in capped["notes"][0]
    empty = await call(pg_relationships, connection="ro", schema="analytics")
    assert empty["rows"] == []
    assert "No foreign keys" in empty["notes"][0]
    with pytest.raises(ToolError, match="No table named"):
        await call(pg_relationships, connection="ro", table="shop.nope")


async def test_relationships_mermaid_is_valid(app: Any) -> None:
    result = await call(pg_relationships, connection="ro", schema="shop", format="mermaid")
    diagram = result["diagram"]
    lines = diagram.split("\n")
    assert lines[0] == "erDiagram"
    entity = re.compile(r"^    \w+ \{$")
    attribute = re.compile(r"^        \w+ \w+( (PK|FK)(, (PK|FK))?)?$")
    relation = re.compile(r'^    \w+ (\|\||\|o)--(o\{|o\|) \w+ : "[^"]+"$')
    state = "outside"
    for line in lines[1:]:
        if state == "outside" and entity.match(line):
            state = "inside"
        elif state == "inside" and line == "    }":
            state = "outside"
        elif state == "inside":
            assert attribute.match(line), line
        else:
            assert relation.match(line), line
    assert state == "outside"
    assert '    customers ||--o{ orders : "customer_id"' in lines
    assert '    customers |o--o{ reviews : "customer_id"' in lines
    assert "        bigint order_id PK, FK" in lines
    assert (
        result["relationships"]
        == len(lines)
        - sum(1 for ln in lines if entity.match(ln) or ln == "    }" or attribute.match(ln))
        - 1
    )

    across = await call(pg_relationships, connection="ro", format="mermaid", table="shop.customers")
    assert "shop_customers" not in across["diagram"]  # a single schema needs no prefix
    none = await call(pg_relationships, connection="ro", schema="analytics", format="mermaid")
    assert none["diagram"] == "erDiagram"


# --------------------------------------------------------------------------- pg_schema_overview


async def test_schema_overview_is_compact(app: Any, ready: Server) -> None:
    result = await call(pg_schema_overview, schema="shop", connection="ro")
    tables = {line.split("(")[0]: line for line in result["tables"]}
    assert set(tables) >= {"customers", "orders", "products", "order_items", "reviews", "no_pk"}
    orders = tables["orders"]
    assert orders.startswith(
        "orders(id bigint PK, customer_id int FK->customers.id, status order_status, "
        "total numeric(12,2), placed_at timestamptz, updated_at timestamptz)"
    )
    assert re.search(r"\) ~\d+$", orders)
    assert "email email UQ" in tables["customers"]
    assert "sku text UQ" in tables["products"]
    assert "order_id bigint PK FK->orders.id" in tables["order_items"]
    assert "product_id int PK FK->products.id" in tables["order_items"]
    assert "tags text[]" in tables["products"]
    assert all("\n" not in line for line in result["tables"])
    assert "customers_public(id, name, created_at)" in result["views"]
    assert result["functions"] >= 5
    assert result["sequences"] >= 6
    assert result["comment"].startswith("Online shop")
    if ready.at_least(11):
        assert result["procedures"] == 1
    else:
        assert "procedures" not in result

    events = (await call(pg_schema_overview, schema="events", connection="ro"))["tables"]
    assert len(events) == 1
    if ready.at_least(10):
        assert "[partitioned RANGE (created_at), 3 partitions]" in events[0]
    else:
        assert "[3 child tables]" in events[0]

    analytics = await call(pg_schema_overview, schema="analytics", connection="ro")
    assert analytics["tables"] == []
    assert analytics["views"] == ["daily_sales(day, orders, revenue) [materialized]"]

    capped = await call(pg_schema_overview, schema="shop", connection="ro", limit=2)
    assert len(capped["tables"]) == 2
    assert capped["has_more"] is True
    with pytest.raises(ToolError) as missing:
        await call(pg_schema_overview, schema="shopp", connection="ro")
    assert "shop" in str(missing.value)
    with pytest.raises(ToolError, match="No schema named"):
        await call(pg_schema_overview, schema="zzz", connection="ro")


# --------------------------------------------------------------------------- pg_compare_schemas


async def _rebuild_in_draft(ready: Server, draft: str, tables: list[str]) -> None:
    for table in tables:
        ddl = (await call(pg_get_ddl, name=f"shop.{table}", connection="ro"))["ddl"]
        with superuser(ready) as conn:
            conn.execute(ddl.replace(f"shop.{table}", f"{draft}.{table}"))


async def test_compare_schemas_finds_every_kind_of_difference(
    app: Any, ready: Server, draft: str
) -> None:
    await _rebuild_in_draft(ready, draft, ["customers", "products"])
    statements = [
        f"ALTER TABLE {draft}.customers DROP COLUMN notes",
        f"ALTER TABLE {draft}.customers ADD COLUMN vip boolean DEFAULT false",
        f"ALTER TABLE {draft}.products ALTER COLUMN price TYPE numeric(12,2)",
        f"ALTER TABLE {draft}.products ADD CONSTRAINT products_sku_len CHECK (length(sku) < 20)",
        f"CREATE INDEX products_name_idx ON {draft}.products (name)",
        f"DROP INDEX {draft}.customers_lower_name_idx",
        f"CREATE TABLE {draft}.extra (id int)",
        f"CREATE VIEW {draft}.customers_public AS SELECT id FROM {draft}.customers",
        f"CREATE FUNCTION {draft}.fmt_price(numeric) RETURNS text LANGUAGE sql IMMUTABLE "
        "AS $$ SELECT 'changed'::text $$",
        f"CREATE TYPE {draft}.order_status AS ENUM ('new', 'paid', 'shipped', 'cancelled', 'lost')",
        f"CREATE SEQUENCE {draft}.invoice_seq START 1000 INCREMENT 7",
    ]
    with superuser(ready) as conn:
        for statement in statements:
            conn.execute(statement)

    result = await call(
        pg_compare_schemas,
        source_connection="ro",
        target_connection="rw",
        schema="shop",
        target_schema=draft,
    )
    assert result["identical"] is False
    assert result["source"] == {
        "connection": "ro",
        "schema": "shop",
        "server": result["source"]["server"],
    }
    assert result["target"]["schema"] == draft
    differences = result["differences"]

    tables = differences["tables"]
    assert tables["only_in_target"] == ["extra"]
    assert {"orders", "order_items", "reviews"} <= set(tables["only_in_source"])
    assert "customers" not in tables["only_in_source"]

    columns = differences["columns"]
    assert "customers.notes" in columns["only_in_source"]
    assert columns["only_in_target"] == ["customers.vip"]
    changed = {d["object"]: d["changes"] for d in columns["different"]}
    assert changed["products.price"]["type"] == ["numeric(10,2)", "numeric(12,2)"]
    # Columns of tables that exist on one side only are not repeated here.
    assert not any(k.startswith("orders.") for k in columns["only_in_source"])

    assert differences["indexes"]["only_in_target"] == ["products_name_idx"]
    assert "customers_lower_name_idx" in differences["indexes"]["only_in_source"]
    assert differences["constraints"]["only_in_target"] == ["products.products_sku_len"]
    assert "orders.orders_pkey" not in differences["constraints"].get("only_in_source", [])

    views = {d["object"]: d["changes"] for d in differences["views"]["different"]}
    assert "diff" in views["customers_public"]["definition"]
    assert "order_summary" in differences["views"]["only_in_source"]

    functions = {d["object"]: d["changes"] for d in differences["functions"]["different"]}
    assert set(functions["fmt_price(numeric)"]) == {"body_md5"}
    assert "order_total(bigint)" in differences["functions"]["only_in_source"]

    types = {d["object"]: d["changes"] for d in differences["types"]["different"]}
    assert types["order_status"]["definition"] == [
        "new, paid, shipped, cancelled",
        "new, paid, shipped, cancelled, lost",
    ]
    if ready.at_least(10):
        sequences = {d["object"]: d["changes"] for d in differences["sequences"]["different"]}
        assert sequences["invoice_seq"] == {"increment": [5, 7]}
    assert result["summary"]["tables"] == {
        "only_in_source": len(tables["only_in_source"]),
        "only_in_target": 1,
    }


async def test_compare_schemas_identical_and_narrowed(app: Any, ready: Server) -> None:
    for target in ("ro", "rw"):  # one transaction, and two
        result = await call(
            pg_compare_schemas, source_connection="ro", target_connection=target, schema="shop"
        )
        assert result["identical"] is True
        assert result["differences"] == {}
        assert any("No differences" in n for n in result["notes"])
    narrow = await call(
        pg_compare_schemas,
        source_connection="ro",
        target_connection="ro",
        schema="shop",
        target_schema="analytics",
        include=["tables"],
    )
    assert set(narrow["differences"]) == {"tables"}
    assert "orders" in narrow["differences"]["tables"]["only_in_source"]

    with pytest.raises(ToolError, match="does not exist"):
        await call(
            pg_compare_schemas,
            source_connection="ro",
            target_connection="rw",
            schema="shop",
            target_schema="nope",
        )
    with pytest.raises(ToolError, match="approval"):
        await call(
            pg_compare_schemas, source_connection="ro", target_connection="prod", schema="shop"
        )
    with pytest.raises(ToolError, match="Available"):
        await call(
            pg_compare_schemas, source_connection="ro", target_connection="nope", schema="shop"
        )


# ------------------------------------------------------------------------- pg_list_partitions


async def test_list_partitions_of_events(app: Any, ready: Server) -> None:
    result = await call(pg_list_partitions, table="events.log", connection="ro")
    rows = rows_as_dicts(result)
    assert [r["name"] for r in rows] == ["events.log_2024", "events.log_2025", "events.log_2026"]
    assert result["partition_count"] == 3
    assert result["total_size"]
    assert rows[2]["rows"] == 3000
    assert all(r["level"] == 1 and r["size"] for r in rows)
    if ready.at_least(10):
        assert result["mechanism"] == "declarative partitioning"
        assert result["strategy"] == "range"
        assert result["partition_key"] == "RANGE (created_at)"
        assert rows[2]["kind"] == "partition"
        assert rows[2]["bound"].startswith("FOR VALUES FROM ('2026-01-01")
    else:
        assert result["mechanism"] == "table inheritance"
        assert "partition_key" not in result
        assert rows[2]["kind"] == "child table"
        assert rows[2]["bound"].startswith("CHECK (created_at >= '2026-01-01")
        assert any("9.6" in n for n in result["notes"])


async def test_list_partitions_nested_and_default(app: Any, ready: Server, draft: str) -> None:
    if not ready.at_least(11):
        with superuser(ready) as conn:
            conn.execute(f"CREATE TABLE {draft}.p (a int)")
            conn.execute(f"CREATE TABLE {draft}.p1 () INHERITS ({draft}.p)")
        result = await call(pg_list_partitions, table=f"{draft}.p", connection="ro")
        assert [r["name"] for r in rows_as_dicts(result)] == [f"{draft}.p1"]
        return
    with superuser(ready) as conn:
        conn.execute(f"CREATE TABLE {draft}.p (a int, b int) PARTITION BY LIST (a)")
        conn.execute(
            f"CREATE TABLE {draft}.p1 PARTITION OF {draft}.p FOR VALUES IN (1) PARTITION BY RANGE (b)"
        )
        conn.execute(
            f"CREATE TABLE {draft}.p1a PARTITION OF {draft}.p1 FOR VALUES FROM (0) TO (10)"
        )
        conn.execute(f"CREATE TABLE {draft}.pd PARTITION OF {draft}.p DEFAULT")
    result = await call(pg_list_partitions, table=f"{draft}.p", connection="ro")
    rows = {r["name"]: r for r in rows_as_dicts(result)}
    assert result["strategy"] == "list"
    assert result["partition_count"] == 2  # leaves: p1a and the default partition
    assert rows[f"{draft}.p1"]["kind"] == "partitioned table"
    assert rows[f"{draft}.p1"]["partition_key"] == "RANGE (b)"
    assert rows[f"{draft}.p1a"]["level"] == 2
    assert rows[f"{draft}.p1a"]["bound"] == "FOR VALUES FROM (0) TO (10)"
    assert rows[f"{draft}.pd"]["bound"] == "DEFAULT"


async def test_list_partitions_of_an_ordinary_table_and_errors(app: Any) -> None:
    plain = await call(pg_list_partitions, table="shop.orders", connection="ro")
    assert plain["rows"] == []
    assert "not partitioned" in " ".join(plain["notes"])
    with pytest.raises(ToolError, match="No table named"):
        await call(pg_list_partitions, table="events.nope", connection="ro")
