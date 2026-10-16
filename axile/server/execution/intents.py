"""执行意图准入：冲突表、落库与状态 CAS."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast

import loguru
from sqlalchemy import update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionKind, ExecutionTaskStatus, ExecutionTerminateMode
from axile.server.core.db import SessionLocal
from axile.server.db.models import Account, now_str
from axile.server.db.models.account import AccountContext
from axile.server.db.models.execution_intent import ExecutionIntent
from axile.server.execution.execution_algorithms import resolve_execution_algorithm_name
from axile.server.execution.live import live_hub
from axile.server.execution.registry import (
    AccountExecutionAlreadyRunningError,
    ExecutionTaskState,
    clear_queued_execution,
    clear_running_execution,
    get_execution_task_state,
    get_queued_execution_id,
    get_running_execution_id,
    set_execution_task_state,
    set_queued_execution,
    try_register_running_execution,
    update_execution_task_state,
)

PROCESS_INTERRUPTED_REASON = "上次执行中断，未自动续跑"
SUPERSEDED_BY_CLEAR = "superseded_by_clear"

_ACTIVE_STATUSES = (
    ExecutionTaskStatus.QUEUED,
    ExecutionTaskStatus.RUNNING,
    ExecutionTaskStatus.TERMINATING,
)
_RUNNING_STATUSES = (ExecutionTaskStatus.RUNNING, ExecutionTaskStatus.TERMINATING)


class IntentNotRunnable(RuntimeError):
    """当前 intent 不能开跑（已终止、CAS 失败或渠道锁占用）."""

    def __init__(self, message: str, *, retry: bool = False) -> None:
        super().__init__(message)
        self.retry = retry


@dataclass(frozen=True)
class IntentSnapshot:
    """会话关闭后仍可使用的 intent 快照."""

    execution_id: str
    account_id: int
    kind: ExecutionKind
    trigger_source: str
    status: ExecutionTaskStatus
    channel: TradeChannel | None
    algorithm: str | None
    payload: dict[str, Any]
    created_at: str
    started_at: str | None
    finished_at: str | None
    error: str | None
    cancel_requested_at: str | None
    cancel_reason: str | None
    terminate_mode: ExecutionTerminateMode | None


@dataclass(frozen=True)
class SubmitResult:
    """一次 submit_intent 的准入结果."""

    outcome: Literal["created", "coalesced", "skipped_busy"]
    execution_id: str | None
    account_id: int


def _snapshot(row: ExecutionIntent) -> IntentSnapshot:
    kind = row.kind if isinstance(row.kind, ExecutionKind) else ExecutionKind(str(row.kind))
    status = row.status if isinstance(row.status, ExecutionTaskStatus) else ExecutionTaskStatus(str(row.status))
    channel = (
        None
        if row.channel is None
        else (row.channel if isinstance(row.channel, TradeChannel) else TradeChannel(str(row.channel)))
    )
    terminate_mode = None
    if row.terminate_mode is not None:
        terminate_mode = (
            row.terminate_mode
            if isinstance(row.terminate_mode, ExecutionTerminateMode)
            else ExecutionTerminateMode(str(row.terminate_mode))
        )
    return IntentSnapshot(
        execution_id=row.execution_id,
        account_id=row.account_id,
        kind=kind,
        trigger_source=row.trigger_source,
        status=status,
        channel=channel,
        algorithm=row.algorithm,
        payload=dict(row.payload or {}),
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        error=row.error,
        cancel_requested_at=row.cancel_requested_at,
        cancel_reason=row.cancel_reason,
        terminate_mode=terminate_mode,
    )


async def _require_account(account_id: int) -> Account:
    async with SessionLocal() as session:
        account = await session.get(Account, account_id)
    if account is None:
        raise ValueError(f"无法执行任务 账户id: {account_id} 不存在")
    return account


async def get_intent(execution_id: str) -> IntentSnapshot | None:
    """按 execution_id 读取一张 intent."""
    async with SessionLocal() as session:
        statement = select(ExecutionIntent).where(col(ExecutionIntent.execution_id) == execution_id)
        row = (await session.execute(statement)).scalar_one_or_none()
    return None if row is None else _snapshot(row)


async def load_active_intents(account_id: int) -> tuple[IntentSnapshot | None, IntentSnapshot | None]:
    """返回账户当前 (running_or_terminating, queued)."""
    async with SessionLocal() as session:
        statement = select(ExecutionIntent).where(
            col(ExecutionIntent.account_id) == account_id,
            col(ExecutionIntent.status).in_(_ACTIVE_STATUSES),
        )
        rows = list((await session.execute(statement)).scalars().all())
    running = next((row for row in rows if _snapshot(row).status in _RUNNING_STATUSES), None)
    queued = next((row for row in rows if _snapshot(row).status == ExecutionTaskStatus.QUEUED), None)
    return (
        None if running is None else _snapshot(running),
        None if queued is None else _snapshot(queued),
    )


async def list_intents_by_status(*statuses: ExecutionTaskStatus) -> list[IntentSnapshot]:
    """列出指定状态的全部 intent（启动恢复用）."""
    async with SessionLocal() as session:
        statement = select(ExecutionIntent).where(col(ExecutionIntent.status).in_(statuses))
        rows = list((await session.execute(statement)).scalars().all())
    return [_snapshot(row) for row in rows]


async def cas_intent_status(
    execution_id: str,
    expected: ExecutionTaskStatus,
    target: ExecutionTaskStatus,
    **fields: object,
) -> bool:
    """仅当当前状态为 expected 时更新 intent，返回是否成功."""
    async with SessionLocal() as session:
        statement = (
            update(ExecutionIntent)
            .where(
                col(ExecutionIntent.execution_id) == execution_id,
                col(ExecutionIntent.status) == expected,
            )
            .values(status=target, **fields)
        )
        result = cast("CursorResult[Any]", await session.execute(statement))
        if result.rowcount == 1 and target in {
            ExecutionTaskStatus.TERMINATED,
            ExecutionTaskStatus.FAILED,
            ExecutionTaskStatus.SUCCEEDED,
            ExecutionTaskStatus.SKIPPED,
        }:
            from axile.server.execution.supplement_notifications import finish_supplement_execution

            row = (
                await session.scalars(select(ExecutionIntent).where(ExecutionIntent.execution_id == execution_id))
            ).one()
            await finish_supplement_execution(session, row)
        await session.commit()
    return result.rowcount == 1


async def mark_intent_finished(
    execution_id: str,
    status: ExecutionTaskStatus,
    *,
    error: str | None = None,
    finished_at: str | None = None,
) -> None:
    """将 intent 标为终态（幂等：已终态则忽略）."""
    finished = finished_at or now_str()
    async with SessionLocal() as session:
        statement = select(ExecutionIntent).where(col(ExecutionIntent.execution_id) == execution_id)
        row = (await session.execute(statement)).scalar_one_or_none()
        if row is None:
            return
        current = row.status if isinstance(row.status, ExecutionTaskStatus) else ExecutionTaskStatus(str(row.status))
        if current in {
            ExecutionTaskStatus.SUCCEEDED,
            ExecutionTaskStatus.FAILED,
            ExecutionTaskStatus.TERMINATED,
            ExecutionTaskStatus.SKIPPED,
        }:
            return
        row.status = status
        row.finished_at = finished
        if error is not None:
            row.error = error
        session.add(row)
        from axile.server.execution.supplement_notifications import finish_supplement_execution

        await finish_supplement_execution(session, row)
        await session.commit()
    from axile.server.execution.notification_outbox import wake_notifications

    wake_notifications(row.account_id)


def serialize_intent(intent: IntentSnapshot) -> dict[str, object]:
    """把 intent 快照编成 ExecutionStatusPublic 同形载荷."""
    return {
        "execution_id": intent.execution_id,
        "account_id": intent.account_id,
        "execution_kind": intent.kind,
        "status": intent.status,
        "created_at": intent.created_at,
        "started_at": intent.started_at,
        "finished_at": intent.finished_at,
        "error": intent.error,
        "output_status": None,
        "record_id": None,
        "is_success": None,
        "cancel_requested_at": intent.cancel_requested_at,
        "cancel_reason": intent.cancel_reason,
        "terminate_mode": intent.terminate_mode,
    }


def _conflict(
    *,
    kind: ExecutionKind,
    on_conflict: Literal["raise", "skip"],
    account_id: int,
    message: str,
) -> SubmitResult:
    if on_conflict == "skip":
        loguru.logger.info(f"intent 跳过 BUSY account_id={account_id} kind={kind.value}")
        return SubmitResult(outcome="skipped_busy", execution_id=None, account_id=account_id)
    raise AccountExecutionAlreadyRunningError(message)


def ensure_memory_from_intent(
    intent: IntentSnapshot,
    *,
    status: ExecutionTaskStatus | None = None,
    channel: TradeChannel | None = None,
) -> ExecutionTaskState:
    """内存没有这张票时按 intent 补一份；已有则原样返回.

    Parameters
    ----------
    intent : IntentSnapshot
        落库快照。
    status : ExecutionTaskStatus, optional
        写入内存的状态；默认用 intent 当前状态。终止补内存时应传入
        ``QUEUED`` / ``RUNNING``，以便随后的 ``request_execution_termination``
        能迁移并点亮 ``cancel_event``。
    channel : TradeChannel, optional
        覆盖渠道（账户行兜底）。
    """
    from threading import Event

    existing = get_execution_task_state(intent.execution_id)
    if existing is not None:
        return existing
    resolved = intent.status if status is None else status
    state = ExecutionTaskState(
        execution_id=intent.execution_id,
        account_id=intent.account_id,
        execution_kind=intent.kind,
        status=resolved,
        created_at=intent.created_at,
        started_at=intent.started_at,
        channel=intent.channel if channel is None else channel,
        algorithm=intent.algorithm,
        cancel_event=Event(),
        cancel_requested_at=intent.cancel_requested_at,
        cancel_reason=intent.cancel_reason,
        terminate_mode=intent.terminate_mode,
    )
    set_execution_task_state(intent.execution_id, state)
    if resolved == ExecutionTaskStatus.QUEUED:
        set_queued_execution(intent.account_id, intent.execution_id)
    elif resolved in _RUNNING_STATUSES:
        try_register_running_execution(intent.account_id, intent.execution_id)
    return state


def _ensure_memory_queued(account: AccountContext, intent: IntentSnapshot) -> None:
    ensure_memory_from_intent(
        intent,
        status=ExecutionTaskStatus.QUEUED,
        channel=intent.channel or account.trade_channel,
    )


def _sync_live(account_id: int) -> None:
    running_id = get_running_execution_id(account_id)
    queued_id = get_queued_execution_id(account_id)
    running_state = None if running_id is None else get_execution_task_state(running_id)
    queued_state = None if queued_id is None else get_execution_task_state(queued_id)
    if running_id is not None and running_state is not None:
        status = "terminating" if running_state.status == ExecutionTaskStatus.TERMINATING else "running"
        live_hub.publish_slots(
            account_id=account_id,
            execution_id=running_id,
            kind=None if running_state.execution_kind is None else running_state.execution_kind.value,
            status=status,
            phase=None,
            pending_execution_id=queued_id,
            pending_kind=(
                None
                if queued_state is None or queued_state.execution_kind is None
                else queued_state.execution_kind.value
            ),
        )
        return
    if queued_id is not None and queued_state is not None:
        live_hub.publish_slots(
            account_id=account_id,
            execution_id=queued_id,
            kind=None if queued_state.execution_kind is None else queued_state.execution_kind.value,
            status="queued",
            phase="queued",
            pending_execution_id=None,
            pending_kind=None,
        )
        return
    live_hub.clear_live_account(account_id)


async def _insert_intent(
    *,
    account: AccountContext,
    kind: ExecutionKind,
    trigger_source: str,
    payload: dict[str, object],
) -> IntentSnapshot:
    algorithm = resolve_execution_algorithm_name(
        account,
        kind,
        algorithm_override=payload if kind == ExecutionKind.CLEAR_POSITIONS and payload else None,
    )
    row = ExecutionIntent(
        account_id=cast("int", account.id),
        kind=kind,
        trigger_source=trigger_source,
        status=ExecutionTaskStatus.QUEUED,
        channel=account.trade_channel,
        algorithm=algorithm,
        payload=payload,
    )
    async with SessionLocal() as session:
        from axile.server.execution.supplements import attach_step

        session.add(row)
        await session.flush()
        await attach_step(session, row, cast("str | None", payload.get("supplement_step_id")))
        await session.commit()
        await session.refresh(row)
        snap = _snapshot(row)
    _ensure_memory_queued(account, snap)
    return snap


async def _supersede_queued_with_clear(
    account: AccountContext,
    queued: IntentSnapshot,
    trigger_source: str,
    payload: dict[str, object],
) -> IntentSnapshot | None:
    requested_at = now_str()
    updated = await cas_intent_status(
        queued.execution_id,
        ExecutionTaskStatus.QUEUED,
        ExecutionTaskStatus.TERMINATED,
        finished_at=requested_at,
        cancel_requested_at=requested_at,
        cancel_reason=SUPERSEDED_BY_CLEAR,
        terminate_mode=ExecutionTerminateMode.GRACEFUL.value,
    )
    if updated:
        from axile.server.execution.registry import request_execution_termination

        request_execution_termination(
            queued.execution_id,
            reason=SUPERSEDED_BY_CLEAR,
            mode=ExecutionTerminateMode.GRACEFUL,
        )
        clear_queued_execution(queued.account_id, queued.execution_id)
        state = get_execution_task_state(queued.execution_id)
        if state is not None:
            from axile.server.execution.registry import _finalize_terminated_queued_execution

            await _finalize_terminated_queued_execution(
                state,
                reason=SUPERSEDED_BY_CLEAR,
                mode=ExecutionTerminateMode.GRACEFUL,
            )
        return await _insert_intent(
            account=account,
            kind=ExecutionKind.CLEAR_POSITIONS,
            trigger_source=trigger_source,
            payload=payload,
        )
    return None


async def submit_intent(
    account_id: int,
    kind: ExecutionKind,
    trigger_source: str,
    *,
    payload: dict[str, object] | None = None,
    on_conflict: Literal["raise", "skip"] = "raise",
) -> SubmitResult:
    """串行化账户入队与补发取消；关联在入队事务内提交。"""
    from axile.server.execution.supplements import account_lock

    async with account_lock(account_id):
        return await _submit_intent(account_id, kind, trigger_source, payload=payload, on_conflict=on_conflict)


async def _coalesce(execution_id: str, account_id: int, payload: dict[str, object]) -> SubmitResult:
    """保留共享执行承接的每一条触发需求。"""
    from axile.server.execution.supplements import attach_step

    async with SessionLocal() as session:
        row = (
            await session.execute(select(ExecutionIntent).where(ExecutionIntent.execution_id == execution_id))
        ).scalar_one()
        await attach_step(session, row, cast("str | None", payload.get("supplement_step_id")))
        session.add(row)
        await session.commit()
    return SubmitResult(outcome="coalesced", execution_id=execution_id, account_id=account_id)


async def _submit_intent(
    account_id: int,
    kind: ExecutionKind,
    trigger_source: str,
    *,
    payload: dict[str, object] | None = None,
    on_conflict: Literal["raise", "skip"] = "raise",
) -> SubmitResult:
    """
    为账户提交一张执行票.

    Parameters
    ----------
    account_id : int
        目标账户。
    kind : ExecutionKind
        调仓或清仓。
    trigger_source : str
        ``manual`` / ``scheduler`` / ``empty_positions``。
    payload : dict[str, object] | None
        清仓算法覆盖等。
    on_conflict : {'raise', 'skip'}
        冲突时抛 409 语义异常或返回 ``skipped_busy``（Cron 用）。
    """
    account = await _require_account(account_id)
    payload = dict(payload or {})
    running, queued = await load_active_intents(account_id)
    memory_running_id = get_running_execution_id(account_id)
    memory_queued_id = get_queued_execution_id(account_id)

    has_running = running is not None or memory_running_id is not None
    has_queued = queued is not None or memory_queued_id is not None
    running_kind = None if running is None else running.kind
    if running_kind is None and memory_running_id is not None:
        memory_state = get_execution_task_state(memory_running_id)
        running_kind = None if memory_state is None else memory_state.execution_kind
    queued_kind = None if queued is None else queued.kind
    if queued_kind is None and memory_queued_id is not None:
        queued_state = get_execution_task_state(memory_queued_id)
        queued_kind = None if queued_state is None else queued_state.execution_kind

    busy_msg = f"账户 {account_id} 已有调仓任务在执行中"

    if running_kind == ExecutionKind.CLEAR_POSITIONS:
        result = _conflict(kind=kind, on_conflict=on_conflict, account_id=account_id, message=busy_msg)
        return result

    if has_running and kind == ExecutionKind.CLEAR_POSITIONS:
        return _conflict(kind=kind, on_conflict=on_conflict, account_id=account_id, message=busy_msg)

    if not has_running and queued_kind == ExecutionKind.CLEAR_POSITIONS:
        if kind == ExecutionKind.CLEAR_POSITIONS:
            execution_id = queued.execution_id if queued is not None else cast("str", memory_queued_id)
            return await _coalesce(execution_id, account_id, payload)
        return _conflict(kind=kind, on_conflict=on_conflict, account_id=account_id, message=busy_msg)

    if not has_running and queued_kind == ExecutionKind.REBALANCE:
        if kind == ExecutionKind.CLEAR_POSITIONS:
            if queued is None:
                return _conflict(kind=kind, on_conflict=on_conflict, account_id=account_id, message=busy_msg)
            created = await _supersede_queued_with_clear(account, queued, trigger_source, payload)
            if created is None:
                return await _submit_intent(
                    account_id,
                    kind,
                    trigger_source,
                    payload=payload,
                    on_conflict=on_conflict,
                )
            _sync_live(account_id)
            _wake(account_id)
            return SubmitResult(outcome="created", execution_id=created.execution_id, account_id=account_id)
        execution_id = queued.execution_id if queued is not None else cast("str", memory_queued_id)
        return await _coalesce(execution_id, account_id, payload)

    if has_running and has_queued:
        if kind == ExecutionKind.CLEAR_POSITIONS:
            return _conflict(kind=kind, on_conflict=on_conflict, account_id=account_id, message=busy_msg)
        if queued_kind == ExecutionKind.REBALANCE:
            execution_id = queued.execution_id if queued is not None else cast("str", memory_queued_id)
            return await _coalesce(execution_id, account_id, payload)
        return _conflict(kind=kind, on_conflict=on_conflict, account_id=account_id, message=busy_msg)

    if has_running and not has_queued:
        if kind == ExecutionKind.CLEAR_POSITIONS:
            return _conflict(kind=kind, on_conflict=on_conflict, account_id=account_id, message=busy_msg)
        try:
            created = await _insert_intent(
                account=account,
                kind=kind,
                trigger_source=trigger_source,
                payload=payload,
            )
        except IntegrityError:
            running_now, queued_now = await load_active_intents(account_id)
            if queued_now is not None:
                return await _coalesce(queued_now.execution_id, account_id, payload)
            raise
        _sync_live(account_id)
        return SubmitResult(outcome="created", execution_id=created.execution_id, account_id=account_id)

    try:
        created = await _insert_intent(
            account=account,
            kind=kind,
            trigger_source=trigger_source,
            payload=payload,
        )
    except IntegrityError:
        # 并发首次提交可能同时读到空槽位；唯一索引决出胜者后，
        # 重新走冲突表，让败者得到 coalesced / busy 而不是 500。
        return await _submit_intent(
            account_id,
            kind,
            trigger_source,
            payload=payload,
            on_conflict=on_conflict,
        )
    _sync_live(account_id)
    _wake(account_id)
    return SubmitResult(outcome="created", execution_id=created.execution_id, account_id=account_id)


def _wake(account_id: int) -> None:
    from axile.server.execution.dispatcher import wake_account_dispatcher

    wake_account_dispatcher(account_id)


async def promote_intent_to_running(execution_id: str, account_id: int) -> None:
    """取消和开跑共享账户锁，防止失效补发越过准入。"""
    from axile.server.execution.supplement_notifications import validate_execution_requests
    from axile.server.execution.supplements import account_lock

    async with account_lock(account_id):
        if not await validate_execution_requests(execution_id):
            await mark_intent_finished(execution_id, ExecutionTaskStatus.TERMINATED)
            clear_queued_execution(account_id, execution_id)
            raise IntentNotRunnable("补发需求已取消或过期")
        await _promote_intent_to_running(execution_id, account_id)


async def _promote_intent_to_running(execution_id: str, account_id: int) -> None:
    """在真正下单前把 QUEUED 推进 RUNNING，并占用渠道锁.

    Parameters
    ----------
    execution_id : str
        当前执行票。
    account_id : int
        账户 ID。

    Raises
    ------
    IntentNotRunnable
        已终止、CAS 失败或渠道锁被刷新占用。``retry=True`` 表示仍应保持 QUEUED 稍后重试。
    """
    intent = await get_intent(execution_id)
    if intent is None:
        raise IntentNotRunnable(f"intent {execution_id} 不存在")
    if intent.status != ExecutionTaskStatus.QUEUED:
        raise IntentNotRunnable(f"intent {execution_id} 状态为 {intent.status.value}，不能开跑")

    memory = get_execution_task_state(execution_id)
    if memory is not None and memory.status in {
        ExecutionTaskStatus.TERMINATED,
        ExecutionTaskStatus.TERMINATING,
        ExecutionTaskStatus.FAILED,
        ExecutionTaskStatus.SUCCEEDED,
        ExecutionTaskStatus.SKIPPED,
    }:
        raise IntentNotRunnable(f"intent {execution_id} 内存态为 {memory.status.value}")

    if not try_register_running_execution(account_id, execution_id):
        raise IntentNotRunnable("账户渠道锁占用中", retry=True)

    started_at = now_str()
    ok = await cas_intent_status(
        execution_id,
        ExecutionTaskStatus.QUEUED,
        ExecutionTaskStatus.RUNNING,
        started_at=started_at,
    )
    if not ok:
        clear_running_execution(account_id, execution_id)
        raise IntentNotRunnable(f"intent {execution_id} 未能从 QUEUED 推进到 RUNNING")

    clear_queued_execution(account_id, execution_id)
    from axile.server.execution.registry import transition_execution_task_to_running

    transition_execution_task_to_running(execution_id)
    update_execution_task_state(execution_id, started_at=started_at)
    _sync_live(account_id)


async def persist_intent_termination(
    execution_id: str,
    *,
    expected: ExecutionTaskStatus,
    target: ExecutionTaskStatus,
    reason: str | None,
    mode: ExecutionTerminateMode,
) -> bool:
    """CAS 更新 intent 终止相关字段."""
    requested_at = now_str()
    return await cas_intent_status(
        execution_id,
        expected,
        target,
        cancel_requested_at=requested_at,
        cancel_reason=reason,
        terminate_mode=mode.value,
        finished_at=requested_at if target == ExecutionTaskStatus.TERMINATED else None,
    )


def sync_account_live(account_id: int) -> None:
    """按内存槽位刷新 SSE/仪表盘镜像."""
    _sync_live(account_id)
