"""账户级飞书通知测试路由测试."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from axile.common.default_account_notification import DEFAULT_ACCOUNT_NOTIFICATION_CODE
from axile.executor.feishu_notifications import build_execution_notification_context
from axile.executor.models.unified_account_assets import Position, PositionDirection, UnifiedAccountAssets
from axile.server.api.routes import account_feishu
from tests.unit.server._execution_test_support import build_account


@pytest.mark.parametrize(
    "snapshot,expected",
    [
        (None, {}),
        ({}, {}),
        ({"normalized_weights": None}, {}),
        ({"normalized_weights": {}}, {}),
        ({"normalized_weights": {"rb2610": 0.0, "ag2612": -0.5}}, {"rb2610": 0.0, "ag2612": -0.5}),
    ],
)
def test_notification_sample_target_preserves_missing_empty_and_signed_weights(monkeypatch, snapshot, expected):
    account = build_account(portfolio_id=7)
    native = AsyncMock(return_value=snapshot)
    monkeypatch.setattr(account_feishu, "get_latest_account_target_snapshot", native)
    assert asyncio.run(account_feishu._load_sample_target_weights(account)) == expected
    native.assert_awaited_once_with(account.id, 7)


def test_notification_preview_uses_native_context(monkeypatch) -> None:
    account = build_account()
    monkeypatch.setattr(account_feishu, "get_latest_account_target_snapshot", AsyncMock(return_value=None))
    output = asyncio.run(account_feishu._build_sample_output(account, _sample_assets()))
    record = SimpleNamespace(
        id=1,
        execution_id="historical",
        created_at="2026-10-12T09:00:00",
    )
    monkeypatch.setattr(
        account_feishu,
        "native_operation",
        AsyncMock(
            return_value={
                "record": vars(record),
                "context": build_execution_notification_context(
                    account_feishu._TestNotificationSource(account.name), output
                ),
            }
        ),
    )
    context = asyncio.run(account_feishu._load_notification_preview_context(account))
    assert context["assets"]["total_assets"] == 100000
    assert context["positions"][0]["market_value"] == 35000
    assert len(context["trades"]) == 2
    assert context["execution"]["id"] == "historical"
    assert "supplements" not in context["execution"]


def _sample_assets() -> UnifiedAccountAssets:
    return UnifiedAccountAssets(
        available_cash=50000.0,
        total_asset=100000.0,
        market_value=50000.0,
        positions=[
            Position(
                symbol="rb2610",
                volume=10.0,
                available_volume=10.0,
                market_value=35000.0,
                direction=PositionDirection.LONG,
                avg_price=3500.0,
            ),
            Position(
                symbol="au2506",
                volume=2.0,
                available_volume=2.0,
                market_value=15000.0,
                direction=PositionDirection.LONG,
                avg_price=750.0,
            ),
        ],
    )


@pytest.mark.parametrize("supplied", [{}, {"feishu_key": "draft-key"}])
@pytest.mark.parametrize("custom", [True, False])
def test_notification_trial_key_matches_mode_without_saving(monkeypatch, supplied, custom) -> None:
    """试跑按通知方式提供凭证，但不修改已保存配置。"""
    account = build_account()
    captured = []
    monkeypatch.setattr(account_feishu, "_get_account_or_404", AsyncMock(return_value=account))
    monkeypatch.setattr(account_feishu, "_load_notification_preview_context", AsyncMock(return_value={}))
    monkeypatch.setattr(
        account_feishu,
        "run_notification_function",
        lambda code, context, **kwargs: captured.append(kwargs) or SimpleNamespace(ok=True),
    )
    for key in (None, "saved-key") if custom else ("saved-key",):
        account.feishu_key = key
        result = asyncio.run(
            account_feishu.test_account_notification_function(
                1,
                account_feishu.AccountNotificationFunctionTestRequest(
                    code="def notify(context): pass" if custom else DEFAULT_ACCOUNT_NOTIFICATION_CODE,
                    **supplied,
                ),
            )
        )
        assert result.ok is True
        assert captured[-1]["feishu_key"] == (None if custom else supplied.get("feishu_key", key))
        assert account.feishu_key == key
        assert account.execution_notification_code is None
