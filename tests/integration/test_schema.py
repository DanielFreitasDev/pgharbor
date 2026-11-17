"""Schema tools on every PostgreSQL version of the matrix."""

from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from pgharbor.tools.schema import pg_describe_table, pg_list_schemas, pg_list_tables

from ..conftest import Server, call, rows_as_dicts

pytestmark = pytest.mark.integration


async def test_list_schemas(app: Any) -> None:
    result = await call(pg_list_schemas, connection="ro")
    by_name = {s["schema"]: s for s in result["schemas"]}
    assert {"shop", "vault", "analytics", "events", "trap", "public"} <= set(by_name)
    assert "pg_catalog" not in by_name
    assert by_name["shop"]["comment"] == "Online shop: customers, products and orders"
    assert by_name["shop"]["tables"] >= 7
    assert by_name["shop"]["views"] == 2
    assert by_name["analytics"]["materialized_views"] == 1
    assert by_name["shop"]["functions"] >= 5
    assert by_name["shop"]["size"]

    with_system = await call(pg_list_schemas, connection="ro", include_system=True)
    assert "pg_catalog" in {s["schema"] for s in with_system["schemas"]}


async def test_list_tables_filters_and_orders(app: Any, ready: Server) -> None:
    shop = rows_as_dicts(await call(pg_list_tables, connection="ro", schema="shop"))
    names = [t["name"] for t in shop]
    assert names == sorted(names)
    assert {"customers", "orders", "products", "order_items"} <= set(names)
    assert all(t["kind"] == "table" for t in shop)
    orders = next(t for t in shop if t["name"] == "orders")
    assert 5000 <= orders["rows"] <= 7000
    customers = next(t for t in shop if t["name"] == "customers")
    assert customers["comment"] == "People who buy from the shop"

    biggest = await call(pg_list_tables, connection="ro", order_by="size", limit=1)
    assert rows_as_dicts(biggest)[0]["name"] == "files"
    assert biggest["has_more"] is True
    assert biggest["next_offset"] == 1

    matched = rows_as_dicts(await call(pg_list_tables, connection="ro", pattern="order*"))
    assert {t["name"] for t in matched} == {"orders", "order_items"}
    contains = rows_as_dicts(await call(pg_list_tables, connection="ro", pattern="item"))
    assert [t["name"] for t in contains] == ["order_items"]

    views = rows_as_dicts(
        await call(pg_list_tables, connection="ro", schema="shop", kinds=["view"])
    )
    assert {v["name"] for v in views} == {"order_summary", "customers_public"}

    events = rows_as_dicts(await call(pg_list_tables, connection="ro", schema="events"))
    if ready.at_least(10):
        assert [(t["name"], t["kind"]) for t in events] == [("log", "partitioned table")]
        everything = rows_as_dicts(
            await call(pg_list_tables, connection="ro", schema="events", include_partitions=True)
        )
        assert len(everything) == 4
    else:
        assert len(events) == 4

    empty = await call(pg_list_tables, connection="ro", schema="does_not_exist")
    assert empty["rows"] == []
    assert "hint" in empty


async def test_describe_table_full(app: Any, ready: Server) -> None:
    table = await call(pg_describe_table, table="shop.orders", connection="ro")
    assert table["table"] == "shop.orders"
    assert table["kind"] == "table"
    columns = {c["name"]: c for c in rows_as_dicts(table["columns"])}
    assert list(columns) == ["id", "customer_id", "status", "total", "placed_at", "updated_at"]
    assert columns["id"]["type"] == "bigint"
    assert columns["id"]["nullable"] is False
    assert "nextval" in columns["id"]["default"]
    assert columns["status"]["type"] == "shop.order_status"
    assert columns["total"]["type"] == "numeric(12,2)"
    assert columns["updated_at"]["nullable"] is True

    constraints = {c["name"]: c for c in table["constraints"]}
    assert constraints["orders_pkey"]["type"] == "primary key"
    foreign = constraints["orders_customer_id_fkey"]
    assert foreign["type"] == "foreign key"
    assert "REFERENCES shop.customers(id)" in foreign["definition"]

    index_names = {i["name"] for i in table["indexes"]}
    assert {
        "orders_pkey",
        "orders_customer_idx",
        "orders_placed_idx",
        "orders_paid_idx",
    } <= index_names
    partial = next(i for i in table["indexes"] if i["name"] == "orders_paid_idx")
    assert "WHERE" in partial["definition"]

    referencing = {r["table"] for r in table["referenced_by"]}
    assert "shop.order_items" in referencing
    assert table["triggers"][0]["name"] == "orders_updated_at"
    assert table["triggers"][0]["state"] == "enabled"
    assert table["size"]["total"]
    assert table["activity"]["live_rows"] is not None


