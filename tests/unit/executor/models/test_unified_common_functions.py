"""目标仓位更新保留禁止交易、风险品种及空目标的业务语义。"""

from axile.executor.models.unified_account_assets import (
    Position,
    PositionDirection,
    UnifiedAccountAssets,
)


class TestUnifiedUpdateCurrTarget:
    """测试统一目标仓位更新方法."""

    def test_basic_functionality(self) -> None:
        """测试基本功能."""
        positions = [
            Position(
                symbol="rb2610",
                volume=0.5,
                available_volume=0.5,
                market_value=15000.0,
                direction=PositionDirection.LONG,
                avg_price=30000.0,
            ),
            Position(
                symbol="ag2612",
                volume=1.0,
                available_volume=1.0,
                market_value=2000.0,
                direction=PositionDirection.LONG,
                avg_price=2000.0,
            ),
            Position(
                symbol="cu2610",
                volume=1000.0,
                available_volume=1000.0,
                market_value=500.0,
                direction=PositionDirection.LONG,
                avg_price=0.5,
            ),
        ]

        account_assets = UnifiedAccountAssets(
            available_cash=10000.0,
            total_asset=27500.0,
            market_value=17500.0,
            positions=positions,
        )

        curr_target: dict[str, float] = {"rb2610": 0.1, "ag2612": 0.2}

        result = account_assets.update_curr_target(curr_target)

        # 应该包含持仓中不在目标中的品种，权重为0
        expected = {"rb2610": 0.1, "ag2612": 0.2, "cu2610": 0.0}
        assert result == expected

    def test_forbidden_symbols(self) -> None:
        """测试禁止交易品种过滤."""
        positions = [
            Position(
                symbol="rb2610",
                volume=0.5,
                available_volume=0.5,
                market_value=15000.0,
                direction=PositionDirection.LONG,
                avg_price=30000.0,
            ),
            Position(
                symbol="ag2612",
                volume=1.0,
                available_volume=1.0,
                market_value=2000.0,
                direction=PositionDirection.LONG,
                avg_price=2000.0,
            ),
            Position(
                symbol="cu2610",
                volume=1000.0,
                available_volume=1000.0,
                market_value=500.0,
                direction=PositionDirection.LONG,
                avg_price=0.5,
            ),
        ]

        account_assets = UnifiedAccountAssets(
            available_cash=10000.0,
            total_asset=27500.0,
            market_value=17500.0,
            positions=positions,
        )

        curr_target = {"rb2610": 0.1, "ag2612": 0.2, "cu2610": 0.05}
        forbidden_symbols = ["cu2610", "IF2609"]

        result = account_assets.update_curr_target(curr_target, forbidden_symbols=forbidden_symbols)

        # 禁止品种应被过滤掉
        expected = {"rb2610": 0.1, "ag2612": 0.2}
        assert result == expected

    def test_risk_symbols(self) -> None:
        """测试风险品种自动清零."""
        positions = [
            Position(
                symbol="rb2610",
                volume=0.5,
                available_volume=0.5,
                market_value=15000.0,
                direction=PositionDirection.LONG,
                avg_price=30000.0,
            ),
            Position(
                symbol="ag2612",
                volume=1.0,
                available_volume=1.0,
                market_value=2000.0,
                direction=PositionDirection.LONG,
                avg_price=2000.0,
            ),
            Position(
                symbol="cu2610",
                volume=1000.0,
                available_volume=1000.0,
                market_value=500.0,
                direction=PositionDirection.LONG,
                avg_price=0.5,
            ),
        ]

        account_assets = UnifiedAccountAssets(
            available_cash=10000.0,
            total_asset=27500.0,
            market_value=17500.0,
            positions=positions,
        )

        curr_target = {"rb2610": 0.1, "ag2612": 0.2, "cu2610": 0.05}
        risk_symbols = ["cu2610"]

        result = account_assets.update_curr_target(curr_target, risk_symbols=risk_symbols)

        # 风险品种应被设置为0
        expected = {"rb2610": 0.1, "ag2612": 0.2, "cu2610": 0.0}
        assert result == expected

    def test_none_target(self) -> None:
        """测试None目标输入."""
        positions = [
            Position(
                symbol="rb2610",
                volume=0.5,
                available_volume=0.5,
                market_value=15000.0,
                direction=PositionDirection.LONG,
                avg_price=30000.0,
            ),
            Position(
                symbol="ag2612",
                volume=1.0,
                available_volume=1.0,
                market_value=2000.0,
                direction=PositionDirection.LONG,
                avg_price=2000.0,
            ),
        ]

        account_assets = UnifiedAccountAssets(
            available_cash=10000.0,
            total_asset=17000.0,
            market_value=7000.0,
            positions=positions,
        )

        # 现在方法支持None输入，会自动补充持仓中的品种但权重为0
        result = account_assets.update_curr_target(None)
        expected = {"rb2610": 0.0, "ag2612": 0.0}
        assert result == expected


class TestIntegration:
    """集成测试."""

    def test_validation_methods(self) -> None:
        """测试验证方法."""
        positions = [
            Position(
                symbol="rb2610",
                volume=0.5,
                available_volume=0.5,
                market_value=15000.0,
                direction=PositionDirection.LONG,
                avg_price=30000.0,
            )
        ]

        assets = UnifiedAccountAssets(
            available_cash=10000.0,
            total_asset=25000.0,
            market_value=15000.0,
            positions=positions,
        )

        # 测试模型的内置验证方法
        assert assets.validate_balance()  # 10000 + 15000 = 25000

        # 测试获取持仓
        rb_position = assets.get_position("rb2610")
        assert rb_position is not None
        assert rb_position.symbol == "rb2610"

        # 测试获取总持仓市值
        total_value = assets.get_total_position_value()
        assert total_value == 15000.0
