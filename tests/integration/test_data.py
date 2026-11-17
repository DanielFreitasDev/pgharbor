"""Data tools on every PostgreSQL version of the matrix."""

import csv
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from pgharbor.app import App, set_app
from pgharbor.config import Config
from pgharbor.tools.data import pg_column_stats, pg_count_rows, pg_export_query, pg_sample_rows

from .. import fixture
from ..conftest import VAULT_RULE, Server, call, profile, superuser

pytestmark = pytest.mark.integration

DRAFT = "pgh_data_draft"


@pytest.fixture
def draft(ready: Server) -> Iterator[str]:
    with superuser(ready) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {DRAFT} CASCADE")
        conn.execute(f"CREATE SCHEMA {DRAFT}")
    try:
        yield DRAFT
    finally:
        with superuser(ready) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {DRAFT} CASCADE")


@pytest.fixture
def exports(tmp_path: Path) -> Path:
    return tmp_path / "exports"


@pytest.fixture
async def data_app(ready: Server, exports: Path) -> AsyncIterator[App]:
    """Profiles for the data tools: exports into a temporary folder, and several rule sets."""
    limited = {"user": fixture.LIMITED_ROLE, "password": fixture.LIMITED_PASSWORD}
    connections = {
        "ro": profile(ready, "ro", export_dir=str(exports)),
        "limited": profile(ready, "limited", export_dir=str(exports), **limited),
        "guarded": profile(ready, "guarded", protect=[VAULT_RULE], export_dir=str(exports)),
        "prot": profile(
            ready,
            "prot",
            protect=[{"table": "shop.customers", "deny_columns": ["notes"], "note": "Private."}],
            export_dir=str(exports),
        ),
        "denied": profile(ready, "denied", deny_relations=["shop.orders"]),
        "noexport": profile(ready, "noexport", allow_export=False, export_dir=str(exports)),
        "home": profile(ready, "home"),
    }
    config = Config.model_validate(
        {"connections": connections, "audit": {"enabled": False}, "source": "tests"}
    )
    application = App(config)
    set_app(application)
    try:
        yield application
    finally:
        await application.close()
        set_app(None)


# ------------------------------------------------------------------------------ pg_sample_rows


async def test_sample_rows_default(data_app: Any) -> None:
    result = await call(pg_sample_rows, table="shop.customers", connection="ro")
    assert result["table"] == "shop.customers"
    assert result["columns"] == [
        "id",
        "name",
        "email",
        "address",
        "created_at",
        "api_token",
        "notes",
    ]
    assert result["row_count"] == 10
    assert len(result["rows"]) == 10
    first = dict(zip(result["columns"], result["rows"][0], strict=True))
    assert first["name"].startswith("Customer ")
    assert first["email"].endswith("@example.com")
    assert first["api_token"] == "[redacted]"
    assert "no particular order" in result["sampling"]
    assert any("Redacted column(s): api_token" in n for n in result["notes"])


async def test_sample_rows_columns_limit_and_views(data_app: Any) -> None:
    some = await call(
        pg_sample_rows, table="customers", connection="ro", columns=["id", "name"], limit=3
    )
    assert some["columns"] == ["id", "name"]
    assert len(some["rows"]) == 3
    assert "notes" not in some
    capped = await call(pg_sample_rows, table="shop.customers", connection="ro", limit=1000)
    assert capped["row_count"] == 100
    view = await call(pg_sample_rows, table="shop.customers_public", connection="ro", limit=2)
    assert view["kind"] == "view"
    assert view["columns"] == ["id", "name", "created_at"]
    matview = await call(pg_sample_rows, table="analytics.daily_sales", connection="ro", limit=2)
    assert matview["columns"] == ["day", "orders", "revenue"]
    assert matview["row_count"] == 2


