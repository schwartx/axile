"""执行状态机与终止控制测试。"""

import asyncio
from threading import Event
from types import SimpleNamespace
from typing import cast

import pytest

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import (
    ExecutionEventType,
    ExecutionKind,
    ExecutionTaskStatus,
    ExecutionTerminateMode,
)
from axile.server.db.models import ExecuteRecord
from axile.server.execution import registry as execution_registry


@pytest.fixture(autouse=True)
def _no_persisted_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    from axile.server.execution import intents

    async def get_intent(_execution_id: str):
        return None

    monkeypatch.setattr(intents, "get_intent", get_intent)


def _persisted_record(monkeypatch: pytest.MonkeyPatch, record: ExecuteRecord) -> None:
    async def read(name: str, request: dict[str, object]) -> dict[str, object]:
        assert name == "execution_read" and request == {"op": "status", "execution_id": record.execution_id}
        return {"record": record.model_dump(), "outcome_symbols": []}

    monkeypatch.setattr(execution_registry, "native_operation", read)


def test_get_execution_status_prefers_in_memory_task_state() -> None:
    """应优先返回内存中的 execution 状态，再回退到持久化记录。"""
    execution_id = "exec-status-1"
    state = execution_registry.ExecutionTaskState(
        execution_id=execution_id,
        account_id=1,
        execution_kind=ExecutionKind.REBALANCE,
        status=ExecutionTaskStatus.RUNNING,
        created_at="2026-03-12T13:00:00",
        started_at="2026-03-12T13:00:01",
    )
    execution_registry.set_execution_task_state(execution_id, state)

    try:
        payload = asyncio.run(execution_registry.get_execution_status(execution_id))
    finally:
        execution_registry._clear_execution_task_state(execution_id)

    assert payload is not None
    assert payload["execution_id"] == execution_id
    assert payload["status"] == ExecutionTaskStatus.RUNNING


def test_get_execution_status_falls_back_to_execute_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """当内存状态不存在时，应回退到已持久化的执行记录。"""
    execution_id = "exec-record-1"
    record = ExecuteRecord(
        id=999,
        execution_id=execution_id,
        account_id=1,
        is_success=1,
        created_at="2026-03-12T13:05:00",
        task_status="SUCCEEDED",
    )

    _persisted_record(monkeypatch, record)

    payload = asyncio.run(execution_registry.get_execution_status(execution_id))

    assert payload is not None
    assert payload["record_id"] == 999
    assert payload["status"] == ExecutionTaskStatus.SUCCEEDED


def test_get_execution_status_uses_persisted_task_status_and_termination_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """持久化回放应优先使用 task_status，并带回 terminate 元信息。"""
    execution_id = "exec-record-terminated-1"
    record = ExecuteRecord(
        id=1001,
        execution_id=execution_id,
        account_id=1,
        is_success=0,
        created_at="2026-03-12T13:05:00",
        task_status="TERMINATED",
        execution_kind="rebalance",
        termination_requested_at="2026-03-12T13:05:01",
        termination_reason="manual stop",
        termination_mode="cancel_pending",
        termination_finished_at="2026-03-12T13:05:03",
    )

    _persisted_record(monkeypatch, record)

    payload = asyncio.run(execution_registry.get_execution_status(execution_id))

    assert payload is not None
    assert payload["status"] == ExecutionTaskStatus.TERMINATED
    assert payload["execution_kind"] == ExecutionKind.REBALANCE
    assert payload["cancel_reason"] == "manual stop"
    assert payload["terminate_mode"] == ExecutionTerminateMode.CANCEL_PENDING
    assert payload["error"] is None
    assert payload["output_status"] is None


def test_request_execution_termination_marks_running_task_terminating() -> None:
    """运行中任务收到终止请求后应切到 TERMINATING 并触发 cancel_event。"""
    execution_id = "exec-running-terminate-1"
    cancel_event = Event()
    state = execution_registry.ExecutionTaskState(
        execution_id=execution_id,
        account_id=1,
        execution_kind=ExecutionKind.REBALANCE,
        status=ExecutionTaskStatus.RUNNING,
        created_at="2026-03-12T13:00:00",
        cancel_event=cancel_event,
    )
    execution_registry.set_execution_task_state(execution_id, state)

    try:
        updated_state, transitioned = execution_registry.request_execution_termination(
            execution_id,
            reason="manual stop",
            mode=ExecutionTerminateMode.CANCEL_PENDING,
        )
    finally:
        execution_registry._clear_execution_task_state(execution_id)

    assert transitioned is True
    assert updated_state is not None
    assert updated_state.status == ExecutionTaskStatus.TERMINATING
    assert updated_state.cancel_reason == "manual stop"
    assert updated_state.terminate_mode == ExecutionTerminateMode.CANCEL_PENDING
    assert cancel_event.is_set()


