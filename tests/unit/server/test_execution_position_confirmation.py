"""持仓确认结果使用真实原生存储跨执行恢复，保留账户隔离。"""

import asyncio
import json
from threading import Event
from unittest.mock import Mock

import pytest

from axile.common.serialization import copy_data, to_payload
from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionEventStatus, ExecutionEventType, ExecutionKind, ExecutionReasonFamily
from axile.executor.algorithms.core.base import AlgorithmInput
from axile.executor.algorithms.defaults.ctp_target_pos_task.impl import _place_volume_slices
from axile.executor.algorithms.defaults.single_maker import impl as single_maker
from axile.executor.algorithms.defaults.twap import impl as twap
from axile.executor.algorithms.utils import order_helper
from axile.executor.algorithms.utils.final_position import read_final_position, save_position_checkpoint
from axile.executor.algorithms.utils.order_tracker import OrderTracker
from axile.executor.algorithms.utils.outcome import summarize_outcome
from axile.executor.constants.order_status import OrderStatus
from axile.executor.execution_runtime import ExecutionRuntime, ExecutionRuntimeBindings
from axile.executor.models.execution_result import AlgorithmResult
from axile.executor.models.unified_account_assets import PositionDirection
from axile.executor.models.unified_input_accounts import GMAccountConfig
from axile.executor.models.unified_order import OrderDirection, TradeRecord
from axile.executor.order_cancellation import CancellationResult, CancelRequestStatus, OrderCancellation
from axile.executor.order_terminal_audit import order_terminal_details
from axile.executor.termination import ExecutionTerminated, ExecutionTerminationController
from axile.server.execution.audit_sink import ServerExecutionAuditSink
from axile.server.execution.lifecycle import _record_terminated_execution, _resolve_termination_snapshot
from axile.server.execution.worker_backend import manager as worker_manager
from axile.server.execution.worker_backend.manager import WorkerBackendManager
from tests.conftest import native_store_call
from tests.fixtures.algorithm_fakes import ClockStub
from tests.unit.executor.algorithms.test_order_tracker_and_single_maker_regressions import _SingleMakerExecutor
from tests.unit.executor.algorithms.utils.test_order_tracker import _TimeoutCancelExecutor
from tests.unit.executor.test_base_executor_symbol_dispatch import _assets
from tests.unit.executor.test_execution_phase_settlement import BUY, SELL, assets, engine, inputs, result
from tests.unit.server._business_facts_test_support import read_result, seed_account
from tests.unit.server.test_execution_derived_views import persist
from tests.unit.server.test_gm_worker_manager import _execute_request, _FakeHandle


def test_position_evidence_roundtrip_and_restart_guard(native_database):
    seed_account(native_database)
    seed_account(native_database, 2)
    row = {
        "symbol": "A",
        "status": "SUCCEEDED",
        "start_volume": 0,
        "expected_volume": 100,
        "position_confirmed": False,
        "final_volume": None,
    }

    def save(identity, symbols, account_id=1):
        return persist(
            native_database,
            account_id=account_id,
            execution_id=identity,
            input_id=identity + "-input",
            result_input_id=identity + "-result-input",
            raw_result={"status": "SUCCEEDED", "success": True, "symbol_results": symbols},
        )

    def pending(account_id=1):
        return native_store_call(
            native_database, "execution_read", {"op": "pending_positions", "account_id": account_id}
        )

    saved = save("pending", {"A": row})
    restored = read_result(native_database, {"id": saved["record_id"]})
    assert restored["symbol_results"]["A"]["start_volume"] == 0
    assert restored["symbol_results"]["A"]["expected_volume"] == 100
    assert restored["symbol_results"]["A"]["position_confirmed"] is False
    assert pending() == {"A": 100}
    assert pending(2) == {}
    save("other-account", {"B": {**row, "symbol": "B", "expected_volume": 200}}, 2)
    save("unrelated-failure", {})
    assert pending() == {"A": 100}
    save("still-pending", {"A": {**row, "status": "BLOCKED"}})
    assert pending() == {"A": 100}
    save("confirmed", {"A": {**row, "position_confirmed": True, "final_volume": 100}})
    assert pending() == {}
    assert pending(2) == {"B": 200}


def attach_audit(runtime, audit, execution_id, account_id):
    runtime.audit_context = {"execution_id": execution_id, "account_id": account_id, "algorithm": "TEST"}
    sequence = iter(range(10, 100))

    def append(*, audit_context=None, **event):
        return audit.append_event(
            **(audit_context or runtime.audit_context), **event, channel=TradeChannel("gm"), seq=next(sequence)
        )

    runtime.emit_audit_event.side_effect = append


