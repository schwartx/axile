"""目标展示、持仓对账及 dashboard 共享数量证据的接口验证。"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.domain.execution import ExecutionArtifactType
from axile.server.api.deps import get_db, get_scheduler
from axile.server.api.routes import account as account_routes
from axile.server.api.routes import account_crud
from axile.server.db.models import (
    AccountAssetSnapshot,
    ExecutionArtifact,
    Portfolio,
    PortfolioAccount,
    TargetWeightSnapshot,
)
from tests.unit.server._execution_test_support import build_account


@pytest.mark.parametrize("reuse_previous", [False, True])
@pytest.mark.parametrize(
    ("current_sm", "current_m", "quantity_ready", "expected_off"),
    [(0, -7, True, 0), (1, -7, True, 1), (0, -3, True, 1), (0, -7, False, None)],
)
def test_recorded_quantities_drive_all_account_views(
    monkeypatch,
    current_sm,
    current_m,
    quantity_ready,
    expected_off,
    reuse_previous,
):
    async def no_performance(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(account_crud, "read_performance_summaries", no_performance)

    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        statements = []

        def record_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                session.add(Portfolio(id=7, name="p", market="期货", custom_calc_py_code=""))
                for account_id in (1, 2):
                    session.add(build_account(id=account_id, name=f"acc-{account_id}", portfolio_id=7))
                    session.add(PortfolioAccount(account_id=account_id, portfolio_id=7))
                    session.add(
                        TargetWeightSnapshot(
                            account_id=account_id,
                            portfolio_id=7,
                            source="execution",
                            raw_weights={"SM2701": 0.02, "m2701": -0.22},
                            normalized_weights={"SM2701": 0.02, "m2701": -0.22},
                            execution_id=f"exec-{account_id}",
                        )
                    )
                    quantities = {"SM701": 0 if account_id == 1 else 1, "m2701": -7}
                    if not quantity_ready and account_id == 1:
                        del quantities["SM701"]
                    session.add(
                        ExecutionArtifact(
                            execution_id=f"exec-{account_id}",
                            artifact_type=ExecutionArtifactType.EXECUTION_SUMMARY,
                            schema_version=2,
                            content={
                                "reconciliation": {
                                    "symbols": [
                                        {
                                            "symbol": symbol,
                                            "sizing": {
                                                "reason_code": "COMMON.SIZING.QUANTIZED",
                                                "target_quantity": quantity,
                                            },
                                        }
                                        for symbol, quantity in quantities.items()
                                    ]
                                }
                            },
                        )
                    )
                    if reuse_previous:
                        await session.flush()
                        session.add(
                            TargetWeightSnapshot(
                                account_id=account_id,
                                portfolio_id=7,
                                source="manual",
                                raw_weights={"SM2701": 0.02, "m2701": -0.22},
                                normalized_weights={"SM2701": 0.02, "m2701": -0.22},
                            )
                        )
                    sm = current_sm if account_id == 1 else 0
                    m = current_m if account_id == 1 else -7
                    session.add(
                        AccountAssetSnapshot(
                            account_id=account_id,
                            source="manual",
                            assets={
                                "total_asset": 2_000_000,
                                "positions": [
                                    {"symbol": "SM701", "volume": sm, "market_value": sm * 20000, "direction": "多头"},
                                    {
                                        "symbol": "m2701",
                                        "volume": abs(m),
                                        "market_value": abs(m) * 30000,
                                        "direction": "空头",
                                    },
                                ],
                            },
                        )
                    )
                await session.commit()

            app = FastAPI()
            app.include_router(account_routes.router)

            async def get_session():
                async with factory() as session:
                    yield session

            app.dependency_overrides[get_db] = get_session
            app.dependency_overrides[get_scheduler] = lambda: SimpleNamespace(get_job=lambda _id: None)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                target = (await client.get("/account/1/target_snapshot")).json()
                response = await client.get("/account/1/rebalance_plan")
                assert response.status_code == 200
                plan = response.json()
                assert plan["off_symbol_count"] == expected_off
                if quantity_ready:
                    assert target["quantities"] == {"SM701": 0, "m2701": -7}
                    rows = {row["symbol"]: row for row in plan["rows"]}
                    assert rows["SM701"]["target_quantity"] == 0
                    assert rows["SM701"]["aligned"] == (current_sm == 0)
                    assert rows["SM701"]["action"] == ("aligned" if current_sm == 0 else "close")
                    assert rows["m2701"]["target_quantity"] == -7
                else:
                    assert target["quantities"] is None
                    assert plan["rows"] == []

                statements.clear()
                response = await client.get("/account/dashboard")
                assert response.status_code == 200
                items = {item["account_id"]: item for item in response.json()["data"]}
                assert items[1]["off_symbol_count"] == expected_off
                assert items[2]["off_symbol_count"] == 1
                artifact_queries = [sql for sql in statements if f"FROM {ExecutionArtifact.__tablename__}" in sql]
                assert len(artifact_queries) == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())
