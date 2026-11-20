"""通知资产使用原生可信观测阶段，读取失败不阻断执行。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from axile.server.execution import notification_assets


def test_notification_uses_one_trusted_snapshot_stage(monkeypatch):
    snapshot = {
        "id": 7,
        "account_id": 1,
        "source": "manual",
        "execution_id": None,
        "created_at": "observed-time",
        "assets": {"total_asset": 0, "positions": []},
    }
    latest = AsyncMock(return_value=snapshot)
    monkeypatch.setattr(notification_assets, "latest_asset_snapshot", latest)
    result = asyncio.run(
        notification_assets.load_notification_snapshot(
            SimpleNamespace(id=1, execution_notification_code="configured"),
        )
    )
    assert result == {key: snapshot[key] for key in ("id", "created_at", "assets")}
    latest.assert_awaited_once_with(1)


@pytest.mark.parametrize("account_id,code", [(None, "configured"), (1, None), (1, "")])
def test_unconfigured_notifications_skip_database(monkeypatch, account_id, code):
    latest = AsyncMock()
    monkeypatch.setattr(notification_assets, "latest_asset_snapshot", latest)
    assert (
        asyncio.run(
            notification_assets.load_notification_snapshot(
                SimpleNamespace(id=account_id, execution_notification_code=code),
            )
        )
        is None
    )
    latest.assert_not_awaited()


@pytest.mark.parametrize("failure", [None, ValueError("invalid owner"), RuntimeError("database unavailable")])
def test_no_snapshot_or_failed_read_does_not_block_execution(monkeypatch, failure):
    latest = AsyncMock(return_value=None, side_effect=failure)
    monkeypatch.setattr(notification_assets, "latest_asset_snapshot", latest)
    assert (
        asyncio.run(
            notification_assets.load_notification_snapshot(
                SimpleNamespace(id=1, execution_notification_code="configured"),
            )
        )
        is None
    )
    latest.assert_awaited_once_with(1)
