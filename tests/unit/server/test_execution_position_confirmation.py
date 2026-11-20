"""持仓确认结果使用真实原生存储跨执行恢复，保留账户隔离。"""

import asyncio
from threading import Event
from unittest.mock import Mock

import pytest

from axile.common.serialization import copy_data, to_payload
from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionEventStatus, ExecutionEventType, ExecutionKind, ExecutionReasonFamily
from axile.executor.algorithms.core.base import AlgorithmInput
from axile.executor.algorithms.defaults.single_maker import impl as single_maker
from axile.executor.algorithms.utils.order_tracker import OrderTracker
from axile.executor.constants.order_status import OrderStatus
from axile.executor.order_cancellation import CancellationResult
from axile.executor.order_terminal_audit import order_terminal_details
from axile.executor.termination import ExecutionTerminated, ExecutionTerminationController
from axile.server.execution.audit_sink import ServerExecutionAuditSink
from axile.server.execution.lifecycle import _record_terminated_execution, _resolve_termination_snapshot
from axile.server.execution.worker_backend import manager as worker_manager
from axile.server.execution.worker_backend.manager import WorkerBackendManager
from tests.conftest import native_store_call
from tests.fixtures.algorithm_fakes import ClockStub
from tests.unit.executor.algorithms.test_order_tracker_and_single_maker_regressions import _SingleMakerExecutor
from tests.unit.executor.test_execution_phase_settlement import SELL, assets, engine, inputs, result
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


def test_forced_worker_recovery_keeps_fill_before_first_position_check(native_database, monkeypatch):
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
    tracker = Mock()
    tracker.get_all_orders.return_value = [terminal]
    monkeypatch.setattr(single_maker, "setup_order_tracker", lambda *_: tracker)
    monkeypatch.setattr(single_maker, "teardown_order_tracker", lambda *_: None)
    monkeypatch.setattr(single_maker, "submit_and_track_split_orders", lambda *_args, **_kwargs: [terminal])

    def dispatch(tasks):
        return [
            single_maker.single_maker_callback(
                algorithm_executor,
                AlgorithmInput(
                    symbol=task.symbol,
                    target_volume=task.algorithm_input.target_volume,
                    params=single_maker.SingleMakerParams(),
                    trade_rule={},
                ),
            )
            for task in tasks
        ]

    monkeypatch.setattr(old, "_run_prepared_symbol_algorithms", dispatch)
    pending_request = {"op": "pending_positions", "account_id": 2}
    clock = ClockStub()
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

    def read():
        nonlocal reads, forced
        reads += 1
        if reads == 1:
            return before
        assert reads == 2
        assert native_store_call(native_database, "execution_read", pending_request) == {SELL: 500}
        controller.cancel_event.set()
        try:
            manager._request_blocking(2, command, timeout=30, termination_controller=controller)
        except ExecutionTerminated as exc:
            forced = exc
            raise WorkerStopped from exc
        raise AssertionError("manager did not stop the blocked worker")

    monkeypatch.setattr(old._owner, "get_account_assets", lambda: before)
    monkeypatch.setattr(algorithm_executor, "get_account_assets", read)
    with pytest.raises(WorkerStopped):
        old.run(inputs(old))
    assert forced is not None and forced.forced and forced.raw_result is None
    tracker.wait_for_completion.assert_called_once()
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

    pending = native_store_call(native_database, "execution_read", pending_request)
    replacement = engine([before], channel="gm")
    dispatch = Mock()
    monkeypatch.setattr(replacement, "_run_prepared_symbol_algorithms", dispatch)
    request = inputs(replacement)
    request.extra["pending_positions"] = pending
    assert replacement.run(request).status == "BLOCKED"
    dispatch.assert_not_called()

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

    replacement = engine([assets(), assets(), assets()], channel="gm")
    replacement._owner.reconcile_terminal_order = Mock(return_value=None)
    attach_audit(replacement._runtime, audit, "new", account_id=1)
    request = inputs(replacement, buy=False)
    request.extra["pending_order_terminals"] = pending
    assert replacement.run(request).status == "BLOCKED"
    replacement._owner.reconcile_terminal_order.assert_called_once_with(SELL, SELL)

    replacement._owner.reconcile_terminal_order.return_value = result(filled=0, status=OrderStatus.CANCELED).orders[0]
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
    late = result(start=800, target=500, filled=0, status=OrderStatus.PENDING).orders[0]
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

    if audit_pending_saved:
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

    terminal = copy_data(late, update={"filled_volume": 300, "status": OrderStatus.FILLED})
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
