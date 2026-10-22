"""资产页直读原生结果；账户隔离和事实完整性不依赖 Python ORM。"""

import asyncio
import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text
from sqlmodel import Session, SQLModel

from axile.server._native import _core
from axile.server.api.deps import get_db
from axile.server.api.routes import account_assets
from axile.server.business_facts.assets import write_snapshot_assets
from axile.server.db.models.account_asset import AccountAssetSnapshot
from tests.unit.server._execution_test_support import build_account


def test_native_asset_page_http_and_integrity(tmp_path, monkeypatch):
    database = tmp_path / "assets.sqlite"
    engine = create_engine(f"sqlite:///{database}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(build_account(id=1))
        session.add(build_account(id=2))
        session.commit()

    async def check():
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        writer = create_async_engine(f"sqlite+aiosqlite:///{database}")
        try:
            async with async_sessionmaker(writer)() as session:
                for account_id in (1, 2):
                    snapshot = AccountAssetSnapshot(account_id=account_id, source="manual")
                    await write_snapshot_assets(
                        session,
                        snapshot,
                        {
                            "total_asset": 0,
                            "available_cash": None,
                            "positions": [{"symbol": "A", "volume": 2}],
                        },
                    )
                await session.commit()
            store = _core.Store(str(database))
            with pytest.raises(_core.NativeError):
                _core.asset_snapshots(store, 1, 0, 501)
            payload = _core.asset_snapshots(store, 1, 0, 1)
            assert isinstance(payload, bytes)
            assert json.loads(payload)["count"] == 1
            monkeypatch.setattr(account_assets, "analysis_store", lambda: store)
            app = FastAPI()
            app.include_router(account_assets.router)

            async def reject_session():
                raise AssertionError("asset page must not open a Python database session")

            app.dependency_overrides[get_db] = reject_session
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
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "UPDATE account_asset_snapshot SET asset_fact_id=(SELECT asset_fact_id FROM account_asset_snapshot WHERE account_id=2) WHERE account_id=1"
                        )
                    )
                assert (await client.get("/1/asset_snapshots")).status_code == 503
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "UPDATE account_asset_snapshot SET asset_fact_id='snapshot:1:/account_assets' WHERE account_id=1"
                        )
                    )
                    connection.execute(
                        text("DELETE FROM business_asset_position WHERE asset_id='snapshot:1:/account_assets'")
                    )
                assert (await client.get("/1/asset_snapshots")).status_code == 503
        finally:
            await writer.dispose()

    try:
        asyncio.run(check())
    finally:
        engine.dispose()