@pytest.mark.parametrize("at_timeout", [False, True])
def test_forced_worker_recovery_keeps_fill_before_first_position_check(native_database, monkeypatch, at_timeout):
    seed_account(native_database, 2)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    execution_id = "forced-position-check"
    native_store_call(
        native_database,
        "intent_insert",
        {
            "execution_id": execution_id,
            "account_id": 2,
            "kind": "rebalance",
            "trigger_source": "manual",
            "channel": "gm",
            "algorithm": "TEST",
            "payload": {},
            "created_at": "2026-11-20T09:32:00+08:00",
        },
    )
    before = assets(sell=900)
    old = engine([], channel="gm")
    audit = ServerExecutionAuditSink()
    attach_audit(old._runtime, audit, execution_id, account_id=2)
    algorithm_executor = _SingleMakerExecutor()
    algorithm_executor.has_audit_sink = True
    algorithm_executor.audit_context = old._runtime.audit_context
    algorithm_executor.emit_audit_event = old._runtime.emit_audit_event
    algorithm_executor.get_current_volume = lambda _: 900
    terminal = result().orders[0]
    clock = ClockStub()
    initial_order = terminal
    if at_timeout:
        initial_order = copy_data(terminal, update={"status": OrderStatus.PENDING, "filled_volume": 0})
        tracker = OrderTracker(executor=algorithm_executor, clock=clock)
        tracker.add_order(initial_order)
        monkeypatch.setattr(single_maker, "get_default_clock", lambda: clock)
    else:
        tracker = Mock()
        tracker.get_all_orders.return_value = [terminal]
    monkeypatch.setattr(single_maker, "setup_order_tracker", lambda *_: tracker)
    monkeypatch.setattr(single_maker, "teardown_order_tracker", lambda *_: None)
    monkeypatch.setattr(single_maker, "submit_and_track_split_orders", lambda *_args, **_kwargs: [initial_order])

    def dispatch(tasks):
        return [
            single_maker.single_maker_callback(
                algorithm_executor,
                AlgorithmInput(
                    symbol=task.symbol,
                    target_volume=task.algorithm_input.target_volume,
                    params=single_maker.SingleMakerParams(max_wait_seconds=1),
                    trade_rule={},
                ),
            )
            for task in tasks
        ]

    monkeypatch.setattr(old, "_run_prepared_symbol_algorithms", dispatch)
    pending_request = {"op": "pending_positions", "account_id": 2}
    orders_request = {"op": "pending_order_terminals", "account_id": 2}
    manager = WorkerBackendManager()
    handle = _FakeHandle(is_alive=True, poll_result=False)
    manager._workers[2] = handle
    controller = ExecutionTerminationController(
        cancel_event=Event(),
        reason_provider=lambda: "operator",
        mode_provider=lambda: "graceful",
    )
    command = _execute_request()
    command.execution_id = execution_id
    monkeypatch.setattr(worker_manager, "clock_monotonic", clock.time)
    monkeypatch.setattr(handle.connection, "poll", lambda delay: clock.sleep(delay) or False)
    reads = 0
    forced = None

    class WorkerStopped(BaseException):
        pass

    def force_stop():
        nonlocal forced
        controller.cancel_event.set()
        try:
            manager._request_blocking(2, command, timeout=30, termination_controller=controller)
        except ExecutionTerminated as exc:
            forced = exc
            raise WorkerStopped from exc
        raise AssertionError("manager did not stop the blocked worker")

    def read():
        nonlocal reads
        reads += 1
        if reads == 1:
            return before
        assert not at_timeout and reads == 2
        assert native_store_call(native_database, "execution_read", pending_request) == {SELL: 500}
        force_stop()

    def query_orders():
        assert reads == 1
        groups = native_store_call(native_database, "execution_read", orders_request)
        assert groups[0]["start_volume"] == 900
        assert groups[0]["pending"] == {SELL: None}
        tracker.on_order_update(terminal)
        old._runtime.emit_audit_event(
            event_type=ExecutionEventType.ORDER_TERMINAL,
            status=ExecutionEventStatus.SUCCESS,
            reason_family=ExecutionReasonFamily.EXCHANGE,
            reason_code="COMMON.ORDER_TERMINAL",
            order_id=SELL,
            symbol=SELL,
            details=order_terminal_details(terminal),
        )
        force_stop()

    if at_timeout:
        monkeypatch.setattr(algorithm_executor, "get_pending_orders", query_orders)
    monkeypatch.setattr(old._owner, "get_account_assets", lambda: before)
    monkeypatch.setattr(algorithm_executor, "get_account_assets", read)
    with pytest.raises(WorkerStopped):
        old.run(inputs(old))
    assert forced is not None and forced.forced and forced.raw_result is None
    old._runtime.emit_audit_artifact.assert_not_called()
    assert 2 not in manager._workers
    record = asyncio.run(
        _record_terminated_execution(
            account_id=2,
            execution_id=execution_id,
            execution_kind=ExecutionKind.REBALANCE,
            current_state=None,
            requested_at="2026-11-20T09:32:00",
            finished_at="2026-11-20T09:32:05",
            termination=_resolve_termination_snapshot(forced, None),
        )
    )
    assert record["status"] == "TERMINATED"

    replacement = engine([before], channel="gm")
    attach_audit(replacement._runtime, audit, "replacement", account_id=2)
    dispatch = Mock()
    monkeypatch.setattr(replacement, "_run_prepared_symbol_algorithms", dispatch)
    request = inputs(replacement)
    request.extra["pending_positions"] = native_store_call(native_database, "execution_read", pending_request)
    request.extra["pending_order_terminals"] = native_store_call(native_database, "execution_read", orders_request)
    assert replacement.run(request).status == "BLOCKED"
    dispatch.assert_not_called()
    pending = native_store_call(native_database, "execution_read", pending_request)
    assert pending == {SELL: 500}

    confirmed = engine([assets(sell=500), assets(sell=500)], channel="gm")
    request = inputs(confirmed, buy=False)
    request.extra["pending_positions"] = pending
    output = confirmed.run(request)
    assert output.symbol_results[SELL].position_confirmed is True
    persist(
        native_database,
        account_id=2,
        execution_id="confirmed-later",
        created_at="2026-11-21T09:35:00+08:00",
        raw_result=to_payload(output, mode="json"),
    )
    assert native_store_call(native_database, "execution_read", pending_request) == {}


def test_unresolved_order_survives_restart_and_terminal_is_written_to_origin(native_database, monkeypatch):
    seed_account(native_database)
    seed_account(native_database, 2)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    audit = ServerExecutionAuditSink()
    previous = {"execution_id": "old-order", "algorithm": "TEST", "symbol": SELL, "order_id": SELL}
    assert audit.append_event(
        **previous,
        account_id=1,
        channel=TradeChannel("gm"),
        seq=1,
        event_type=ExecutionEventType.ORDER_TERMINAL_PENDING,
        status=ExecutionEventStatus.WARNING,
        reason_family=ExecutionReasonFamily.ACCOUNT_STATE,
        reason_code="COMMON.ORDER.TERMINAL_UNCONFIRMED",
        details={"order": {"status": "终态待确认"}},
    )
    read_pending = {"op": "pending_order_terminals", "account_id": 1}
    pending = native_store_call(native_database, "execution_read", read_pending)
    assert pending == [
        {
            "execution_id": "old-order",
            "algorithm": "TEST",
            "symbol": SELL,
            "start_volume": None,
            "orders": None,
            "pending": {SELL: None},
        }
    ]
    assert native_store_call(native_database, "execution_read", {**read_pending, "account_id": 2}) == []

    replacement = engine([assets()] * 4, channel="gm")
    replacement._owner.reconcile_terminal_order = Mock(return_value=None)
    attach_audit(replacement._runtime, audit, "new", account_id=1)
    request = inputs(replacement, buy=False)
    request.extra["pending_order_terminals"] = pending
    assert replacement.run(request).status == "BLOCKED"
    replacement._owner.reconcile_terminal_order.assert_called_once_with(SELL, SELL)

    replacement._owner.reconcile_terminal_order.return_value = result(filled=0, status=OrderStatus.CANCELED).orders[0]
    append = replacement._runtime.emit_audit_event.side_effect
    replacement._runtime.emit_audit_event.side_effect = lambda **event: (
        False if event["event_type"] == ExecutionEventType.ORDER_TERMINAL else append(**event)
    )
    assert replacement.run(request).status == "BLOCKED"
    assert native_store_call(native_database, "execution_read", read_pending) == pending
    replacement._runtime.emit_audit_event.side_effect = append
    assert replacement.run(request).status == "NOOP"
    assert native_store_call(native_database, "execution_read", read_pending) == []


