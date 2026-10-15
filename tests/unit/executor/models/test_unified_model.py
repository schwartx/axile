"""账户估值不完整时保留未知状态，禁止输出部分市值。"""

from axile.executor.models.unified_account_assets import (
    UnifiedAccountAssets,
)


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
