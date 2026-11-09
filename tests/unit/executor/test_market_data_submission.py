"""行情证据须跨账户限流保留，并在提交前重新校验。"""

from concurrent.futures import ThreadPoolExecutor

import pytest

from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.guard import AccountControlGuard
from axile.executor.account_control.models import AccountControlOverride
from axile.executor.account_control.presets import resolve_account_control_policy
from axile.executor.account_control.registry import ensure_default_account_control_registry_bootstrapped
from axile.executor.account_control.snapshot import AccountControlCounterSnapshot
from axile.executor.algorithms.utils import clock as clock_module
from axile.executor.algorithms.utils.clock import get_default_clock
from axile.executor.execution_session import ExecutionSession
from axile.executor.market_data_validation import MarketDataUnavailableError
from axile.executor.models.unified_order import OrderDirection, OrderType
from axile.executor.order_submission_context import (
    get_order_submission_context,
    order_submission_context,
    validate_order_submission,
)
from tests.fixtures.algorithm_fakes import ClockStub
from tests.unit.executor.test_base_executor_symbol_dispatch import _TestExecutor
from tests.unit.executor.test_market_data_validation import Owner, quote


@pytest.fixture(autouse=True)
def use_replayable_clock(monkeypatch):
    monkeypatch.setattr(clock_module, "_default_clock", ClockStub())


@pytest.mark.parametrize("publish_fresh_quote", [False, True])
def test_submission_revalidates_consumed_quote_after_real_guard_wait(monkeypatch, publish_fresh_quote):
    clock = ClockStub()
    clock.current = 1000.0
    monkeypatch.setattr(get_default_clock(), "time", clock.time)
    owner = _TestExecutor()
    submitted = []
    events = []
    callbacks = []
    monkeypatch.setattr(owner, "_place_order_impl", lambda *args, **kwargs: submitted.append(args))
    monkeypatch.setattr(owner, "emit_audit_event", lambda **kwargs: events.append(kwargs))
    monkeypatch.setattr(owner, "register_price_callback_for_symbol", lambda cb, symbol: callbacks.append(cb))
    session = ExecutionSession(owner, "rb2610")
    consumed = session.get_market_data()
    session.register_price_callback(lambda q: None)
    ensure_default_account_control_registry_bootstrapped()
    guard = AccountControlGuard(
        account_id=1,
        execution_id="market-data-wait",
        channel=TradeChannel.CTP,
        policy=resolve_account_control_policy(
            "default",
            AccountControlOverride.model_validate(
                {"operations": {"place_order": {"account": {"min_interval_ms": {"limit": 11000}}}}}
            ),
        ),
        baseline=AccountControlCounterSnapshot(),
        wait_clock=clock,
        wait_poll_interval_ms=11000,
    )
    guard.begin_operation("place_order", symbol="other").record_outcome("submitted")
    monkeypatch.setattr(owner, "get_account_control_guard", lambda: guard)

    def sleep(seconds):
        clock.sleep(seconds)
        if publish_fresh_quote:
            callbacks[0](quote(clock.time(), symbol="rb2610", bids=[(89, 100)], asks=[(91, 100)]))

    guard._sleep = sleep
    with pytest.raises(MarketDataUnavailableError, match="已过期"):
        session.place_order(OrderDirection.BUY, OrderType.LIMIT, 1, consumed.bids[0][0])
    assert clock.time() >= 1011
    assert submitted == []
    failure = owner.account_orders.activity.snapshot()["failures"][-1]
    assert failure["outcome"] == "blocked"
    assert "已过期" in failure["message"]
    assert events[-1]["details"]["quote_timestamp"] == consumed.timestamp
    assert get_order_submission_context() is None
    validate_order_submission()


