"""资产页直读原生结果；账户隔离和事实完整性不依赖 Python ORM。"""

import asyncio
import json
import sqlite3
from contextlib import closing

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from axile.server._native import _core
from axile.server.api.routes import account_assets
from tests.conftest import native_store_call
from tests.sqlite_oracle import connect as oracle_connect
from tests.unit.server._business_facts_test_support import seed_account, sql


def test_native_asset_page_http_and_integrity(native_database, monkeypatch):
    for account_id in (1, 2):
        seed_account(native_database, account_id)
        native_store_call(
            native_database,
            "asset_snapshot_write",
            {
                "account_id": account_id,
                "created_at": "2026-10-20",
                "account_assets": {
                    "total_asset": 0,
                    "available_cash": None,
                    "positions": [{"symbol": "A", "volume": 2}],
                },
            },
        )

    async def check():
        store = _core.Store(str(native_database))
        with pytest.raises(_core.NativeError):
            _core.asset_snapshots(store, 1, 0, 501)
        payload = _core.asset_snapshots(store, 1, 0, 1)
        assert isinstance(payload, bytes)
        assert json.loads(payload)["count"] == 1
        monkeypatch.setattr(account_assets, "analysis_store", lambda: store)
        app = FastAPI()
        app.include_router(account_assets.router)

        route = next(
            route
            for route in account_assets.router.routes
            if isinstance(route, APIRoute) and route.path == "/{account_id}/asset_snapshots"
        )
        assert [parameter.name for parameter in route.dependant.path_params] == ["account_id"]
        assert [parameter.name for parameter in route.dependant.query_params] == ["skip", "limit"]
        assert not route.dependant.dependencies
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            response = await client.get("/1/asset_snapshots?limit=1")
            assert response.status_code == 200, response.text
            page = response.json()
            assert page["count"] == 1
            assets = page["data"][0]["assets"]
            assert assets["total_asset"] == 0
            assert assets["available_cash"] is None
            assert assets["positions"][0]["volume"] == 2
            assert (await client.get("/1/asset_snapshots?skip=1")).json() == {"data": [], "count": 1}
            assert (await client.get("/99/asset_snapshots")).status_code == 404
            assert (await client.get("/1/asset_snapshots?limit=501")).status_code == 422
            # 历史损坏绕过当前写入触发器，仅在原生读取边界验证拒绝。
            with closing(oracle_connect(native_database)) as connection, connection:
                connection.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_TRIGGER, False)
                connection.execute(
                    "UPDATE account_asset_snapshot SET asset_fact_id=(SELECT asset_fact_id "
                    "FROM account_asset_snapshot WHERE account_id=2) WHERE account_id=1"
                )
            assert (await client.get("/1/asset_snapshots")).status_code == 503
            with closing(oracle_connect(native_database)) as connection, connection:
                connection.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_TRIGGER, False)
                connection.execute(
                    "UPDATE account_asset_snapshot SET asset_fact_id='snapshot:1:/account_assets' WHERE account_id=1"
                )
            sql(native_database, "DELETE FROM business_asset_position WHERE asset_id='snapshot:1:/account_assets'")
            assert (await client.get("/1/asset_snapshots")).status_code == 503

    asyncio.run(check())