def test_request_execution_termination_marks_queued_task_terminated() -> None:
    """排队中的任务收到终止请求后应直接切到 TERMINATED。"""
    execution_id = "exec-queued-terminate-1"
    state = execution_registry.ExecutionTaskState(
        execution_id=execution_id,
        account_id=1,
        execution_kind=ExecutionKind.REBALANCE,
        status=ExecutionTaskStatus.QUEUED,
        created_at="2026-03-12T13:00:00",
    )
    execution_registry.set_execution_task_state(execution_id, state)

    try:
        updated_state, transitioned = execution_registry.request_execution_termination(
            execution_id,
            reason="manual stop",
            mode=ExecutionTerminateMode.GRACEFUL,
        )
    finally:
        execution_registry._clear_execution_task_state(execution_id)

    assert transitioned is True
    assert updated_state is not None
    assert updated_state.status == ExecutionTaskStatus.TERMINATED
    assert updated_state.cancel_reason == "manual stop"
    assert updated_state.terminate_mode == ExecutionTerminateMode.GRACEFUL
    assert updated_state.finished_at is not None


def test_registry_helper_functions_cover_status_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """状态机辅助函数应覆盖序列化与枚举收敛。"""
    monkeypatch.setattr(execution_registry, "now_str", lambda: "2026-03-22T18:00:00")

    queued_state = execution_registry.ExecutionTaskState(
        execution_id="queued-exec",
        account_id=1,
        execution_kind=ExecutionKind.REBALANCE,
        status=ExecutionTaskStatus.QUEUED,
        created_at="2026-03-22T17:59:00",
    )
    execution_registry.set_execution_task_state("queued-exec", queued_state)

    try:
        transitioned = execution_registry.transition_execution_task_to_running("queued-exec")
        assert transitioned is not None
        assert transitioned.status == ExecutionTaskStatus.RUNNING
        assert transitioned.started_at == "2026-03-22T18:00:00"
        assert execution_registry._serialize_execution_task_state(transitioned) == {
            "execution_id": "queued-exec",
            "account_id": 1,
            "execution_kind": ExecutionKind.REBALANCE,
            "status": ExecutionTaskStatus.RUNNING,
            "created_at": "2026-03-22T17:59:00",
            "started_at": "2026-03-22T18:00:00",
            "finished_at": None,
            "error": None,
            # 柔和版保留展示结论三元组。
            "outcome": None,
            "outcome_reason": None,
            "outcome_symbols": [],
            "output_status": None,
            "record_id": None,
            "is_success": None,
            "cancel_requested_at": None,
            "cancel_reason": None,
            "terminate_mode": None,
        }
    finally:
        execution_registry._clear_execution_task_state("queued-exec")

    assert execution_registry._coerce_execution_task_status("RUNNING") == ExecutionTaskStatus.RUNNING
    assert execution_registry._coerce_execution_task_status("bad") is None
    assert execution_registry._coerce_execution_kind("clear_positions") == ExecutionKind.CLEAR_POSITIONS
    assert execution_registry._coerce_execution_kind("bad") is None
    assert (
        execution_registry._coerce_execution_terminate_mode("cancel_pending") == ExecutionTerminateMode.CANCEL_PENDING
    )
    assert execution_registry._coerce_execution_terminate_mode("bad") is None

    record = ExecuteRecord(
        execution_id="persisted-exec",
        account_id=1,
        task_status="FAILED",
        is_success=0,
        created_at="2026-03-22T17:00:00",
        id=501,
    )
    assert execution_registry._build_persisted_execution_status(record) == {
        "execution_id": "persisted-exec",
        "account_id": 1,
        "execution_kind": None,
        "status": ExecutionTaskStatus.FAILED,
        "created_at": "2026-03-22T17:00:00",
        "started_at": None,
        "finished_at": "2026-03-22T17:00:00",
        "error": None,
        "output_status": None,
        # 柔和版保留展示结论三元组。
        "outcome": None,
        "outcome_reason": None,
        "outcome_symbols": [],
        "record_id": 501,
        "is_success": 0,
        "cancel_requested_at": None,
        "cancel_reason": None,
        "terminate_mode": None,
    }


