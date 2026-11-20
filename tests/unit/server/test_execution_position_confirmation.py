"""持仓确认结果使用真实原生存储跨执行恢复，保留账户隔离。"""

import asyncio
from threading import Event
from unittest.mock import Mock

import pytest

from axile.common.serialization import to_payload
from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionEventStatus, ExecutionEventType, ExecutionKind, ExecutionReasonFamily
from axile.executor.constants.order_status import OrderStatus
from axile.executor.termination import ExecutionTerminated, ExecutionTerminationController
from axile.server.execution.audit_sink import ServerExecutionAuditSink
from axile.server.execution.lifecycle import _record_terminated_execution, _resolve_termination_snapshot
from axile.server.execution.worker_backend import manager as worker_manager
from axile.server.execution.worker_backend.manager import WorkerBackendManager
from tests.conftest import native_store_call
from tests.fixtures.algorithm_fakes import ClockStub
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
    old._runtime.audit_context = {"execution_id": execution_id}
    old._runtime.emit_audit_artifact.side_effect = lambda **kwargs: audit.append_artifact(
        execution_id=execution_id, **kwargs
    )
    monkeypatch.setattr(old, "_run_prepared_symbol_algorithms", Mock(return_value=[result(observed=900)]))
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

    monkeypatch.setattr(old._owner, "get_account_assets", read)
    with pytest.raises(WorkerStopped):
        old.run(inputs(old))
    assert forced is not None and forced.forced and forced.raw_result is None
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
    assert pending == [previous]
    assert native_store_call(native_database, "execution_read", {**read_pending, "account_id": 2}) == []

    replacement = engine([assets(), assets(), assets()], channel="gm")
    replacement._owner.reconcile_terminal_order = Mock(return_value=None)
    request = inputs(replacement, buy=False)
    request.extra["pending_order_terminals"] = pending
    assert replacement.run(request).status == "BLOCKED"
    replacement._owner.reconcile_terminal_order.assert_called_once_with(SELL, SELL)

    replacement._owner.reconcile_terminal_order.return_value = result(filled=0, status=OrderStatus.CANCELED).orders[0]
    replacement._runtime.audit_context = {"execution_id": "new", "algorithm": "TEST", "account_id": 1}

    def append_origin_terminal(*, audit_context=None, **event):
        if event["event_type"] == ExecutionEventType.ORDER_TERMINAL:
            return audit.append_event(**audit_context, **event, channel=TradeChannel("gm"), seq=2)
        return True

    replacement._runtime.emit_audit_event.side_effect = append_origin_terminal
    assert replacement.run(request).status == "NOOP"
    assert native_store_call(native_database, "execution_read", read_pending) == []
