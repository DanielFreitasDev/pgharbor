"""The optional server with third-party extensions really has them."""

from typing import Any

import pytest

from pgharbor.tools.core import pg_query, pg_server_info

from ..conftest import call

pytestmark = pytest.mark.integration


async def test_extras_server_has_the_extensions(extras_app: Any) -> None:
    info = await call(pg_server_info, connection="ro")
    assert {"hypopg", "vector", "postgis", "pg_cron"} <= set(info["extensions"])
    assert info["features"]["hypothetical_indexes"] is True
    nearest = await call(
        pg_query,
        connection="ro",
        sql="SELECT product_id FROM shop.embeddings ORDER BY embedding <-> '[3,3,0]' LIMIT 1",
    )
    assert nearest["rows"] == [[3]]
    stores = await call(
        pg_query, connection="ro", sql="SELECT count(*) FROM shop.stores WHERE ST_X(geom) < 0"
    )
    assert stores["rows"] == [[25]]