@pytest.mark.parametrize("terminal_in_db", [False, True])
@pytest.mark.parametrize("audit_pending_saved", [False, True])
def test_recovered_late_fill_blocks_until_account_position_catches_up(
    native_database, monkeypatch, terminal_in_db, audit_pending_saved
):
    seed_account(native_database)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    audit = ServerExecutionAuditSink()
    origin = {"execution_id": "late-fill", "account_id": 1, "channel": TradeChannel("gm"), "algorithm": "TEST"}
    known = copy_data(result(start=900, target=800, filled=100).orders[0], update={"order_id": "first"})
    late = result(start=800, target=400 if audit_pending_saved else 500, filled=0, status=OrderStatus.PENDING).orders[0]
    tracker = OrderTracker(executor=Mock())
    tracker.add_order(late)
    assert late.order_id in tracker.record_cancellation_result(CancellationResult())
    row = result(start=900, target=500)
    row.orders = [known, *tracker.get_all_orders()]
    row.position_confirmed = None
    row.expected_volume = None
    persist(
        native_database, execution_id="late-fill", raw_result={"symbol_results": {SELL: to_payload(row, mode="json")}}
    )

    terminal = copy_data(late, update={"filled_volume": 300, "status": OrderStatus.FILLED})
    if audit_pending_saved:
        # 完整结果含上一阶段 100 手。本阶段两个回报中较高的累计成交量为 300 手。
        observed = copy_data(
            late,
            update={
                "filled_volume": 200 if terminal_in_db else 300,
                "status": OrderStatus.CANCELED if terminal_in_db else OrderStatus.PARTIALLY_FILLED,
            },
        )
        terminal = copy_data(
            late, update={"filled_volume": 300 if terminal_in_db else 0, "status": OrderStatus.CANCELED}
        )
        assert audit.append_event(
            **origin,
            symbol=SELL,
            seq=0,
            event_type=ExecutionEventType.POSITION_CHECKPOINT,
            status=ExecutionEventStatus.INFO,
            reason_family=ExecutionReasonFamily.ACCOUNT_STATE,
            reason_code="COMMON.POSITION.UNCONFIRMED",
            details={"start_volume": 800, "orders": [to_payload(observed, mode="json")]},
        )
        assert audit.append_event(
            **origin,
            symbol=SELL,
            order_id=late.order_id,
            seq=1,
            event_type=ExecutionEventType.ORDER_TERMINAL_PENDING,
            status=ExecutionEventStatus.WARNING,
            reason_family=ExecutionReasonFamily.ACCOUNT_STATE,
            reason_code="COMMON.ORDER.TERMINAL_UNCONFIRMED",
        )

    if terminal_in_db:
        assert audit.append_event(
            **origin,
            symbol=SELL,
            order_id=late.order_id,
            seq=2,
            event_type=ExecutionEventType.ORDER_TERMINAL,
            status=ExecutionEventStatus.SUCCESS,
            reason_family=ExecutionReasonFamily.EXCHANGE,
            reason_code="COMMON.ORDER_TERMINAL",
            details=order_terminal_details(terminal),
        )

    pending_orders = {"op": "pending_order_terminals", "account_id": 1}
    pending_positions = {"op": "pending_positions", "account_id": 1}
    replacement = engine([assets(sell=900)], channel="gm")
    if not terminal_in_db:
        replacement._owner.reconcile_terminal_order.return_value = terminal
        replacement._owner.reconcile_terminal_order.side_effect = None
    attach_audit(replacement._runtime, audit, "replacement", account_id=1)
    dispatch = Mock()
    monkeypatch.setattr(replacement, "_run_prepared_symbol_algorithms", dispatch)
    request = inputs(replacement)
    request.extra["pending_order_terminals"] = native_store_call(native_database, "execution_read", pending_orders)
    assert request.extra["pending_order_terminals"]
    output = replacement.run(request)
    assert output.status == "BLOCKED"
    assert output.symbol_results[SELL].expected_volume == 500
    dispatch.assert_not_called()
    assert native_store_call(native_database, "execution_read", pending_orders) == []
    assert native_store_call(native_database, "execution_read", pending_positions) == {SELL: 500}

    caught_up = engine([assets(sell=500), assets(sell=500)], channel="gm")
    request = inputs(caught_up, buy=False)
    request.extra["pending_positions"] = native_store_call(native_database, "execution_read", pending_positions)
    caught_up_result = caught_up.run(request)
    assert caught_up_result.status == "NOOP"
    persist(
        native_database,
        execution_id="late-fill-confirmed",
        created_at="2026-11-21T10:00:00+08:00",
        raw_result=to_payload(caught_up_result, mode="json"),
    )
    assert native_store_call(native_database, "execution_read", pending_positions) == {}


def test_fill_and_zero_fill_rejection_preserve_checkpoint(native_database, monkeypatch):
    seed_account(native_database)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    before = assets(sell=900)
    executor = _SingleMakerExecutor()
    executor.has_audit_sink = True
    runtime = Mock()
    audit = ServerExecutionAuditSink()
    attach_audit(runtime, audit, "fill-and-rejection", account_id=1)
    executor.audit_context = runtime.audit_context
    executor.emit_audit_event = runtime.emit_audit_event
    executor.get_account_assets = lambda: before
    filled = result(start=900, target=500, filled=400).orders[0]
    rejected = copy_data(
        result(start=500, target=400, filled=0, status=OrderStatus.REJECTED).orders[0], update={"order_id": "rejected"}
    )
    orders = [filled, rejected]
    _, final, error = read_final_position(executor, lambda _: 900, start=900, orders=orders)
    outcome = summarize_outcome(900, final, 400, orders, [], position_error=error)
    assert outcome["status"] == "PARTIAL"
    assert outcome["error"] == "报单被拒绝"
    assert outcome["expected_volume"] == 500
    assert not outcome["position_confirmed"]
    prior = AlgorithmResult(**outcome, symbol=SELL, algorithm="TEST", target_volume=400, orders=orders)
    persist(
        native_database,
        execution_id="fill-and-rejection",
        raw_result={"symbol_results": {SELL: to_payload(prior, mode="json")}},
    )
    pending = native_store_call(native_database, "execution_read", {"op": "pending_positions", "account_id": 1})
    assert pending == {SELL: 500}
    replacement = engine([before], channel="gm")
    replacement._run_prepared_symbol_algorithms = Mock()
    request = inputs(replacement, buy=False)
    request.extra["pending_positions"] = pending
    assert replacement.run(request).status == "BLOCKED"
    replacement._run_prepared_symbol_algorithms.assert_not_called()