async def test_describe_table_summary_is_smaller(app: Any) -> None:
    summary = await call(pg_describe_table, table="shop.orders", connection="ro", detail="summary")
    assert "indexes" not in summary
    assert "columns" in summary
    assert "constraints" in summary


async def test_describe_view_matview_and_policies(app: Any) -> None:
    view = await call(pg_describe_table, table="shop.order_summary", connection="ro")
    assert view["kind"] == "view"
    assert "shop.orders" in view["definition"] or "orders o" in view["definition"]
    assert view["comment"] == "One row per order with customer name"

    matview = await call(pg_describe_table, table="analytics.daily_sales", connection="ro")
    assert matview["kind"] == "materialized view"
    assert matview["indexes"][0]["name"] == "daily_sales_day_idx"

    secured = await call(pg_describe_table, table="shop.tenant_docs", connection="ro")
    assert secured["row_security"]["enabled"] is True
    policy = secured["row_security"]["policies"][0]
    assert policy["name"] == "tenant_isolation"
    assert policy["command"] == "ALL"
    assert policy["roles"] == ["public"]
    assert "app.tenant" in policy["using"]


async def test_describe_partitioned_table(app: Any, ready: Server) -> None:
    table = await call(pg_describe_table, table="events.log", connection="ro")
    if ready.at_least(10):
        assert table["kind"] == "partitioned table"
        assert "RANGE (created_at)" in table["partition_key"]
        assert [p["name"] for p in table["partitions"]] == [
            "events.log_2024",
            "events.log_2025",
            "events.log_2026",
        ]
        assert "2026" in table["partitions"][2]["bound"]
        child = await call(pg_describe_table, table="events.log_2026", connection="ro")
        assert child["inherits_from"] == ["events.log"]
        assert "FOR VALUES" in child["partition_bound"]
    else:
        assert table["kind"] == "table"
        assert len(table["inherited_by"]) == 3


async def test_describe_identity_and_generated_columns(app: Any, ready: Server) -> None:
    if ready.at_least(10):
        invoices = await call(pg_describe_table, table="shop.invoices", connection="ro")
        columns = {c["name"]: c for c in rows_as_dicts(invoices["columns"])}
        assert columns["id"]["extra"] == "identity always"
    if ready.at_least(12):
        lines = await call(pg_describe_table, table="shop.lines", connection="ro")
        columns = {c["name"]: c for c in rows_as_dicts(lines["columns"])}
        assert columns["gross"]["extra"] == "generated stored"
        assert "net + tax" in columns["gross"]["default"]


async def test_describe_reports_protection(app: Any) -> None:
    table = await call(pg_describe_table, table="vault.files", connection="guarded")
    assert table["pgharbor_rules"]["columns_never_readable"] == ["access_key"]
    assert table["pgharbor_rules"]["must_filter_by_index"] is True
    columns = {c["name"]: c for c in rows_as_dicts(table["columns"])}
    assert columns["access_key"]["comment"].startswith("[protected")
    plain = await call(pg_describe_table, table="vault.files", connection="ro")
    assert "pgharbor_rules" not in plain


async def test_names_are_resolved_helpfully(app: Any) -> None:
    unqualified = await call(pg_describe_table, table="orders", connection="ro")
    assert unqualified["table"] == "shop.orders"
    with pytest.raises(ToolError) as missing:
        await call(pg_describe_table, table="shop.order", connection="ro")
    assert "shop.orders" in str(missing.value)
    with pytest.raises(ToolError):
        await call(pg_describe_table, table="a.b.c", connection="ro")
    catalog = await call(pg_describe_table, table="pg_class", connection="ro", detail="summary")
    assert catalog["table"] == "pg_catalog.pg_class"


async def test_unknown_connection_is_explained(app: Any) -> None:
    with pytest.raises(ToolError) as caught:
        await call(pg_list_schemas, connection="nope")
    assert "Available:" in str(caught.value)
