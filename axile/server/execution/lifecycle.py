"""执行生命周期与后台任务编排."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import loguru

from axile.domain.execution import (
    ExecutionEventStatus,
    ExecutionEventType,
    ExecutionKind,
    ExecutionReasonFamily,
    ExecutionTaskStatus,
    ExecutionTerminateMode,
)
from axile.executor.termination import (
    TERMINATION_TRIGGER_TIMEOUT,
    ExecutionTerminated,
)
from axile.server.core.db import SessionLocal
from axile.server.db.models import (
    Account,
    ExecuteRecord,
    now_str,
)
from axile.server.db.models.account import AccountContext, AccountSnapshot
from axile.server.error_details import exception_details
from axile.server.error_notifications import send_feishu_error
from axile.server.execution.intents import (
    IntentNotRunnable,
    SubmitResult,
    ensure_memory_from_intent,
    get_intent,
    mark_intent_finished,
    submit_intent,
    sync_account_live,
)
from axile.server.execution.records import append_terminated_execute_record, resolve_record_task_status
from axile.server.execution.registry import (
    ExecutionTaskState,
    clear_queued_execution,
    clear_running_execution,
    execution_record_outcome,
    execution_record_output_error,
    execution_record_output_status,
    finalize_execution_task_state,
    get_execution_task_state,
    update_execution_task_state,
)
from axile.server.execution_audit import append_execution_event

type ExecutionRunner = Callable[[str], Awaitable[ExecuteRecord | None]]

_TIMEOUT_ALERT_BUDGET_SECONDS = 5.0
"""总超时告警的发送预算（秒）。.