async def test_sample_rows_random(data_app: Any, ready: Server, draft: str) -> None:
    small = await call(pg_sample_rows, table="shop.orders", connection="ro", limit=5, random=True)
    assert small["row_count"] == 5
    assert small["sampling"] == "random order of the whole table"
    ids = {row[0] for row in small["rows"]}
    again = await call(pg_sample_rows, table="shop.orders", connection="ro", limit=5, random=True)
    # Two random draws of 5 out of 6000 are never the same by accident.
    assert ids != {row[0] for row in again["rows"]}

    with superuser(ready) as conn:
        conn.execute(
            f"CREATE TABLE {draft}.big AS SELECT g AS id, md5(g::text) AS v "
            "FROM generate_series(1, 60000) g"
        )
        conn.execute(f"ANALYZE {draft}.big")
    big = await call(pg_sample_rows, table=f"{draft}.big", connection="ro", limit=10, random=True)
    assert big["row_count"] == 10
    assert big["sampling"].startswith("random ")
    spread = {row[0] for row in big["rows"]}
    assert len(spread) == 10

    view = await call(
        pg_sample_rows, table="shop.customers_public", connection="ro", limit=4, random=True
    )
    assert view["row_count"] == 4
    assert any("cannot be sampled" in n for n in view["notes"])
    events = await call(pg_sample_rows, table="events.log", connection="ro", limit=4, random=True)
    assert events["row_count"] == 4


async def test_sample_rows_empty_and_never_analyzed(
    data_app: Any, ready: Server, draft: str
) -> None:
    with superuser(ready) as conn:
        conn.execute(f"CREATE TABLE {draft}.empty (a int)")
        conn.execute(f"CREATE TABLE {draft}.fresh AS SELECT g AS a FROM generate_series(1, 30) g")
    empty = await call(pg_sample_rows, table=f"{draft}.empty", connection="ro")
    assert empty["rows"] == []
    assert any("no rows" in n for n in empty["notes"])
    fresh = await call(
        pg_sample_rows, table=f"{draft}.fresh", connection="ro", limit=5, random=True
    )
    assert fresh["row_count"] == 5


async def test_sample_rows_respects_protection(data_app: Any) -> None:
    result = await call(pg_sample_rows, table="shop.customers", connection="prot", limit=2)
    assert "notes" not in result["columns"]
    assert result["columns"][-1] == "api_token"
    assert any(
        "Protected column(s) left out: notes" in n and "Private." in n for n in result["notes"]
    )
    with pytest.raises(ToolError) as refused:
        await call(
            pg_sample_rows, table="shop.customers", connection="prot", columns=["id", "notes"]
        )
    assert "protected" in str(refused.value)
    assert "notes" in str(refused.value)

    # vault.files may only be read through an index, which a sample does not use.
    with pytest.raises(ToolError, match=r"vault\.files"):
        await call(pg_sample_rows, table="vault.files", connection="guarded")
    folders = await call(pg_sample_rows, table="vault.folders", connection="guarded", limit=2)
    assert folders["columns"] == ["id", "path"]
    with pytest.raises(ToolError, match="protected"):
        await call(
            pg_sample_rows, table="vault.files", connection="guarded", columns=["access_key"]
        )


async def test_sample_rows_errors(data_app: Any) -> None:
    with pytest.raises(ToolError) as column:
        await call(pg_sample_rows, table="shop.orders", connection="ro", columns=["id", "nope"])
    assert "'nope'" in str(column.value)
    assert "customer_id" in str(column.value)
    with pytest.raises(ToolError) as table:
        await call(pg_sample_rows, table="shop.order", connection="ro")
    assert "shop.orders" in str(table.value)
    limited = await call(
        pg_sample_rows,
        table="shop.customers",
        connection="limited",
        columns=["id", "name"],
        limit=2,
    )
    assert limited["row_count"] == 2
    with pytest.raises(ToolError, match=r"[Pp]ermission denied"):
        await call(pg_sample_rows, table="shop.customers", connection="limited")


# ----------------------------------------------------------------------------- pg_count_rows


async def test_count_rows_estimate_and_exact(data_app: Any, ready: Server) -> None:
    estimate = await call(pg_count_rows, table="shop.orders", connection="ro")
    assert 5000 <= estimate["estimated_rows"] <= 7000
    assert estimate["statistics_from"]
    assert "exact_rows" not in estimate

    exact = await call(pg_count_rows, table="shop.orders", connection="ro", exact=True)
    assert exact["exact_rows"] == 6000
    assert abs(exact["estimate_error_percent"]) < 20

    partitioned = await call(pg_count_rows, table="events.log", connection="ro", exact=True)
    assert partitioned["exact_rows"] == 3000
    assert 2500 <= partitioned["estimated_rows"] <= 3500  # the sum over the partitions
    if ready.at_least(10):
        assert partitioned["kind"] == "partitioned table"

    small = await call(pg_count_rows, table="shop.no_pk", connection="ro")
    assert small["estimated_rows"] == 2
    assert any("exact=true is cheap" in n for n in small["notes"])
    matview = await call(pg_count_rows, table="analytics.daily_sales", connection="ro", exact=True)
    assert matview["exact_rows"] >= 1