def test_terminate_running_account_execution_persists_immediately_terminated_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """排队任务被立即终止时，应同步写 terminate record 与事件。"""

    class _Task:
        def __init__(self) -> None:
            self.cancel_called = False

        def cancel(self) -> None:
            self.cancel_called = True

    task = _Task()
    execution_id = "queued-terminate-inline"
    state = execution_registry.ExecutionTaskState(
        execution_id=execution_id,
        account_id=1,
        execution_kind=ExecutionKind.REBALANCE,
        status=ExecutionTaskStatus.QUEUED,
        created_at="2026-03-22T18:00:00",
        channel=TradeChannel.CTP,
        algorithm="SINGLE-MAKER",
        task=task,
    )
    execution_registry.set_execution_task_state(execution_id, state)
    with execution_registry._running_account_executions_lock:
        execution_registry._running_account_executions[1] = execution_id

    captured_events: list[ExecutionEventType] = []

    async def fake_append_execution_event(**kwargs: object) -> bool:
        captured_events.append(cast(ExecutionEventType, kwargs["event_type"]))
        return True

    async def fake_append_terminated_execute_record(**kwargs: object) -> SimpleNamespace:
        assert kwargs["execution_id"] == execution_id
        assert kwargs["cancel_attempted"] is False
        return SimpleNamespace(id=888, is_success=0)

    monkeypatch.setattr(execution_registry, "append_execution_event", fake_append_execution_event)
    monkeypatch.setattr(execution_registry, "append_terminated_execute_record", fake_append_terminated_execute_record)
    monkeypatch.setattr(execution_registry, "now_str", lambda: "2026-03-22T18:01:00")

    try:
        updated_state = asyncio.run(
            execution_registry.terminate_running_account_execution(
                1,
                reason="manual stop",
                mode=ExecutionTerminateMode.CANCEL_PENDING,
            )
        )
        assert updated_state is not None
        assert updated_state.status == ExecutionTaskStatus.TERMINATED
        assert updated_state.record_id == 888
        assert updated_state.is_success == 0
        assert updated_state.cancel_reason == "manual stop"
        assert updated_state.terminate_mode == ExecutionTerminateMode.CANCEL_PENDING
        assert task.cancel_called is True
        assert captured_events == [
            ExecutionEventType.EXECUTION_TERMINATION_REQUESTED,
            ExecutionEventType.EXECUTION_TERMINATED,
        ]
    finally:
        execution_registry._clear_execution_task_state(execution_id)
        execution_registry.clear_running_execution(1, execution_id)


def test_request_execution_termination_is_idempotent_when_already_terminating() -> None:
    """对已 TERMINATING 的任务重复请求应幂等：不再迁移、不再重复触发 cancel。"""
    execution_id = "exec-terminate-idempotent-1"
    cancel_event = Event()
    state = execution_registry.ExecutionTaskState(
        execution_id=execution_id,
        account_id=1,
        execution_kind=ExecutionKind.REBALANCE,
        status=ExecutionTaskStatus.RUNNING,
        created_at="2026-03-12T13:00:00",
        cancel_event=cancel_event,
    )
    execution_registry.set_execution_task_state(execution_id, state)

    try:
        first_state, first_transitioned = execution_registry.request_execution_termination(
            execution_id,
            reason="manual stop",
            mode=ExecutionTerminateMode.CANCEL_PENDING,
        )
        # 清掉事件，验证二次调用不会再度 set。
        cancel_event.clear()
        second_state, second_transitioned = execution_registry.request_execution_termination(
            execution_id,
            reason="second stop",
            mode=ExecutionTerminateMode.GRACEFUL,
        )
    finally:
        execution_registry._clear_execution_task_state(execution_id)

    assert first_transitioned is True
    assert first_state is not None
    assert first_state.status == ExecutionTaskStatus.TERMINATING
    # 二次调用幂等 no-op：不迁移、reason/mode 不被后来者覆盖、不再触发 cancel_event。
    assert second_transitioned is False
    assert second_state is not None
    assert second_state.status == ExecutionTaskStatus.TERMINATING
    assert second_state.cancel_reason == "manual stop"
    assert second_state.terminate_mode == ExecutionTerminateMode.CANCEL_PENDING
    assert cancel_event.is_set() is False