def test_reversal_reconciles_late_open_fill_ahead_of_intermediate_flat_checkpoint(native_database, monkeypatch):
    seed_account(native_database)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    execution_id = "reversal"
    native_store_call(
        native_database,
        "intent_insert",
        {
            "execution_id": execution_id,
            "account_id": 1,
            "kind": "rebalance",
            "trigger_source": "manual",
            "channel": "ctp",
            "algorithm": "TEST",
            "payload": {},
            "created_at": "2026-11-20T09:32:00+08:00",
        },
    )
    short = _assets(total_asset=280000, positions=[(SELL, 200, PositionDirection.SHORT)])
    previous = engine([assets(sell=900), assets(sell=0), short])
    audit = ServerExecutionAuditSink()
    attach_audit(previous._runtime, audit, execution_id, account_id=1)
    previous._runtime.emit_audit_artifact.side_effect = lambda **kwargs: audit.append_artifact(
        execution_id=execution_id, **kwargs
    )
    close = result(start=900, observed=0, target=0, filled=900)
    close.orders[0] = copy_data(close.orders[0], update={"order_id": "close"})
    opened = result(start=0, observed=-200, target=-400, filled=200, status=OrderStatus.PARTIALLY_FILLED)
    opened.orders[0].extra["terminal_confirmation_pending"] = True

    def dispatch(tasks):
        if not tasks:
            return []
        return [close if tasks[0].algorithm_input.target_volume == 0 else opened]

    previous._run_prepared_symbol_algorithms = Mock(side_effect=dispatch)
    request = copy_data(inputs(previous, buy=False), update={"target_mode": "quantity", "curr_target": {SELL: -400.0}})
    output = previous.run(request)
    assert [
        c.args[0][0].algorithm_input.target_volume for c in previous._run_prepared_symbol_algorithms.call_args_list
    ] == [0, -400]
    assert output.symbol_results[SELL].expected_volume is None
    persist(native_database, execution_id=execution_id, raw_result=to_payload(output, mode="json"))
    assert audit.append_event(
        execution_id=execution_id,
        account_id=1,
        channel=TradeChannel("ctp"),
        algorithm="TEST",
        event_type=ExecutionEventType.ORDER_TERMINAL,
        status=ExecutionEventStatus.SUCCESS,
        reason_family=ExecutionReasonFamily.EXCHANGE,
        reason_code="COMMON.ORDER_TERMINAL",
        symbol=SELL,
        order_id=SELL,
        seq=1,
        details=order_terminal_details(copy_data(opened.orders[0], update={"status": OrderStatus.CANCELED})),
    )
    pending_positions = {"op": "pending_positions", "account_id": 1}
    pending_orders = {"op": "pending_order_terminals", "account_id": 1}
    replacement = engine([short, short])
    attach_audit(replacement._runtime, audit, "already-short", account_id=1)
    request = copy_data(
        inputs(replacement, buy=False), update={"target_mode": "quantity", "curr_target": {SELL: -200.0}}
    )
    request.extra["pending_positions"] = native_store_call(native_database, "execution_read", pending_positions)
    assert request.extra["pending_positions"] == {SELL: 0}
    request.extra["pending_order_terminals"] = native_store_call(native_database, "execution_read", pending_orders)
    output = replacement.run(request)
    assert output.status == "NOOP"
    assert output.symbol_results[SELL].expected_volume == -200
    assert output.symbol_results[SELL].position_confirmed is True
    assert native_store_call(native_database, "execution_read", pending_orders) == []
    persist(
        native_database,
        execution_id="already-short",
        input_id="short-input",
        result_input_id="short-result-input",
        created_at="2026-11-21T09:35:00+08:00",
        raw_result=to_payload(output, mode="json"),
    )
    assert native_store_call(native_database, "execution_read", pending_positions) == {}


def append_terminal(audit, account_id, execution_id, order, *, seq):
    assert audit.append_event(
        execution_id=execution_id,
        account_id=account_id,
        channel=TradeChannel("gm"),
        algorithm="TEST",
        event_type=ExecutionEventType.ORDER_TERMINAL,
        status=ExecutionEventStatus.SUCCESS,
        reason_family=ExecutionReasonFamily.EXCHANGE,
        reason_code="COMMON.ORDER_TERMINAL",
        symbol=order.symbol,
        order_id=order.order_id,
        seq=seq,
        details=order_terminal_details(order),
    )


@pytest.mark.parametrize("full_checkpoint", [False, True])
def test_exchange_fill_after_zero_fill_cancel_reopens_position_check(native_database, monkeypatch, full_checkpoint):
    seed_account(native_database)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    audit = ServerExecutionAuditSink()
    source = Mock(symbol=SELL, has_audit_sink=True)
    attach_audit(source, audit, "old", 1)
    tracker = OrderTracker(source, start_volume=900)
    pending = result(filled=0, status=OrderStatus.PENDING).orders[0]
    tracker.add_order(pending)
    canceled = copy_data(pending, update={"status": OrderStatus.CANCELED})
    tracker.record_terminal_order(canceled)
    if full_checkpoint:
        assert save_position_checkpoint(source, 900, [canceled])
        prior = result(filled=0, status=OrderStatus.CANCELED, observed=900)
        persist(native_database, execution_id="old", raw_result={"symbol_results": {SELL: to_payload(prior)}})
    query = {"op": "pending_order_terminals", "account_id": 1}
    assert native_store_call(native_database, "execution_read", query) == []

    for seq, filled in [(80, 100), (82, 300)]:
        append_terminal(audit, 1, "old", copy_data(canceled, update={"filled_volume": filled}), seq=seq)
        replacement = engine([assets(sell=900)])
        attach_audit(replacement._runtime, audit, f"restart-{seq}", 1)
        request = inputs(replacement, buy=False)
        request.extra["pending_order_terminals"] = native_store_call(native_database, "execution_read", query)
        replacement._run_prepared_symbol_algorithms = Mock()
        if seq == 80:
            append_terminal(audit, 1, "old", copy_data(canceled, update={"filled_volume": 200}), seq=81)
        output = replacement.run(request)
        replacement._run_prepared_symbol_algorithms.assert_not_called()
        if seq == 80:
            assert output.status == "BLOCKED"
            assert (
                native_store_call(native_database, "execution_read", query)[0]["pending"][SELL]["filled_volume"] == 200
            )
            continue
        assert output.symbol_results[SELL].expected_volume == 900 - filled
        assert native_store_call(native_database, "execution_read", query) == []


