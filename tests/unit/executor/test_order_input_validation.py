"""公开下单入口在渠道实现之前校验并规范化订单意图。"""

from unittest.mock import Mock

import pytest

from axile.executor.models.unified_order import OrderDirection, OrderType
from tests.unit.executor.test_base_executor_account_control_wrappers import _build_guard, _WrapperExecutor


@pytest.mark.parametrize("field", ["direction", "order_type"])
@pytest.mark.parametrize("invalid", [None, "", " ", "!", "UNKNOWN"])
def test_public_validation_blocks_even_without_channel_validation(field, invalid):
    """渠道自身缺少校验时，公开入口仍不得把非法意图下传。"""
    executor = _WrapperExecutor(_build_guard({}))
    channel_place = Mock(side_effect=AssertionError("非法意图不能进入渠道下单"))
    executor._place_order_impl = channel_place
    failure_log = Mock(wraps=executor.account_orders.activity.record_failure)
    executor.account_orders.activity.record_failure = failure_log
    params = {"direction": OrderDirection.BUY, "order_type": OrderType.LIMIT}
    params[field] = invalid

    with pytest.raises(ValueError, match="不支持的买卖方向" if field == "direction" else "不支持的订单类型"):
        executor.place_order("SHSE.600000", **params, volume=100, price=12.3)

    channel_place.assert_not_called()
    assert executor.raw_calls == []
    failure_log.assert_called_once()
    assert "已在发送前拦截" in failure_log.call_args.args[-1]
    assert ("不支持的买卖方向" if field == "direction" else "不支持的订单类型") in failure_log.call_args.args[-1]


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
@pytest.mark.parametrize("order_type", ["LIMIT", "MARKET"])
def test_public_validation_normalizes_valid_strings_before_channel_dispatch(direction, order_type):
    """合法字符串在交给任何渠道前已经转换成统一枚举。"""
    executor = _WrapperExecutor(_build_guard({}))
    channel_place = Mock(wraps=executor._place_order_impl)
    executor._place_order_impl = channel_place

    executor.place_order("SHSE.600000", direction, order_type, 100, price=12.3)

    channel_place.assert_called_once()
    assert channel_place.call_args.args[1] is OrderDirection(direction)
    assert channel_place.call_args.args[2] is OrderType(order_type)