def test_terminate_running_account_execution_idempotent_for_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连点终止 QUEUED 任务：仅一轮受理+终止事件、terminate record 只写一次（不二次 finalize）。"""
    execution_id = "queued-terminate-connt"
    state = execution_registry.ExecutionTaskState(
        execution_id=execution_id,
        account_id=1,
        execution_kind=ExecutionKind.REBALANCE,
        status=ExecutionTaskStatus.QUEUED,
        created_at="2026-03-22T18:00:00",
        channel=TradeChannel.CTP,
        algorithm="SINGLE-MAKER",
    )
    execution_registry.set_execution_task_state(execution_id, state)
    with execution_registry._running_account_executions_lock:
        execution_registry._running_account_executions[1] = execution_id

    captured_events: list[ExecutionEventType] = []
    record_append_calls = 0

    async def fake_append_execution_event(**kwargs: object) -> bool:
        captured_events.append(cast(ExecutionEventType, kwargs["event_type"]))
        return True

    async def fake_append_terminated_execute_record(**_: object) -> SimpleNamespace:
        nonlocal record_append_calls
        record_append_calls += 1
        return SimpleNamespace(id=888, is_success=0)

    monkeypatch.setattr(execution_registry, "append_execution_event", fake_append_execution_event)
    monkeypatch.setattr(execution_registry, "append_terminated_execute_record", fake_append_terminated_execute_record)
    monkeypatch.setattr(execution_registry, "now_str", lambda: "2026-03-22T18:01:00")

    async def _terminate_twice() -> None:
        await execution_registry.terminate_running_account_execution(
            1, reason="manual stop", mode=ExecutionTerminateMode.CANCEL_PENDING
        )
        # 账户仍登记为 running（finalize 不清 running），二次调用命中幂等 no-op。
        await execution_registry.terminate_running_account_execution(
            1, reason="manual stop", mode=ExecutionTerminateMode.CANCEL_PENDING
        )

    try:
        asyncio.run(_terminate_twice())
        assert captured_events == [
            ExecutionEventType.EXECUTION_TERMINATION_REQUESTED,
            ExecutionEventType.EXECUTION_TERMINATED,
        ]
        assert record_append_calls == 1
    finally:
        execution_registry._clear_execution_task_state(execution_id)
        execution_registry.clear_running_execution(1, execution_id)


def test_terminate_running_account_execution_idempotent_for_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连点终止 RUNNING 任务：仅一条受理事件，二次调用为幂等 no-op、不再追加。"""
    execution_id = "running-terminate-connt"
    cancel_event = Event()
    state = execution_registry.ExecutionTaskState(
        execution_id=execution_id,
        account_id=1,
        execution_kind=ExecutionKind.REBALANCE,
        status=ExecutionTaskStatus.RUNNING,
        created_at="2026-03-22T18:00:00",
        started_at="2026-03-22T18:00:01",
        channel=TradeChannel.CTP,
        algorithm="SINGLE-MAKER",
        cancel_event=cancel_event,
    )
    execution_registry.set_execution_task_state(execution_id, state)
    with execution_registry._running_account_executions_lock:
        execution_registry._running_account_executions[1] = execution_id

    captured_events: list[ExecutionEventType] = []

    async def fake_append_execution_event(**kwargs: object) -> bool:
        captured_events.append(cast(ExecutionEventType, kwargs["event_type"]))
        return True

    monkeypatch.setattr(execution_registry, "append_execution_event", fake_append_execution_event)
    monkeypatch.setattr(execution_registry, "now_str", lambda: "2026-03-22T18:01:00")

    async def _terminate_twice() -> execution_registry.ExecutionTaskState | None:
        await execution_registry.terminate_running_account_execution(
            1, reason="manual stop", mode=ExecutionTerminateMode.CANCEL_PENDING
        )
        return await execution_registry.terminate_running_account_execution(
            1, reason="manual stop", mode=ExecutionTerminateMode.CANCEL_PENDING
        )

    try:
        second_state = asyncio.run(_terminate_twice())
        # 首次迁移到 TERMINATING 写一条受理；二次幂等 no-op 不再追加。
        assert captured_events == [ExecutionEventType.EXECUTION_TERMINATION_REQUESTED]
        assert second_state is not None
        assert second_state.status == ExecutionTaskStatus.TERMINATING
    finally:
        execution_registry._clear_execution_task_state(execution_id)
        execution_registry.clear_running_execution(1, execution_id)


