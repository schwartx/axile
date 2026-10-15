"""
订单操作辅助函数单元测试.

测试 order_helper 模块中的所有公共函数。
"""

from __future__ import annotations

from unittest.mock import MagicMock, Mock, patch

import pytest

from axile.executor.algorithms.exceptions import SubMinQuantityError
from axile.executor.algorithms.utils.order_helper import (
    determine_position_side,
    resolve_reduce_intent,
    setup_order_tracker,
    submit_and_track_order,
    teardown_order_tracker,
)
from axile.executor.models.unified_order import OrderDirection, OrderType
from axile.executor.models.unified_price import UnifiedPriceData


class TestDeterminePositionSide:
    """测试 determine_position_side 函数."""

    @pytest.fixture
    def executor(self):
        """创建模拟的执行器."""
        executor = MagicMock()
        executor.logger = MagicMock()
        executor.symbol = "rb2610"
        return executor

    @pytest.fixture
    def account_assets_with_long(self):
        """创建带有多头持仓的账户资产."""
        assets = MagicMock()
        return assets

    @pytest.fixture
    def account_assets_with_short(self):
        """创建带有空头持仓的账户资产."""
        assets = MagicMock()
        return assets

    @pytest.fixture
    def account_assets_empty(self):
        """创建空持仓的账户资产."""
        assets = MagicMock()
        return assets

    def test_get_positions_error_handling(self, executor):
        """测试获取持仓失败时的错误处理."""
        assets = MagicMock()
        executor.symbol = "rb2610"
        executor.get_positions = Mock(side_effect=Exception("Connection error"))

        result = determine_position_side(
            executor,
            OrderDirection.SELL,
            assets,
        )

        # 应该返回空字典并记录警告
        assert result == {}
        # 验证 warning 被调用
        executor.logger.warning.assert_called_once()
        # 验证调用包含错误信息
        call_args = str(executor.logger.warning.call_args)
        assert "获取 rb2610 持仓失败" in call_args

    def test_get_positions_error_fails_loud_on_offset_channel(self, executor):
        """测试期货渠道读取持仓失败时拒绝猜测开平语义(issue #53 防护)."""
        from axile.common.order_param_model import OrderParamModel

        assets = MagicMock()
        executor.symbol = "rb2610"
        executor.get_positions = Mock(side_effect=Exception("Connection error"))
        executor.order_param_model = OrderParamModel.OFFSET

        with pytest.raises(RuntimeError, match="拒绝猜测开平语义"):
            determine_position_side(executor, OrderDirection.SELL, assets)


class TestSetupOrderTracker:
    """测试 setup_order_tracker 函数."""

    @pytest.fixture
    def executor(self):
        """创建模拟的执行器."""
        executor = MagicMock()
        executor.logger = MagicMock()
        executor.symbol = "rb2610"
        return executor

    @pytest.fixture
    def price_data(self):
        """创建模拟的单品种市场数据."""
        return UnifiedPriceData(
            symbol="rb2610",
            last_price=50000,
            bid_price=49990,
            ask_price=50010,
            bid_volume=1.0,
            ask_volume=1.0,
            volume=10.0,
            turnover=1000.0,
            timestamp=1,
            update_time="2026-03-22T00:00:00",
        )

    @pytest.fixture
    def chase_config(self):
        """创建追单配置."""
        return Mock(enabled=True)

    @pytest.fixture
    def tracker(self):
        """创建模拟的订单跟踪器."""
        tracker = MagicMock()
        tracker.latest_prices = {}
        return tracker

    def test_setup_with_chase_enabled_registers_price_callback_without_initial_tick(
        self,
        executor,
        chase_config,
        tracker,
    ):
        """启用追单时，即使没有首个快照也应注册价格回调。"""
        with patch(
            "axile.executor.algorithms.utils.order_helper.OrderTracker",
            return_value=tracker,
        ):
            result = setup_order_tracker(
                executor,
                None,
                chase_config,
            )

            assert result is tracker
            executor.register_trade_callback.assert_called_once_with(tracker.on_trade_record)
            executor.register_price_callback.assert_called_once_with(tracker.on_price_update)
            assert tracker.latest_prices == {}


class TestIntegration:
    """集成测试：测试工具函数的组合使用."""

    @pytest.fixture
    def mock_executor(self):
        """创建完整的模拟执行器."""
        executor = MagicMock()
        executor.logger = MagicMock()
        executor.symbol = "rb2610"
        return executor

    def test_full_order_workflow(self, mock_executor):
        """测试完整的订单工作流程."""
        # 模拟订单
        order = MagicMock()
        order.order_id = "test_order_123"
        mock_executor.place_order = Mock(return_value=order)

        with patch("axile.executor.algorithms.utils.order_helper.OrderTracker") as mock_tracker_class:
            mock_tracker = MagicMock()
            mock_tracker_class.return_value = mock_tracker
            mock_tracker.latest_prices = {}

            # 步骤 1: 设置跟踪器
            tracker = setup_order_tracker(
                mock_executor,
                UnifiedPriceData(
                    symbol="rb2610",
                    last_price=50000,
                    bid_price=49990,
                    ask_price=50010,
                    bid_volume=1.0,
                    ask_volume=1.0,
                    volume=10.0,
                    turnover=1000.0,
                    timestamp=1,
                    update_time="2026-03-22T00:00:00",
                ),
                None,  # 不启用追单
            )

            # 步骤 2: 提交订单
            result = submit_and_track_order(
                mock_executor,
                tracker,
                OrderDirection.BUY,
                OrderType.LIMIT,
                1.0,
                50000.0,
                target_volume=2.0,
                current_volume=1.0,
            )

            # 步骤 3: 清理跟踪器
            teardown_order_tracker(mock_executor, tracker, None)

            # 验证完整流程
            mock_executor.initialize_websocket.assert_not_called()
            assert mock_executor.register_order_callback.called
            assert mock_executor.unregister_order_callback.called
            assert mock_executor.place_order.called
            assert result.order_id == "test_order_123"