def test_callback_in_other_thread_cannot_replace_consumed_quote(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(get_default_clock(), "time", lambda: now[0])
    owner = Owner(quote())
    session = ExecutionSession(owner, "600584.SH")
    session.get_market_data()
    session.register_price_callback(lambda q: session.place_order(OrderDirection.BUY, OrderType.LIMIT, 1, q.bids[0][0]))
    now[0] += 11
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(owner.callbacks[0], quote(now[0])).result(timeout=2)
    with pytest.raises(MarketDataUnavailableError, match="已过期"):
        session.place_order(OrderDirection.BUY, OrderType.LIMIT, 1, 9.9)
    assert len(owner.orders) == 1


@pytest.mark.parametrize(
    ("order_type", "changes", "reason"),
    [
        (OrderType.LIMIT, {"timestamp": 0}, "时间无效"),
        (OrderType.LIMIT, {"timestamp": 989000}, "已过期"),
        (OrderType.LIMIT, {"bids": [], "asks": []}, "缺少真实盘口"),
        (OrderType.MARKET, {"last_price": 0}, "参考价格无效"),
    ],
)
def test_direct_facade_rejects_unusable_quote(monkeypatch, order_type, changes, reason):
    monkeypatch.setattr(get_default_clock(), "time", lambda: 1000.0)
    owner = _TestExecutor()
    monkeypatch.setattr(owner, "get_market_data", lambda symbols: {symbols[0]: quote(symbol=symbols[0], **changes)})
    submitted = []
    monkeypatch.setattr(owner, "_place_order_impl", lambda *args, **kwargs: submitted.append(args))
    with pytest.raises(MarketDataUnavailableError, match=reason):
        owner.place_order("rb2610", OrderDirection.BUY, order_type, 1, 10)
    assert submitted == []


def test_direct_facade_freezes_quote_and_preserves_audit_context(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(get_default_clock(), "time", lambda: now[0])
    owner = _TestExecutor()
    consumed = quote(symbol="rb2610")
    monkeypatch.setattr(owner, "get_market_data", lambda symbols: {symbols[0]: consumed})
    seen = []

    def send(*args, **kwargs):
        seen.append((get_order_submission_context(), kwargs))
        consumed.timestamp = 1_011_000
        now[0] += 11
        validate_order_submission()

    monkeypatch.setattr(owner, "_place_order_impl", send)
    with order_submission_context({"execution_id": "direct"}):
        with pytest.raises(MarketDataUnavailableError, match="已过期"):
            owner.place_order("rb2610", OrderDirection.BUY, OrderType.LIMIT, 1, 10)
    assert seen == [({"execution_id": "direct"}, {})]


def test_direct_market_order_accepts_reference_without_book(monkeypatch):
    monkeypatch.setattr(get_default_clock(), "time", lambda: 1000.0)
    owner = _TestExecutor()
    monkeypatch.setattr(
        owner, "get_market_data", lambda symbols: {symbols[0]: quote(symbol=symbols[0], bids=[], asks=[])}
    )
    order = owner.place_order("rb2610", OrderDirection.BUY, OrderType.MARKET, 1)
    assert order.symbol == "rb2610"


@pytest.mark.parametrize("order_type,purpose", [(OrderType.MARKET, "order"), (OrderType.LIMIT, "sizing")])
def test_session_cannot_bypass_required_order_book(monkeypatch, order_type, purpose):
    monkeypatch.setattr(get_default_clock(), "time", lambda: 1000.0)
    owner = Owner(quote(bids=[], asks=[]))
    session = ExecutionSession(owner, "600584.SH")
    with pytest.raises(MarketDataUnavailableError, match="缺少真实盘口"):
        session.place_order(OrderDirection.BUY, order_type, 1, market_data=owner.quote, market_data_purpose=purpose)
    assert owner.orders == []


def test_submission_context_restores_validation_after_nested_exception():
    calls = []
    assert get_order_submission_context() is None
    with order_submission_context({"symbol": "outer"}, validate=lambda: calls.append("outer")):
        with pytest.raises(RuntimeError, match="nested"):
            with order_submission_context({"symbol": "inner"}, validate=lambda: calls.append("inner")):
                assert get_order_submission_context() == {"symbol": "inner"}
                validate_order_submission()
                raise RuntimeError("nested")
        assert get_order_submission_context() == {"symbol": "outer"}
        validate_order_submission()
        with order_submission_context({}):
            validate_order_submission()
    assert get_order_submission_context() is None
    validate_order_submission()
    assert calls == ["inner", "outer"]