def test_finalize_execution_task_state_drops_refs_and_defers_evict() -> None:
    """终态任务应立即释放 task/cancel_event 重引用，并在有事件循环时延迟淘汰条目。"""
    execution_id = "exec-finalize-terminal"
    state = execution_registry.ExecutionTaskState(
        execution_id=execution_id,
        account_id=7,
        execution_kind=ExecutionKind.REBALANCE,
        status=ExecutionTaskStatus.SUCCEEDED,
        created_at="2026-03-12T13:00:00",
        task=cast("asyncio.Task[object]", object()),
        cancel_event=Event(),
    )

    async def _run() -> None:
        execution_registry.set_execution_task_state(execution_id, state)
        execution_registry.finalize_execution_task_state(execution_id)

    try:
        asyncio.run(_run())
        retained = execution_registry.get_execution_task_state(execution_id)
        # 有运行循环时走延迟淘汰，条目仍在，但重引用已被立即抛弃。
        assert retained is not None
        assert retained.task is None
        assert retained.cancel_event is None
    finally:
        execution_registry._clear_execution_task_state(execution_id)


def test_finalize_execution_task_state_evicts_immediately_without_loop() -> None:
    """无事件循环时，终态任务状态应被立即淘汰以保证内存有界。"""
    execution_id = "exec-finalize-noloop"
    state = execution_registry.ExecutionTaskState(
        execution_id=execution_id,
        account_id=7,
        execution_kind=ExecutionKind.CLEAR_POSITIONS,
        status=ExecutionTaskStatus.FAILED,
        created_at="2026-03-12T13:00:00",
    )
    execution_registry.set_execution_task_state(execution_id, state)

    try:
        execution_registry.finalize_execution_task_state(execution_id)
        assert execution_registry.get_execution_task_state(execution_id) is None
    finally:
        execution_registry._clear_execution_task_state(execution_id)


def test_finalize_execution_task_state_ignores_non_terminal() -> None:
    """非终态任务不应被 finalize 释放重引用或淘汰，以免打断进行中的状态查询与终止控制。"""
    execution_id = "exec-finalize-running"
    event = Event()
    state = execution_registry.ExecutionTaskState(
        execution_id=execution_id,
        account_id=7,
        execution_kind=ExecutionKind.REBALANCE,
        status=ExecutionTaskStatus.RUNNING,
        created_at="2026-03-12T13:00:00",
        task=cast("asyncio.Task[object]", object()),
        cancel_event=event,
    )
    execution_registry.set_execution_task_state(execution_id, state)

    try:
        execution_registry.finalize_execution_task_state(execution_id)
        retained = execution_registry.get_execution_task_state(execution_id)
        assert retained is not None
        assert retained.cancel_event is event
        assert retained.task is not None
    finally:
        execution_registry._clear_execution_task_state(execution_id)


@pytest.mark.parametrize(
    ("output_status", "expected_error"),
    [
        ("BLOCKED", "5 个品种因交易时段不可执行"),
        ("FAILED", "调仓执行失败"),
        ("BLOCKED", "当前不在交易时间"),
    ],
)
def test_persisted_execution_status_reads_output_error_and_status(
    output_status: str,
    expected_error: str,
) -> None:
    """回放直接读取已迁移的执行器状态和错误。"""
    record = ExecuteRecord(
        execution_id="exec-blocked-1",
        account_id=1,
        status=output_status,
        error=expected_error,
        display_status=output_status,
        display_error=expected_error,
        task_status="FAILED",
        is_success=0,
        created_at="2026-08-26T20:34:30",
        id=6,
    )
    payload = execution_registry._build_persisted_execution_status(record)
    assert payload["error"] == expected_error
    assert payload["output_status"] == output_status


