"""公共报撤旁路覆盖控制拒绝，不把同步返回当成交确认。"""

from types import SimpleNamespace

import pytest

from axile.executor.account_control.decorators import controlled_operation
from axile.executor.order_submission_context import order_submission_context
from axile.executor.replay import current_command_id, set_replay_observer
from axile.executor.replay_actions import replay_operation


class _Executor:
    def __init__(self):
        self.calls = []
        self._account_control_guard = None

    @replay_operation("place_order")
    @controlled_operation("place_order", symbol_arg="symbol")
    def place_order(self, symbol, direction, volume, price=0):
        self.calls.append((symbol, direction, volume, price, current_command_id()))
        return SimpleNamespace(order_id="order-1")

    @replay_operation("cancel_order")
    @controlled_operation("cancel_order", symbol_arg="symbol")
    def cancel_order(self, symbol, order_id):
        self.calls.append((symbol, order_id, current_command_id()))
        return False


def test_control_rejection_is_observed_before_the_facade_body():
    executor = _Executor()
    rows = []
    set_replay_observer(executor, lambda topic, data: rows.append((topic, dict(data))))

    class Guard:
        def begin_operation(self, *_args, **_kwargs):
            raise PermissionError("private rejection text")

    executor._account_control_guard = Guard()
    with pytest.raises(PermissionError, match="private rejection text"):
        executor.place_order("TEST", "BUY", 3, 10)
    assert executor.calls == []
    assert [row[1]["phase"] for row in rows] == ["request", "error"]
    assert rows[-1][1]["error_type"] == "PermissionError"
    assert "private rejection text" not in repr(rows)
    assert current_command_id() is None


def test_result_preserves_execution_identity_and_does_not_confirm_a_resting_order():
    executor = _Executor()
    rows = []
    set_replay_observer(executor, lambda topic, data: rows.append(dict(data)))
    with order_submission_context({"execution_id": "exec-1"}):
        result = executor.place_order("TEST", "BUY", 3, 10)
    assert result.order_id == "order-1"
    assert executor.calls[0][-1] == rows[0]["command_id"]
    assert rows[-1]["phase"] == "result"
    assert rows[-1]["order_id"] == "order-1"
    assert rows[-1]["execution_id"] == "exec-1"
    assert rows[-1]["origin"] == "execution"
    assert "confirmed_book" not in rows[-1]
    assert current_command_id() is None


def test_disabled_or_broken_observer_does_not_change_order_submission():
    executor = _Executor()
    assert executor.place_order("TEST", "BUY", 3).order_id == "order-1"
    assert executor.calls[0][-1] is None

    def broken(*_args):
        raise RuntimeError("writer failed")

    set_replay_observer(executor, broken)
    assert executor.place_order("TEST", "BUY", 3).order_id == "order-1"
    assert len(executor.calls) == 2


def test_manual_cancel_failure_retains_origin_without_claiming_cancellation():
    executor = _Executor()
    executor._account_control_guard = SimpleNamespace(
        source="manual_cancel",
        context_kind="account_operation",
        execution_id=None,
        begin_operation=lambda *_args, **_kwargs: SimpleNamespace(record_outcome=lambda *_a, **_kw: None),
    )
    rows = []
    set_replay_observer(executor, lambda topic, data: rows.append(dict(data)))
    assert executor.cancel_order("TEST", "order-1") is False
    assert rows[-1]["origin"] == "manual"
    assert rows[-1]["success"] is False
    assert rows[-1]["order_id"] == "order-1"
    assert "execution_id" not in rows[-1]
    assert "confirmed_book" not in rows[-1]
