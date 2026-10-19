"""CTP 非法字段在查询或发送前拒绝，合法请求保持原样。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from axile.executor.ctp.ctp_execute import CTPExecutor
from axile.executor.ctp.requests import build_authenticate, build_query_account, build_query_depth_market_data
from axile.executor.ctp_input_validation import CTPInputValidationError, validate_cancel_id, validate_instrument
from axile.executor.models.unified_input_accounts import CTPAccountConfig
from tests.unit.executor.ctp import test_order_input_validation


@pytest.fixture
def config():
    return test_order_input_validation.config.__wrapped__()


BAD = [" ", "\n", "\r", "!", "@", "中文", "A\nB", "A" * 1024]


@pytest.mark.parametrize(
    ("value", "entry"),
    [
        (value, entry)
        for value in ["", *BAD, "A" * 81, "rb2612 ", " rb2612", "rb\r2612", "rb\t2612"]
        for entry in ["order", "subscribe", "cancel", "orders", "trades"]
        if value or entry not in {"orders", "trades"}
    ],
)
def test_bad_instrument_precedes_readiness_catalog_and_native_requests(value, entry):
    executor = SimpleNamespace(
        _require_new_order_ready=Mock(),
        _ensure_instrument_symbols=Mock(),
        _query=Mock(),
        _config=Mock(),
        _send_trader_request=Mock(),
        _order_keys=Mock(),
        _get_pending_orders_impl=Mock(),
        _market_api=Mock(),
        _trader_api=Mock(),
    )
    with pytest.raises(CTPInputValidationError, match="合约代码格式"):
        if entry == "order":
            CTPExecutor._place_order_impl(executor, value, "buy", "limit", 1)
        elif entry == "subscribe":
            CTPExecutor.initialize_websocket(executor, ["rb2612", value])
        elif entry == "cancel":
            CTPExecutor._cancel_order_impl(executor, value, "20261018:1:2:123")
        elif entry == "orders":
            CTPExecutor._get_pending_orders_impl(executor, value)
        else:
            CTPExecutor._query_trades_impl(executor, value, "20261018:1:2:123")
    for name in ("_require_new_order_ready", "_ensure_instrument_symbols", "_query", "_send_trader_request"):
        getattr(executor, name).assert_not_called()
    assert executor._market_api.mock_calls == executor._trader_api.mock_calls == []


@pytest.mark.parametrize("value", BAD)
def test_cancel_rejected_before_lookup_or_query(value):
    executor = SimpleNamespace(_order_keys=Mock(), _get_pending_orders_impl=Mock(), _send_trader_request=Mock())
    with pytest.raises(ValueError):
        CTPExecutor._cancel_order_impl(executor, "rb2612", value)
    executor._order_keys.get.assert_not_called()
    executor._get_pending_orders_impl.assert_not_called()
    executor._send_trader_request.assert_not_called()


@pytest.mark.parametrize("value", BAD)
@pytest.mark.parametrize("entry", ["catalog", "commission", "depth"])
def test_query_rejected_before_any_send(value, entry):
    executor = SimpleNamespace(_query=Mock(), _config=Mock())
    with pytest.raises(ValueError):
        if entry == "catalog":
            CTPExecutor._query_catalog(executor, [value], 1)
        elif entry == "commission":
            CTPExecutor._query_commission_rates(executor, value)
        else:
            build_query_depth_market_data(value, "SHFE")
    executor._query.assert_not_called()
    executor._config.assert_not_called()


@pytest.mark.parametrize("field", ["broker_id", "investor_id", "app_id", "auth_code"])
@pytest.mark.parametrize("value", BAD)
def test_config_and_bypassed_config_rejected(config, field, value):
    data = config.model_dump()
    with pytest.raises(ValueError):
        CTPAccountConfig.model_validate({**data, field: value})
    bypassed = config.model_copy(update={field: value})
    for build in (build_authenticate, build_query_account):
        with pytest.raises(ValueError):
            build(bypassed)


@pytest.mark.parametrize(
    "symbol", ["rb2612", "IO2612-C-4000", "m2701C3000", "SP b2702&b2703", "SPC m2701C3000&m2701P3000"]
)
def test_valid_instruments_preserved(symbol):
    assert validate_instrument(symbol) == symbol
    assert build_query_depth_market_data(symbol, "DCE").InstrumentID == symbol


def test_all_query_and_legal_delimiters_preserved(config):
    assert validate_instrument("", allow_all=True) == ""
    with pytest.raises(ValueError):
        validate_instrument("")
    assert validate_cancel_id("20261018:1:2:123") == "20261018:1:2:123"
    assert CTPAccountConfig.model_validate({**config.model_dump(), "app_id": "test_app-1.0"}).app_id == "test_app-1.0"
    assert build_authenticate(config).UserID == config.investor_id


@pytest.mark.parametrize("value", BAD[:-1] + ["wrong:id", "20261018:1:2:!"])
def test_manual_cancel_rejected_before_worker_dispatch(value, monkeypatch):
    import asyncio

    from axile.common.trade_channel import TradeChannel
    from axile.server import account_orders
    from axile.server.db.models.account_order import OrderCancelRequest

    state = Mock(side_effect=AssertionError("非法撤单不得创建工作进程或查询委托"))
    monkeypatch.setattr(account_orders, "_state", state)
    payload = OrderCancelRequest(request_id="offline", order_id=value, symbol="rb2612")
    with pytest.raises(ValueError):
        asyncio.run(account_orders.cancel_account_order(SimpleNamespace(trade_channel=TradeChannel.CTP), payload))
    state.assert_not_called()


@pytest.mark.parametrize("value", BAD)
@pytest.mark.parametrize(
    "name", ["ReqOrderInsert", "ReqOrderAction", "ReqQryInstrument", "ReqQryOrder", "ReqQryDepthMarketData"]
)
def test_raw_native_boundary_rejects_before_state_checks(value, name):
    executor = SimpleNamespace(_trader_api=Mock())
    with pytest.raises(CTPInputValidationError, match="合约代码格式"):
        CTPExecutor._call_trader_request(executor, name, SimpleNamespace(InstrumentID=value))
    assert executor._trader_api.mock_calls == []