@pytest.mark.parametrize("trade_first", [False, True])
def test_trade_fill_survives_zero_fill_order_snapshot(native_database, monkeypatch, trade_first):
    seed_account(native_database)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    source = Mock(symbol=SELL, has_audit_sink=True)
    attach_audit(source, ServerExecutionAuditSink(), "trade", 1)
    tracker = OrderTracker(source, start_volume=900)
    order = result(filled=0, status=OrderStatus.PENDING).orders[0]
    tracker.add_order(order)
    trade = TradeRecord.create(trade_id="fill", order_id=order.order_id, symbol=SELL, trade_volume=40)
    if trade_first:
        tracker.on_trade_record(trade)
    tracker.record_terminal_order(copy_data(order, update={"status": OrderStatus.CANCELED}))
    tracker.on_trade_record(trade)
    tracker.require_saved_orders()
    groups = native_store_call(native_database, "execution_read", {"op": "pending_order_terminals", "account_id": 1})
    assert groups[0]["orders"][0]["filled_volume"] == 40
    orders = tracker.get_all_orders()
    assert orders[0].filled_volume == 0
    outcome = summarize_outcome(900, 900, 500, orders, tracker.get_all_trades())
    assert outcome["expected_volume"] == 860
    assert outcome["position_confirmed"] is False
    assert save_position_checkpoint(source, 900, tracker.get_all_orders(for_position=True))
    assert native_store_call(native_database, "execution_read", {"op": "pending_positions", "account_id": 1}) == {
        SELL: 860
    }


@pytest.mark.parametrize(("start", "fills"), [(900, [900]), (0.3, [0.1, 0.2])])
def test_reversal_restores_close_trade_before_late_open_fill(native_database, monkeypatch, start, fills):
    seed_account(native_database)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    audit = ServerExecutionAuditSink()
    source = Mock(symbol=SELL, has_audit_sink=True)
    attach_audit(source, audit, "reverse", 1)
    close = result(start=start, target=0, filled=0, status=OrderStatus.CANCELED).orders[0]
    opening = copy_data(
        result(start=0, target=-400, filled=0, status=OrderStatus.CANCELED).orders[0], update={"order_id": "open"}
    )
    trades = [
        TradeRecord.create(trade_id=str(i), order_id=close.order_id, symbol=SELL, trade_volume=amount)
        for i, amount in enumerate(fills)
    ]
    assert save_position_checkpoint(source, 0, [opening])
    orders = [close, opening]
    record = AlgorithmResult(
        **summarize_outcome(start, 0, -400, orders, trades),
        orders=orders,
        trades=trades,
        symbol=SELL,
        target_volume=-400,
    )
    persist(native_database, execution_id="reverse", raw_result={"symbol_results": {SELL: to_payload(record)}})
    append_terminal(audit, 1, "reverse", copy_data(opening, update={"filled_volume": 200}), seq=80)
    short = _assets(total_asset=280000, positions=[(SELL, 200, PositionDirection.SHORT)])
    replacement = engine([short, short])
    attach_audit(replacement._runtime, audit, "restart", 1)
    request = copy_data(
        inputs(replacement, buy=False), update={"target_mode": "quantity", "curr_target": {SELL: -200.0}}
    )
    request.extra["pending_order_terminals"] = native_store_call(
        native_database, "execution_read", {"op": "pending_order_terminals", "account_id": 1}
    )
    output = replacement.run(request)
    assert output.status == "NOOP"
    assert output.symbol_results[SELL].expected_volume == -200


def finish_terminated(account_id, execution_id, error):
    return asyncio.run(
        _record_terminated_execution(
            account_id=account_id,
            execution_id=execution_id,
            execution_kind=ExecutionKind.REBALANCE,
            current_state=None,
            requested_at="2026-11-20T09:32:00",
            finished_at="2026-11-20T09:32:05",
            termination=_resolve_termination_snapshot(error, None),
        )
    )


def test_operator_stop_before_timeout_recovers_late_fill(native_database, monkeypatch):
    seed_account(native_database)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    audit = ServerExecutionAuditSink()
    execution_id = "stop-before-timeout"
    cancel = Event()
    controller = ExecutionTerminationController(
        cancel_event=cancel, reason_provider=lambda: "operator", mode_provider=lambda: "graceful"
    )
    terminal = result().orders[0]
    pending = copy_data(terminal, update={"status": OrderStatus.PENDING, "filled_volume": 0})
    owner = engine([], channel="gm")._owner
    runtime = ExecutionRuntime(
        owner=owner,
        bindings=ExecutionRuntimeBindings(
            audit_context={"execution_id": execution_id, "account_id": 1, "algorithm": "TEST"},
            audit_sink=audit,
            termination_controller=controller,
        ),
    )
    executor = _SingleMakerExecutor()
    executor.has_audit_sink = True
    executor.audit_context = runtime.audit_context
    executor.emit_audit_event = runtime.emit_audit_event
    executor.handle_termination_checkpoint = runtime.handle_termination_checkpoint
    clock = ClockStub()
    tracker = OrderTracker(executor=executor, clock=clock, start_volume=900)
    tracker.add_order(pending)
    orders_request = {"op": "pending_order_terminals", "account_id": 1}
    groups = native_store_call(native_database, "execution_read", orders_request)
    assert groups[0]["start_volume"] == 900 and groups[0]["pending"] == {SELL: None}
    cancel.set()
    with pytest.raises(ExecutionTerminated) as stopped:
        tracker.wait_for_completion(timeout=60)
    assert not stopped.value.forced
    finish_terminated(1, execution_id, stopped.value)
    append_terminal(audit, 1, execution_id, terminal, seq=99)

    replacement = engine([assets(sell=900)], channel="gm")
    attach_audit(replacement._runtime, audit, "replacement", account_id=1)
    replacement._run_prepared_symbol_algorithms = Mock()
    request = inputs(replacement, buy=False)
    request.extra["pending_order_terminals"] = native_store_call(native_database, "execution_read", orders_request)
    output = replacement.run(request)
    assert output.status == "BLOCKED"
    assert output.symbol_results[SELL].expected_volume == 500
    replacement._run_prepared_symbol_algorithms.assert_not_called()


