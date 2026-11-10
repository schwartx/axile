"""渠道排队及发送锁等待后，必须重验实际消费的旧行情。"""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from axile.channels.gm_sessions import SHANGHAI
from axile.executor.algorithms.utils.clock import get_default_clock
from axile.executor.execution_session import ExecutionSession
from axile.executor.gm.core import gm_strategy
from axile.executor.gm.core.api_bridge import GMApiBridge
from axile.executor.gm.core.strategy_bridge import GMStrategyBridge
from axile.executor.gm.gm_execute import GMExecutor
from axile.executor.market_data_validation import DEFAULT_QUOTE_MAX_AGE_SECONDS, MarketDataUnavailableError
from axile.executor.models.unified_input import GMAccountConfig
from axile.executor.models.unified_order import OrderDirection, OrderType
from axile.executor.order_submissions import OrderSubmissionRecorder
from axile.executor.tq.tq_execute import TQTradingTimeCheck, TQTradingTimeStatus
from tests.fixtures.algorithm_fakes import ClockStub
from tests.unit.executor.ctp.test_requests import _at_clock, _submit_point_executor, config  # noqa: F401
from tests.unit.executor.test_market_data_validation import quote
from tests.unit.executor.tq.test_executor import executor  # noqa: F401


@pytest.fixture
def clock(monkeypatch):
    clock = ClockStub()
    clock.current = datetime(2026, 8, 24, 10, 0, tzinfo=SHANGHAI).timestamp()
    monkeypatch.setattr(get_default_clock(), "time", clock.time)
    return clock


def bind_session(monkeypatch, owner, consumed):
    events, callbacks = [], []
    monkeypatch.setattr(owner, "register_price_callback_for_symbol", lambda cb, _symbol: callbacks.append(cb))
    session = ExecutionSession(owner, consumed.symbol)
    monkeypatch.setattr(session, "emit_audit_event", lambda **event: events.append(event))
    session.register_price_callback(lambda _quote: None)
    return session, events, callbacks[0]


def assert_stale_evidence(error, session, events, consumed, clock, reason="行情已过期"):
    assert isinstance(error, MarketDataUnavailableError)
    assert error.reason_code == "COMMON.MARKET_DATA.UNAVAILABLE"
    assert events[-1]["reason_code"] == error.reason_code
    assert events[-1]["details"] == {
        "reason": reason,
        "quote_timestamp": consumed.timestamp,
        "checked_at": clock.time(),
        "source": consumed.extra["source"],
        "time_basis": "exchange",
    }


