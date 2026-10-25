"""GM 单队列 envelope 保留 SDK 参数、账户过滤与取消/过期约束。"""

from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock, sentinel

import pytest

from axile.executor.gm.core import api_bridge, gm_strategy
from axile.executor.gm.core.strategy_bridge import GMBridgeRequest, GMStrategyBridge
from axile.executor.gm.gm_execute import GMExecutor
from axile.executor.models.unified_input import GMAccountConfig
from axile.executor.models.unified_order import OrderDirection, OrderType


@pytest.fixture
def runtime(monkeypatch):
    bridge = GMStrategyBridge(token="offline", account_id="target", callback_dispatcher=Mock())
    bridge._running = True
    context = SimpleNamespace(request_queue=bridge._request_queue, subscribe_symbols=bridge._subscribe_symbols)
    monkeypatch.setattr(gm_strategy, "_get_bridge_context", lambda: context)
    monkeypatch.setattr(gm_strategy, "_is_stopped", lambda: False)
    monkeypatch.setattr(gm_strategy, "_check_account_startup", lambda _context: True)
    return bridge


@pytest.fixture
def draining_runtime(runtime, monkeypatch):
    queued = []
    put = runtime._request_queue.put

    def submit(request):
        queued.append(request)
        put(request)
        gm_strategy._process_bridge_requests(None)

    monkeypatch.setattr(runtime._request_queue, "put", submit)
    return runtime, queued


@pytest.mark.parametrize(
    "operation,kwargs,expected_sdk_kwargs",
    [
        ("get_cash", {"account_id": None}, {}),
        ("get_position", {"account_id": ""}, {"account_id": ""}),
        ("current", {"symbols": ["SZSE.000001", "SHSE.600000"]}, {"symbols": ["SZSE.000001", "SHSE.600000"]}),
        ("get_orders", {"account_id": "target"}, {}),
        ("get_unfinished_orders", {"account_id": "target"}, {}),
        ("get_execution_reports", {"account_id": "target"}, {}),
        (
            "order_cancel",
            {
                "wait_cancel_orders": [
                    {"cl_ord_id": "one", "account_id": "target"},
                    {"cl_ord_id": "two", "account_id": ""},
                ]
            },
            {
                "wait_cancel_orders": [
                    {"cl_ord_id": "one", "account_id": "target"},
                    {"cl_ord_id": "two", "account_id": ""},
                ]
            },
        ),
        (
            "order_volume",
            {
                "symbol": "SHSE.600000",
                "volume": 100,
                "side": 1,
                "order_type": 2,
                "position_effect": 1,
                "price": 0,
                "account": "target",
            },
            {
                "symbol": "SHSE.600000",
                "volume": 100,
                "side": 1,
                "order_type": 2,
                "position_effect": 1,
                "price": 0,
                "account": "target",
            },
        ),
    ],
)
def test_sdk_kwargs_are_sent_only_after_the_actual_queue_consumer_runs(
    draining_runtime, monkeypatch, operation, kwargs, expected_sdk_kwargs
):
    bridge, queued = draining_runtime
    records = [{"account_id": "target"}, {"account_id": "other"}, {}]
    filtered = operation in {"get_orders", "get_unfinished_orders", "get_execution_reports"}

    def sdk_call(**actual):
        assert len(queued) == 1
        assert queued[0].future.running()
        assert actual == expected_sdk_kwargs
        return records if filtered else sentinel.result

    sdk = Mock(side_effect=sdk_call)
    monkeypatch.setattr(api_bridge, operation, sdk)
    sdk.assert_not_called()
    assert queued == []
    result = bridge.call(operation, timeout=None, **kwargs)
    sdk.assert_called_once_with(**expected_sdk_kwargs)
    assert result == records[:1] if filtered else result is sentinel.result
    assert queued[0].operation == operation
    assert queued[0].kwargs == kwargs
    assert queued[0].deadline is None
    assert queued[0].future.done()