async def test_count_rows_views_and_unanalyzed(data_app: Any, ready: Server, draft: str) -> None:
    view = await call(pg_count_rows, table="shop.customers_public", connection="ro")
    assert "estimated_rows" not in view
    assert "has no row estimate" in view["notes"][0]
    counted = await call(pg_count_rows, table="shop.customers_public", connection="ro", exact=True)
    assert counted["exact_rows"] == 400

    with superuser(ready) as conn:
        conn.execute(f"CREATE TABLE {draft}.fresh AS SELECT g FROM generate_series(1, 7) g")
    fresh = await call(pg_count_rows, table=f"{draft}.fresh", connection="ro", exact=True)
    assert fresh["exact_rows"] == 7
    if "estimated_rows" not in fresh:
        assert any("never been analyzed" in n for n in fresh["notes"])


async def test_count_rows_on_protected_tables(data_app: Any) -> None:
    estimate = await call(pg_count_rows, table="vault.files", connection="guarded")
    assert 15000 <= estimate["estimated_rows"] <= 25000
    with pytest.raises(ToolError, match=r"vault\.files"):
        await call(pg_count_rows, table="vault.files", connection="guarded", exact=True)
    with pytest.raises(ToolError, match="No table named"):
        await call(pg_count_rows, table="shop.nope", connection="ro")


# ---------------------------------------------------------------------------- pg_column_stats


async def test_column_stats_of_orders(data_app: Any) -> None:
    result = await call(pg_column_stats, table="shop.orders", connection="ro")
    assert 5000 <= result["estimated_rows"] <= 7000
    assert result["last_analyze"]
    columns = {c["column"]: c for c in result["columns"]}
    assert list(columns) == ["id", "customer_id", "status", "total", "placed_at", "updated_at"]

    status = columns["status"]
    assert status["type"] == "shop.order_status"
    assert status["n_distinct"] == 4
    assert status["distinct_estimate"] == 4
    assert status["null_fraction"] == 0
    common = {m["value"]: m["frequency"] for m in status["most_common"]}
    assert set(common) == {"new", "paid", "shipped", "cancelled"}
    assert all(0.2 < f < 0.3 for f in common.values())

    identifier = columns["id"]
    assert identifier["distinct_estimate"] == pytest.approx(result["estimated_rows"], rel=0.05)
    assert identifier["correlation"] == 1.0  # sequential inserts
    assert identifier["histogram"]["first"] == "1"
    assert identifier["histogram"]["last"] == "6000"
    assert identifier["histogram"]["buckets"] > 10
    assert "most_common" not in identifier  # all values are different
    assert columns["updated_at"]["null_fraction"] == 1.0
    assert columns["customer_id"]["avg_width"] == 4
    assert len(columns["total"].get("most_common", [])) <= 10


async def test_column_stats_selection_and_errors(data_app: Any, ready: Server, draft: str) -> None:
    some = await call(
        pg_column_stats, table="shop.orders", connection="ro", columns=["status", "total"]
    )
    assert [c["column"] for c in some["columns"]] == ["status", "total"]
    with pytest.raises(ToolError, match="'nope'"):
        await call(pg_column_stats, table="shop.orders", connection="ro", columns=["nope"])
    with pytest.raises(ToolError, match="No table named"):
        await call(pg_column_stats, table="shop.customers_public", connection="ro")
    with pytest.raises(ToolError, match="deny_relations"):
        await call(pg_column_stats, table="shop.orders", connection="denied")

    with superuser(ready) as conn:
        conn.execute(f"CREATE TABLE {draft}.fresh (a int, b text)")
        conn.execute(f"INSERT INTO {draft}.fresh SELECT g, 'x' FROM generate_series(1, 5) g")
    fresh = await call(pg_column_stats, table=f"{draft}.fresh", connection="ro")
    if fresh["columns"] == []:
        assert fresh["no_statistics"] == ["a", "b"]
        assert any("never analyzed" in n for n in fresh["notes"])
    with superuser(ready) as conn:
        conn.execute(f"ANALYZE {draft}.fresh")
    analyzed = await call(pg_column_stats, table=f"{draft}.fresh", connection="ro")
    assert [c["column"] for c in analyzed["columns"]] == ["a", "b"]
    assert analyzed["columns"][1]["most_common"] == [{"value": "x", "frequency": 1.0}]