@pytest.mark.parametrize(
    ("filled", "other_pending", "recorded"), [(0, False, True), (400, False, True), (0, True, True), (0, True, False)]
)
def test_clear_empty_snapshot_cancels_and_reconciles_old_buy(
    native_database, monkeypatch, filled, other_pending, recorded
):
    seed_account(native_database)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    audit = ServerExecutionAuditSink()
    order = result(
        start=0, observed=0, target=400, filled=0, direction=OrderDirection.BUY, status=OrderStatus.PENDING
    ).orders[0]
    old = engine([], channel="gm")
    attach_audit(old._runtime, audit, "old-buy", 1)
    source = _SingleMakerExecutor()
    source.has_audit_sink = True
    source.audit_context = old._runtime.audit_context
    source.emit_audit_event = old._runtime.emit_audit_event
    assert save_position_checkpoint(source, 0, [order])
    groups = native_store_call(native_database, "execution_read", {"op": "pending_order_terminals", "account_id": 1})
    terminal = copy_data(
        order,
        update={
            "status": OrderStatus.FILLED if filled else OrderStatus.CANCELED,
            "filled_volume": filled,
        },
    )
    owner = engine([_assets()], channel="ctp")._owner
    owner.set_audit_context({"execution_id": "clear", "account_id": 1, "algorithm": "TEST"})
    owner.set_audit_sink(audit)
    monkeypatch.setattr(owner, "_get_pending_orders_impl", Mock(return_value=[order]))
    monkeypatch.setattr(owner, "cancel_order", Mock(return_value=True))
    owner.reconcile_terminal_order.return_value = terminal
    owner.reconcile_terminal_order.side_effect = None

    output = owner.empty_positions(
        extra={
            "pending_order_terminals": groups if recorded else [],
            "pending_positions": {BUY: 100} if other_pending else {},
        }
    )
    owner.cancel_order.assert_called_once_with(order.symbol, order.order_id)
    owner.reconcile_terminal_order.assert_any_call(order.symbol, order.order_id)
    if filled:
        assert output.status == "BLOCKED"
        assert output.symbol_results[order.symbol].expected_volume == filled
    elif other_pending:
        assert output.status == "BLOCKED"
        assert output.symbol_results[BUY].expected_volume == 100
    else:
        assert output.status == "NOOP"


@pytest.mark.parametrize(
    "extra, expected_reason",
    [
        ({"pending_order_terminals": [{"symbol": SELL}]}, "COMMON.ORDER.TERMINAL_UNCONFIRMED"),
        ({"pending_positions": {SELL: 400}}, "COMMON.POSITION.UNCONFIRMED"),
    ],
)
def test_clear_after_restart_while_closed_reports_persisted_blocker(monkeypatch, extra, expected_reason):
    owner = engine([_assets()], channel="gm")._owner
    owner.account_config = GMAccountConfig(
        connection_mode="service", account_id="test-account", token="test-token", serv_addr="localhost:7001"
    )
    monkeypatch.setattr(owner, "_check_trading_time", lambda: False)
    cancel = Mock()
    monkeypatch.setattr(owner, "cancel_all_orders", cancel)

    output = owner.empty_positions(extra=extra)
    assert output.status == "BLOCKED"
    assert not output.success
    assert output.reason_code == expected_reason
    cancel.assert_not_called()


@pytest.mark.parametrize("from_cancel_query", [False, True])
def test_twap_preserves_zero_fill_terminal_before_next_slice(native_database, monkeypatch, from_cancel_query):
    seed_account(native_database)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    audit = ServerExecutionAuditSink()
    execution_id = "twap-interrupted-between-slices"
    old = engine([], channel="ctp")
    attach_audit(old._runtime, audit, execution_id, 1)
    executor = _SingleMakerExecutor()
    executor.has_audit_sink = True
    executor.audit_context = old._runtime.audit_context
    executor.emit_audit_event = old._runtime.emit_audit_event
    stopped = ExecutionTerminated(reason="operator", mode="graceful")
    executor.get_account_assets = Mock(side_effect=[_assets(), _assets(), stopped])
    executor.sleep_or_terminate = Mock()
    clock = ClockStub()
    tracker = OrderTracker(executor=executor, clock=clock)
    order = result(
        start=0, observed=0, target=200, direction=OrderDirection.BUY, filled=0, status=OrderStatus.PENDING
    ).orders[0]
    terminal = copy_data(order, update={"status": OrderStatus.CANCELED})
    cancellation = CancellationResult()
    if from_cancel_query:
        cancellation.orders[order.symbol, order.order_id] = OrderCancellation(
            CancelRequestStatus.SUBMITTED, "已撤销", terminal_order=terminal
        )

    def submit(*_args, **_kwargs):
        tracker.add_order(order)
        if not from_cancel_query:
            tracker.on_order_update(terminal)
        return [order]

    monkeypatch.setattr(twap, "get_default_clock", lambda: clock)
    monkeypatch.setattr(twap, "setup_order_tracker", lambda *_: tracker)
    monkeypatch.setattr(twap, "teardown_order_tracker", Mock())
    monkeypatch.setattr(order_helper, "get_default_clock", lambda: clock)
    monkeypatch.setattr(order_helper, "submit_and_track_split_orders", submit)
    monkeypatch.setattr(order_helper, "cancel_pending_orders_via_query", lambda _, **_kwargs: cancellation)
    if from_cancel_query:
        # 第一片等单结束后才从撤单查询取到终态。
        monkeypatch.setattr(tracker, "_wait_callback", lambda _: False)

    with pytest.raises(ExecutionTerminated):
        twap.twap(
            executor,
            AlgorithmInput(symbol=order.symbol, target_volume=400, trade_rule={}, params=twap.TwapParams(slices=2)),
        )
    assert executor.get_account_assets.call_count == 3
    finish_terminated(1, execution_id, stopped)

    assert (
        native_store_call(native_database, "execution_read", {"op": "pending_order_terminals", "account_id": 1}) == []
    )
    assert native_store_call(native_database, "execution_read", {"op": "pending_positions", "account_id": 1}) == {}


