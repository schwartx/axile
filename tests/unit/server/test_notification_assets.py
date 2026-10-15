"""飞书通知备用资产查询的边界测试。"""

import asyncio
from types import SimpleNamespace

from axile.server.execution import notification_assets


def test_load_notification_snapshot_uses_shared_query(monkeypatch) -> None:
    """仅配置通知函数的账户读取页面同源快照，并保留 ID 与时间。"""
    calls: list[tuple[list[int], int]] = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    async def fake_query(_session, account_ids, *, limit):
        calls.append((account_ids, limit))
        return {7: [SimpleNamespace(id=42, created_at="2026-09-23 11:19", assets={"total_asset": 1000})]}

    monkeypatch.setattr(notification_assets, "SessionLocal", FakeSession)
    monkeypatch.setattr(notification_assets, "get_recent_account_asset_snapshots_for_accounts", fake_query)
    account = SimpleNamespace(id=7, execution_notification_code="def notify(context): pass")
    result = asyncio.run(notification_assets.load_notification_snapshot(account))
    assert result == {"id": 42, "created_at": "2026-09-23 11:19", "assets": {"total_asset": 1000}}
    assert calls == [([7], 1)]
    assert (
        asyncio.run(
            notification_assets.load_notification_snapshot(SimpleNamespace(id=7, execution_notification_code=None))
        )
        is None
    )
    assert calls == [([7], 1)]


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
