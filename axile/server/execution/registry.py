"""执行任务内存状态与终止控制."""

import asyncio
from dataclasses import dataclass, field
from threading import Event, Lock
from typing import cast

import loguru
from sqlmodel import desc, select

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import (
    ExecutionEventStatus,
    ExecutionEventType,
    ExecutionKind,
    ExecutionReasonFamily,
    ExecutionTaskStatus,
    ExecutionTerminateMode,
)
from axile.executor.termination import ExecutionTerminationController
from axile.server.core.db import SessionLocal
from axile.server.db.models import (
    ExecuteRecord,
    new_execution_id,
    now_str,
)
from axile.server.db.models.account import AccountContext
from axile.server.execution.execution_summaries import build_execution_outcome_details
from axile.server.execution.legacy_compat import normalize_legacy_result
from axile.server.execution.records import append_terminated_execute_record, resolve_record_task_status
from axile.server.execution_audit import append_execution_event


@dataclass
class ExecutionTaskState:
    """异步运行中的执行任务在内存中的状态快照."""

    execution_id: str
    account_id: int
    execution_kind: ExecutionKind
    status: ExecutionTaskStatus
    created_at: str
    channel: TradeChannel | None = None
    algorithm: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    output_status: str | None = None
    outcome: str | None = None
    outcome_reason: str | None = None
    outcome_symbols: list[str] = field(default_factory=list)
    record_id: int | None = None
    is_success: int | None = None
    task: asyncio.Task[object] | None = None
    cancel_event: Event | None = None
    cancel_requested_at: str | None = None
    cancel_reason: str | None = None
    terminate_mode: ExecutionTerminateMode | None = None


class AccountExecutionAlreadyRunningError(RuntimeError):
    """同一账户已在执行交易任务时抛出的异常."""


_running_account_executions: dict[int, str] = {}
_account_queued_executions: dict[int, str] = {}
_running_account_asset_refreshes: set[int] = set()
_running_account_target_refreshes: set[int] = set()
_running_portfolio_target_refreshes: set[int] = set()
_running_account_executions_lock = Lock()
_execution_tasks: dict[str, ExecutionTaskState] = {}
_execution_tasks_lock = Lock()

_TERMINAL_EXECUTION_TASK_STATES: frozenset[ExecutionTaskStatus] = frozenset(
    {
        ExecutionTaskStatus.SUCCEEDED,
        ExecutionTaskStatus.FAILED,
        ExecutionTaskStatus.TERMINATED,
        ExecutionTaskStatus.SKIPPED,
        ExecutionTaskStatus.PARTIAL,
    }
)
"""被视为终态、可释放重引用并安排淘汰的任务状态集合。."""

_EXECUTION_TASK_EVICT_DELAY_SEC = 300.0
"""任务进入终态后，内存状态在被淘汰前额外保留的秒数（供刚完成即轮询的快路径）。."""


def try_register_running_execution(account_id: int, execution_id: str) -> bool:
    """
    尝试为账户登记一个运行中的 execution.

    Parameters
    ----------
    account_id : int
        目标账户 ID。
    execution_id : str
        待登记的 execution ID。

    Returns
    -------
    bool
        登记成功返回 ``True``；若账户已有运行任务则返回 ``False``。
    """
    with _running_account_executions_lock:
        if (
            account_id in _running_account_executions
            or account_id in _running_account_asset_refreshes
            or account_id in _running_account_target_refreshes
        ):
            return False
        _running_account_executions[account_id] = execution_id
        return True


def _get_running_execution_id(account_id: int) -> str | None:
    with _running_account_executions_lock:
        return _running_account_executions.get(account_id)


def set_queued_execution(account_id: int, execution_id: str) -> None:
    """登记账户当前 QUEUED intent（不占渠道锁）."""
    with _running_account_executions_lock:
        _account_queued_executions[account_id] = execution_id


def clear_queued_execution(account_id: int, execution_id: str) -> None:
    """清除账户 QUEUED 槽位（仅当 execution_id 匹配）."""
    with _running_account_executions_lock:
        if _account_queued_executions.get(account_id) == execution_id:
            del _account_queued_executions[account_id]


def get_queued_execution_id(account_id: int) -> str | None:
    """读取账户当前 QUEUED intent id."""
    with _running_account_executions_lock:
        return _account_queued_executions.get(account_id)


