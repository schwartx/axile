"""账户估值不完整时保留未知状态，禁止输出部分市值。"""

import pytest

from axile.common.trade_channel import TradeChannel
from axile.executor.models.unified_account_assets import UnifiedAccountAssets


class TestUnifiedAccountAssetsMethods:
    """测试统一账户资产方法."""

    def test_missing_market_value_does_not_aggregate_partial_notional(self) -> None:
        """有持仓缺价时保留权益与数量，但不输出部分市值。"""
        assets = UnifiedAccountAssets.create(
            available_cash=200,
            total_asset=500,
            positions_data=[
                {"symbol": "rb", "volume": 1, "available_volume": 1, "market_value": 100, "direction": "多头"},
                {"symbol": "ag", "volume": 2, "available_volume": 2, "market_value": None, "direction": "空头"},
            ],
        )
        assert assets.total_asset == 500
        assert assets.positions[1].volume == 2
        assert assets.market_value is None
        assert assets.get_total_position_value() is None
        assert assets.validate_balance() is False


@pytest.mark.parametrize("field", ["nav", "available", "order_frozen", "market_value"])
@pytest.mark.parametrize("invalid", [None, True, "10000", float("nan"), float("inf")])
def test_gm_balance_does_not_claim_success_with_unknown_diagnostic_fields(field, invalid):
    cash = {"nav": 100_000, "available": 30_000, "order_frozen": 10_000, "market_value": 60_000}
    cash[field] = invalid
    assets = UnifiedAccountAssets(
        available_cash=30_000,
        total_asset=100_000,
        market_value=60_000,
        extra={"channel_type": TradeChannel.GM, "total_asset_source": "gm.cash.nav", "gm_cash": cash},
    )
    assert assets.validate_balance() is False
    assert assets.total_asset == 100_000


@pytest.mark.parametrize("frozen", [None, -1, 0, 9000, 11_000])
def test_gm_balance_does_not_override_nav_to_force_reconciliation(frozen):
    assets = UnifiedAccountAssets(
        available_cash=30_000,
        total_asset=100_000,
        market_value=60_000,
        extra={
            "channel_type": TradeChannel.GM,
            "total_asset_source": "gm.cash.nav",
            "gm_cash": {
                "nav": 100_000,
                "available": 30_000,
                "order_frozen": frozen,
                "market_value": 60_000,
            },
        },
    )
    assert assets.validate_balance() is False
    assert assets.total_asset == 100_000


def test_gm_balance_without_cash_evidence_cannot_self_validate_old_formula():
    assets = UnifiedAccountAssets(
        available_cash=30_000,
        total_asset=90_000,
        market_value=60_000,
        extra={"channel_type": TradeChannel.GM},
    )
    assert assets.validate_balance() is False


def test_gm_cash_balance_survives_serialization_and_checks_snapshot_identity():
    assets = UnifiedAccountAssets(
        available_cash=30_000,
        total_asset=100_000,
        market_value=59_999,
        extra={
            "channel_type": TradeChannel.GM,
            "total_asset_source": "gm.cash.nav",
            "gm_cash": {
                "nav": 100_000,
                "available": 30_000,
                "order_frozen": 10_000,
                "market_value": 60_000,
            },
        },
    )
    restored = UnifiedAccountAssets.model_validate_json(assets.model_dump_json())
    assert restored.validate_balance() is True
    assert restored.model_copy(update={"total_asset": 90_000}).validate_balance() is False
    assert restored.model_copy(update={"available_cash": 40_000}).validate_balance() is False


@pytest.mark.parametrize("channel", [None, TradeChannel.CTP])
def test_other_channel_balance_keeps_existing_relationship(channel):
    assets = UnifiedAccountAssets(
        available_cash=30_000,
        total_asset=90_000,
        market_value=60_000,
        extra={"channel_type": channel},
    )
    assert assets.validate_balance() is True
    assert assets.model_copy(update={"total_asset": 100_000}).validate_balance() is False