@pytest.mark.parametrize("operation", ["get_orders", "get_unfinished_orders", "get_execution_reports"])
@pytest.mark.parametrize("account_id", [None, ""])
def test_local_order_and_trade_filters_keep_none_distinct_from_empty_account(
    draining_runtime, monkeypatch, operation, account_id
):
    bridge, _ = draining_runtime
    records = [{"account_id": "target"}, {"account_id": ""}, {}]
    sdk = Mock(return_value=records)
    monkeypatch.setattr(api_bridge, operation, sdk)
    assert bridge.call(operation, account_id=account_id) == (records if account_id is None else records[1:2])
    sdk.assert_called_once_with()


@pytest.mark.parametrize("operation", ["get_cash", "get_position"])
@pytest.mark.parametrize("account_id", [None, ""])
def test_optional_sdk_account_key_is_omitted_only_for_none(draining_runtime, monkeypatch, operation, account_id):
    bridge, _ = draining_runtime
    sdk = Mock(return_value=sentinel.result)
    monkeypatch.setattr(api_bridge, operation, sdk)
    assert bridge.call(operation, account_id=account_id) is sentinel.result
    sdk.assert_called_once_with(**({} if account_id is None else {"account_id": ""}))


@pytest.mark.parametrize("operation", ["order_volume", "order_cancel"])
@pytest.mark.parametrize("state", ["cancelled", "expired"])
def test_cancelled_or_expired_trading_requests_never_reach_sdk(runtime, monkeypatch, operation, state):
    sdk = Mock()
    monkeypatch.setattr(api_bridge, operation, sdk)
    future = Future()
    if state == "cancelled":
        future.cancel()
    request = GMBridgeRequest(operation=operation, kwargs={}, future=future, deadline=0 if state == "expired" else None)
    runtime._request_queue.put(request)
    gm_strategy._process_bridge_requests(None)
    sdk.assert_not_called()
    assert runtime._request_queue.empty()
    if state == "cancelled":
        assert future.cancelled()
    else:
        assert isinstance(future.exception(), TimeoutError)


def test_submit_timeout_cancels_the_queued_future_before_late_consumption(runtime, monkeypatch):
    sdk = Mock()
    monkeypatch.setattr(api_bridge, "order_volume", sdk)
    with pytest.raises(TimeoutError, match="operation=order_volume, timeout=0"):
        runtime.call("order_volume", timeout=0, symbol="SHSE.600000")
    request = runtime._request_queue.get_nowait()
    assert request.future.cancelled()
    assert request.deadline is not None
    runtime._request_queue.put(request)
    gm_strategy._process_bridge_requests(None)
    sdk.assert_not_called()


def test_sdk_error_is_returned_through_the_same_future(draining_runtime, monkeypatch):
    bridge, queued = draining_runtime
    error = RuntimeError("SDK rejected request")
    monkeypatch.setattr(api_bridge, "current", Mock(side_effect=error))
    with pytest.raises(RuntimeError) as caught:
        bridge.call("current", symbols=["SHSE.600000"])
    assert caught.value is error
    assert queued[0].future.exception() is error


def test_unknown_operation_still_fails_the_closed_whitelist(draining_runtime):
    bridge, queued = draining_runtime
    with pytest.raises(ValueError, match="未知的 GM bridge 操作"):
        bridge.call("not-an-sdk-operation")
    assert isinstance(queued[0].future.exception(), ValueError)


def test_stop_drain_fails_pending_future_and_keeps_cancelled_future_cancelled(runtime):
    pending, cancelled = Future(), Future()
    cancelled.cancel()
    for future in (pending, cancelled):
        runtime._request_queue.put(GMBridgeRequest(operation="order_cancel", kwargs={}, future=future))
    error = RuntimeError("bridge stopped")
    runtime._fail_pending_requests(error)
    assert pending.exception() is error
    assert cancelled.cancelled()
    assert runtime._request_queue.empty()