def iter_running_account_executions() -> list[tuple[int, str]]:
    """返回当前占用渠道锁的 (account_id, execution_id) 列表."""
    with _running_account_executions_lock:
        return list(_running_account_executions.items())


def clear_running_execution(account_id: int, execution_id: str) -> None:
    """
    清除账户当前登记的运行中 execution.

    Parameters
    ----------
    account_id : int
        目标账户 ID。
    execution_id : str
        仅当与当前登记值一致时才会被清除的 execution ID。

    Returns
    -------
    None
        该函数仅更新内存状态，不返回结果。
    """
    with _running_account_executions_lock:
        current_execution_id = _running_account_executions.get(account_id)
        if current_execution_id == execution_id:
            del _running_account_executions[account_id]


def try_register_account_asset_refresh(account_id: int) -> bool:
    """尝试占用账户的资产刷新槽位，并与交易执行互斥."""
    with _running_account_executions_lock:
        if (
            account_id in _running_account_executions
            or account_id in _running_account_asset_refreshes
            or account_id in _running_account_target_refreshes
        ):
            return False
        _running_account_asset_refreshes.add(account_id)
        return True


def clear_account_asset_refresh(account_id: int) -> None:
    """释放账户的资产刷新槽位."""
    with _running_account_executions_lock:
        _running_account_asset_refreshes.discard(account_id)


def try_register_target_refresh(portfolio_id: int, account_id: int | None) -> bool:
    """尝试占用目标计算槽，并与同组合刷新及同账户操作互斥."""
    with _running_account_executions_lock:
        if portfolio_id in _running_portfolio_target_refreshes:
            return False
        if account_id is not None and (
            account_id in _running_account_executions
            or account_id in _running_account_asset_refreshes
            or account_id in _running_account_target_refreshes
        ):
            return False
        _running_portfolio_target_refreshes.add(portfolio_id)
        if account_id is not None:
            _running_account_target_refreshes.add(account_id)
        return True


def clear_target_refresh(portfolio_id: int, account_id: int | None) -> None:
    """释放目标计算占用槽."""
    with _running_account_executions_lock:
        _running_portfolio_target_refreshes.discard(portfolio_id)
        if account_id is not None:
            _running_account_target_refreshes.discard(account_id)


def set_execution_task_state(execution_id: str, state: ExecutionTaskState) -> None:
    """
    覆盖写入某个 execution 的内存状态.

    Parameters
    ----------
    execution_id : str
        目标 execution ID。
    state : ExecutionTaskState
        待写入的任务状态对象。

    Returns
    -------
    None
        该函数仅更新内存状态，不返回结果。
    """
    with _execution_tasks_lock:
        _execution_tasks[execution_id] = state


def _clear_execution_task_state(execution_id: str) -> None:
    with _execution_tasks_lock:
        _execution_tasks.pop(execution_id, None)


def _evict_terminal_execution_task_state(execution_id: str) -> None:
    """仅在任务仍处于终态时，从内存注册表移除其状态条目。"""
    with _execution_tasks_lock:
        state = _execution_tasks.get(execution_id)
        if state is not None and state.status in _TERMINAL_EXECUTION_TASK_STATES:
            del _execution_tasks[execution_id]