async def test_column_stats_never_show_values_of_secret_columns(data_app: Any) -> None:
    customers = await call(pg_column_stats, table="shop.customers", connection="ro")
    token = next(c for c in customers["columns"] if c["column"] == "api_token")
    assert token["values_hidden"] == "name looks like a credential"
    assert "most_common" not in token
    assert "histogram" not in token
    assert token["distinct_estimate"] == 400  # shape only
    assert any("api_token" in n for n in customers["notes"])
    name = next(c for c in customers["columns"] if c["column"] == "name")
    assert "histogram" in name
    # md5('token1') is the first value the histogram would have shown.
    assert "KEY-" not in json.dumps(customers)

    guarded = await call(pg_column_stats, table="vault.files", connection="guarded")
    key = next(c for c in guarded["columns"] if c["column"] == "access_key")
    assert key["values_hidden"] == "protected column"
    assert "most_common" not in key
    assert "histogram" not in key
    assert "KEY-" not in json.dumps(guarded)
    checksum = next(c for c in guarded["columns"] if c["column"] == "checksum")
    assert "histogram" in checksum  # other columns of a protected table are fine

    prot = await call(pg_column_stats, table="shop.customers", connection="prot")
    notes = next(c for c in prot["columns"] if c["column"] == "notes")
    assert notes["values_hidden"] == "protected column"

    limited = await call(pg_column_stats, table="shop.customers", connection="limited")
    assert "api_token" in limited["no_statistics"]  # the role cannot read that column
    assert any("No statistics for" in n for n in limited["notes"])


# --------------------------------------------------------------------------- pg_export_query


def _resolved(path: Path) -> Path:
    return path.resolve()


def _size(path: str) -> int:
    return Path(path).stat().st_size


def _mode(path: str | Path) -> int:
    return Path(path).stat().st_mode & 0o777


def _text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def _read_csv(path: str) -> list[list[str]]:
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.reader(handle))


async def test_export_csv(data_app: Any, exports: Path) -> None:
    result = await call(
        pg_export_query,
        sql="SELECT id, name, price, tags FROM shop.products ORDER BY id LIMIT 5",
        filename="products.csv",
        connection="ro",
    )
    assert result["path"] == str(_resolved(exports) / "products.csv")
    assert result["rows"] == 5
    assert result["columns"] == ["id", "name", "price", "tags"]
    assert result["format"] == "csv"
    assert result["bytes"] == _size(result["path"])
    assert "truncated" not in result
    table = _read_csv(result["path"])
    assert table[0] == ["id", "name", "price", "tags"]
    assert table[1][:3] == ["1", "Product 1", "3.50"]
    assert table[1][3] == "{tag1,tag1}"
    assert len(table) == 6
    assert _mode(result["path"]) == 0o600
    assert _names(exports) == ["products.csv"]  # no temporary files


async def test_export_json_jsonl_and_parameters(data_app: Any) -> None:
    sql = (
        "SELECT p.id, p.name, p.attrs, p.photo IS NULL AS no_photo FROM shop.products p "
        "WHERE p.id <= $1 ORDER BY p.id"
    )
    as_json = await call(
        pg_export_query, sql=sql, params=[3], filename="p.json", format="json", connection="ro"
    )
    data = json.loads(_text(as_json["path"]))
    assert [d["id"] for d in data] == [1, 2, 3]
    assert data[0]["attrs"] == {"color": "green", "size": 1}
    assert data[0]["no_photo"] is True
    assert as_json["rows"] == 3

    as_lines = await call(
        pg_export_query, sql=sql, params=[4], filename="p", format="jsonl", connection="ro"
    )
    assert as_lines["path"].endswith("p.jsonl")
    lines = _text(as_lines["path"]).splitlines()
    assert [json.loads(line)["id"] for line in lines] == [1, 2, 3, 4]

    bare = await call(pg_export_query, sql="SELECT 1 AS n", filename="bare", connection="ro")
    assert bare["path"].endswith("bare.csv")
    empty = await call(
        pg_export_query,
        sql="SELECT id FROM shop.products WHERE id < 0",
        filename="e.csv",
        connection="ro",
    )
    assert empty["rows"] == 0
    assert _read_csv(empty["path"]) == [["id"]]
    unicode_value = await call(
        pg_export_query, sql="SELECT 'çã, \"x\"' AS t", filename="u.csv", connection="ro"
    )
    assert _read_csv(unicode_value["path"]) == [["t"], ['çã, "x"']]