def test_execution_record_output_error_reads_typed_field() -> None:
    """展示直接读取 error，其他过程文案不覆盖它。"""
    record = ExecuteRecord(
        account_id=1,
        is_success=0,
        error="5 个品种因交易时段不可执行",
        msg="执行未成功完成",
        status="BLOCKED",
        display_status="BLOCKED",
        display_error="5 个品种因交易时段不可执行",
    )
    assert execution_registry.execution_record_output_error(record) == "5 个品种因交易时段不可执行"
    assert execution_registry.execution_record_output_status(record) == "BLOCKED"


def test_persisted_status_keeps_task_output_and_outcome_separate() -> None:
    """任务休市跳过、执行器受阻和业务展示结论分别保留。"""
    record = ExecuteRecord(
        id=42,
        account_id=1,
        execution_id="three-status-domains",
        is_success=0,
        task_status="SKIPPED",
        status="BLOCKED",
        outcome="blocked",
        outcome_reason="非交易时段",
        error="非交易时段",
        display_status="BLOCKED",
        display_outcome="blocked",
        display_outcome_reason="非交易时段",
        display_error="非交易时段",
    )
    payload = execution_registry._build_persisted_execution_status(record, outcome_symbols=["second", "first"])
    assert payload["status"] == ExecutionTaskStatus.SKIPPED
    assert payload["output_status"] == "BLOCKED"
    assert payload["outcome"] == "blocked"
    assert payload["outcome_symbols"] == ["second", "first"]
    assert not hasattr(record, "raw_result")


def test_status_uses_persisted_display_fields_without_exposing_technical_error() -> None:
    """状态读取保留已冻结的展示语义，原始异常文案只留在证据列。"""
    record = ExecuteRecord(
        account_id=1,
        is_success=0,
        task_status="FAILED",
        status="FAILED",
        error="RuntimeError: channel query failed",
        display_status="FAILED",
        display_error="执行失败，具体原因见执行证据",
        display_outcome="error",
    )
    assert execution_registry.execution_record_output_error(record) == "执行失败，具体原因见执行证据"
    assert execution_registry.execution_record_outcome(record)["outcome"] == "error"
    assert record.error == "RuntimeError: channel query failed"


def test_terminal_intent_does_not_hide_persisted_outcome(monkeypatch):
    """重启后 intent 仍存在，也应取落库的明确结论。"""
    from axile.server.execution import intents

    async def get_intent(_execution_id):
        return SimpleNamespace(status=ExecutionTaskStatus.FAILED)

    record = ExecuteRecord(
        id=1000,
        execution_id="restart-shortfall",
        account_id=1,
        is_success=0,
        created_at="2026-09-14T14:00:00",
        status="PARTIAL",
        error="目标尚未到位",
        outcome="not_reached",
        task_status="FAILED",
        display_status="PARTIAL",
        display_error="目标尚未到位",
        display_outcome="not_reached",
    )
    monkeypatch.setattr(intents, "get_intent", get_intent)
    _persisted_record(monkeypatch, record)
    payload = asyncio.run(execution_registry.get_execution_status(record.execution_id))
    assert payload["record_id"] == 1000
    assert payload["output_status"] == "PARTIAL"
    assert payload["error"] == "目标尚未到位"
    assert payload["outcome"] == "not_reached"


def test_partial_session_replay_preserves_later_task_exception(monkeypatch):
    from axile.server.execution import intents

    async def get_intent(_execution_id):
        return SimpleNamespace(status=ExecutionTaskStatus.FAILED, error="收尾异常")

    record = ExecuteRecord(
        id=1001,
        execution_id="partial-worker-error",
        account_id=1,
        is_success=0,
        created_at="2026-10-17T01:00:00",
        status="PARTIAL",
        task_status="PARTIAL",
        outcome="not_reached",
        error="非交易时段",
        display_status="PARTIAL",
        display_error="非交易时段",
        display_outcome="not_reached",
    )
    monkeypatch.setattr(intents, "get_intent", get_intent)
    _persisted_record(monkeypatch, record)
    payload = asyncio.run(execution_registry.get_execution_status(record.execution_id))
    assert payload["status"] == ExecutionTaskStatus.FAILED
    assert payload["error"] == "收尾异常"
    assert payload["output_status"] == "FAILED"
    assert payload["is_success"] == 0
