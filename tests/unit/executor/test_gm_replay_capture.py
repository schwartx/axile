"""GM 实际回调和 bridge 队列的录制身份，不连接真实渠道。"""

import queue
from concurrent.futures import Future
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from axile.executor import replay as recording
from axile.executor.gm.core import gm_strategy
from axile.executor.gm.core.strategy_bridge import GMBridgeRequest, GMStrategyBridge
from axile.executor.gm.gm_execute import GMExecutor
from axile.executor.models.unified_input import GMAccountConfig


@pytest.fixture
def instance():
    owner = GMExecutor(
        GMAccountConfig(
            connection_mode="service", account_id="private-account", token="offline", serv_addr="127.0.0.1:7001"
        ),
        connect=False,
    )
    owner._replay_sync_generation = 4
    owner._replay_account_id = 7
    return owner


@pytest.fixture
def callback_context(instance, monkeypatch):
    context = SimpleNamespace(
        dispatcher=instance._callback_dispatcher,
        stats={"tick_received": 0, "order_status_received": 0, "execution_report_received": 0, "errors": 0},
    )
    monkeypatch.setattr(gm_strategy, "_get_bridge_context", lambda: context)
    monkeypatch.setattr(gm_strategy, "_is_stopped", lambda: False)
    return context


def _subscribe(owner, received):
    recording.set_replay_observer(owner, lambda topic, data: received.append((topic, data)))


@pytest.mark.parametrize(
    "callback,conversion,topic,row",
    [
        (
            "on_order_status",
            "_convert_order_to_unified",
            "/order",
            {"cl_ord_id": "order-1", "symbol": "SHSE.600000", "status": 999},
        ),
        (
            "on_execution_report",
            "_convert_execrpt_to_trade",
            "/trade",
            {"cl_ord_id": "order-1", "symbol": "SHSE.600000", "price": 12.3, "volume": 100, "exec_type": 15},
        ),
    ],
)
def test_native_callback_is_observed_before_lossy_conversion(
    instance, callback_context, monkeypatch, callback, conversion, topic, row
):
    received = []
    _subscribe(instance, received)
    monkeypatch.setattr(gm_strategy, conversion, Mock(side_effect=ValueError("business conversion failed")))
    getattr(gm_strategy, callback)(None, SimpleNamespace(**row))
    assert received[0][0] == topic
    assert received[0][1]["symbol"] == "600000.SH"
    assert received[0][1]["sync_generation"] == 4
    assert callback_context.stats["errors"] == 1


def test_tick_is_observed_without_algorithm_price_listeners(instance, callback_context):
    received = []
    _subscribe(instance, received)
    tick = SimpleNamespace(
        symbol="SHSE.600000",
        created_at=datetime.fromisoformat("2026-10-30T10:00:00+08:00"),
        price=12.3,
        last_volume=100,
        quotes=[SimpleNamespace(bid_p=12.2, bid_v=300, ask_p=12.4, ask_v=200)],
    )
    gm_strategy.on_tick(None, tick)
    assert received[0][0] == "/quote/600000.SH"
    assert received[0][1]["source"] == "gm.tick"
    assert received[0][1]["available_depth"] == {"bid": 1, "ask": 1}
    assert callback_context.stats["tick_received"] == 1
    assert instance._callback_dispatcher.get_callback_count()["price_callbacks"] == 0


def test_callback_entered_while_off_cannot_borrow_later_observer(instance, callback_context, monkeypatch):
    received = []

    def switch_on():
        _subscribe(instance, received)
        return False

    monkeypatch.setattr(gm_strategy, "_is_stopped", switch_on)
    monkeypatch.setattr(gm_strategy, "_convert_order_to_unified", Mock(side_effect=ValueError("stop conversion")))
    gm_strategy.on_order_status(None, SimpleNamespace(symbol="SHSE.600000", cl_ord_id="old"))
    assert received == []


def test_query_and_rows_keep_entry_receipt_and_release_token(instance, monkeypatch):
    old, new = [], []
    _subscribe(instance, old)
    token = {"sync_generation": 4, "query_basis": {"start_seq": 10, "request_revision": 2}}
    recorder = SimpleNamespace(begin_query=Mock(return_value=token), finish_query=Mock())
    instance._replay_recorder = recorder
    rows = [{"symbol": "SHSE.600000", "cl_ord_id": "one", "status": 1, "order_type": 1}]

    def query(*_args, **_kwargs):
        assert recorder.begin_query.call_count == 1
        _subscribe(instance, new)
        return rows

    monkeypatch.setattr(instance, "_call_bridge_impl", query)
    monkeypatch.setattr(instance, "_verify_connection", lambda: True)
    assert instance._call_bridge("get_orders") is rows
    assert new == []
    assert old[0][0] == "/order_set" and old[0][1]["query_basis"] == token["query_basis"]
    assert old[0][1]["scope_complete"] is True
    recorder.finish_query.assert_called_once_with(token)