async def test_export_limits(data_app: Any) -> None:
    capped = await call(
        pg_export_query,
        sql="SELECT g FROM generate_series(1, 50) g",
        filename="capped.csv",
        max_rows=10,
        connection="ro",
    )
    assert capped["rows"] == 10
    assert capped["truncated"] is True
    assert "max_rows=10" in capped["notes"][0]
    assert len(_read_csv(capped["path"])) == 11

    hard = await call(
        pg_export_query,
        sql="SELECT g FROM generate_series(1, 100500) g",
        filename="hard.csv",
        max_rows=10**9,
        connection="ro",
    )
    assert hard["rows"] == 100_000
    assert hard["truncated"] is True
    assert "100,000" in hard["notes"][0]


async def test_export_does_not_overwrite_unless_asked(data_app: Any) -> None:
    first = await call(pg_export_query, sql="SELECT 1 AS a", filename="o.csv", connection="ro")
    with pytest.raises(ToolError, match="already exists"):
        await call(pg_export_query, sql="SELECT 2 AS a", filename="o.csv", connection="ro")
    assert _read_csv(first["path"]) == [["a"], ["1"]]
    again = await call(
        pg_export_query, sql="SELECT 3 AS a", filename="o.csv", overwrite=True, connection="ro"
    )
    assert again["path"] == first["path"]
    assert _read_csv(first["path"]) == [["a"], ["3"]]


@pytest.mark.parametrize(
    "filename",
    [
        "../x.csv",
        "a/b.csv",
        "..",
        "",
        "x\\y.csv",
        ".hidden",
        "a..b.csv",
        "/etc/passwd",
        "x\x00.csv",
    ],
)
async def test_export_refuses_paths(data_app: Any, exports: Path, filename: str) -> None:
    with pytest.raises(ToolError) as caught:
        await call(pg_export_query, sql="SELECT 1", filename=filename, connection="ro")
    assert "file name" in str(caught.value)
    assert _names(exports) == []


async def test_export_follows_the_profile(
    data_app: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ToolError, match="disabled"):
        await call(pg_export_query, sql="SELECT 1", filename="a.csv", connection="noexport")
    assert not (tmp_path / "exports").exists()

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    default = await call(pg_export_query, sql="SELECT 1 AS a", filename="d.csv", connection="home")
    assert default["path"] == str(_resolved(tmp_path / "home" / "pgharbor-exports") / "d.csv")
    assert ((tmp_path / "home" / "pgharbor-exports").stat().st_mode & 0o777) == 0o700


async def test_export_keeps_every_safety_check(data_app: Any, exports: Path) -> None:
    redacted = await call(
        pg_export_query,
        sql="SELECT id, api_token FROM shop.customers ORDER BY id LIMIT 3",
        filename="r.csv",
        connection="ro",
    )
    table = _read_csv(redacted["path"])
    assert [row[1] for row in table[1:]] == ["[redacted]"] * 3
    assert any("Redacted" in n for n in redacted["notes"])

    with pytest.raises(ToolError, match="protected"):
        await call(
            pg_export_query,
            sql="SELECT access_key FROM vault.files WHERE id = 1",
            filename="k.csv",
            connection="guarded",
        )
    with pytest.raises(ToolError, match="changes the database"):
        await call(pg_export_query, sql="DELETE FROM shop.no_pk", filename="w.csv", connection="ro")
    with pytest.raises(ToolError, match="parameter"):
        await call(pg_export_query, sql="SELECT $1::int", filename="m.csv", connection="ro")
    with pytest.raises(ToolError):
        await call(
            pg_export_query, sql="SELECT nope FROM shop.orders", filename="n.csv", connection="ro"
        )
    assert not (exports / "k.csv").exists()
    assert not (exports / "w.csv").exists()
    assert not (exports / "n.csv").exists()
    assert not any(n.startswith(".pgharbor-") for n in _names(exports))