Notes
-----
告警发生在终止收尾里，而账户的运行位要等收尾返回才释放，所以这里的耗时会直接推迟
下一次调度。取值与单个外网探测的超时同量级：够一个健康服务应答，又不至于让整条
串行探测链把运行位拖住几十秒。
"""

_TERMINAL_INTENT_STATUSES = frozenset(
    {
        ExecutionTaskStatus.TERMINATED,
        ExecutionTaskStatus.FAILED,
        ExecutionTaskStatus.SUCCEEDED,
        ExecutionTaskStatus.SKIPPED,
        ExecutionTaskStatus.PARTIAL,
    }
)


@dataclass(frozen=True, slots=True)
class _TerminationSnapshot:
    """收敛 execution terminated 异常中的关键字段。"""

    reason: str | None
    mode: str | None
    acked_at: str | None
    cancel_failed_order_ids: list[str]
    trigger: str
    forced: bool
    cancel_attempted: bool | None
    cancel_unconfirmed: bool


def _resolve_termination_snapshot(
    exc: ExecutionTerminated,
    current_state: ExecutionTaskState | None,
) -> _TerminationSnapshot:
    """
    将终止异常与当前内存态合并为最终终止快照.

    Parameters
    ----------
    exc : ExecutionTerminated
        上层捕获到的终止异常对象。
    current_state : ExecutionTaskState | None
        当前 execution 的内存状态快照，用于补齐异常里缺失的 reason / mode。

    Returns
    -------
    _TerminationSnapshot
        统一收敛后的终止快照。
    """
    state_mode = (
        None if current_state is None or current_state.terminate_mode is None else current_state.terminate_mode.value
    )
    return _TerminationSnapshot(
        reason=exc.reason if exc.reason is not None else None if current_state is None else current_state.cancel_reason,
        mode=exc.mode if exc.mode is not None else state_mode,
        acked_at=exc.acked_at,
        cancel_failed_order_ids=exc.cancel_failed_order_ids,
        trigger=exc.trigger,
        forced=exc.forced,
        cancel_attempted=exc.cancel_attempted,
        cancel_unconfirmed=exc.cancel_unconfirmed,
    )


async def _load_account(account_id: int) -> AccountSnapshot | None:
    """
    按 ID 读取账户；不存在时返回 ``None``.

    Parameters
    ----------
    account_id : int
        目标账户 ID。

    Returns
    -------
    Account | None
        命中时返回账户对象；否则返回 ``None``。
    """
    async with SessionLocal() as session:
        account = await session.get(Account, account_id)
        return account.snapshot() if account is not None else None


async def _require_account(account_id: int) -> AccountSnapshot:
    """
    按 ID 读取账户；不存在时直接拒绝本次执行.

    Parameters
    ----------
    account_id : int
        目标账户 ID。

    Returns
    -------
    Account
        命中的账户对象。

    Raises
    ------
    ValueError
        目标账户不存在。
    """
    account = await _load_account(account_id)
    if account is None:
        raise ValueError(f"无法执行任务 账户id: {account_id} 不存在")
    return account


def _resolve_termination_requested_at(
    current_state: ExecutionTaskState | None,
    finished_at: str,
) -> str:
    """解析终止记录应写入的 requested_at。"""
    if current_state is None or current_state.cancel_requested_at is None:
        return finished_at
    return current_state.cancel_requested_at


def _resolve_completion_status(record: ExecuteRecord | None) -> ExecutionTaskStatus:
    """根据执行记录解析后台任务的最终状态。"""
    return resolve_record_task_status(record)


def _mark_execution_finished(
    execution_id: str,
    *,
    status: ExecutionTaskStatus,
    finished_at: str,
    record: ExecuteRecord | None = None,
    error: Exception | None = None,
) -> None:
    """统一更新后台任务的完成态字段。"""
    raw_result = None if record is None else getattr(record, "raw_result", None)
    update_execution_task_state(
        execution_id,
        status=status,
        finished_at=finished_at,
        record_id=None if record is None else record.id,
        is_success=0 if error is not None else None if record is None else record.is_success,
        # 异常优先；业务失败（如全员 BLOCKED）没有异常，必须从记录里把原因带出来。
        # 任务状态保留具体原因；完整 traceback 另存执行记录。
        error=str(error) if error is not None else execution_record_output_error(raw_result),
        output_status="FAILED" if error is not None else execution_record_output_status(raw_result),
        **(
            {"outcome": "error", "outcome_reason": str(error)}
            if error is not None
            else execution_record_outcome(raw_result)
        ),
    )


async def _notify_timeout_termination_if_needed(
    account_id: int,
    execution_id: str,
    termination: _TerminationSnapshot,
) -> None:
    """
    在执行被总超时兜底截断时发出告警.

    Parameters
    ----------
    account_id : int
        当前 execution 所属账户 ID。
    execution_id : str
        当前 execution 标识。
    termination : _TerminationSnapshot
        已规范化的终止元信息。

    Returns
    -------
    None
        该函数仅发送通知，不返回结果。

    Notes
    -----
    人工终止不通知：发起的人本来就知道。总超时正相反——它恰恰是「没人知道」的那种，
    而终止在前端被归为「提前收尾」而非失败，一个每次都超时的账户看上去会毫无异常。
    通知失败不影响终止收尾本身。
    """
    if termination.trigger != TERMINATION_TRIGGER_TIMEOUT:
        return

    try:
        from axile.common.config import settings

        account = await _load_account(account_id)
        if account is None:
            return
        await send_feishu_error(
            RuntimeError(f"执行被总超时终止（execution_id={execution_id}）：{termination.reason}"),
            account,
            settings.exe_err_feishu_key,
            event_type="execution_timeout",
            execution_id=execution_id,
        )
    except Exception as exc:  # noqa: BLE001
        loguru.logger.warning(f"总超时告警发送失败 execution_id={execution_id}: {exc}")


async def _record_terminated_execution(
    *,
    account_id: int,
    execution_id: str,
    execution_kind: ExecutionKind,
    current_state: ExecutionTaskState | None,
    requested_at: str,
    finished_at: str,
    termination: _TerminationSnapshot,
) -> ExecuteRecord:
    """
    持久化 terminated 执行记录并同步更新内存态.

    Parameters
    ----------
    account_id : int
        当前 execution 所属账户 ID。
    execution_id : str
        当前 execution 标识。
    execution_kind : ExecutionKind
        当前执行类型。
    current_state : ExecutionTaskState | None
        当前 execution 的内存状态快照。
    requested_at : str
        terminate 请求生效时间。
    finished_at : str
        终止完成时间。
    termination : _TerminationSnapshot
        已规范化的终止元信息。

    Returns
    -------
    ExecuteRecord
        持久化后的 terminated 执行记录。
    """
    record = await append_terminated_execute_record(
        account_id=account_id,
        execution_id=execution_id,
        execution_kind=execution_kind,
        reason=termination.reason,
        mode=termination.mode or ExecutionTerminateMode.GRACEFUL.value,
        requested_at=requested_at,
        acked_at=termination.acked_at,
        finished_at=finished_at,
        cancel_attempted=(
            termination.cancel_attempted
            if termination.cancel_attempted is not None
            else bool(termination.mode == ExecutionTerminateMode.CANCEL_PENDING.value)
        ),
        cancel_failed_order_ids=termination.cancel_failed_order_ids,
        trigger=termination.trigger,
        forced=termination.forced,
        cancel_unconfirmed=termination.cancel_unconfirmed,
    )
    if current_state is not None and current_state.channel is not None and current_state.algorithm:
        await append_execution_event(
            execution_id=execution_id,
            account_id=account_id,
            channel=current_state.channel,
            algorithm=current_state.algorithm,
            event_type=ExecutionEventType.EXECUTION_TERMINATED,
            status=ExecutionEventStatus.WARNING,
            reason_family=ExecutionReasonFamily.SYSTEM,
            reason_code="COMMON.EXECUTION_TERMINATED",
            details={
                "termination": {
                    "reason": termination.reason,
                    "mode": termination.mode,
                    "trigger": termination.trigger,
                    "execution_kind": execution_kind.value,
                    "cancel_failed_order_ids": termination.cancel_failed_order_ids,
                    "forced": termination.forced,
                    "cancel_unconfirmed": termination.cancel_unconfirmed,
                }
            },
        )
    # 先落内存态再发告警：告警链路里的 get_external_ip 会串行探测多个外网服务，
    # 而超时终止恰恰常发生在网络劣化时——放在前面会把 TERMINATED 的可见时间拖上几十秒。
    update_execution_task_state(
        execution_id,
        status=ExecutionTaskStatus.TERMINATED,
        outcome="terminated",
        finished_at=finished_at,
        record_id=record.id,
        is_success=record.is_success,
    )
    # 同一条外网探测链路还会占住账户的运行位：调用方要等这里返回才走到 finally 里的
    # clear_running_execution，探测全超时的话下一次定时调仓会被判为「已在运行」而直接失败。
    # 六个服务各 5s 串行 = 最坏 30s，故在这里单独封顶；超时由内部 except 吞掉，不影响收尾。
    try:
        await asyncio.wait_for(
            _notify_timeout_termination_if_needed(account_id, execution_id, termination),
            timeout=_TIMEOUT_ALERT_BUDGET_SECONDS,
        )
    except TimeoutError:
        loguru.logger.warning(f"总超时告警发送超时，已放弃 execution_id={execution_id}")
    return record


async def mark_inline_execution_succeeded(execution_id: str, record: ExecuteRecord) -> None:
    """
    将内联 execution 标记为成功或失败完成.

    Parameters
    ----------
    execution_id : str
        当前 execution 标识。
    record : ExecuteRecord
        已持久化的执行记录。

    Returns
    -------
    None
        该函数仅更新内存状态，不返回结果。
    """
    raw_result = getattr(record, "raw_result", None)
    update_execution_task_state(
        execution_id,
        status=_resolve_completion_status(record),
        finished_at=now_str(),
        record_id=record.id,
        is_success=record.is_success,
        error=execution_record_output_error(raw_result),
        output_status=execution_record_output_status(raw_result),
        **execution_record_outcome(raw_result),
    )


async def handle_inline_execution_terminated(
    *,
    account_id: int,
    execution_id: str,
    execution_kind: ExecutionKind,
    exc: ExecutionTerminated,
) -> ExecuteRecord:
    """
    持久化内联 execution 的终止结果并更新内存状态.

    Parameters
    ----------
    account_id : int
        当前 execution 所属账户 ID。
    execution_id : str
        当前 execution 标识。
    execution_kind : ExecutionKind
        当前执行类型。
    exc : ExecutionTerminated
        上层捕获到的终止异常对象。

    Returns
    -------
    ExecuteRecord
        持久化后的终止执行记录。
    """
    current_state = get_execution_task_state(execution_id)
    finished_at = now_str()
    termination = _resolve_termination_snapshot(exc, current_state)
    return await _record_terminated_execution(
        account_id=account_id,
        execution_id=execution_id,
        execution_kind=execution_kind,
        current_state=current_state,
        requested_at=_resolve_termination_requested_at(current_state, finished_at),
        finished_at=finished_at,
        termination=termination,
    )


async def handle_inline_execution_failure(
    *,
    account: AccountContext | None,
    execution_id: str,
    error: Exception,
) -> None:
    """
    将内联 execution 标记为失败并发送飞书告警.

    Parameters
    ----------
    account : Account | None
        当前执行所属账户；为空时仅更新内存状态。
    execution_id : str
        当前 execution 标识。
    error : Exception
        触发失败收尾的异常对象。

    Returns
    -------
    None
        该函数仅更新状态并发出告警，不返回结果。
    """
    update_execution_task_state(
        execution_id,
        status=ExecutionTaskStatus.FAILED,
        finished_at=now_str(),
        error=str(error),
        is_success=0,
    )
    if account is None:
        return

    from axile.common.config import settings

    await send_feishu_error(error, account, settings.exe_err_feishu_key, execution_id=execution_id)


def _release_execution_slots(account_id: int, execution_id: str) -> None:
    """释放账户槽位并丢掉该 execution 的内存态."""
    clear_queued_execution(account_id, execution_id)
    clear_running_execution(account_id, execution_id)
    finalize_execution_task_state(execution_id)


async def _bind_queued_execution_state(account_id: int, execution_id: str) -> ExecutionTaskState | None:
    """准备可开跑的内存态.

    内存缺失且 intent 仍是 QUEUED 时补水合，不把票标成终止。
    已终态返回 ``None``；intent 是 RUNNING/TERMINATING 则抛 ``IntentNotRunnable``.
    """
    state = get_execution_task_state(execution_id)
    if state is not None:
        if state.status == ExecutionTaskStatus.TERMINATED:
            _release_execution_slots(account_id, execution_id)
            await mark_intent_finished(execution_id, ExecutionTaskStatus.TERMINATED)
            return None
        return state

    intent = await get_intent(execution_id)
    if intent is not None and intent.status == ExecutionTaskStatus.QUEUED:
        return ensure_memory_from_intent(intent, status=ExecutionTaskStatus.QUEUED)

    _release_execution_slots(account_id, execution_id)
    if intent is None or intent.status in _TERMINAL_INTENT_STATUSES:
        return None
    raise IntentNotRunnable(f"intent {execution_id} 状态为 {intent.status.value}，不能开跑")


async def _run_execution_task(
    *,
    account_id: int,
    execution_id: str,
    execution_kind: ExecutionKind,
    runner: ExecutionRunner,
) -> None:
    """
    运行已入队的后台 execution，并收敛任务生命周期状态.

    Parameters
    ----------
    account_id : int
        当前执行所属账户 ID。
    execution_id : str
        当前 execution 标识。
    execution_kind : ExecutionKind
        当前执行类型。
    runner : ExecutionRunner
        真正执行任务的异步回调。

    Returns
    -------
    None
        该函数仅推进任务状态，不返回结果。
    """
    state = await _bind_queued_execution_state(account_id, execution_id)
    if state is None:
        sync_account_live(account_id)
        return

    keep_queued = False
    try:
        record = await runner(execution_id)
        status = _resolve_completion_status(record)
        finished_at = now_str()
        _mark_execution_finished(
            execution_id,
            status=status,
            finished_at=finished_at,
            record=record,
        )
        await mark_intent_finished(execution_id, status, finished_at=finished_at)
    except IntentNotRunnable as exc:
        keep_queued = exc.retry
        raise
    except ExecutionTerminated as exc:
        current_state = get_execution_task_state(execution_id)
        requested_at = None if current_state is None else current_state.cancel_requested_at
        finished_at = now_str()
        termination = _resolve_termination_snapshot(exc, current_state)
        await _record_terminated_execution(
            account_id=account_id,
            execution_id=execution_id,
            execution_kind=execution_kind,
            current_state=current_state,
            requested_at=requested_at or _resolve_termination_requested_at(current_state, finished_at),
            finished_at=finished_at,
            termination=termination,
        )
        await mark_intent_finished(execution_id, ExecutionTaskStatus.TERMINATED, finished_at=finished_at)
    except Exception as e:
        from axile.server.execution.records import append_error_execute_record

        await append_error_execute_record(
            account_id=account_id, msg=str(e), execution_id=execution_id, raw_result=exception_details(e)
        )
        finished_at = now_str()
        _mark_execution_finished(
            execution_id,
            status=ExecutionTaskStatus.FAILED,
            finished_at=finished_at,
            error=e,
        )
        await mark_intent_finished(execution_id, ExecutionTaskStatus.FAILED, error=str(e), finished_at=finished_at)
    finally:
        if keep_queued:
            # 渠道锁被刷新占用：intent 仍是 QUEUED，不能拆内存槽，否则终止会 409。
            sync_account_live(account_id)
        else:
            _release_execution_slots(account_id, execution_id)
            sync_account_live(account_id)


async def _run_execute_trade_task(account_id: int, execution_id: str, trigger_source: str) -> None:
    """
    运行后台调仓任务.

    Parameters
    ----------
    account_id : int
        目标账户 ID。
    execution_id : str
        当前 execution 标识。
    trigger_source : str
        本次执行的触发来源。

    Returns
    -------
    None
        该函数仅负责任务调度，不返回结果。
    """
    from axile.server.execution import rebalance as rebalance_execution

    async def _runner(tracked_execution_id: str) -> ExecuteRecord:
        return await rebalance_execution.execute_trade(
            account_id,
            execution_id=tracked_execution_id,
            trigger_source=trigger_source,
            lock_acquired=True,
        )

    await _run_execution_task(
        account_id=account_id,
        execution_id=execution_id,
        execution_kind=ExecutionKind.REBALANCE,
        runner=_runner,
    )


async def enqueue_execute_trade(account_id: int, trigger_source: str = "manual") -> SubmitResult:
    """
    为账户提交一张调仓 intent.

    Parameters
    ----------
    account_id : int
        目标账户 ID。
    trigger_source : str, optional
        触发来源标识，默认值为 ``"manual"``。

    Returns
    -------
    SubmitResult
        准入结果（新建或合并）。

    Raises
    ------
    ValueError
        当目标账户不存在时抛出。
    AccountExecutionAlreadyRunningError
        当冲突表拒绝本次调仓时抛出。
    """
    return await submit_intent(account_id, ExecutionKind.REBALANCE, trigger_source)


async def _run_empty_positions_task(
    account_id: int,
    execution_id: str,
    algorithm: dict[str, object] | None,
) -> None:
    """
    运行后台清仓任务.

    Parameters
    ----------
    account_id : int
        目标账户 ID。
    execution_id : str
        当前 execution 标识。
    algorithm : dict[str, object] | None
        显式指定的清仓算法配置；为空时使用账户级默认配置。

    Returns
    -------
    None
        该函数仅负责任务调度，不返回结果。
    """
    from axile.server.execution import clear_positions as clear_positions_execution

    async def _runner(tracked_execution_id: str) -> ExecuteRecord:
        account = await _require_account(account_id)
        return await clear_positions_execution.empty_positions(
            account,
            algorithm=algorithm,
            execution_id=tracked_execution_id,
            lock_acquired=True,
        )

    await _run_execution_task(
        account_id=account_id,
        execution_id=execution_id,
        execution_kind=ExecutionKind.CLEAR_POSITIONS,
        runner=_runner,
    )


async def enqueue_empty_positions(
    account_id: int,
    algorithm: dict[str, object] | None = None,
) -> SubmitResult:
    """
    为账户提交一张清仓 intent.

    Parameters
    ----------
    account_id : int
        目标账户 ID。
    algorithm : dict[str, object] | None, optional
        显式指定的清仓算法配置；为空时使用账户级默认配置。

    Returns
    -------
    SubmitResult
        准入结果（新建、合并或替换排队中的调仓）。

    Raises
    ------
    ValueError
        当目标账户不存在时抛出。
    AccountExecutionAlreadyRunningError
        当冲突表拒绝本次清仓时抛出。
    """
    return await submit_intent(
        account_id,
        ExecutionKind.CLEAR_POSITIONS,
        "empty_positions",
        payload=None if algorithm is None else dict(algorithm),
    )