def test_query_failure_releases_token_and_preserves_error(instance, monkeypatch):
    _subscribe(instance, [])
    token = {"query_basis": {"start_seq": 10, "request_revision": 2}}
    recorder = SimpleNamespace(begin_query=Mock(return_value=token), finish_query=Mock())
    instance._replay_recorder = recorder
    error = ValueError("SDK error")
    monkeypatch.setattr(instance, "_call_bridge_impl", Mock(side_effect=error))
    with pytest.raises(ValueError) as raised:
        instance._call_bridge("get_orders")
    assert raised.value is error
    recorder.finish_query.assert_called_once_with(token)


def test_normal_current_and_position_queries_are_observed_without_extra_sdk_calls(instance, monkeypatch):
    received = []
    _subscribe(instance, received)
    rows = [{"symbol": "SHSE.600000", "side": 1, "volume": 1000, "available_now": 500, "order_frozen": 500}]
    query = Mock(return_value=rows)
    monkeypatch.setattr(instance, "_call_bridge_impl", query)
    assert instance._call_bridge("get_position") is rows
    assert received[0][1]["positions"][0]["available_quantity"] == 500
    assert received[0][1]["positions"][0]["frozen_quantity"] == 500
    query.assert_called_once()
    query.reset_mock()
    assert instance._call_bridge("current") is rows
    assert received[-1][0] == "/quote/600000.SH"
    assert received[-1][1]["source"] == "gm.current"
    query.assert_called_once()


def test_observation_failure_cannot_change_sdk_return_or_exception(instance, monkeypatch):
    def broken_observer(*_args):
        raise RuntimeError("recorder stopped")

    recording.set_replay_observer(instance, broken_observer)
    rows = [{"symbol": "INVALID"}]
    monkeypatch.setattr(instance, "_call_bridge_impl", Mock(return_value=rows))
    assert instance._call_bridge("current") is rows


def test_native_command_is_only_recorded_when_queue_actually_executes(instance, monkeypatch):
    old, new = [], []
    _subscribe(instance, old)
    original = recording.capture_receipt(instance)
    request = GMBridgeRequest(
        operation="order_cancel",
        kwargs={"wait_cancel_orders": [{"cl_ord_id": "one", "account_id": "private-account"}]},
        future=Future(),
        replay_owner=instance,
        replay_receipt=original,
        replay_command_id="command-1",
    )
    context = SimpleNamespace(request_queue=queue.Queue())
    context.request_queue.put(request)
    monkeypatch.setattr(gm_strategy, "_is_stopped", lambda: False)
    monkeypatch.setattr(gm_strategy, "_get_bridge_context", lambda: context)
    monkeypatch.setattr(gm_strategy, "_check_account_startup", lambda _context: True)
    sdk = Mock(return_value=None)
    monkeypatch.setattr(gm_strategy, "_dispatch_bridge_request", sdk)
    _subscribe(instance, new)
    gm_strategy._process_bridge_requests(None)
    assert new == []
    assert [data["phase"] for _, data in old] == ["native_request", "native_return"]
    assert old[0][1]["command_id"] == "command-1"
    assert old[0][1]["order_id"] == "one"
    assert "private-account" not in str(old)
    sdk.assert_called_once_with("order_cancel", request.kwargs)
    assert request.future.result() is None


@pytest.mark.parametrize("cancelled", [False, True])
def test_expired_or_cancelled_queue_request_does_not_record_native_send(instance, monkeypatch, cancelled):
    received = []
    _subscribe(instance, received)
    request = GMBridgeRequest(
        operation="order_volume",
        kwargs={},
        future=Future(),
        deadline=0,
        replay_owner=instance,
        replay_receipt=recording.capture_receipt(instance),
    )
    if cancelled:
        request.future.cancel()
    context = SimpleNamespace(request_queue=queue.Queue())
    context.request_queue.put(request)
    monkeypatch.setattr(gm_strategy, "_is_stopped", lambda: False)
    monkeypatch.setattr(gm_strategy, "_get_bridge_context", lambda: context)
    monkeypatch.setattr(gm_strategy, "_check_account_startup", lambda _context: True)
    sdk = Mock()
    monkeypatch.setattr(gm_strategy, "_dispatch_bridge_request", sdk)
    gm_strategy._process_bridge_requests(None)
    assert received == []
    sdk.assert_not_called()


def test_bridge_submission_freezes_command_receipt_before_thread_handoff(instance, monkeypatch):
    received = []
    _subscribe(instance, received)
    bridge = GMStrategyBridge(
        token="offline", account_id="private-account", callback_dispatcher=instance._callback_dispatcher
    )
    bridge._running = True
    bridge._replay_owner = instance
    queued = []

    def put(request):
        queued.append(request)
        request.future.set_result(None)

    monkeypatch.setattr(bridge._request_queue, "put", put)
    with recording.replay_command(instance, "cancel_order", {}) as identity:
        bridge.call("order_cancel", wait_cancel_orders=[])
    assert queued[0].replay_receipt.observer is instance._replay_observer
    assert queued[0].replay_command_id == identity


