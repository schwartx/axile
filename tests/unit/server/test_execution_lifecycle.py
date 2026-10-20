"""执行生命周期重构后的回归测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionKind, ExecutionTaskStatus, ExecutionTerminateMode
from axile.executor.termination import ExecutionTerminated
from axile.server.db.models import ExecuteRecord
from axile.server.execution import lifecycle as execution_lifecycle
from axile.server.execution import registry as execution_registry
from axile.server.execution.intents import IntentSnapshot


@pytest.fixture(autouse=True)
def _isolate_outcome_symbol_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """生命周期编排用例的品种名单由定向读取桩提供。"""

    async def read_symbols(_record: object) -> list[str]:
        return []

    monkeypatch.setattr(execution_lifecycle, "load_record_outcome_symbols", read_symbols)


def test_handle_inline_execution_terminated_persists_record_and_updates_state(monkeypatch) -> None:
    """内联 terminate 应复用统一收尾逻辑，并同步更新状态与事件。"""
    execution_id = "exec-inline-terminated-1"
    state = execution_registry.ExecutionTaskState(
        execution_id=execution_id,
        account_id=1,
        execution_kind=ExecutionKind.REBALANCE,
        status=ExecutionTaskStatus.RUNNING,
        created_at="2026-03-27T10:00:00",
        cancel_requested_at="2026-03-27T10:00:01",
        channel=TradeChannel.CTP,
        algorithm="SINGLE-MAKER",
    )
    captured: dict[str, object] = {"events": []}
    execution_registry.set_execution_task_state(execution_id, state)

    async def fake_append_terminated_execute_record(**kwargs: object) -> SimpleNamespace:
        captured["record_kwargs"] = kwargs
        return SimpleNamespace(id=321, is_success=0)

    async def fake_append_execution_event(**kwargs: object) -> None:
        events = captured["events"]
        assert isinstance(events, list)
        events.append(kwargs)

    monkeypatch.setattr(execution_lifecycle, "append_terminated_execute_record", fake_append_terminated_execute_record)
    monkeypatch.setattr(execution_lifecycle, "append_execution_event", fake_append_execution_event)
    monkeypatch.setattr(execution_lifecycle, "now_str", lambda: "2026-03-27T10:00:05")

    try:
        record = asyncio.run(
            execution_lifecycle.handle_inline_execution_terminated(
                account_id=1,
                execution_id=execution_id,
                execution_kind=ExecutionKind.REBALANCE,
                exc=ExecutionTerminated(
                    reason="manual stop",
                    mode=ExecutionTerminateMode.GRACEFUL.value,
                    acked_at="2026-03-27T10:00:03",
                    cancel_failed_order_ids=["order-1"],
                ),
            )
        )
        current_state = execution_registry.get_execution_task_state(execution_id)
    finally:
        execution_registry._clear_execution_task_state(execution_id)

    assert record.id == 321
    assert captured["record_kwargs"] == {
        "account_id": 1,
        "execution_id": execution_id,
        "execution_kind": ExecutionKind.REBALANCE,
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
                    "execution_kind": ExecutionKind.REBALANCE.value,
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


def test_run_execution_task_uses_state_termination_fallbacks(monkeypatch) -> None:
    """后台 terminate 分支应回退到内存状态中的 reason/mode。"""
    execution_id = "exec-queued-terminated-1"
    state = execution_registry.ExecutionTaskState(
        execution_id=execution_id,
        account_id=1,
        execution_kind=ExecutionKind.REBALANCE,
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

    async def fake_append_terminated_execute_record(**kwargs: object) -> SimpleNamespace:
        captured["record_kwargs"] = kwargs
        return SimpleNamespace(id=654, is_success=0)

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
                execution_kind=ExecutionKind.REBALANCE,
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
        "execution_kind": ExecutionKind.REBALANCE,
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
                    "execution_kind": ExecutionKind.REBALANCE.value,
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


class _FakeAccountSession:
    """伪造账户查询会话，供入队路径读取账户对象。"""

    def __init__(self, account: object) -> None:
        self._account = account

    async def __aenter__(self) -> "_FakeAccountSession":
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def get(self, _model: object, _account_id: object) -> object:
        return self._account


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


def test_run_execution_task_persists_intent_on_success(monkeypatch) -> None:
    """后台任务成功后应把 intent 标终态。"""
    execution_id = "exec-intent-finish-1"
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
    marked: dict[str, object] = {}

    async def fake_mark(execution_id: str, status: ExecutionTaskStatus, **kwargs: object) -> None:
        marked["execution_id"] = execution_id
        marked["status"] = status
        marked.update(kwargs)

    async def fake_runner(_tracked: str) -> SimpleNamespace:
        return ExecuteRecord(
            id=9, account_id=1, is_success=1, status="SUCCEEDED", display_status="SUCCEEDED", task_status="SUCCEEDED"
        )

    monkeypatch.setattr(execution_lifecycle, "mark_intent_finished", fake_mark)
    monkeypatch.setattr(execution_lifecycle, "sync_account_live", lambda *_a, **_k: None)
    monkeypatch.setattr(execution_lifecycle, "now_str", lambda: "2026-03-27T12:00:05")

    try:
        asyncio.run(
            execution_lifecycle._run_execution_task(
                account_id=1,
                execution_id=execution_id,
                execution_kind=ExecutionKind.REBALANCE,
                runner=fake_runner,
            )
        )
    finally:
        execution_registry._clear_execution_task_state(execution_id)

    assert marked["execution_id"] == execution_id
    assert marked["status"] == ExecutionTaskStatus.SUCCEEDED


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
    marked: dict[str, object] = {}

    async def fake_get_intent(eid: str) -> IntentSnapshot:
        assert eid == execution_id
        return snapshot

    async def fake_runner(tracked: str) -> SimpleNamespace:
        ran["id"] = tracked
        assert execution_registry.get_execution_task_state(tracked) is not None
        return ExecuteRecord(
            id=1, account_id=1, is_success=1, status="SUCCEEDED", display_status="SUCCEEDED", task_status="SUCCEEDED"
        )

    async def fake_mark(eid: str, status: ExecutionTaskStatus, **kwargs: object) -> None:
        marked["execution_id"] = eid
        marked["status"] = status
        marked.update(kwargs)

    monkeypatch.setattr(execution_lifecycle, "get_intent", fake_get_intent)
    monkeypatch.setattr(execution_lifecycle, "mark_intent_finished", fake_mark)
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
        assert marked["status"] == ExecutionTaskStatus.SUCCEEDED
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


def test_timeout_termination_is_distinguishable_from_operator(monkeypatch) -> None:
    """总超时终止必须在落库与审计里与人工终止区分开（trigger=timeout）。"""
    execution_id = "exec-inline-terminated-timeout"
    state = execution_registry.ExecutionTaskState(
        execution_id=execution_id,
        account_id=1,
        execution_kind=ExecutionKind.REBALANCE,
        status=ExecutionTaskStatus.RUNNING,
        created_at="2026-07-27T10:00:00",
        channel=TradeChannel.CTP,
        algorithm="SINGLE-MAKER",
    )
    captured: dict[str, object] = {"events": []}
    execution_registry.set_execution_task_state(execution_id, state)

    async def fake_append_terminated_execute_record(**kwargs: object) -> SimpleNamespace:
        captured["record_kwargs"] = kwargs
        return SimpleNamespace(id=777, is_success=0)

    async def fake_append_execution_event(**kwargs: object) -> None:
        events = captured["events"]
        assert isinstance(events, list)
        events.append(kwargs)

    monkeypatch.setattr(execution_lifecycle, "append_terminated_execute_record", fake_append_terminated_execute_record)
    monkeypatch.setattr(execution_lifecycle, "append_execution_event", fake_append_execution_event)
    monkeypatch.setattr(execution_lifecycle, "now_str", lambda: "2026-07-27T10:03:00")

    try:
        asyncio.run(
            execution_lifecycle.handle_inline_execution_terminated(
                account_id=1,
                execution_id=execution_id,
                execution_kind=ExecutionKind.REBALANCE,
                exc=ExecutionTerminated(
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


def test_timeout_termination_sends_alert(monkeypatch) -> None:
    """总超时终止必须告警：它是「没人知道」的那种终止，静默等于兜底白装。"""
    from axile.server.db.models import Account

    execution_id = "exec-timeout-alert"
    execution_registry.set_execution_task_state(
        execution_id,
        execution_registry.ExecutionTaskState(
            execution_id=execution_id,
            account_id=1,
            execution_kind=ExecutionKind.REBALANCE,
            status=ExecutionTaskStatus.RUNNING,
            created_at="2026-07-27T10:00:00",
            channel=TradeChannel.CTP,
            algorithm="SINGLE-MAKER",
        ),
    )
    sent: list[str] = []

    async def fake_append_terminated_execute_record(**_: object) -> SimpleNamespace:
        return SimpleNamespace(id=888, is_success=0)

    async def fake_append_execution_event(**_: object) -> None:
        return None

    async def fake_send_feishu_error(error: Exception, _account: object, _key: object, **_kwargs: object) -> None:
        sent.append(str(error))

    class _FakeSession:
        async def __aenter__(self) -> "_FakeSession":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, _model: object, _pk: object) -> object:
            return Account(
                id=1,
                name="acc-1",
                market="期货",
                trade_channel="ctp",
                brokerage="test",
                account_config={},
                account_control_preset="default",
                is_started=False,
                cron_expr="",
                weight_precision=0.01,
                algorithm={"method": "SINGLE-MAKER"},
                feishu_key=None,
            )

    monkeypatch.setattr(execution_lifecycle, "append_terminated_execute_record", fake_append_terminated_execute_record)
    monkeypatch.setattr(execution_lifecycle, "append_execution_event", fake_append_execution_event)
    monkeypatch.setattr(execution_lifecycle, "send_feishu_error", fake_send_feishu_error)
    monkeypatch.setattr(execution_lifecycle, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(execution_lifecycle, "now_str", lambda: "2026-07-27T10:03:00")

    def _run_termination(trigger: str) -> None:
        asyncio.run(
            execution_lifecycle.handle_inline_execution_terminated(
                account_id=1,
                execution_id=execution_id,
                execution_kind=ExecutionKind.REBALANCE,
                exc=ExecutionTerminated(
                    reason="执行总超时（180s）" if trigger == "timeout" else "manual stop",
                    mode=ExecutionTerminateMode.CANCEL_PENDING.value,
                    trigger=trigger,
                ),
            )
        )

    try:
        _run_termination("timeout")
        assert len(sent) == 1
        assert "总超时" in sent[0]

        # 人工终止不该再打扰运维——发起的人本来就知道。
        _run_termination("operator")
        assert len(sent) == 1
    finally:
        execution_registry._clear_execution_task_state(execution_id)


def test_mark_inline_execution_copies_blocked_output_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """业务失败没有异常，完成态也必须把 output.error / status 写进轮询载荷。"""
    execution_id = "exec-blocked-inline-1"
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

    try:
        asyncio.run(execution_lifecycle.mark_inline_execution_succeeded(execution_id, record))
        current = execution_registry.get_execution_task_state(execution_id)
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


def test_mark_execution_finished_copies_record_error_unless_exception() -> None:
    """worker 收尾：有记录时拷贝 output；有异常时异常字符串优先。"""
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
            status=ExecutionTaskStatus.FAILED,
            finished_at="2026-08-26T20:34:30",
            record=record,
        )
        blocked = execution_registry.get_execution_task_state(execution_id)
        assert blocked is not None
        assert blocked.error == "当前不在交易时间"
        assert blocked.output_status == "BLOCKED"

        execution_lifecycle._mark_execution_finished(
            execution_id,
            status=ExecutionTaskStatus.FAILED,
            finished_at="2026-08-26T20:34:31",
            error=RuntimeError("worker exploded"),
        )
        failed = execution_registry.get_execution_task_state(execution_id)
        assert failed is not None
        assert failed.error == "worker exploded"
        assert failed.output_status == "FAILED"
    finally:
        execution_registry._clear_execution_task_state(execution_id)