def test_subscription_uses_runtime_branch_and_preserves_symbol_order(draining_runtime, monkeypatch):
    bridge, queued = draining_runtime
    subscribe = Mock()
    monkeypatch.setattr(gm_strategy, "subscribe", subscribe, raising=False)
    symbols = ["SHSE.600000", "SHSE.600000", "SZSE.000001"]
    assert bridge.request_symbols(symbols) == ["SHSE.600000", "SZSE.000001"]
    assert queued[0].operation == "subscribe_symbols"
    assert [call.kwargs for call in subscribe.call_args_list] == [
        {"symbols": "SHSE.600000", "frequency": "tick"},
        {"symbols": "SZSE.000001", "frequency": "tick"},
    ]


@pytest.mark.parametrize(
    "cash,ok",
    [
        ({}, False),
        (None, False),
        ({"account_id": "other"}, False),
        ({"account_id": "target"}, True),
        ({"available": 0}, True),
    ],
)
def test_executor_cash_guard_keeps_target_account_ownership(cash, ok):
    instance = GMExecutor(
        GMAccountConfig(connection_mode="service", account_id="target", token="offline", serv_addr="127.0.0.1:7001"),
        connect=False,
    )
    call = Mock(return_value=cash)
    instance._strategy_bridge = SimpleNamespace(is_running=lambda: True, is_ready=lambda: True, call=call)
    if ok:
        assert instance._call_bridge("get_cash", account_id="target") is cash
    else:
        with pytest.raises(RuntimeError, match="有效资金信息"):
            instance._call_bridge("get_cash", account_id="target")
    call.assert_called_once_with("get_cash", timeout=30.0, account_id="target")


@pytest.mark.parametrize("direction,side,effect", [(OrderDirection.BUY, 1, 1), (OrderDirection.SELL, 2, 2)])
def test_executor_order_intent_reaches_sdk_with_native_integer_constants(
    draining_runtime, monkeypatch, direction, side, effect
):
    bridge, _ = draining_runtime
    instance = GMExecutor(
        GMAccountConfig(connection_mode="service", account_id="target", token="offline", serv_addr="127.0.0.1:7001"),
        connect=False,
    )
    instance._strategy_bridge = bridge
    monkeypatch.setattr(instance, "_verify_connection", lambda: True)
    sell_guard = Mock()
    monkeypatch.setattr(instance, "_ensure_gm_sell_close_supported", sell_guard)
    sdk = Mock(return_value=[{"cl_ord_id": "sdk-order-one"}])
    monkeypatch.setattr(api_bridge, "order_volume", sdk)
    order = instance._place_order_impl("600000.SH", direction, OrderType.LIMIT, 100.9, price=12.3)
    sdk.assert_called_once_with(
        symbol="SHSE.600000", volume=100, side=side, order_type=1, position_effect=effect, price=12.3, account="target"
    )
    assert order.order_id == "sdk-order-one"
    if direction is OrderDirection.SELL:
        sell_guard.assert_called_once_with(symbol="600000.SH", requested_volume=100)
    else:
        sell_guard.assert_not_called()


def test_executor_cancel_keeps_sdk_target_account_id_key(draining_runtime, monkeypatch):
    bridge, _ = draining_runtime
    instance = GMExecutor(
        GMAccountConfig(connection_mode="service", account_id="target", token="offline", serv_addr="127.0.0.1:7001"),
        connect=False,
    )
    instance._strategy_bridge = bridge
    monkeypatch.setattr(instance, "_verify_connection", lambda: True)
    sdk = Mock()
    monkeypatch.setattr(api_bridge, "order_cancel", sdk)
    assert instance._cancel_order_impl("600000.SH", "sdk-order-one")
    sdk.assert_called_once_with(wait_cancel_orders=[{"cl_ord_id": "sdk-order-one", "account_id": "target"}])
