"""飞书通知备用资产查询的边界测试。"""

import asyncio
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server.business_facts.assets import write_snapshot_assets
from axile.server.db.models import AccountAssetSnapshot
from axile.server.execution import notification_assets
from tests.unit.server._execution_test_support import build_account


def test_load_notification_snapshot_selects_latest_valid_account_assets(monkeypatch) -> None:
    """按账户和 ID 选最新可信快照，保留零权益，坏事实读取失败不影响执行。"""

    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(notification_assets, "SessionLocal", sessions)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            async with sessions() as session:
                session.add_all([build_account(id=i) for i in (1, 2, 3)])
                for snapshot_id, account_id, value in [(1, 1, 100), (2, 1, 0), (3, 1, "bad"), (4, 2, 500)]:
                    snapshot = AccountAssetSnapshot(
                        id=snapshot_id,
                        account_id=account_id,
                        source="manual",
                        created_at=f"2026-10-{20 - snapshot_id:02d}T00:00:00",
                    )
                    session.add(snapshot)
                    await session.flush()
                    await write_snapshot_assets(session, snapshot, {"total_asset": value, "positions": []})
                await session.commit()
            account = SimpleNamespace(id=1, execution_notification_code="configured")
            result = await notification_assets.load_notification_snapshot(account)
            assert result["id"] == 2
            assert result["created_at"] == "2026-10-18T00:00:00"
            assert result["assets"]["total_asset"] == 0
            for account_id, code in [(3, "configured"), (None, "configured"), (1, None)]:
                assert (
                    await notification_assets.load_notification_snapshot(
                        SimpleNamespace(id=account_id, execution_notification_code=code)
                    )
                    is None
                )
            async with sessions() as session:
                snapshot = await session.get(AccountAssetSnapshot, 2)
                other = await session.get(AccountAssetSnapshot, 4)
                snapshot.asset_fact_id = other.asset_fact_id
                await session.commit()
            assert await notification_assets.load_notification_snapshot(account) is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_load_notification_snapshot_failure_does_not_block(monkeypatch) -> None:
    """备用查询异常时继续执行，不把读取故障升级为交易故障。"""

    class FailingSession:
        async def __aenter__(self):
            raise RuntimeError("database unavailable")

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(notification_assets, "SessionLocal", FailingSession)
    result = asyncio.run(
        notification_assets.load_notification_snapshot(
            SimpleNamespace(id=7, execution_notification_code="def notify(context): pass")
        )
    )
    assert result is None
