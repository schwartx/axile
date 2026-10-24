"""执行生命周期重构后的回归测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from loguru import logger

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionKind, ExecutionTaskStatus, ExecutionTerminateMode
from axile.executor.termination import ExecutionTerminated
from axile.server.db.models import ExecuteRecord
from axile.server.execution import backend, rebalance, records
from axile.server.execution import lifecycle as execution_lifecycle
from axile.server.execution import registry as execution_registry
from axile.server.execution.intents import IntentSnapshot
from tests.unit.server._business_facts_test_support import seed_account
from tests.unit.server._execution_test_support import build_account


@pytest.fixture(autouse=True)
def _isolate_outcome_symbol_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """生命周期编排用例的品种名单由定向读取桩提供。"""

    async def read_symbols(_record: object) -> list[str]:
        return []

    monkeypatch.setattr(execution_lifecycle, "load_record_outcome_symbols", read_symbols)


async def _terminate_task(monkeypatch, state, exc):
    finished = AsyncMock()
    monkeypatch.setattr(execution_lifecycle, "mark_intent_finished", finished)
    monkeypatch.setattr(execution_lifecycle, "sync_account_live", lambda _account_id: None)

    async def runner(_execution_id):
        raise exc

    await execution_lifecycle._run_execution_task(
        account_id=state.account_id,
        execution_id=state.execution_id,
        execution_kind=state.execution_kind,
        runner=runner,
    )
    finished.assert_not_awaited()
    assert execution_registry.get_running_execution_id(state.account_id) is None
    assert execution_registry.get_queued_execution_id(state.account_id) is None


@pytest.mark.parametrize("kind", list(ExecutionKind))
def test_queued_execution_terminated_persists_record_and_updates_state(monkeypatch, kind) -> None:
    """调仓与清仓 terminate 共用收尾逻辑，并同步更新状态与事件。"""
    execution_id = "exec-queued-terminated-1"
    state = execution_registry.ExecutionTaskState(
        execution_id=execution_id,
        account_id=1,
        execution_kind=kind,
        status=ExecutionTaskStatus.RUNNING,
        created_at="2026-03-27T10:00:00",
        cancel_requested_at="2026-03-27T10:00:01",
        channel=TradeChannel.CTP,
        algorithm="SINGLE-MAKER",
    )
    captured: dict[str, object] = {"events": []}
    execution_registry.set_execution_task_state(execution_id, state)

    async def fake_append_terminated_execute_record(**kwargs: object) -> ExecuteRecord:
        captured["record_kwargs"] = kwargs
        return ExecuteRecord(id=321, account_id=1, is_success=0, task_status="TERMINATED")

    async def fake_append_execution_event(**kwargs: object) -> None:
        events = captured["events"]
        assert isinstance(events, list)
        events.append(kwargs)

    monkeypatch.setattr(execution_lifecycle, "append_terminated_execute_record", fake_append_terminated_execute_record)
    monkeypatch.setattr(execution_lifecycle, "append_execution_event", fake_append_execution_event)
    monkeypatch.setattr(execution_lifecycle, "now_str", lambda: "2026-03-27T10:00:05")

    try:
        asyncio.run(
            _terminate_task(
                monkeypatch,
                state,
                ExecutionTerminated(
                    reason="manual stop",
                    mode=ExecutionTerminateMode.GRACEFUL.value,
                    acked_at="2026-03-27T10:00:03",
                    cancel_failed_order_ids=["order-1"],
                ),
            )
        )
        current_state = state
    finally:
        execution_registry._clear_execution_task_state(execution_id)

    assert captured["record_kwargs"] == {
        "account_id": 1,
        "execution_id": execution_id,
        "execution_kind": kind,
        "reason": "manual stop",
        "mode": ExecutionTerminateMode.GRACEFUL.value,
        "requested_at": "2026-03-27T10:00:01",
        "acked_at": "2026-03-27T10:00:03",
        "finished_at": "2026-03-27T10:00:05",
        "cancel_attempted": False,
        "cancel_failed_order_ids": ["order-1"],
        # 人工终止的默认来源；超时终止会在同一位置落 "timeout"。
        "trigger": "operator",
        "forced": False,
        "cancel_unconfirmed": False,
    }
    assert captured["events"] == [
        {
            "execution_id": execution_id,
            "account_id": 1,
            "channel": TradeChannel.CTP,
            "algorithm": "SINGLE-MAKER",
            "event_type": execution_lifecycle.ExecutionEventType.EXECUTION_TERMINATED,
            "status": execution_lifecycle.ExecutionEventStatus.WARNING,
            "reason_family": execution_lifecycle.ExecutionReasonFamily.SYSTEM,
            "reason_code": "COMMON.EXECUTION_TERMINATED",
            "details": {
                "termination": {
                    "reason": "manual stop",
                    "mode": ExecutionTerminateMode.GRACEFUL.value,
                    "trigger": "operator",
                    "execution_kind": kind.value,
                    "cancel_failed_order_ids": ["order-1"],
                    "forced": False,
                    "cancel_unconfirmed": False,
                }
            },
        }
    ]
    assert current_state is not None
    assert current_state.status == ExecutionTaskStatus.TERMINATED
    assert current_state.finished_at == "2026-03-27T10:00:05"
    assert current_state.record_id == 321
    assert current_state.is_success == 0


@pytest.mark.parametrize("kind", list(ExecutionKind))
def test_run_execution_task_uses_state_termination_fallbacks(monkeypatch, kind) -> None:
    """后台 terminate 分支应回退到内存状态中的 reason/mode。"""
    execution_id = "exec-queued-terminated-1"
    state = execution_registry.ExecutionTaskState(
        execution_id=execution_id,
        account_id=1,
        execution_kind=kind,
        status=ExecutionTaskStatus.QUEUED,
        created_at="2026-03-27T11:00:00",
        cancel_requested_at="2026-03-27T11:00:01",
        cancel_reason="risk stop",
        terminate_mode=ExecutionTerminateMode.CANCEL_PENDING,
        channel=TradeChannel.CTP,
        algorithm="SINGLE-MAKER",
    )
    captured: dict[str, object] = {"events": []}
    execution_registry.set_execution_task_state(execution_id, state)
    execution_registry.try_register_running_execution(1, execution_id)

    async def fake_append_terminated_execute_record(**kwargs: object) -> ExecuteRecord:
        captured["record_kwargs"] = kwargs
        return ExecuteRecord(id=654, account_id=1, is_success=0, task_status="TERMINATED")

    async def fake_append_execution_event(**kwargs: object) -> None:
        events = captured["events"]
        assert isinstance(events, list)
        events.append(kwargs)

    async def fake_runner(_tracked_execution_id: str) -> SimpleNamespace:
        raise ExecutionTerminated(
            reason=None,
            mode=None,
            acked_at="2026-03-27T11:00:02",
            cancel_failed_order_ids=["order-9"],
        )

    async def fake_mark(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(execution_lifecycle, "append_terminated_execute_record", fake_append_terminated_execute_record)
    monkeypatch.setattr(execution_lifecycle, "append_execution_event", fake_append_execution_event)
    monkeypatch.setattr(execution_lifecycle, "mark_intent_finished", fake_mark)
    monkeypatch.setattr(execution_lifecycle, "sync_account_live", lambda *_a, **_k: None)
    monkeypatch.setattr(execution_lifecycle, "now_str", lambda: "2026-03-27T11:00:05")

    try:
        asyncio.run(
            execution_lifecycle._run_execution_task(
                account_id=1,
                execution_id=execution_id,
                execution_kind=kind,
                runner=fake_runner,
            )
        )
        current_state = execution_registry.get_execution_task_state(execution_id)
    finally:
        execution_registry._clear_execution_task_state(execution_id)
        execution_registry.clear_running_execution(1, execution_id)

    assert captured["record_kwargs"] == {
        "account_id": 1,
        "execution_id": execution_id,
        "execution_kind": kind,
        "reason": "risk stop",
        "mode": ExecutionTerminateMode.CANCEL_PENDING.value,
        "requested_at": "2026-03-27T11:00:01",
        "acked_at": "2026-03-27T11:00:02",
        "finished_at": "2026-03-27T11:00:05",
        "cancel_attempted": True,
        "cancel_failed_order_ids": ["order-9"],
        "trigger": "operator",
        "forced": False,
        "cancel_unconfirmed": False,
    }
    assert captured["events"] == [
        {
            "execution_id": execution_id,
            "account_id": 1,
            "channel": TradeChannel.CTP,
            "algorithm": "SINGLE-MAKER",
            "event_type": execution_lifecycle.ExecutionEventType.EXECUTION_TERMINATED,
            "status": execution_lifecycle.ExecutionEventStatus.WARNING,
            "reason_family": execution_lifecycle.ExecutionReasonFamily.SYSTEM,
            "reason_code": "COMMON.EXECUTION_TERMINATED",
            "details": {
                "termination": {
                    "reason": "risk stop",
                    "mode": ExecutionTerminateMode.CANCEL_PENDING.value,
                    "trigger": "operator",
                    "execution_kind": kind.value,
                    "cancel_failed_order_ids": ["order-9"],
                    "forced": False,
                    "cancel_unconfirmed": False,
                }
            },
        }
    ]
    assert current_state is not None
    assert current_state.status == ExecutionTaskStatus.TERMINATED
    assert current_state.finished_at == "2026-03-27T11:00:05"
    assert current_state.record_id == 654
    assert current_state.is_success == 0


def test_enqueue_empty_positions_delegates_to_submit_intent(monkeypatch) -> None:
    """清仓入队应走 submit_intent，不再直接占渠道锁。"""
    captured: dict[str, object] = {}

    async def fake_submit(
        account_id: int,
        kind: ExecutionKind,
        trigger_source: str,
        payload: dict[str, object] | None = None,
        on_conflict: str = "raise",
    ) -> execution_lifecycle.SubmitResult:
        captured["args"] = (account_id, kind, trigger_source, payload, on_conflict)
        return execution_lifecycle.SubmitResult(outcome="created", execution_id="exec-clear-1", account_id=account_id)

    monkeypatch.setattr(execution_lifecycle, "submit_intent", fake_submit)
    result = asyncio.run(execution_lifecycle.enqueue_empty_positions(71, algorithm={"method": "SINGLE-MAKER"}))
    assert result.execution_id == "exec-clear-1"
    assert captured["args"] == (
        71,
        ExecutionKind.CLEAR_POSITIONS,
        "empty_positions",
        {"method": "SINGLE-MAKER"},
        "raise",
    )


@pytest.mark.parametrize("kind", list(ExecutionKind))
def test_run_execution_task_uses_saved_result_without_finishing_twice(monkeypatch, kind) -> None:
    """后台任务按保存的记录收尾，不重复写 intent。"""
    execution_id = "exec-intent-finish-1"
    state = execution_registry.ExecutionTaskState(
        execution_id=execution_id,
        account_id=1,
        execution_kind=kind,
        status=ExecutionTaskStatus.QUEUED,
        created_at="2026-03-27T12:00:00",
        channel=TradeChannel.CTP,
        algorithm="SINGLE-MAKER",
    )
    execution_registry.set_execution_task_state(execution_id, state)
    finished = AsyncMock()

    async def fake_runner(_tracked: str) -> SimpleNamespace:
        return ExecuteRecord(
            id=9, account_id=1, is_success=1, status="SUCCEEDED", display_status="SUCCEEDED", task_status="SUCCEEDED"
        )

    monkeypatch.setattr(execution_lifecycle, "mark_intent_finished", finished)
    monkeypatch.setattr(execution_lifecycle, "sync_account_live", lambda *_a, **_k: None)
    monkeypatch.setattr(execution_lifecycle, "now_str", lambda: "2026-03-27T12:00:05")

    try:
        asyncio.run(
            execution_lifecycle._run_execution_task(
                account_id=1,
                execution_id=execution_id,
                execution_kind=kind,
                runner=fake_runner,
            )
        )
    finally:
        execution_registry._clear_execution_task_state(execution_id)

    assert state.status == ExecutionTaskStatus.SUCCEEDED
    assert state.record_id == 9
    finished.assert_not_awaited()


def test_queue_serializes_coalesced_rebalances_and_clear(native_database, monkeypatch) -> None:
    """真实队列合并重复调仓，串行完成后用相同生命周期执行清仓。"""
    from axile.server.execution import clear_positions, dispatcher, intents

    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    monkeypatch.setattr(intents, "_wake", lambda _account_id: None)
    started, release = asyncio.Event(), asyncio.Event()
    executed = []
    finished = AsyncMock(wraps=execution_lifecycle.mark_intent_finished)
    monkeypatch.setattr(execution_lifecycle, "mark_intent_finished", finished)
    algorithm = {"method": "SINGLE-MAKER", "params": {"max_wait_seconds": 30}}

    async def complete(execution_id):
        return await records._persist_execute_record(
            account_id=1,
            execution_id=execution_id,
            raw_input={},
            raw_result={"status": "SUCCEEDED", "orders": [], "trades": []},
            is_success=1,
        )

    async def run_rebalance(account_id, execution_id, trigger_source):
        assert account_id == 1 and trigger_source == "manual"
        await intents.promote_intent_to_running(execution_id, account_id)
        assert execution_registry.get_running_execution_id(1) == execution_id
        executed.append(execution_id)
        started.set()
        await release.wait()
        return await complete(execution_id)

    async def run_clear(account, execution_id, algorithm):
        assert account.id == 1 and algorithm == {"method": "SINGLE-MAKER", "params": {"max_wait_seconds": 30}}
        await intents.promote_intent_to_running(execution_id, account.id)
        executed.append(execution_id)
        return await complete(execution_id)

    monkeypatch.setattr(rebalance, "_run_account_rebalance", run_rebalance)
    monkeypatch.setattr(clear_positions, "_run_clear_positions", run_clear)

    async def run():
        await records.native_operation(
            "account_create",
            {"account": build_account(portfolio_id=None).model_dump(mode="json"), "now": "2026-10-24T10:00:00+08:00"},
        )
        first = await execution_lifecycle.enqueue_execute_trade(1)
        task = asyncio.create_task(dispatcher._dispatch_account(1))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            second = await execution_lifecycle.enqueue_execute_trade(1)
            duplicate = await execution_lifecycle.enqueue_execute_trade(1)
            assert second.outcome == "created" and duplicate.outcome == "coalesced"
            assert first.execution_id != second.execution_id == duplicate.execution_id
            assert executed == [first.execution_id]
        finally:
            release.set()
            await asyncio.wait_for(task, timeout=2)

        clear = await execution_lifecycle.enqueue_empty_positions(1, algorithm=algorithm)
        await dispatcher._dispatch_account(1)
        assert executed == [first.execution_id, second.execution_id, clear.execution_id]
        finished.assert_not_awaited()
        for execution_id in executed:
            intent = await intents.get_intent(execution_id)
            assert intent.status == ExecutionTaskStatus.SUCCEEDED
        assert execution_registry.get_running_execution_id(1) is None
        assert execution_registry.get_queued_execution_id(1) is None

    try:
        asyncio.run(run())
    finally:
        for execution_id in executed:
            execution_registry.clear_running_execution(1, execution_id)
            execution_registry.clear_queued_execution(1, execution_id)
            execution_registry._clear_execution_task_state(execution_id)


def test_run_execution_task_rehydrates_queued_intent_without_memory(monkeypatch) -> None:
    """内存缺失但 intent 仍是 QUEUED 时应补水合再跑，而不是直接标终止。"""
    execution_id = "exec-rehydrate-queued-1"
    snapshot = IntentSnapshot(
        execution_id=execution_id,
        account_id=1,
        kind=ExecutionKind.REBALANCE,
        trigger_source="manual",
        status=ExecutionTaskStatus.QUEUED,
        channel=TradeChannel.CTP,
        algorithm="SINGLE-MAKER",
        payload={},
        created_at="2026-03-27T12:00:00",
        started_at=None,
        finished_at=None,
        error=None,
        cancel_requested_at=None,
        cancel_reason=None,
        terminate_mode=None,
    )
    ran: dict[str, str] = {}
    finished = AsyncMock()

    async def fake_get_intent(eid: str) -> IntentSnapshot:
        assert eid == execution_id
        return snapshot

    async def fake_runner(tracked: str) -> SimpleNamespace:
        ran["id"] = tracked
        assert execution_registry.get_execution_task_state(tracked) is not None
        return ExecuteRecord(
            id=1, account_id=1, is_success=1, status="SUCCEEDED", display_status="SUCCEEDED", task_status="SUCCEEDED"
        )

    monkeypatch.setattr(execution_lifecycle, "get_intent", fake_get_intent)
    monkeypatch.setattr(execution_lifecycle, "mark_intent_finished", finished)
    monkeypatch.setattr(execution_lifecycle, "sync_account_live", lambda *_a, **_k: None)

    try:
        asyncio.run(
            execution_lifecycle._run_execution_task(
                account_id=1,
                execution_id=execution_id,
                execution_kind=ExecutionKind.REBALANCE,
                runner=fake_runner,
            )
        )
        assert ran["id"] == execution_id
        assert execution_registry.get_execution_task_state(execution_id).status == ExecutionTaskStatus.SUCCEEDED
        finished.assert_not_awaited()
    finally:
        execution_registry.clear_queued_execution(1, execution_id)
        execution_registry._clear_execution_task_state(execution_id)


def test_run_execution_task_missing_memory_skips_terminal_intent(monkeypatch) -> None:
    """内存缺失且 intent 已终态时不应再跑 runner。"""
    execution_id = "exec-missing-terminal-1"
    snapshot = IntentSnapshot(
        execution_id=execution_id,
        account_id=1,
        kind=ExecutionKind.REBALANCE,
        trigger_source="manual",
        status=ExecutionTaskStatus.TERMINATED,
        channel=TradeChannel.CTP,
        algorithm="SINGLE-MAKER",
        payload={},
        created_at="2026-03-27T12:00:00",
        started_at=None,
        finished_at="2026-03-27T12:00:01",
        error=None,
        cancel_requested_at=None,
        cancel_reason=None,
        terminate_mode=None,
    )
    ran = False

    async def fake_get_intent(eid: str) -> IntentSnapshot:
        assert eid == execution_id
        return snapshot

    async def fake_runner(_tracked: str) -> SimpleNamespace:
        nonlocal ran
        ran = True
        return ExecuteRecord(id=1, account_id=1, is_success=1, task_status="SUCCEEDED")

    monkeypatch.setattr(execution_lifecycle, "get_intent", fake_get_intent)
    monkeypatch.setattr(execution_lifecycle, "sync_account_live", lambda *_a, **_k: None)

    asyncio.run(
        execution_lifecycle._run_execution_task(
            account_id=1,
            execution_id=execution_id,
            execution_kind=ExecutionKind.REBALANCE,
            runner=fake_runner,
        )
    )
    assert ran is False


def test_run_execution_task_retry_keeps_queued_slot(monkeypatch) -> None:
    """渠道锁占用时应保留 QUEUED 槽，好让终止仍能找到这张票."""
    execution_id = "exec-retry-queued-1"
    state = execution_registry.ExecutionTaskState(
        execution_id=execution_id,
        account_id=1,
        execution_kind=ExecutionKind.REBALANCE,
        status=ExecutionTaskStatus.QUEUED,
        created_at="2026-03-27T12:00:00",
        channel=TradeChannel.CTP,
        algorithm="SINGLE-MAKER",
    )
    execution_registry.set_execution_task_state(execution_id, state)
    execution_registry.set_queued_execution(1, execution_id)

    async def fake_runner(_tracked: str) -> SimpleNamespace:
        raise execution_lifecycle.IntentNotRunnable("账户渠道锁占用中", retry=True)

    monkeypatch.setattr(execution_lifecycle, "sync_account_live", lambda *_a, **_k: None)

    try:
        with pytest.raises(execution_lifecycle.IntentNotRunnable):
            asyncio.run(
                execution_lifecycle._run_execution_task(
                    account_id=1,
                    execution_id=execution_id,
                    execution_kind=ExecutionKind.REBALANCE,
                    runner=fake_runner,
                )
            )
        assert execution_registry.get_queued_execution_id(1) == execution_id
        assert execution_registry.get_execution_task_state(execution_id) is not None
    finally:
        execution_registry.clear_queued_execution(1, execution_id)
        execution_registry._clear_execution_task_state(execution_id)


@pytest.mark.parametrize("kind", list(ExecutionKind))
def test_timeout_termination_is_distinguishable_from_operator(monkeypatch, kind) -> None:
    """总超时终止必须在落库与审计里与人工终止区分开（trigger=timeout）。"""
    execution_id = "exec-queued-terminated-timeout"
    state = execution_registry.ExecutionTaskState(
        execution_id=execution_id,
        account_id=1,
        execution_kind=kind,
        status=ExecutionTaskStatus.RUNNING,
        created_at="2026-07-27T10:00:00",
        channel=TradeChannel.CTP,
        algorithm="SINGLE-MAKER",
    )
    captured: dict[str, object] = {"events": []}
    execution_registry.set_execution_task_state(execution_id, state)

    async def fake_append_terminated_execute_record(**kwargs: object) -> ExecuteRecord:
        captured["record_kwargs"] = kwargs
        return ExecuteRecord(id=777, account_id=1, is_success=0, task_status="TERMINATED")

    async def fake_append_execution_event(**kwargs: object) -> None:
        events = captured["events"]
        assert isinstance(events, list)
        events.append(kwargs)

    monkeypatch.setattr(execution_lifecycle, "append_terminated_execute_record", fake_append_terminated_execute_record)
    monkeypatch.setattr(execution_lifecycle, "append_execution_event", fake_append_execution_event)
    monkeypatch.setattr(execution_lifecycle, "now_str", lambda: "2026-07-27T10:03:00")

    try:
        asyncio.run(
            _terminate_task(
                monkeypatch,
                state,
                ExecutionTerminated(
                    reason="执行总超时（180s）",
                    mode=ExecutionTerminateMode.CANCEL_PENDING.value,
                    acked_at="2026-07-27T10:03:00",
                    trigger="timeout",
                ),
            )
        )
    finally:
        execution_registry._clear_execution_task_state(execution_id)

    record_kwargs = captured["record_kwargs"]
    assert isinstance(record_kwargs, dict)
    assert record_kwargs["trigger"] == "timeout"
    # 超时同样按 cancel_pending 收尾，因此 mode 不能用来区分二者。
    assert record_kwargs["mode"] == ExecutionTerminateMode.CANCEL_PENDING.value
    assert record_kwargs["cancel_attempted"] is True

    events = captured["events"]
    assert isinstance(events, list)
    assert events[0]["details"]["termination"]["trigger"] == "timeout"


def test_worker_backend_terminated_response_round_trips_trigger() -> None:
    """worker 进程内触发的超时，需经跨进程响应还原为 trigger=timeout 的异常。"""
    from axile.server.execution.worker_backend.manager import WorkerBackendManager
    from axile.server.execution.worker_backend.protocol import WorkerBackendResponse
    from axile.server.execution.worker_backend.worker_responses import _build_terminated_response

    response = _build_terminated_response(
        request_id="req-1",
        account=SimpleNamespace(trade_channel=TradeChannel.GM),  # type: ignore[arg-type]
        exc=ExecutionTerminated(
            reason="执行总超时（180s）",
            mode=ExecutionTerminateMode.CANCEL_PENDING.value,
            acked_at="2026-07-27T10:03:00",
            trigger="timeout",
        ),
    )
    assert isinstance(response, WorkerBackendResponse)
    assert response.trigger == "timeout"

    with pytest.raises(ExecutionTerminated) as exc_info:
        WorkerBackendManager._handle_response(response)

    assert exc_info.value.trigger == "timeout"
    assert exc_info.value.mode == ExecutionTerminateMode.CANCEL_PENDING.value


def test_worker_backend_terminated_response_defaults_trigger_to_operator() -> None:
    """缺省 trigger 的历史响应应按人工终止兜底，避免误标成超时。"""
    from axile.server.execution.worker_backend.manager import WorkerBackendManager
    from axile.server.execution.worker_backend.protocol import WorkerBackendResponse

    response = WorkerBackendResponse(
        request_id="req-2",
        kind="terminated",
        channel_type=TradeChannel.GM,
        reason="manual stop",
        mode=ExecutionTerminateMode.GRACEFUL.value,
    )

    with pytest.raises(ExecutionTerminated) as exc_info:
        WorkerBackendManager._handle_response(response)

    assert exc_info.value.trigger == "operator"


@pytest.mark.parametrize("kind", list(ExecutionKind))
@pytest.mark.parametrize("trigger", ["timeout", "operator"])
def test_timeout_termination_sends_alert(monkeypatch, kind, trigger) -> None:
    """两种执行都只对总超时告警，人工终止不重复通知。"""
    execution_id = f"exec-alert-{kind.value}-{trigger}"
    state = execution_registry.ExecutionTaskState(
        execution_id=execution_id,
        account_id=1,
        execution_kind=kind,
        status=ExecutionTaskStatus.RUNNING,
        created_at="2026-07-27T10:00:00",
        channel=TradeChannel.CTP,
        algorithm="SINGLE-MAKER",
    )
    execution_registry.set_execution_task_state(execution_id, state)
    sent = AsyncMock()
    monkeypatch.setattr(
        execution_lifecycle,
        "append_terminated_execute_record",
        AsyncMock(return_value=ExecuteRecord(id=888, account_id=1, is_success=0, task_status="TERMINATED")),
    )
    monkeypatch.setattr(execution_lifecycle, "append_execution_event", AsyncMock())
    monkeypatch.setattr(execution_lifecycle, "send_feishu_error", sent)
    monkeypatch.setattr(execution_lifecycle, "get_account_snapshot", AsyncMock(return_value=build_account().snapshot()))
    monkeypatch.setattr(execution_lifecycle, "now_str", lambda: "2026-07-27T10:03:00")

    try:
        asyncio.run(
            _terminate_task(
                monkeypatch,
                state,
                ExecutionTerminated(
                    reason="执行总超时（180s）" if trigger == "timeout" else "manual stop",
                    mode=ExecutionTerminateMode.CANCEL_PENDING.value,
                    trigger=trigger,
                ),
            )
        )
        assert sent.await_count == (1 if trigger == "timeout" else 0)
        if trigger == "timeout":
            assert "总超时" in str(sent.await_args.args[0])
    finally:
        execution_registry._clear_execution_task_state(execution_id)


def test_queued_execution_copies_blocked_output_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """业务失败没有异常，完成态也必须把 output.error / status 写进轮询载荷。"""
    execution_id = "exec-blocked-queued-1"
    state = execution_registry.ExecutionTaskState(
        execution_id=execution_id,
        account_id=1,
        execution_kind=ExecutionKind.REBALANCE,
        status=ExecutionTaskStatus.RUNNING,
        created_at="2026-08-26T20:34:24",
    )
    execution_registry.set_execution_task_state(execution_id, state)
    record = ExecuteRecord(
        id=6,
        account_id=1,
        is_success=0,
        task_status="FAILED",
        status="BLOCKED",
        error="5 个品种因交易时段不可执行",
        outcome="blocked",
        display_status="BLOCKED",
        display_error="5 个品种因交易时段不可执行",
        display_outcome="blocked",
    )

    async def read_symbols(actual: ExecuteRecord) -> list[str]:
        assert actual is record
        return ["ag2612", "m2701"]

    monkeypatch.setattr(execution_lifecycle, "load_record_outcome_symbols", read_symbols)

    finished = AsyncMock()
    monkeypatch.setattr(execution_lifecycle, "mark_intent_finished", finished)
    monkeypatch.setattr(execution_lifecycle, "sync_account_live", lambda _account_id: None)

    async def runner(_execution_id):
        return record

    try:
        asyncio.run(
            execution_lifecycle._run_execution_task(
                account_id=1, execution_id=execution_id, execution_kind=ExecutionKind.REBALANCE, runner=runner
            )
        )
        current = state
    finally:
        execution_registry._clear_execution_task_state(execution_id)

    assert current is not None
    assert current.status == ExecutionTaskStatus.FAILED
    assert current.is_success == 0
    assert current.error == "5 个品种因交易时段不可执行"
    assert current.output_status == "BLOCKED"
    assert current.outcome == "blocked"
    assert current.outcome_symbols == ["ag2612", "m2701"]
    assert current.record_id == 6
    finished.assert_not_awaited()


def test_mark_execution_finished_copies_saved_record_status_and_error() -> None:
    """worker 收尾沿用已保存记录的终态与展示错误。"""
    execution_id = "exec-worker-blocked-1"
    state = execution_registry.ExecutionTaskState(
        execution_id=execution_id,
        account_id=1,
        execution_kind=ExecutionKind.REBALANCE,
        status=ExecutionTaskStatus.RUNNING,
        created_at="2026-08-26T20:34:24",
    )
    execution_registry.set_execution_task_state(execution_id, state)
    record = ExecuteRecord(
        id=8,
        account_id=1,
        is_success=0,
        task_status="FAILED",
        status="BLOCKED",
        error="当前不在交易时间",
        display_status="BLOCKED",
        display_error="当前不在交易时间",
    )

    try:
        execution_lifecycle._mark_execution_finished(
            execution_id,
            finished_at="2026-08-26T20:34:30",
            record=record,
        )
        blocked = execution_registry.get_execution_task_state(execution_id)
        assert blocked is not None
        assert blocked.status == ExecutionTaskStatus.FAILED
        assert blocked.record_id == record.id
        assert blocked.error == "当前不在交易时间"
        assert blocked.output_status == "BLOCKED"
    finally:
        execution_registry._clear_execution_task_state(execution_id)


@pytest.mark.parametrize("kind", [ExecutionKind.REBALANCE, ExecutionKind.CLEAR_POSITIONS])
def test_worker_failure_persists_once_through_task_lifecycle(native_database, monkeypatch, kind):
    """底层与任务收尾保留各自异常语义，同一失败只提交一次原生记录。"""
    seed_account(native_database)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    execution_id = f"failed-{kind.value}"
    account = build_account(id=1).snapshot()
    failure = RuntimeError("worker exploded")
    worker = SimpleNamespace(
        execute_trade=AsyncMock(side_effect=failure), empty_positions=AsyncMock(side_effect=failure)
    )
    monkeypatch.setattr(backend, "get_worker_backend_manager", lambda: worker)
    native = AsyncMock(wraps=records.native_operation)
    monkeypatch.setattr(records, "native_operation", native)
    finished = AsyncMock()
    monkeypatch.setattr(execution_lifecycle, "mark_intent_finished", finished)
    monkeypatch.setattr(execution_lifecycle, "sync_account_live", lambda _account_id: None)
    state = execution_registry.ExecutionTaskState(
        execution_id=execution_id,
        account_id=1,
        execution_kind=kind,
        status=ExecutionTaskStatus.QUEUED,
        created_at="2026-10-23T10:00:00",
    )
    execution_registry.set_execution_task_state(execution_id, state)

    async def runner(_execution_id):
        if kind == ExecutionKind.REBALANCE:
            standard_input = rebalance._build_rebalance_standard_input(
                account=account,
                curr_target={"A": 0.1},
                last_target={},
                execution_id=execution_id,
                trigger_source="manual",
            )
            request = backend.RebalanceBackendRequest(
                account=account,
                standard_input_dict=standard_input.to_dict(),
                audit_input={},
                execution_id=execution_id,
                trigger_source="manual",
                logger=logger,
            )
            return await backend._run_rebalance_via_worker_process(request)
        request = backend.ClearPositionsBackendRequest(
            account=account,
            resolved_algorithm={"method": "SINGLE-MAKER"},
            empty_kwargs={},
            audit_input={},
            execution_id=execution_id,
            trigger_source="empty_positions",
            logger=logger,
        )
        return await backend._run_clear_positions_via_worker_process(request)

    try:
        asyncio.run(
            execution_lifecycle._run_execution_task(
                account_id=1, execution_id=execution_id, execution_kind=kind, runner=runner
            )
        )
        assert native.await_count == 1
        name, request = native.await_args.args
        assert name == "persist_execute_record"
        assert request["raw_result"]["technical_detail"] == "worker exploded"
        assert "RuntimeError: worker exploded" in request["raw_result"]["traceback"]
        if kind == ExecutionKind.REBALANCE:
            assert request["raw_input"]["curr_target"] == {"A": 0.1}
        else:
            assert request["raw_input"] is None
        expected_error = (
            "调仓执行失败，具体原因未确认" if kind == ExecutionKind.REBALANCE else "清仓执行失败，具体原因未确认"
        )
        assert state.status == ExecutionTaskStatus.FAILED
        assert state.error == expected_error
        finished.assert_not_awaited()
    finally:
        execution_registry._clear_execution_task_state(execution_id)
