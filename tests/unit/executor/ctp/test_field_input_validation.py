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


def test_legacy_quote_max_age_is_ignored(config):
    legacy_data = {**config.model_dump(), "quote_max_age_seconds": 30.0}

    assert CTPAccountConfig.model_validate(legacy_data).model_dump() == config.model_dump()
    assert "quote_max_age_seconds" not in CTPAccountConfig.model_json_schema()["properties"]


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


@pytest.mark.parametrize("session_id", [-978444561, -2147483648, 0, 2, 2147483647])
def test_generated_cancel_id_preserves_signed_session_in_native_request(config, session_id):
    from axile.executor.ctp.converters import stable_order_id

    order_id = stable_order_id("20261104", 1, session_id, "8")
    assert validate_cancel_id(order_id) == order_id
    executor = SimpleNamespace(
        _order_keys={
            order_id: dict(order_ref="8", front_id=1, session_id=session_id, exchange_id="SHFE", order_sys_id="100")
        },
        _config=lambda: config,
        _get_pending_orders_impl=Mock(),
        _send_trader_request=Mock(),
    )
    assert CTPExecutor._cancel_order_impl(executor, "rb2612", order_id)
    executor._send_trader_request.assert_called_once()
    operation, method, request = executor._send_trader_request.call_args.args
    assert (operation, method) == ("cancel_order_ctp", "ReqOrderAction")
    assert (request.FrontID, request.SessionID, request.OrderRef) == (1, session_id, "8")
    executor._get_pending_orders_impl.assert_not_called()


@pytest.mark.parametrize(
    "value",
    [
        "20261104:1:--2:8",
        "20261104:1:+2:8",
        "20261104:1:-:8",
        "20261104:1:2.0:8",
        "20261104:-1:2:8",
        "20261104:1:2:-8",
        "20261104:1:-2:1234567890123",
        "20261104:1:-2:8\n",
    ],
)
def test_invalid_signed_cancel_id_never_reaches_lookup_or_send(value):
    test_cancel_rejected_before_lookup_or_query(value)


def test_manual_cancel_accepts_negative_session_before_worker_dispatch(monkeypatch):
    import asyncio

    from axile.common.trade_channel import TradeChannel
    from axile.server import account_orders
    from axile.server.db.models.account_order import OrderCancelRequest

    state = Mock(side_effect=RuntimeError("worker dispatch reached"))
    monkeypatch.setattr(account_orders, "_state", state)
    payload = OrderCancelRequest(request_id="offline", order_id="20261104:1:-978444561:8", symbol="rb2612")
    account = SimpleNamespace(trade_channel=TradeChannel.CTP)
    with pytest.raises(RuntimeError, match="worker dispatch reached"):
        asyncio.run(account_orders.cancel_account_order(account, payload))
    state.assert_called_once_with(account)


def test_cancel_all_preserves_local_validation_reason():
    from tests.unit.executor.test_base_executor_account_control_wrappers import (
        _build_guard,
        _make_pending_order,
        _WrapperExecutor,
    )

    executor = _WrapperExecutor(_build_guard({}))
    executor.pending_orders = [_make_pending_order("rb2612", "invalid-id")]
    executor.cancel_results = {"invalid-id": CTPInputValidationError("订单编号格式不正确")}
    with pytest.raises(RuntimeError, match="invalid-id.*CTP 参数无效.*订单编号格式不正确"):
        executor.cancel_all_orders()
