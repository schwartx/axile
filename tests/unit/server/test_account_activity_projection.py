"""活动流列表只发布摘要，不把执行原料 JSON 带给前端。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server.analysis_history import AnalysisRecord
from axile.server.analysis_source import write_source_facts
from axile.server.api.routes import account_schedule
from axile.server.db.models import ExecuteRecord

_FAT_RAW = {
    "status": "SUCCEEDED",
    "execution_kind": "rebalance",
    "execution_time": 1.5,
    "memory": {"scratch": True},
    "inputs": {"ignored": 1},
    "extra": {"channel": "tq"},
    "account_assets": {
        "total_asset": 10000.0,
        "available_cash": 1.0,
        "positions": [{"symbol": "rb2610", "volume": 1, "direction": "long"}],
    },
    "symbol_results": {
        "rb2610": {
            "status": "SUCCEEDED",
            "error": None,
            "outcome": "success",
            "sizing": {"unit_multiplier": 10, "current_quantity": 0, "target_quantity": 1},
            "first_tick": {"bid_price": 99, "ask_price": 101, "last_price": 100},
            "orders": [{"order_id": "o1", "direction": "BUY", "filled_volume": 1}],
            "trades": [
                {
                    "trade_price": 100,
                    "trade_volume": 1,
                    "order_id": "o1",
                    "trade_time": "2026-09-01T09:00:01",
                    "extra": {},
                }
            ],
            "memory": {"tick": 1},
        }
    },
}


def test_activity_symbols_include_fills_when_one_symbol_is_requested(monkeypatch):
    """展开某个品种时，同一接口带上该品种成交。"""

    async def account_exists(_session, account_id):
        return SimpleNamespace(id=account_id)

    monkeypatch.setattr(account_schedule, "_get_account_or_404", account_exists)

    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                record = ExecuteRecord(
                    id=7,
                    account_id=1,
                    execution_id="exec-7",
                    is_success=1,
                    created_at="2026-09-01T09:00:00",
                )
                session.add(record)
                await session.flush()
                assert await write_source_facts(
                    session,
                    AnalysisRecord(
                        id=7,
                        account_id=1,
                        execution_id="exec-7",
                        created_at=record.created_at,
                        is_success=1,
                        input={"curr_target": {"rb2610": 1}, "algorithm": {"method": "TWAP"}},
                        result=dict(_FAT_RAW),
                    ),
                )
                payload = await account_schedule.account_activity_symbols(
                    session=session,
                    account_id=1,
                    since="2026-09-01T00:00:00",
                    until="2026-09-02T00:00:00",
                    symbol="rb2610",
                )
                assert payload.count == 1
                row = payload.data[0]
                assert row.symbol == "rb2610"
                assert len(row.trades) == 1
                assert row.trades[0]["value"] == 1000.0
                assert row.trades[0]["record_id"] == 7
        finally:
            await engine.dispose()

    asyncio.run(scenario())