def _intent_executor(min_notional, last_price=0.0):
    """构造供 resolve_reduce_intent 测试使用的执行器桩."""
    executor = MagicMock()
    executor.symbol = "rb2610"
    executor.logger = MagicMock()
    executor.get_min_notional = Mock(return_value=min_notional)
    if last_price:
        market = MagicMock()
        market.last_price = last_price
        market.bid_price = last_price
        market.ask_price = last_price
        executor.get_market_data = Mock(return_value=market)
    return executor


class TestResolveReduceIntent:
    """测试 resolve_reduce_intent 单向持仓决策表."""

    def test_market_order_uses_reference_price_from_snapshot(self):
        """市价单价格为 0 时应回退到行情快照估算名义价值."""
        executor = _intent_executor(50.0, last_price=100.0)
        decision = resolve_reduce_intent(executor, OrderDirection.SELL, 0.3, 0.0, 1.0, 0.0)
        assert decision.action == "SEND"
        assert decision.reduce_only is True

    def test_market_order_falls_back_to_bid_ask_mid(self):
        """最新价缺失时应退到买一卖一中值估算名义价值."""
        executor = _intent_executor(50.0)
        market = MagicMock()
        market.last_price = 0.0
        market.bid_price = 99.0
        market.ask_price = 101.0
        executor.get_market_data = Mock(return_value=market)
        # 中值 100，纯减仓 0.3 * 100 = 30 < 50 → 借 reduceOnly 突破。
        decision = resolve_reduce_intent(executor, OrderDirection.SELL, 0.3, 0.0, 1.0, 0.0)
        assert decision.action == "SEND"
        assert decision.reduce_only is True

    def test_sends_when_reference_price_unavailable(self):
        """无法估算名义价值时不阻断，退化为照常下单."""
        executor = _intent_executor(50.0)
        executor.get_market_data = Mock(return_value=None)
        decision = resolve_reduce_intent(executor, OrderDirection.SELL, 0.3, 0.0, 1.0, 0.0)
        assert decision.action == "SEND"
        assert decision.reduce_only is False


class TestSubmitAndTrackOrderProjection:
    """测试 submit_and_track_order 接入投影后的跳过与 reduce_only 注入."""

    def _executor(self, min_notional=50.0, last_price=100.0):
        executor = MagicMock()
        executor.symbol = "rb2610"
        executor.logger = MagicMock()
        executor.get_min_notional = Mock(return_value=min_notional)
        market = MagicMock()
        market.last_price = last_price
        market.bid_price = last_price
        market.ask_price = last_price
        executor.get_market_data = Mock(return_value=market)
        return executor

    def test_skip_sub_min_increase_returns_none_and_audits(self):
        """加仓 sub-min 应跳过：返回 None、不下单、不入 tracker、记审计事件."""
        executor = self._executor()
        tracker = MagicMock()

        result = submit_and_track_order(
            executor,
            tracker,
            OrderDirection.BUY,
            OrderType.MARKET,
            0.3,
            0.0,
            target_volume=0.3,
            current_volume=0.0,
        )

        assert result is None
        executor.place_order.assert_not_called()
        tracker.add_order.assert_not_called()
        executor.emit_audit_event.assert_called_once()
        assert executor.emit_audit_event.call_args.kwargs["reason_code"] == "COMMON.SUB_MIN_NOTIONAL"

    def test_sub_min_qty_from_channel_skips_and_audits(self):
        """渠道取整后归零抛 SubMinQuantityError 应跳过：返回 None、不入 tracker、记 SUB_MIN_QTY 事件."""
        executor = self._executor()
        executor.place_order = Mock(side_effect=SubMinQuantityError("qty rounded to 0"))
        tracker = MagicMock()

        # volume*ref=1.0*100 >= min_notional=50 → resolve_reduce_intent 判 SEND，进入 place_order。
        result = submit_and_track_order(
            executor,
            tracker,
            OrderDirection.SELL,
            OrderType.MARKET,
            1.0,
            0.0,
            target_volume=0.0,
            current_volume=1.0,
        )

        assert result is None
        executor.place_order.assert_called_once()
        tracker.add_order.assert_not_called()
        assert executor.emit_audit_event.call_args.kwargs["reason_code"] == "COMMON.SUB_MIN_QTY"

    def test_pure_reduce_injects_reduce_only(self):
        """纯减仓 sub-min 应注入 reduce_only 后照常下单."""
        executor = self._executor()
        order = MagicMock()
        order.order_id = "oid"
        executor.place_order = Mock(return_value=order)
        tracker = MagicMock()

        result = submit_and_track_order(
            executor,
            tracker,
            OrderDirection.SELL,
            OrderType.MARKET,
            0.3,
            0.0,
            target_volume=0.7,
            current_volume=1.0,
        )

        assert result is order
        assert executor.place_order.call_args.kwargs["reduce_only"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
