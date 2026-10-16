"""#109 使用内存 SQLite 表征丢失最终刷盘后的额度放行；不创建 CTP 连接。"""

import asyncio
import json
from unittest.mock import patch

from axile.common.trade_channel import TradeChannel
from axile.server.account_control import monitor as monitoring
from axile.server.account_control import providers
from axile.server.execution import execution_account_control as persistence
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from tests.unit.server._execution_test_support import build_account


async def main():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    account = build_account(
        id=1,
        trade_channel=TradeChannel.CTP,
        portfolio_id=None,
        account_control_preset="ctp",
        account_control_override={
            "operations": {"place_order": {"account": {"per_day": {"limit": 1, "on_trigger": "block"}}}}
        },
    )
    async with factory() as db:
        db.add(account)
        await db.commit()
    with (
        patch.object(persistence, "SessionLocal", factory),
        patch.object(monitoring, "SessionLocal", factory),
        patch.object(providers, "_guards", {}),
        patch.object(providers, "_final", {}),
        patch.object(providers, "_updates", {}),
    ):
        first = await persistence.build_account_control_guard(account, "before-crash")
        first.begin_operation("place_order", symbol="T2612").record_outcome("broker_acknowledged")
        providers._guards.clear()
        await monitoring.recover_sessions()
        second = await persistence.build_account_control_guard(account, "after-crash")
        second.begin_operation("place_order", symbol="T2612").record_outcome("broker_acknowledged")
        report = {
            "probe": "new worker guard after lost final flush",
            "daily_limit": 1,
            "allowed_before_crash": 1,
            "allowed_after_restart": 1,
            "persisted_baseline_count": sum(second.baseline.account_counts.values()),
            "new_guard_monitor_incomplete": second.monitor_incomplete,
            "total_local_allowed": 2,
        }
        assert report["persisted_baseline_count"] == 0 and report["total_local_allowed"] == 2
        print(json.dumps(report, ensure_ascii=False))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
