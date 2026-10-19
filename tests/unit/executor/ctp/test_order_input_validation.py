"""非法方向和订单类型必须在原生报单前拒绝。"""

from datetime import datetime
from unittest.mock import Mock

import pytest
from openctp_ctp import thosttraderapi as td

from axile.executor.ctp.requests import build_order_insert
from axile.executor.ctp_input_validation import CTPInputValidationError
from axile.executor.models.unified_input import CTPAccountConfig
from axile.executor.models.unified_order import OrderDirection, OrderType
from tests.unit.executor.ctp.test_requests import _SHANGHAI, _at_clock, _submit_point_executor

_INVALID_VALUES = [None, "", " ", "\n", "\r", "!", "A" * 1024, True, 1, [], {}, "buy", "BUY\n", "limit", "LIMIT "]


@pytest.fixture
def config() -> CTPAccountConfig:
    """仅供构造请求和模拟发送使用的离线账户配置。"""
    return CTPAccountConfig(
        broker_id="9999",
        investor_id="100001",
        password="test-only",
        td_front="tcp://127.0.0.1:1",
        md_front="tcp://127.0.0.1:2",
        app_id="test-app",
        auth_code="testcode",
    )


@pytest.mark.parametrize("entry", ["public", "ctp"])
@pytest.mark.parametrize("field", ["direction", "order_type"])
@pytest.mark.parametrize("invalid", _INVALID_VALUES)
def test_invalid_order_intent_never_reaches_native_send(config, entry, field, invalid):
    """公开入口及直接渠道调用均不得查询资料、占用订单号或发送报单。"""
    executor = _submit_point_executor(config)
    params = {"direction": OrderDirection.BUY, "order_type": OrderType.LIMIT}
    params[field] = invalid
    place = executor.place_order if entry == "public" else executor._place_order_impl

    with pytest.raises(ValueError, match="不支持的买卖方向" if field == "direction" else "不支持的订单类型"):
        place("ag2612", **params, volume=1, price=9000, offset_flag="open")

    executor._trader_api.ReqOrderInsert.assert_not_called()
    executor._ensure_instrument_symbols.assert_not_called()
    assert executor._order_keys == {}
    assert executor._order_ref == 1
    assert executor._request_id == 0


@pytest.mark.parametrize("entry", ["public", "ctp"])
@pytest.mark.parametrize(
    "offsets",
    [
        {"offset_flag": None},
        {"offset": None},
        {"offset_flag": None, "offset": "open"},
        {"offset_flag": "open", "offset": None},
    ],
)
def test_explicit_none_offset_never_reaches_native_send(config, entry, offsets):
    """显式空值不能触发默认开仓，也不能被另一个别名掩盖。"""
    executor = _submit_point_executor(config)
    place = executor.place_order if entry == "public" else executor._place_order_impl

    with pytest.raises(CTPInputValidationError, match="开平标志.*不能为 None"):
        place("ag2612", OrderDirection.BUY, OrderType.LIMIT, 1, 9000, **offsets)

    executor._trader_api.ReqOrderInsert.assert_not_called()
    executor._ensure_instrument_symbols.assert_not_called()
    assert executor._order_keys == {}
    assert executor._order_ref == 1
    assert executor._request_id == 0


@pytest.mark.parametrize("entry", ["public", "ctp"])
@pytest.mark.parametrize("offsets", [{}, {"offset_flag": "open"}, {"offset": "open"}, {"offset_flag": "close_today"}])
def test_omitted_and_valid_offsets_keep_order_submission(config, monkeypatch, entry, offsets):
    """未提供开平字段时继续推导，显式合法值保持原样发送。"""
    executor = _submit_point_executor(config)
    _at_clock(monkeypatch, datetime(2026, 8, 24, 21, 29, tzinfo=_SHANGHAI))
    place = executor.place_order if entry == "public" else executor._place_order_impl

    place("ag2612", OrderDirection.BUY, OrderType.LIMIT, 1, 9000, **offsets)

    executor._trader_api.ReqOrderInsert.assert_called_once()
    request = executor._trader_api.ReqOrderInsert.call_args.args[0]
    assert request.CombOffsetFlag == (
        td.THOST_FTDC_OF_CloseToday if offsets.get("offset_flag") == "close_today" else td.THOST_FTDC_OF_Open
    )


@pytest.mark.parametrize("field", ["direction", "order_type"])
@pytest.mark.parametrize("invalid", _INVALID_VALUES)
def test_native_builder_rejects_invalid_intent_before_field_construction(config, monkeypatch, field, invalid):
    """直接调用原生构造函数也不能回退成卖单或市价单。"""
    factory = Mock(side_effect=AssertionError("非法报单不应构造原生请求"))
    monkeypatch.setattr(td, "CThostFtdcInputOrderField", factory)
    params = {"direction": OrderDirection.BUY, "order_type": OrderType.LIMIT}
    params[field] = invalid

    with pytest.raises(ValueError, match="不支持的买卖方向" if field == "direction" else "不支持的订单类型"):
        build_order_insert(config, symbol="ag2612", order_ref="1", **params, volume=1, price=9000, offset="0")

    factory.assert_not_called()


@pytest.mark.parametrize("entry", ["public", "ctp"])
@pytest.mark.parametrize("direction", [OrderDirection.BUY, OrderDirection.SELL, "BUY", "SELL"])
@pytest.mark.parametrize("order_type", [OrderType.LIMIT, OrderType.MARKET, "LIMIT", "MARKET"])
def test_valid_order_intent_keeps_native_side_and_price_type(config, monkeypatch, entry, direction, order_type):
    """合法枚举和精确字符串均只发送一次，并返回规范化订单。"""
    executor = _submit_point_executor(config)
    _at_clock(monkeypatch, datetime(2026, 8, 24, 21, 29, tzinfo=_SHANGHAI))
    place = executor.place_order if entry == "public" else executor._place_order_impl

    order = place("ag2612", direction, order_type, 1, 9000, offset_flag="open")

    executor._trader_api.ReqOrderInsert.assert_called_once()
    request = executor._trader_api.ReqOrderInsert.call_args.args[0]
    assert request.Direction == {"BUY": td.THOST_FTDC_D_Buy, "SELL": td.THOST_FTDC_D_Sell}[direction]
    assert (
        request.OrderPriceType
        == {"LIMIT": td.THOST_FTDC_OPT_LimitPrice, "MARKET": td.THOST_FTDC_OPT_AnyPrice}[order_type]
    )
    assert order.direction == OrderDirection(direction).value
    assert order.order_type == OrderType(order_type).value
