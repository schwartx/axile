"""常驻 TQ 的真实终态、并发去重、跨执行归属及补写回归。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from axile.domain.execution import ExecutionEventType
from axile.executor.algorithms.utils.order_tracker import OrderTracker
from axile.executor.constants.order_status import OrderStatus
from axile.executor.models.unified_input import TQAccountConfig
from axile.executor.models.unified_order import OrderDirection, OrderType
from axile.executor.tq import TQExecutor
from axile.executor.tq.runtime import TQRuntime
from axile.executor.tq.tq_execute import TQTradingTimeCheck, TQTradingTimeStatus
from tests.unit.executor.tq.test_executor import FakeApi


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.failures: list[object] = []

    def append_event(self, **event) -> bool:
        if self.failures:
            failure = self.failures.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return False
        self.events.append(event)
        return True

    def append_artifact(self, **_artifact) -> bool:
        return True


@pytest.fixture
def audited_executor(monkeypatch):
    # 用真实 SDK owner 线程执行命令，回报时机由用例控制，避免依赖线程调度运气。
    monkeypatch.setattr(TQRuntime, "_pump_changes", lambda *_args: None)
    api = FakeApi()
    monkeypatch.setattr(TQExecutor, "_build_api", staticmethod(lambda _config: api))
    monkeypatch.setattr(
        TQExecutor,
        "_check_tq_symbol_trading_time",
        lambda *_args: TQTradingTimeCheck(TQTradingTimeStatus.OPEN),
    )
    instance = TQExecutor(TQAccountConfig(account_mode="kq", tq_username="test", tq_password="test"))
    sink = RecordingSink()
    bind_execution(instance, sink, "execution-A")
    try:
        yield instance, api, sink
    finally:
        instance.close()


def bind_execution(instance, sink, execution_id):
    instance.clear_execution_runtime()
    instance.set_audit_context({"execution_id": execution_id, "account_id": 2, "algorithm": "TARGET-POS-TASK"})
    instance.set_audit_sink(sink)
    runtime = instance.prepare_execution_runtime()
    runtime.reset_for_execute(execution_timeout=180)
    return runtime


def submit(instance):
    return instance._place_order_impl("rb2610", OrderDirection.BUY, OrderType.LIMIT, 2, 3200)


def finish(api, *, left=0, message=""):
    api.order.update(status="FINISHED", volume_left=left, trade_price=3200, last_msg=message)
    return dict(api.order)


@pytest.mark.parametrize(
    ("left", "message", "expected"),
    [(0, "", OrderStatus.FILLED), (1, "撤单", OrderStatus.CANCELED), (2, "拒绝", OrderStatus.REJECTED)],
)
def test_terminal_callback_emits_confirmed_state_and_volume(audited_executor, left, message, expected):
    instance, api, sink = audited_executor
    order = submit(instance)
    tracker = OrderTracker(executor=instance)
    tracker.add_order(order)
    instance.register_order_callback(tracker.on_order_update)
    instance._on_runtime_event("order", finish(api, left=left, message=message))

    assert len(sink.events) == 1
    event = sink.events[0]
    assert event["event_type"] == ExecutionEventType.ORDER_TERMINAL
    assert event["reason_code"] == "COMMON.ORDER_TERMINAL"
    assert event["execution_id"] == "execution-A"
    assert event["account_id"] == 2
    assert event["algorithm"] == "TARGET-POS-TASK"
    assert event["symbol"] == "rb2610"
    assert event["order_id"] == order.order_id
    assert event["details"]["order"]["terminal_status"] == expected
    assert event["details"]["order"]["filled_volume"] == 2 - left
    assert event["details"]["order"]["remaining_volume"] == left
    assert event["details"]["exchange"]["last_msg"] == message
    assert tracker.all_done_event.is_set()
    assert order.order_id in tracker.completed_orders
    assert not instance._terminal_audit._origins


@pytest.mark.parametrize("status,left", [("ALIVE", 2), ("ALIVE", 1), ("UNKNOWN", 0)])
def test_nonterminal_and_unknown_states_never_emit_terminal(audited_executor, status, left):
    instance, api, sink = audited_executor
    order = submit(instance)
    api.order.update(status=status, volume_left=left)
    instance._on_runtime_event("order", dict(api.order))
    assert instance.reconcile_terminal_order("rb2610", order.order_id) is None
    assert not sink.events


def test_callback_and_reconciliation_concurrently_emit_once(audited_executor):
    instance, api, sink = audited_executor
    order = submit(instance)
    row = finish(api)
    barrier = threading.Barrier(8)

    def report(index):
        barrier.wait(timeout=5)
        if index % 2:
            instance.reconcile_terminal_order("rb2610", order.order_id)
        else:
            instance._on_runtime_event("order", row)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(report, range(8)))
    instance._get_pending_orders_impl()
    assert len(sink.events) == 1
    assert not instance._terminal_audit._origins


def test_late_callback_keeps_original_execution_sink_and_sequence(audited_executor):
    instance, api, original_sink = audited_executor
    old_runtime = instance.require_execution_runtime()
    old_runtime.next_audit_seq()
    old_order = submit(instance)
    old_row = finish(api)

    # 同一个 runtime 绑定先被改写，再切到下一轮，原订单仍不能跟随当前归属。
    instance.set_audit_context({"execution_id": "wrong", "account_id": 99, "algorithm": "wrong"})
    next_sink = RecordingSink()
    bind_execution(instance, next_sink, "execution-B")
    new_order = submit(instance)
    instance._on_runtime_event("order", old_row)
    instance._on_runtime_event("order", finish(api))
    instance._on_runtime_event("order", old_row)

    assert len(original_sink.events) == len(next_sink.events) == 1
    assert original_sink.events[0]["execution_id"] == "execution-A"
    assert original_sink.events[0]["account_id"] == 2
    assert original_sink.events[0]["algorithm"] == "TARGET-POS-TASK"
    assert original_sink.events[0]["order_id"] == old_order.order_id
    assert original_sink.events[0]["seq"] == 2
    assert next_sink.events[0]["execution_id"] == "execution-B"
    assert next_sink.events[0]["order_id"] == new_order.order_id
    assert next_sink.events[0]["seq"] == 1


def test_late_reconciliation_after_runtime_reset_keeps_original_execution(audited_executor):
    instance, api, original_sink = audited_executor
    order = submit(instance)
    finish(api)
    next_sink = RecordingSink()
    runtime = instance.require_execution_runtime()
    instance.set_audit_context({"execution_id": "execution-B", "account_id": 2, "algorithm": "OTHER"})
    instance.set_audit_sink(next_sink)
    runtime.reset_for_execute(execution_timeout=180)
    assert instance.reconcile_terminal_order("rb2610", order.order_id).is_completed()
    assert len(original_sink.events) == 1
    assert original_sink.events[0]["execution_id"] == "execution-A"
    assert not next_sink.events


@pytest.mark.parametrize("early_callback", [False, True])
def test_terminal_during_insert_is_not_lost(audited_executor, monkeypatch, early_callback):
    instance, api, sink = audited_executor
    insert = api.insert_order

    def immediate_fill(**kwargs):
        insert(**kwargs)
        row = finish(api)
        if early_callback:
            instance._on_runtime_event("order", row)
        return row

    monkeypatch.setattr(api, "insert_order", immediate_fill)
    order = submit(instance)
    instance._on_runtime_event("order", dict(api.order))
    assert order.is_completed()
    assert len(sink.events) == 1
    assert sink.events[0]["order_id"] == order.order_id


def test_missing_callback_reconciles_tracker_and_backfills_once(audited_executor):
    instance, api, sink = audited_executor
    order = submit(instance)
    # 使用 tracker 的 missing-pending 补偿入口，不靠回报通知。
    proxy = SimpleNamespace(logger=instance.logger, reconcile_terminal_order=instance.reconcile_terminal_order)
    tracker = OrderTracker(executor=proxy)
    tracker.add_order(order)
    instance.register_order_callback(tracker.on_order_update)
    finish(api)
    tracker._reconcile_missing_pending([])
    assert tracker.get_pending_count() == 0
    assert tracker.all_done_event.is_set()
    assert len(sink.events) == 1
    instance._on_runtime_event("order", dict(api.order))
    assert len(sink.events) == 1


def test_open_order_snapshot_backfills_missing_terminal_callback(audited_executor):
    instance, api, sink = audited_executor
    submit(instance)
    finish(api)
    assert instance._get_pending_orders_impl() == []
    assert instance._get_pending_orders_impl() == []
    assert len(sink.events) == 1


@pytest.mark.parametrize("failure", [False, RuntimeError("sink unavailable")])
def test_failed_audit_write_preserves_reconciliation_retry(audited_executor, failure):
    instance, api, sink = audited_executor
    order = submit(instance)
    tracker = OrderTracker(executor=instance)
    tracker.add_order(order)
    instance.register_order_callback(tracker.on_order_update)
    sink.failures.append(failure)
    instance._on_runtime_event("order", finish(api))
    assert tracker.all_done_event.is_set()
    assert not sink.events
    assert instance.reconcile_terminal_order("rb2610", order.order_id).is_completed()
    assert len(sink.events) == 1
    instance.reconcile_terminal_order("rb2610", order.order_id)
    assert len(sink.events) == 1


def test_unowned_historical_order_is_not_assigned_to_current_execution(audited_executor):
    instance, api, sink = audited_executor
    row = finish(api)
    instance._on_runtime_event("order", row)
    assert instance.reconcile_terminal_order("rb2610", "o1").is_completed()
    instance._get_pending_orders_impl()
    assert not sink.events


def test_order_without_audit_context_is_not_adopted_by_later_execution(audited_executor):
    instance, api, sink = audited_executor
    instance.clear_execution_runtime()
    instance.set_audit_context({})
    order = submit(instance)
    bind_execution(instance, sink, "execution-B")
    instance._on_runtime_event("order", finish(api))
    assert instance.reconcile_terminal_order("rb2610", order.order_id).is_completed()
    assert not sink.events


def test_uncertain_submit_failure_retains_origin_for_late_terminal(audited_executor, monkeypatch):
    instance, api, sink = audited_executor
    insert = api.insert_order

    def accepted_but_unconfirmed(**kwargs):
        insert(**kwargs)
        raise TimeoutError("submission result unknown")

    monkeypatch.setattr(api, "insert_order", accepted_but_unconfirmed)
    with pytest.raises(RuntimeError, match="submission result unknown"):
        submit(instance)
    next_sink = RecordingSink()
    bind_execution(instance, next_sink, "execution-B")
    instance._on_runtime_event("order", finish(api))
    assert len(sink.events) == 1
    assert sink.events[0]["execution_id"] == "execution-A"
    assert not next_sink.events


def test_session_block_discards_registration_without_submitting(audited_executor, monkeypatch):
    instance, api, sink = audited_executor
    monkeypatch.setattr(
        instance,
        "_check_tq_symbol_trading_time",
        lambda *_args: TQTradingTimeCheck(TQTradingTimeStatus.CLOSED),
    )
    from axile.executor.account_control.exceptions import AccountControlBlockedError

    with pytest.raises(AccountControlBlockedError):
        submit(instance)
    assert not api.insert_args
    assert not instance._terminal_audit._origins
    assert not sink.events


def test_reset_snapshot_backfills_terminal_without_callback_once(audited_executor):
    instance, api, sink = audited_executor
    order = submit(instance)
    row = finish(api)

    instance._on_runtime_event("orders.reset", {"rows": [row]})
    instance._on_runtime_event("orders.reset", {"rows": [row]})
    instance._on_runtime_event("order", row)
    instance.reconcile_terminal_order("rb2610", order.order_id)

    assert len(sink.events) == 1
    assert sink.events[0]["order_id"] == order.order_id
    assert sink.events[0]["details"]["order"]["terminal_status"] == OrderStatus.FILLED
    snapshot = instance.account_orders.snapshot()
    assert snapshot["complete"]
    assert not snapshot["data"]
    assert snapshot["resolved"][0].order_id == order.order_id
    assert not instance._terminal_audit._origins


@pytest.mark.parametrize("failure", [False, RuntimeError("sink unavailable")])
def test_reset_snapshot_retries_failed_terminal_write(audited_executor, failure):
    instance, api, sink = audited_executor
    submit(instance)
    row = finish(api)
    sink.failures.append(failure)
    instance._on_runtime_event("order", row)
    assert not sink.events

    instance._on_runtime_event("orders.reset", {"rows": [row]})
    instance._on_runtime_event("orders.reset", {"rows": [row]})

    assert len(sink.events) == 1
    assert not instance._terminal_audit._origins


def test_reset_snapshot_failure_keeps_order_book_and_can_retry(audited_executor):
    instance, api, sink = audited_executor
    order = submit(instance)
    row = finish(api)
    sink.failures.append(RuntimeError("sink unavailable"))

    instance._on_runtime_event("orders.reset", {"rows": [row]})

    assert not sink.events
    snapshot = instance.account_orders.snapshot()
    assert snapshot["complete"]
    assert snapshot["resolved"][0].order_id == order.order_id
    assert order.order_id in instance._terminal_audit._origins

    instance._on_runtime_event("orders.reset", {"rows": [row]})
    assert len(sink.events) == 1
    assert not instance._terminal_audit._origins


def test_reset_snapshot_after_next_execution_keeps_original_sink(audited_executor):
    instance, api, original_sink = audited_executor
    order = submit(instance)
    row = finish(api)
    next_sink = RecordingSink()
    bind_execution(instance, next_sink, "execution-B")

    instance._on_runtime_event("orders.reset", {"rows": [row]})

    assert len(original_sink.events) == 1
    assert original_sink.events[0]["execution_id"] == "execution-A"
    assert original_sink.events[0]["order_id"] == order.order_id
    assert not next_sink.events


def test_reset_snapshot_does_not_adopt_history_or_infer_missing_terminal(audited_executor):
    instance, api, sink = audited_executor
    instance._on_runtime_event("orders.reset", {"rows": [finish(api)]})
    api.order.update(status="ALIVE", volume_left=2)
    order = submit(instance)
    instance._on_runtime_event("orders.reset", {"rows": [dict(api.order)]})
    instance._on_runtime_event("orders.reset", {"rows": []})

    assert not sink.events
    assert order.order_id in instance._terminal_audit._origins


def test_reset_snapshot_and_callback_concurrently_emit_once(audited_executor):
    instance, api, sink = audited_executor
    submit(instance)
    row = finish(api)
    barrier = threading.Barrier(8)

    def report(index):
        barrier.wait(timeout=5)
        if index % 2:
            instance._on_runtime_event("orders.reset", {"rows": [row]})
        else:
            instance._on_runtime_event("order", row)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(report, range(8)))

    assert len(sink.events) == 1
    assert not instance._terminal_audit._origins