def test_tq_queue_revalidates_before_audit_registration_and_sdk(executor, monkeypatch, clock):  # noqa: F811
    instance, api = executor
    instance._ensure_instrument_symbols(["rb2610"])
    monkeypatch.setattr(
        instance, "_check_tq_symbol_trading_time", lambda *_: TQTradingTimeCheck(TQTradingTimeStatus.OPEN)
    )
    audit_register = Mock(wraps=instance._terminal_audit.register)
    monkeypatch.setattr(instance._terminal_audit, "register", audit_register)
    consumed = quote(clock.time(), symbol="rb2610", last_price=3200, bid_price=3199, ask_price=3201)
    session, events, publish = bind_session(monkeypatch, instance, consumed)
    runtime = instance._require_runtime()
    blocked, release, queued = threading.Event(), threading.Event(), threading.Event()

    def block_owner(_api):
        blocked.set()
        assert release.wait(2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        blocking = pool.submit(runtime.call, block_owner)
        try:
            assert blocked.wait(2)
            original_put = runtime._commands.put

            def enqueue(command):
                original_put(command)
                queued.set()

            monkeypatch.setattr(runtime._commands, "put", enqueue)
            order = pool.submit(
                session.place_order, OrderDirection.BUY, OrderType.LIMIT, 1, consumed.bid_price, market_data=consumed
            )
            assert queued.wait(2)
            clock.sleep(11)
            fresh = consumed.model_copy(update={"timestamp": int(clock.time() * 1000)})
            publish(fresh)
            quote_calls = api.quote_calls
            release.set()
            with pytest.raises(MarketDataUnavailableError, match="已过期") as rejected:
                order.result(timeout=2)
            blocking.result(timeout=2)
        finally:
            release.set()
    assert_stale_evidence(rejected.value, session, events, consumed, clock)
    assert api.insert_args == {}
    assert api.quote_calls == quote_calls
    audit_register.assert_not_called()
    assert instance._terminal_audit._origins == {}


def gm_executor(monkeypatch):
    instance = GMExecutor(
        GMAccountConfig(connection_mode="service", account_id="test", token="test", serv_addr="127.0.0.1:7001"),
        connect=False,
    )
    bridge = GMStrategyBridge("test", "test", instance._callback_dispatcher, strategy_id="offline-quote-boundary")
    bridge._running = True
    instance._strategy_bridge = bridge
    monkeypatch.setattr(instance, "_verify_connection", lambda: True)
    return instance, bridge


def test_gm_queue_preserves_validator_error_and_excludes_callback_from_sdk(monkeypatch, clock):
    instance, bridge = gm_executor(monkeypatch)
    sdk = Mock()
    monkeypatch.setitem(GMApiBridge._SDK_OPERATION_METHODS, "order_volume", sdk)
    context = SimpleNamespace(request_queue=bridge._request_queue)
    monkeypatch.setattr(gm_strategy, "_get_bridge_context", lambda: context)
    monkeypatch.setattr(gm_strategy, "_is_stopped", lambda: False)
    monkeypatch.setattr(gm_strategy, "_check_account_startup", lambda _: True)
    consumed = quote(clock.time(), symbol="600000.SH")
    session, events, publish = bind_session(monkeypatch, instance, consumed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        order = pool.submit(
            session.place_order, OrderDirection.BUY, OrderType.LIMIT, 100, consumed.bid_price, market_data=consumed
        )
        request = bridge._request_queue.get(timeout=2)
        try:
            assert request.request.submission_validator is not None
            assert "submission_validator" not in request.request.as_kwargs()
            assert "session" not in request.request.as_kwargs()
            bridge._request_queue.put(request)
            clock.sleep(11)
            publish(consumed.model_copy(update={"timestamp": int(clock.time() * 1000)}))
            gm_strategy._process_bridge_requests(None)
            with pytest.raises(MarketDataUnavailableError, match="已过期") as rejected:
                order.result(timeout=2)
        finally:
            bridge._running = False
            bridge._fail_pending_requests(RuntimeError("offline test finished"))
    assert request.future.exception() is rejected.value
    assert_stale_evidence(rejected.value, session, events, consumed, clock)
    sdk.assert_not_called()


def test_gm_without_session_preserves_sdk_arguments(monkeypatch, clock):
    instance, bridge = gm_executor(monkeypatch)
    sdk = Mock(return_value=[{"cl_ord_id": "direct-order"}])
    monkeypatch.setitem(GMApiBridge._SDK_OPERATION_METHODS, "order_volume", sdk)
    monkeypatch.setattr(instance, "_call_bridge", lambda request, **_: GMApiBridge.execute_request(request))
    try:
        order = instance._place_order_impl("600000.SH", OrderDirection.BUY, OrderType.LIMIT, 100, 10)
    finally:
        bridge._running = False
    assert order.order_id == "direct-order"
    sdk.assert_called_once_with(
        symbol="SHSE.600000", volume=100, side=1, order_type=1, position_effect=1, price=10, account="test"
    )


def test_ctp_revalidates_frozen_quote_after_final_send_lock_wait(config, monkeypatch, clock):  # noqa: F811
    instance = _submit_point_executor(config)
    _at_clock(monkeypatch, datetime.fromtimestamp(clock.time(), SHANGHAI))
    consumed = instance._quotes["ag2612"]
    consumed.timestamp = int(clock.time() * 1000)
    consumed.extra.update(received_at=clock.time(), source="ctp.subscription", time_basis="exchange")
    session, events, publish = bind_session(monkeypatch, instance, consumed)
    facts = []
    instance.order_submission_recorder = OrderSubmissionRecorder(1, facts.append)
    ready, start_send, waiting = threading.Event(), threading.Event(), threading.Event()
    native_lock = instance._lock
    next_id = instance._next_id

    class ObservedLock:
        def __enter__(self):
            if start_send.is_set():
                waiting.set()
            native_lock.acquire()
            return self

        def __exit__(self, *_):
            native_lock.release()

    def before_final_lock():
        rid = next_id()
        ready.set()
        assert start_send.wait(2)
        return rid

    monkeypatch.setattr(instance, "_lock", ObservedLock())
    monkeypatch.setattr(instance, "_next_id", before_final_lock)
    with ThreadPoolExecutor(max_workers=1) as pool:
        order = pool.submit(
            session.place_order, OrderDirection.BUY, OrderType.LIMIT, 1, consumed.bid_price, market_data=consumed
        )
        try:
            assert ready.wait(2)
            with native_lock:
                start_send.set()
                assert waiting.wait(2)
                clock.sleep(DEFAULT_QUOTE_MAX_AGE_SECONDS + 1)
                fresh = consumed.model_copy(deep=True)
                fresh.timestamp = int(clock.time() * 1000)
                fresh.extra["received_at"] = clock.time()
                instance._quotes["ag2612"] = fresh
                publish(fresh)
            with pytest.raises(MarketDataUnavailableError, match="行情时间过期") as rejected:
                order.result(timeout=2)
        finally:
            start_send.set()
    assert_stale_evidence(rejected.value, session, events, consumed, clock, reason="交易所行情时间过期或超前")
    instance._trader_api.ReqOrderInsert.assert_not_called()
    assert facts == []
    assert instance._order_keys == {}