def test_market_transport_disconnect_is_a_recorded_gap(instance, callback_context):
    received = []
    _subscribe(instance, received)
    gm_strategy.on_market_data_disconnected(None)
    assert received[0][0] == "/connection"
    assert received[0][1]["connected"] is False
    assert received[0][1]["synchronized"] is False
    assert received[0][1]["reason"] == "market_transport_disconnected"


def test_real_sdk_dictlike_reports_reach_capture_and_business_dispatch(instance, callback_context):
    from gm.model import DictLikeExecRpt, DictLikeOrder

    received = []
    _subscribe(instance, received)
    now = datetime.fromisoformat("2026-10-30T10:00:00+08:00")
    order = DictLikeOrder(
        symbol="SHSE.600000",
        cl_ord_id="one",
        side=1,
        order_type=1,
        price=12.3,
        volume=100,
        status=1,
        created_at=now,
        updated_at=now,
    )
    report = DictLikeExecRpt(
        symbol="SHSE.600000",
        cl_ord_id="one",
        exec_id="trade-1",
        order_id="",
        account_id="",
        exec_type=15,
        side=1,
        position_effect=1,
        price=12.3,
        volume=100,
        created_at=now,
    )
    assert gm_strategy._convert_execrpt_to_trade(report).trade_id == "trade-1"
    gm_strategy.on_order_status(None, order)
    gm_strategy.on_execution_report(None, report)
    assert [topic for topic, _ in received] == ["/order", "/trade"]
    assert received[1][1]["quantity"] == 100
    assert received[0][1]["field_quality"]["order_id"] == "known"
    assert received[1][1]["field_quality"]["order_id"] == "known"
    assert instance._callback_dispatcher.get_stats()["order_updates_received"] == 1
    assert instance._callback_dispatcher.get_stats()["trade_records_received"] == 1
    assert callback_context.stats["errors"] == 0


def test_native_order_response_query_is_bound_to_its_symbol(instance, monkeypatch):
    received = []
    _subscribe(instance, received)
    token = {"sync_generation": 4, "query_basis": {"start_seq": 1, "request_revision": 1}}
    recorder = SimpleNamespace(begin_query=Mock(return_value=token), finish_query=Mock())
    instance._replay_recorder = recorder
    rows = [{"symbol": "SHSE.600000", "cl_ord_id": "one", "status": 1, "order_type": 1}]
    monkeypatch.setattr(instance, "_call_bridge_impl", Mock(return_value=rows))
    assert instance._call_bridge("order_volume", symbol="SHSE.600000") is rows
    recorder.begin_query.assert_called_once_with(["600000.SH"])
    assert received[0][1]["query_basis"] == token["query_basis"]


def test_direct_sdk_action_entered_off_keeps_off_receipt_through_queue(instance, monkeypatch):
    received, queued = [], []
    bridge = GMStrategyBridge(
        token="offline", account_id="private-account", callback_dispatcher=instance._callback_dispatcher
    )
    bridge._running = True
    bridge._replay_owner = instance

    def put(request):
        queued.append(request)
        gm_strategy._observe_native_request(request, "native_request")
        request.future.set_result(None)

    def invoke(operation, **kwargs):
        _subscribe(instance, received)
        return bridge.call(operation, **kwargs)

    monkeypatch.setattr(bridge._request_queue, "put", put)
    monkeypatch.setattr(instance, "_call_bridge_impl", invoke)
    assert instance._call_bridge("order_cancel", wait_cancel_orders=[]) is None
    assert queued[0].replay_receipt is None
    assert received == []


@pytest.mark.parametrize("exec_type", [4, 5, 8, 18, 19])
def test_nonfill_callback_and_normal_query_keep_report_out_of_trade_stream(
    instance, callback_context, monkeypatch, exec_type
):
    from gm.model import DictLikeExecRpt

    received = []
    _subscribe(instance, received)
    report = DictLikeExecRpt(
        symbol="SHSE.600000",
        cl_ord_id="one",
        order_id="",
        account_id="",
        exec_id="nonfill-report",
        exec_type=exec_type,
        side=1,
        price=12.3,
        volume=100,
        created_at=datetime.fromisoformat("2026-10-30T10:00:00+08:00"),
    )
    gm_strategy.on_execution_report(None, report)
    monkeypatch.setattr(instance, "_call_bridge_impl", Mock(return_value=[report]))
    instance._call_bridge("get_execution_reports")
    assert [topic for topic, _data in received] == ["/command", "/command"]
    assert all(data["operation"] == "execution_report" and data["phase"] == "native_report" for _, data in received)
    assert all(data["quantity"] is None and data["price"] is None for _, data in received)
    assert all(data["attributes"]["exec_type"] == exec_type for _, data in received)
    assert callback_context.stats["execution_report_received"] == 1