def test_zero_fill_terminal_is_saved_before_interrupted_position_query(native_database, monkeypatch):
    seed_account(native_database)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    execution_id = "old-cancel"
    audit = ServerExecutionAuditSink()
    old = engine([], channel="ctp")
    attach_audit(old._runtime, audit, execution_id, 1)
    executor = _SingleMakerExecutor()
    executor.has_audit_sink = True
    executor.audit_context = old._runtime.audit_context
    executor.emit_audit_event = old._runtime.emit_audit_event
    order = result(filled=0, status=OrderStatus.PENDING).orders[0]
    tracker = OrderTracker(executor=executor, clock=ClockStub(), start_volume=900)
    tracker.add_order(order)
    tracker.on_order_update(copy_data(order, update={"status": OrderStatus.CANCELED}))

    monkeypatch.setattr(
        executor, "get_account_assets", Mock(side_effect=ExecutionTerminated(reason="operator", mode="graceful"))
    )
    with pytest.raises(ExecutionTerminated) as stopped:
        read_final_position(executor, lambda _: 900, start=900, orders=tracker.get_all_orders())
    finish_terminated(1, execution_id, stopped.value)

    assert (
        native_store_call(native_database, "execution_read", {"op": "pending_order_terminals", "account_id": 1}) == []
    )
    assert native_store_call(native_database, "execution_read", {"op": "pending_positions", "account_id": 1}) == {}


def test_cancellation_preserves_first_terminal_when_second_query_is_interrupted(native_database, monkeypatch):
    seed_account(native_database)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    execution_id = "interrupted-cancellation"
    audit = ServerExecutionAuditSink()
    old = engine([], channel="ctp")
    attach_audit(old._runtime, audit, execution_id, 1)
    executor = _SingleMakerExecutor()
    executor.has_audit_sink = True
    executor.audit_context = old._runtime.audit_context
    executor.emit_audit_event = old._runtime.emit_audit_event
    first = copy_data(result(filled=0, status=OrderStatus.PENDING).orders[0], update={"order_id": "first"})
    second = copy_data(first, update={"order_id": "second"})
    terminal = copy_data(first, update={"status": OrderStatus.CANCELED, "filled_volume": 100})
    tracker = OrderTracker(executor, clock=ClockStub(), start_volume=900)
    tracker.add_order(first)
    tracker.add_order(second)
    executor.get_pending_orders = Mock(return_value=[first, second])
    stopped = ExecutionTerminated(reason="operator", mode="graceful")
    request = {"op": "pending_order_terminals", "account_id": 1}

    def reconcile(_symbol, order_id):
        if order_id == first.order_id:
            return terminal
        group = native_store_call(native_database, "execution_read", request)[0]
        assert group["pending"]["first"]["filled_volume"] == 100
        raise stopped

    executor.reconcile_terminal_order = reconcile
    with pytest.raises(ExecutionTerminated):
        tracker.wait_for_completion(timeout=0)
    finish_terminated(1, execution_id, stopped)
    append_terminal(audit, 1, execution_id, copy_data(second, update={"status": OrderStatus.CANCELED}), seq=99)
    replacement = engine([assets(sell=900)], channel="ctp")
    attach_audit(replacement._runtime, audit, "next", 1)
    next_input = inputs(replacement, buy=False)
    next_input.extra["pending_order_terminals"] = native_store_call(native_database, "execution_read", request)
    output = replacement.run(next_input)
    assert output.status == "BLOCKED"
    assert output.symbol_results[SELL].expected_volume == 800
    replacement._owner.reconcile_terminal_order.assert_not_called()


def test_slice_checkpoints_store_only_changed_orders(native_database, monkeypatch):
    seed_account(native_database)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    execution_id = "split-order-checkpoints"
    source = Mock()
    source.has_audit_sink = True
    source.symbol = SELL
    attach_audit(source, ServerExecutionAuditSink(), execution_id, 1)
    tracker = OrderTracker(source, clock=ClockStub(), start_volume=900)
    order = result(filled=0, status=OrderStatus.PENDING).orders[0]
    for index in range(12):
        pending = copy_data(order, update={"order_id": f"slice-{index}"})
        tracker.add_order(pending)
        tracker.record_terminal_order(copy_data(pending, update={"status": OrderStatus.CANCELED}))
        assert tracker.wait_for_completion()

    page = json.loads(native_store_call(native_database, "list_execution_events_json", {"execution_id": execution_id}))
    events = page["data"]
    assert len(events) == 24
    assert sum("start_volume" in row["details"] for row in events) == 1
    assert all([order["order_id"] for order in row["details"]["orders"]] == [row["order_id"]] for row in events)
    assert (
        native_store_call(native_database, "execution_read", {"op": "pending_order_terminals", "account_id": 1}) == []
    )