def _schedule_execution_task_eviction(execution_id: str) -> None:
    """在事件循环上安排终态任务状态的延迟淘汰；无循环时立即淘汰。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 无运行中的事件循环（同步上下文/测试）时直接淘汰，仍保证有界。
        _evict_terminal_execution_task_state(execution_id)
        return
    loop.call_later(
        _EXECUTION_TASK_EVICT_DELAY_SEC,
        _evict_terminal_execution_task_state,
        execution_id,
    )


def finalize_execution_task_state(execution_id: str) -> None:
    """
    在 execution 进入终态后释放重引用并安排延迟淘汰.

    Parameters
    ----------
    execution_id : str
        目标 execution ID。

    Returns
    -------
    None
        该函数仅更新内存状态与调度淘汰，不返回结果。

    Notes
    -----
    分两层收敛已完成任务的内存占用：

    1. **立即释放重引用**：将 ``task`` 与 ``cancel_event`` 置空。已完成的
       ``asyncio.Task`` 会持有异常 traceback，其帧进而钉住整条执行链的
       局部对象（账户快照、执行输出、runner 闭包等）；序列化状态载荷不使用
       这两个字段，故置空对外部完全不可见。
    2. **延迟淘汰条目**：给「刚完成即轮询」保留一个短窗口后移除条目，避免
       全局注册表随执行次数无界增长；持久化记录仍是状态查询的兜底真源。

    仅对处于终态的任务生效；非终态任务保持不变，避免打断进行中的状态查询
    与终止控制。
    """
    with _execution_tasks_lock:
        state = _execution_tasks.get(execution_id)
        if state is None or state.status not in _TERMINAL_EXECUTION_TASK_STATES:
            return
        state.task = None
        state.cancel_event = None
    _schedule_execution_task_eviction(execution_id)


def get_execution_task_state(execution_id: str) -> ExecutionTaskState | None:
    """
    读取某个 execution 的内存状态.

    Parameters
    ----------
    execution_id : str
        目标 execution ID。

    Returns
    -------
    ExecutionTaskState | None
        命中时返回内存中的任务状态；否则返回 ``None``。
    """
    with _execution_tasks_lock:
        return _execution_tasks.get(execution_id)


def update_execution_task_state(execution_id: str, **changes: object) -> None:
    """
    对某个 execution 的内存状态执行局部更新.

    Parameters
    ----------
    execution_id : str
        目标 execution ID。
    **changes : object
        需要覆盖写入状态对象的字段值。

    Returns
    -------
    None
        该函数仅更新内存状态，不返回结果。
    """
    with _execution_tasks_lock:
        state = _execution_tasks.get(execution_id)
        if state is None:
            return
        for key, value in changes.items():
            setattr(state, key, value)


def _serialize_execution_task_state(state: ExecutionTaskState) -> dict[str, object]:
    """将内存中的 execution task state 序列化为公开状态载荷."""
    return {
        "execution_id": state.execution_id,
        "account_id": state.account_id,
        "execution_kind": state.execution_kind,
        "status": state.status,
        "created_at": state.created_at,
        "started_at": state.started_at,
        "finished_at": state.finished_at,
        "error": state.error,
        "output_status": state.output_status,
        "outcome": state.outcome,
        "outcome_reason": state.outcome_reason,
        "outcome_symbols": state.outcome_symbols,
        "record_id": state.record_id,
        "is_success": state.is_success,
        "cancel_requested_at": state.cancel_requested_at,
        "cancel_reason": state.cancel_reason,
        "terminate_mode": state.terminate_mode,
    }


def _coerce_execution_task_status(value: object) -> ExecutionTaskStatus | None:
    """将持久化值安全收敛为任务生命周期状态."""
    if isinstance(value, ExecutionTaskStatus):
        return value
    if isinstance(value, str):
        try:
            return ExecutionTaskStatus(value)
        except ValueError:
            return None
    return None


def _coerce_execution_kind(value: object) -> ExecutionKind | None:
    """将持久化值安全收敛为 execution 类型."""
    if isinstance(value, ExecutionKind):
        return value
    if isinstance(value, str):
        try:
            return ExecutionKind(value)
        except ValueError:
            return None
    return None


def _coerce_execution_terminate_mode(value: object) -> ExecutionTerminateMode | None:
    """将持久化值安全收敛为 terminate mode."""
    if isinstance(value, ExecutionTerminateMode):
        return value
    if isinstance(value, str):
        try:
            return ExecutionTerminateMode(value)
        except ValueError:
            return None
    return None


def _coerce_optional_text(value: object) -> str | None:
    """把任意值收敛为非空字符串；空白与非字符串视为缺失."""
    if isinstance(value, str) and value.strip():
        return value
    return None


def execution_record_outcome(raw_result: object) -> dict[str, object]:
    """恒定返回展示结论三元组；status/error 由专门入参传递，避免关键字冲突。"""
    raw = raw_result if isinstance(raw_result, dict) else {}
    forwarded = build_execution_outcome_details(raw)
    return {
        "outcome": forwarded.get("outcome"),
        "outcome_reason": forwarded.get("outcome_reason"),
        "outcome_symbols": forwarded.get("outcome_symbols", []),
    }


def execution_record_output_status(raw_result: object) -> str | None:
    """从执行记录结果中读取执行器 ``status``；旧记录经读时归一回填。"""
    normalized = normalize_legacy_result(raw_result)
    if not isinstance(normalized, dict):
        return None
    return _coerce_optional_text(normalized.get("status"))


def execution_record_output_error(raw_result: object) -> str | None:
    """
    从执行记录结果中读取人话错误.

    新记录读 ``error``；旧记录经 :func:`normalize_legacy_result` 读时补齐：
    说人话的遗留文案沿用，异常类名/SDK 原文只进 ``technical_detail``，
    展示层拿固定人话，不再回退扫描 ``msg``/``memory.message``。
    """
    normalized = normalize_legacy_result(raw_result)
    if not isinstance(normalized, dict):
        return None
    return _coerce_optional_text(normalized.get("error"))


def _build_persisted_execution_status(record: ExecuteRecord) -> dict[str, object]:
    """根据执行记录回放任务生命周期状态."""
    normalized_raw = normalize_legacy_result(record.raw_result)
    task_status = _coerce_execution_task_status(
        normalized_raw.get("task_status") if isinstance(normalized_raw, dict) else None
    )
    if task_status is None:
        task_status = resolve_record_task_status(record)

    termination_raw = record.raw_result.get("termination")
    termination = cast("dict[str, object]", termination_raw) if isinstance(termination_raw, dict) else {}

    return {
        "execution_id": cast("str", record.execution_id),
        "account_id": record.account_id,
        "execution_kind": _coerce_execution_kind(record.raw_result.get("execution_kind")),
        "status": task_status,
        "created_at": record.created_at,
        "started_at": None,
        "finished_at": cast("str | None", termination.get("finished_at")) or record.created_at,
        "error": (
            None if task_status == ExecutionTaskStatus.TERMINATED else execution_record_output_error(record.raw_result)
        ),
        "output_status": execution_record_output_status(record.raw_result),
        **execution_record_outcome(record.raw_result),
        "record_id": record.id,
        "is_success": record.is_success,
        "cancel_requested_at": cast("str | None", termination.get("requested_at")),
        "cancel_reason": cast("str | None", termination.get("reason")),
        "terminate_mode": _coerce_execution_terminate_mode(termination.get("mode")),
    }


def _acknowledge_execution_termination(execution_id: str, acked_at: str) -> None:
    """在执行线程首次观测到 terminate 请求时更新内存状态."""
    _ = acked_at
    update_execution_task_state(execution_id, status=ExecutionTaskStatus.TERMINATING)


def request_execution_termination(
    execution_id: str,
    *,
    reason: str | None,
    mode: ExecutionTerminateMode,
) -> tuple[ExecutionTaskState | None, bool]:
    """
    登记 terminate 请求并更新任务状态.

    对已处于 ``TERMINATING``/``TERMINATED`` 或终态的任务重复调用是幂等 no-op，
    只补空的 reason/mode，不再二次触发取消。是否发生真实迁移由第二个返回值报告，
    调用方据此决定要不要写审计事件——该判断必须落在这把锁内，锁外比状态挡不住
    「两个并发 terminate 都读到 RUNNING 后各自 append」的竞态。

    Parameters
    ----------
    execution_id : str
        目标 execution ID。
    reason : str | None
        终止原因描述。
    mode : ExecutionTerminateMode
        终止模式。

    Returns
    -------
    tuple[ExecutionTaskState | None, bool]
        ``(更新后的任务状态, 本次是否发生真实状态迁移)``。任务不存在时返回
        ``(None, False)``；对已终止/终止中/终态任务的幂等 no-op 返回
        ``(state, False)``；仅 ``QUEUED→TERMINATED`` 与 ``RUNNING→TERMINATING``
        两条真实迁移返回 ``(state, True)``。
    """
    with _execution_tasks_lock:
        state = _execution_tasks.get(execution_id)
        if state is None:
            return None, False

        if state.status in _TERMINAL_EXECUTION_TASK_STATES:
            return state, False

        if state.status == ExecutionTaskStatus.QUEUED:
            requested_at = now_str()
            state.status = ExecutionTaskStatus.TERMINATED
            state.outcome = "terminated"
            state.finished_at = requested_at
            state.cancel_requested_at = state.cancel_requested_at or requested_at
            state.cancel_reason = reason
            state.terminate_mode = mode
            return state, True

        if state.status == ExecutionTaskStatus.RUNNING:
            state.status = ExecutionTaskStatus.TERMINATING
            state.cancel_requested_at = state.cancel_requested_at or now_str()
            state.cancel_reason = reason
            state.terminate_mode = mode
            if state.cancel_event is not None:
                state.cancel_event.set()
            return state, True

        if state.status in {ExecutionTaskStatus.TERMINATING, ExecutionTaskStatus.TERMINATED}:
            if state.cancel_reason is None:
                state.cancel_reason = reason
            if state.terminate_mode is None:
                state.terminate_mode = mode
            return state, False

        return state, False


async def get_execution_status(execution_id: str) -> dict[str, object] | None:
    """
    返回某次 execution 当前的内存态、intent 或持久化结果.

    Parameters
    ----------
    execution_id : str
        目标 execution ID。

    Returns
    -------
    dict[str, object] | None
        命中时返回序列化后的状态载荷；否则返回 ``None``。
    """
    state = get_execution_task_state(execution_id)
    if state is not None:
        return _serialize_execution_task_state(state)

    from axile.server.execution.intents import get_intent, serialize_intent

    intent = await get_intent(execution_id)
    if intent is not None and intent.status in {
        ExecutionTaskStatus.QUEUED,
        ExecutionTaskStatus.RUNNING,
        ExecutionTaskStatus.TERMINATING,
    }:
        return serialize_intent(intent)

    async with SessionLocal() as session:
        statement = (
            select(ExecuteRecord)
            .where(ExecuteRecord.execution_id == execution_id)
            .order_by(desc(ExecuteRecord.id))
            .limit(1)
        )
        record = (await session.execute(statement)).scalar_one_or_none()

    if record is None:
        return serialize_intent(intent) if intent is not None else None

    payload = _build_persisted_execution_status(record)
    # 业务记录写入后，任务收尾仍可能异常；重启不能用休市结果掩盖该异常。
    if (
        payload["status"] == ExecutionTaskStatus.PARTIAL
        and intent is not None
        and intent.status == ExecutionTaskStatus.FAILED
        and getattr(intent, "error", None)
    ):
        payload.update(status=ExecutionTaskStatus.FAILED, error=intent.error, output_status="FAILED")
    return payload


def create_termination_controller(execution_id: str, cancel_event: Event) -> ExecutionTerminationController:
    """
    为当前 execution 构造 server 驱动的 terminate controller.

    Parameters
    ----------
    execution_id : str
        当前 execution 标识。
    cancel_event : Event
        用于通知执行器观测终止请求的事件对象。

    Returns
    -------
    ExecutionTerminationController
        绑定了状态读取和终止确认回调的 terminate controller。
    """

    def _get_state() -> ExecutionTaskState | None:
        return get_execution_task_state(execution_id)

    def _reason_provider() -> str | None:
        state = _get_state()
        return None if state is None else state.cancel_reason

    def _mode_provider() -> str | None:
        state = _get_state()
        if state is None or state.terminate_mode is None:
            return None
        return state.terminate_mode.value

    def _acknowledge_callback(acked_at: str) -> None:
        _acknowledge_execution_termination(execution_id, acked_at)

    return ExecutionTerminationController(
        cancel_event=cancel_event,
        reason_provider=_reason_provider,
        mode_provider=_mode_provider,
        acknowledge_callback=_acknowledge_callback,
    )


async def _append_termination_requested_event(
    state: ExecutionTaskState,
    *,
    reason: str | None,
    mode: ExecutionTerminateMode,
) -> None:
    """为 terminate 请求追加受理事件。"""
    if state.channel is None or not state.algorithm:
        return

    await append_execution_event(
        execution_id=state.execution_id,
        account_id=state.account_id,
        channel=state.channel,
        algorithm=state.algorithm,
        event_type=ExecutionEventType.EXECUTION_TERMINATION_REQUESTED,
        status=ExecutionEventStatus.WARNING,
        reason_family=ExecutionReasonFamily.SYSTEM,
        reason_code="COMMON.EXECUTION_TERMINATION_REQUESTED",
        details={
            "termination": {
                "reason": reason,
                "mode": mode.value,
                "execution_kind": state.execution_kind.value,
            }
        },
    )


def _is_terminated_before_start(state: ExecutionTaskState) -> bool:
    """判断任务是否在真正启动前就已经进入终止态。"""
    return state.status == ExecutionTaskStatus.TERMINATED and state.started_at is None


async def _finalize_terminated_queued_execution(
    state: ExecutionTaskState,
    *,
    reason: str | None,
    mode: ExecutionTerminateMode,
) -> None:
    """为尚未启动就被终止的任务补写记录与事件。"""
    if state.task is not None:
        state.task.cancel()

    record = await append_terminated_execute_record(
        account_id=state.account_id,
        execution_id=state.execution_id,
        execution_kind=state.execution_kind,
        reason=reason,
        mode=mode,
        requested_at=state.cancel_requested_at or state.finished_at or now_str(),
        acked_at=None,
        finished_at=state.finished_at or now_str(),
        cancel_attempted=False,
        cancel_failed_order_ids=[],
    )
    update_execution_task_state(
        state.execution_id,
        record_id=record.id,
        is_success=record.is_success,
    )
    if state.channel is None or not state.algorithm:
        return

    await append_execution_event(
        execution_id=state.execution_id,
        account_id=state.account_id,
        channel=state.channel,
        algorithm=state.algorithm,
        event_type=ExecutionEventType.EXECUTION_TERMINATED,
        status=ExecutionEventStatus.WARNING,
        reason_family=ExecutionReasonFamily.SYSTEM,
        reason_code="COMMON.EXECUTION_TERMINATED",
        details={
            "termination": {
                "reason": reason,
                "mode": mode.value,
                "execution_kind": state.execution_kind.value,
            }
        },
    )


async def terminate_running_account_execution(
    account_id: int,
    *,
    reason: str | None,
    mode: ExecutionTerminateMode,
) -> ExecutionTaskState | None:
    """
    按账户查找当前运行的 execution 并发起 terminate.

    Parameters
    ----------
    account_id : int
        目标账户 ID。
    reason : str | None
        终止原因描述。
    mode : ExecutionTerminateMode
        终止模式。

    Returns
    -------
    ExecutionTaskState | None
        命中时返回更新后的任务状态；账户当前无运行任务时返回 ``None``。
    """
    running_id = _get_running_execution_id(account_id)
    queued_id = get_queued_execution_id(account_id)
    if running_id is None and queued_id is None:
        from axile.server.execution.intents import load_active_intents

        intent_running, intent_queued = await load_active_intents(account_id)
        running_id = None if intent_running is None else intent_running.execution_id
        queued_id = None if intent_queued is None else intent_queued.execution_id
    if running_id is None and queued_id is None:
        return None

    primary: ExecutionTaskState | None = None
    for execution_id, expected_queued in (
        (running_id, False),
        (queued_id, True),
    ):
        if execution_id is None:
            continue
        state, transitioned = await _terminate_one_execution(
            account_id,
            execution_id,
            expected_queued=expected_queued,
            reason=reason,
            mode=mode,
        )
        if state is not None and primary is None:
            primary = state
        _ = transitioned

    from axile.server.execution.intents import sync_account_live

    sync_account_live(account_id)
    return primary


async def _terminate_one_execution(
    account_id: int,
    execution_id: str,
    *,
    expected_queued: bool,
    reason: str | None,
    mode: ExecutionTerminateMode,
) -> tuple[ExecutionTaskState | None, bool]:
    """先 CAS 落库再改内存；无 intent 行时退回纯内存路径（测试/遗留）。"""
    from axile.server.execution.intents import (
        ensure_memory_from_intent,
        get_intent,
        persist_intent_termination,
    )

    intent = await get_intent(execution_id)
    expected = ExecutionTaskStatus.QUEUED if expected_queued else ExecutionTaskStatus.RUNNING
    target = ExecutionTaskStatus.TERMINATED if expected_queued else ExecutionTaskStatus.TERMINATING
    if intent is not None:
        persisted = await persist_intent_termination(
            execution_id,
            expected=expected,
            target=target,
            reason=reason,
            mode=mode,
        )
        if not persisted:
            latest = await get_intent(execution_id)
            if latest is None or latest.status in _TERMINAL_EXECUTION_TASK_STATES:
                state = get_execution_task_state(execution_id)
                if state is None and latest is not None:
                    state = ensure_memory_from_intent(latest, status=latest.status)
                elif state is not None:
                    state, _ = request_execution_termination(execution_id, reason=reason, mode=mode)
                return state, False
            if expected_queued and latest.status == ExecutionTaskStatus.RUNNING:
                persisted = await persist_intent_termination(
                    execution_id,
                    expected=ExecutionTaskStatus.RUNNING,
                    target=ExecutionTaskStatus.TERMINATING,
                    reason=reason,
                    mode=mode,
                )
                expected_queued = False
            elif latest.status == ExecutionTaskStatus.TERMINATING:
                # 落库已是终止中：仍要补内存并点亮 cancel_event。
                expected_queued = False
                persisted = True
            if not persisted:
                state, _ = request_execution_termination(execution_id, reason=reason, mode=mode)
                return state, False

    if get_execution_task_state(execution_id) is None and intent is not None:
        hydrate_status = ExecutionTaskStatus.QUEUED if expected_queued else ExecutionTaskStatus.RUNNING
        ensure_memory_from_intent(intent, status=hydrate_status)

    state, transitioned = request_execution_termination(execution_id, reason=reason, mode=mode)
    if state is None:
        return None, False
    if expected_queued or _is_terminated_before_start(state):
        clear_queued_execution(account_id, execution_id)
    if not transitioned:
        return state, False
    await _append_termination_requested_event(state, reason=reason, mode=mode)
    if _is_terminated_before_start(state):
        await _finalize_terminated_queued_execution(state, reason=reason, mode=mode)
    return state, True


def transition_execution_task_to_running(execution_id: str) -> ExecutionTaskState | None:
    """
    仅当任务仍处于 QUEUED 时，将其推进到 RUNNING.

    Parameters
    ----------
    execution_id : str
        目标 execution ID。

    Returns
    -------
    ExecutionTaskState | None
        更新后的任务状态；若任务不存在则返回 ``None``。
    """
    with _execution_tasks_lock:
        state = _execution_tasks.get(execution_id)
        if state is None:
            return None
        if state.status == ExecutionTaskStatus.QUEUED:
            state.status = ExecutionTaskStatus.RUNNING
            state.started_at = now_str()
        return state


def register_inline_execution(
    *,
    account: AccountContext,
    execution_kind: ExecutionKind,
    execution_id: str | None,
    algorithm_name: str,
) -> str:
    """
    为直接执行链路注册一个 RUNNING 状态的 execution.

    Parameters
    ----------
    account : Account
        当前执行所属账户。
    execution_kind : ExecutionKind
        当前执行类型。
    execution_id : str | None
        显式指定的 execution ID；为空时自动生成。
    algorithm_name : str
        需要登记到状态表中的算法名。

    Returns
    -------
    str
        已登记的 execution ID。

    Raises
    ------
    AccountExecutionAlreadyRunningError
        当账户已有执行任务在运行时抛出。
    """
    tracked_execution_id = execution_id or new_execution_id()
    if not try_register_running_execution(cast("int", account.id), tracked_execution_id):
        msg = f"账户 {account.id} 已有调仓任务在执行中"
        loguru.logger.warning(msg)
        raise AccountExecutionAlreadyRunningError(msg)

    state = ExecutionTaskState(
        execution_id=tracked_execution_id,
        account_id=cast("int", account.id),
        execution_kind=execution_kind,
        status=ExecutionTaskStatus.RUNNING,
        created_at=now_str(),
        channel=account.trade_channel,
        algorithm=algorithm_name,
        started_at=now_str(),
        task=asyncio.current_task(),
        cancel_event=Event(),
    )
    set_execution_task_state(tracked_execution_id, state)
    return tracked_execution_id


def get_running_execution_id(account_id: int) -> str | None:
    """
    公开读取账户当前运行中的 execution_id.

    Parameters
    ----------
    account_id : int
        目标账户 ID。

    Returns
    -------
    str | None
        账户当前运行中的 execution_id；若不存在则返回 ``None``。
    """
    return _get_running_execution_id(account_id)
