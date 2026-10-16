"""真实 session／guard 的离线终止清理：保留限流、身份隔离和未知结果。"""

from unittest.mock import MagicMock

import pytest

from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.guard import AccountControlGuard
from axile.executor.account_control.presets import resolve_account_control_policy
from axile.executor.account_control.registry import ensure_default_account_control_registry_bootstrapped
from axile.executor.account_control.snapshot import AccountControlCounterSnapshot
from axile.executor.execution_session import ExecutionSession
from axile.executor.models.unified_order import OrderDirection, OrderType, UnifiedOrder
from axile.executor.termination import ExecutionTerminated
from axile.executor.termination_cleanup import cleanup_query_allowed, termination_cleanup_scope


class Clock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def event_wait(self, event, timeout):
        self.sleep(timeout)
        return event.is_set()


class Owner:
    channel_type = TradeChannel.CTP
    logger = MagicMock()

    def __init__(self, clock, *, query_error=False, terminal=True):
        ensure_default_account_control_registry_bootstrapped()
        self.clock, self.query_error, self.terminal = clock, query_error, terminal
        self.queries, self.cancels = 0, []
        self.guard = AccountControlGuard(
            account_id=1,
            execution_id="exec",
            channel=TradeChannel.CTP,
            policy=resolve_account_control_policy("ctp"),
            baseline=AccountControlCounterSnapshot(),
            wait_clock=clock,
            termination_checkpoint=self.handle_termination_checkpoint,
        )

    def get_account_control_guard(self):
        return self.guard

    def is_termination_requested(self):
        return True

    def handle_termination_checkpoint(self, symbol=None):
        raise ExecutionTerminated(reason="stop", mode="cancel_pending", acked_at="ack")

    def _get_pending_orders_for_execution(self, symbol):
        assert cleanup_query_allowed(self)
        if self.query_error:
            raise TimeoutError("query failed")
        self.guard.begin_operation("query_orders", symbol=symbol)
        self.queries += 1
        return [
            UnifiedOrder(
                order_id="native-1",
                symbol=symbol,
                direction=OrderDirection.BUY,
                order_type=OrderType.LIMIT,
                volume=1,
                price=100,
                status="待成交",
            )
        ]

    def cancel_order(self, symbol, order_id):
        self.guard.begin_operation("cancel_order_ctp", symbol=symbol)
        self.cancels.append(order_id)
        return True

    def reconcile_terminal_order(self, symbol, order_id):
        return (
            UnifiedOrder(
                order_id=order_id,
                symbol=symbol,
                direction=OrderDirection.BUY,
                order_type=OrderType.LIMIT,
                volume=1,
                price=100,
                status="已撤销",
            )
            if self.terminal
            else None
        )


@pytest.mark.parametrize("terminal", [True, False])
def test_cleanup_can_wait_for_shared_quota_without_suppressing_unknown_terminal(monkeypatch, terminal):
    clock = Clock()
    monkeypatch.setattr("axile.executor.termination_cleanup.clock_monotonic", clock.time)
    owner = Owner(clock, terminal=terminal)
    owner.guard.begin_operation("query_orders", symbol="rb")
    session = ExecutionSession(owner, "rb")
    with pytest.raises(ExecutionTerminated) as caught:
        session.handle_termination_checkpoint()
    assert owner.queries == 1 and owner.cancels == ["native-1"]
    assert clock.now >= 3.0
    assert caught.value.acked_at == "ack"
    assert caught.value.cancel_attempted is True
    assert caught.value.cancel_unconfirmed is (not terminal)
    assert not cleanup_query_allowed(owner)


def test_query_failure_is_not_reported_as_no_pending_orders(monkeypatch):
    clock = Clock()
    monkeypatch.setattr("axile.executor.termination_cleanup.clock_monotonic", clock.time)
    owner = Owner(clock, query_error=True)
    with pytest.raises(ExecutionTerminated) as caught:
        ExecutionSession(owner, "rb").handle_termination_checkpoint()
    assert caught.value.cancel_unconfirmed is True
    assert caught.value.cancel_attempted is False
    assert not owner.cancels


def test_cleanup_scope_rejects_new_orders_other_symbols_and_expired_queries(monkeypatch):
    clock = Clock()
    monkeypatch.setattr("axile.executor.termination_cleanup.clock_monotonic", clock.time)
    owner = Owner(clock)
    with termination_cleanup_scope(owner, owner.guard, "rb", seconds=1):
        with pytest.raises(ExecutionTerminated):
            owner.guard.begin_operation("place_order", symbol="rb")
        with pytest.raises(ExecutionTerminated):
            owner.guard.begin_operation("cancel_order_ctp", symbol="ag")
        assert not cleanup_query_allowed(object())
        clock.sleep(1.1)
        with pytest.raises(TimeoutError):
            cleanup_query_allowed(owner)
    assert not cleanup_query_allowed(owner)


def test_parallel_termination_keeps_sibling_unknown_cleanup():
    from axile.executor.execution_engine import _merge_symbol_terminations

    first = ExecutionTerminated(reason="stop", mode="cancel_pending", cancel_attempted=False)
    sibling = ExecutionTerminated(reason="stop", mode="cancel_pending", cancel_attempted=True, cancel_unconfirmed=True)
    merged = _merge_symbol_terminations([first, sibling])
    assert merged.cancel_unconfirmed and merged.cancel_attempted