@pytest.mark.parametrize("compact", [False, True])
def test_reversal_without_result_uses_latest_phase_orders(native_database, monkeypatch, compact):
    seed_account(native_database)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    audit = ServerExecutionAuditSink()
    execution_id = "reverse-killed-open-leg"
    source = Mock()
    source.has_audit_sink = True
    source.symbol = SELL
    attach_audit(source, audit, execution_id, account_id=1)

    close = copy_data(result(start=900, target=0, filled=900).orders[0], update={"order_id": "close"})
    closing = copy_data(close, update={"status": OrderStatus.PENDING, "filled_volume": 0})
    if compact:
        tracker = OrderTracker(source, clock=ClockStub(), start_volume=900)
        tracker.add_order(closing)
    else:
        assert save_position_checkpoint(source, 900, [closing])
    source.emit_audit_event(
        event_type=ExecutionEventType.ORDER_TERMINAL_PENDING,
        status=ExecutionEventStatus.WARNING,
        reason_family=ExecutionReasonFamily.ACCOUNT_STATE,
        reason_code="COMMON.ORDER.TERMINAL_UNCONFIRMED",
        symbol=SELL,
        order_id=close.order_id,
    )
    outside_tracker = copy_data(
        close, update={"order_id": "outside-tracker", "status": OrderStatus.CANCELED, "filled_volume": 0}
    )
    source.emit_audit_event(
        event_type=ExecutionEventType.ORDER_TERMINAL_PENDING,
        status=ExecutionEventStatus.WARNING,
        reason_family=ExecutionReasonFamily.ACCOUNT_STATE,
        reason_code="COMMON.ORDER.TERMINAL_UNCONFIRMED",
        symbol=SELL,
        order_id=outside_tracker.order_id,
    )
    opened = result(start=0, target=-400, filled=200, status=OrderStatus.PARTIALLY_FILLED).orders[0]
    if compact:
        tracker.record_terminal_order(close)
        OrderTracker(source, clock=ClockStub(), start_volume=0).add_order(opened)
    else:
        assert save_position_checkpoint(source, 900, [close])
        assert save_position_checkpoint(source, 0, [opened])
    append_terminal(audit, 1, execution_id, close, seq=91)
    append_terminal(audit, 1, execution_id, copy_data(opened, update={"status": OrderStatus.CANCELED}), seq=92)
    append_terminal(audit, 1, execution_id, outside_tracker, seq=93)
    finish_terminated(1, execution_id, ExecutionTerminated(reason="operator", mode="graceful", forced=True))

    orders_request = {"op": "pending_order_terminals", "account_id": 1}
    positions_request = {"op": "pending_positions", "account_id": 1}
    groups = native_store_call(native_database, "execution_read", orders_request)
    assert len(groups) == 1 and groups[0]["start_volume"] == 0
    assert set(groups[0]["pending"]) == {opened.order_id, outside_tracker.order_id}

    short = _assets(total_asset=280000, positions=[(SELL, 200, PositionDirection.SHORT)])
    replacement = engine([short, short])
    attach_audit(replacement._runtime, audit, "verify-short", account_id=1)
    request = copy_data(
        inputs(replacement, buy=False), update={"target_mode": "quantity", "curr_target": {SELL: -200.0}}
    )
    request.extra["pending_order_terminals"] = groups
    request.extra["pending_positions"] = native_store_call(native_database, "execution_read", positions_request)
    output = replacement.run(request)
    assert output.status == "NOOP"
    assert output.symbol_results[SELL].expected_volume == -200
    assert output.symbol_results[SELL].position_confirmed is True


@pytest.mark.parametrize("at_timeout", [False, True])
def test_checkpoint_write_failure_still_cancels_known_order(native_database, monkeypatch, at_timeout):
    seed_account(native_database)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    audit = ServerExecutionAuditSink()
    pending = result(filled=0, status=OrderStatus.PENDING).orders[0]
    executor = _TimeoutCancelExecutor(pending)
    executor.has_audit_sink = True
    executor.audit_context = {"execution_id": "checkpoint-failure"}
    executor.emit_audit_event = lambda **event: audit.append_event(
        account_id=1, execution_id="checkpoint-failure", channel=TradeChannel("gm"), algorithm="TEST", **event
    )
    tracker = OrderTracker(executor=executor, clock=ClockStub(), start_volume=900)

    async def fail_write(*args, **kwargs):
        raise TimeoutError("writer busy")

    if at_timeout:
        tracker.add_order(pending)
    monkeypatch.setattr("axile.server.execution_audit.native_operation", fail_write)
    executor.get_pending_orders = Mock(side_effect=AssertionError("use the known order without a fresh lookup"))
    with pytest.raises(RuntimeError, match="订单终态尚未确认"):
        if at_timeout:
            tracker.wait_for_completion(timeout=0)
        else:
            tracker.add_order(pending)

    assert executor.cancel_calls == [pending.order_id]
    assert tracker.explicit_error == "待核对仓位保存失败"
    with pytest.raises(RuntimeError, match="待核对仓位保存失败"):
        tracker.assert_ready_for_submission()


@pytest.mark.parametrize("record_first_slice", [False, True])
def test_restart_after_second_ctp_slice_is_accepted(native_database, monkeypatch, record_first_slice):
    seed_account(native_database)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    executor = _SingleMakerExecutor()
    executor.has_audit_sink = True
    runtime = Mock()
    audit = ServerExecutionAuditSink()
    attach_audit(runtime, audit, "split-close", 1)
    executor.audit_context = runtime.audit_context
    first = copy_data(result(filled=0, status=OrderStatus.PENDING).orders[0], update={"order_id": "first"})
    second = copy_data(result(filled=400).orders[0], update={"order_id": "second"})
    tracker = OrderTracker(executor, clock=ClockStub(), start_volume=900)
    executor.get_order_volume_bounds = lambda *_: (1, 400)

    def place(*args, **kwargs):
        if executor.place_order.call_count == 1:
            return first
        tracker.on_order_update(copy_data(first, update={"status": OrderStatus.CANCELED}))
        return second

    class WorkerStopped(BaseException):
        pass

    def write(**event):
        saved = runtime.emit_audit_event(**event)
        if executor.place_order.call_count == 2 and event["event_type"] == ExecutionEventType.POSITION_CHECKPOINT:
            assert saved
            raise WorkerStopped
        return saved

    executor.place_order = Mock(side_effect=place)
    executor.emit_audit_event = write
    with pytest.raises(WorkerStopped):
        _place_volume_slices(
            executor, OrderDirection.SELL, 800, "ACTIVE", offset_flag="1", trade_rule={}, tracker=tracker
        )

    if record_first_slice:
        prior = result(start=900, filled=0, status=OrderStatus.CANCELED)
        prior.orders = [copy_data(first, update={"status": OrderStatus.CANCELED})]
        persist(
            native_database,
            execution_id="split-close",
            raw_result={"symbol_results": {SELL: to_payload(prior, mode="json")}},
        )

    replacement = engine([assets(sell=900)], channel="ctp")
    attach_audit(replacement._runtime, audit, "replacement", 1)
    request = inputs(replacement, buy=False, sell_target=100)
    request.extra["pending_order_terminals"] = native_store_call(
        native_database, "execution_read", {"op": "pending_order_terminals", "account_id": 1}
    )
    dispatch = Mock()
    replacement._run_prepared_symbol_algorithms = dispatch
    assert request.extra["pending_order_terminals"][0]["pending"]["second"]["filled_volume"] == 400
    output = replacement.run(request)
    dispatch.assert_not_called()
    assert output.status == "BLOCKED"
    assert output.symbol_results[SELL].expected_volume == 500
