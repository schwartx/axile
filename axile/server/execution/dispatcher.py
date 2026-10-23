"""每账户一个 dispatcher：认领 QUEUED intent 并串行跑完."""

from __future__ import annotations

import asyncio
from threading import Lock

import loguru

from axile.domain.execution import ExecutionKind, ExecutionTaskStatus
from axile.executor.algorithms.utils.clock import clock_async_sleep
from axile.server.db.models import now_str
from axile.server.execution.intents import (
    IntentNotRunnable,
    IntentSnapshot,
    load_active_intents,
    mark_intent_finished,
    sync_account_live,
)
from axile.server.execution.registry import (
    clear_queued_execution,
    clear_running_execution,
    get_execution_task_state,
    get_queued_execution_id,
    get_running_execution_id,
    set_execution_task_state,
)
from axile.server.repositories import get_account_snapshot

_dispatch_tasks: dict[int, asyncio.Task[None]] = {}
_dispatch_guard = Lock()


def wake_account_dispatcher(account_id: int) -> None:
    """若该账户尚无 dispatcher 协程，则启动一个."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loguru.logger.warning(f"无事件循环，无法唤醒 dispatcher account_id={account_id}")
        return

    with _dispatch_guard:
        current = _dispatch_tasks.get(account_id)
        if current is not None and not current.done():
            return
        _dispatch_tasks[account_id] = loop.create_task(
            _dispatch_account(account_id),
            name=f"axile-dispatch-{account_id}",
        )


async def _dispatch_account(account_id: int) -> None:
    """串行消费该账户的 QUEUED intent，直到没有可跑的票."""
    try:
        while True:
            _running, queued = await load_active_intents(account_id)
            if queued is None:
                return
            if get_running_execution_id(account_id) is not None:
                return
            memory_queued = get_queued_execution_id(account_id)
            if memory_queued is not None and memory_queued != queued.execution_id:
                return
            try:
                await _run_claimed_intent(queued)
            except IntentNotRunnable as exc:
                if exc.retry:
                    await clock_async_sleep(0.4)
                    continue
                loguru.logger.info(f"dispatcher 跳过不可跑 intent execution_id={queued.execution_id}: {exc}")
                continue
    finally:
        with _dispatch_guard:
            current = _dispatch_tasks.get(account_id)
            if current is asyncio.current_task():
                _dispatch_tasks.pop(account_id, None)
        _running, queued = await load_active_intents(account_id)
        if queued is not None and get_running_execution_id(account_id) is None:
            wake_account_dispatcher(account_id)


async def _run_claimed_intent(intent: IntentSnapshot) -> None:
    """跑一张已排队的票（子 task，终止 QUEUED 时可单独取消）."""
    from axile.server.execution import lifecycle as execution_lifecycle

    state = get_execution_task_state(intent.execution_id)
    if state is not None and state.status == ExecutionTaskStatus.TERMINATED:
        clear_queued_execution(intent.account_id, intent.execution_id)
        return

    if intent.kind == ExecutionKind.CLEAR_POSITIONS:
        algorithm = intent.payload if intent.payload else None
        runner = execution_lifecycle._run_empty_positions_task(
            intent.account_id,
            intent.execution_id,
            algorithm,
        )
    else:
        runner = execution_lifecycle._run_execute_trade_task(
            intent.account_id,
            intent.execution_id,
            intent.trigger_source,
        )

    task = asyncio.create_task(runner, name=f"axile-exec-{intent.execution_id}")
    if state is not None:
        state.task = task
        set_execution_task_state(intent.execution_id, state)

    done, _ = await asyncio.wait({task})
    _ = done
    if not task.cancelled():
        exc = task.exception()
        if isinstance(exc, IntentNotRunnable):
            raise exc
    if task.cancelled():
        await mark_intent_finished(
            intent.execution_id,
            ExecutionTaskStatus.TERMINATED,
            finished_at=now_str(),
        )
    sync_account_live(intent.account_id)


async def _append_interrupted_record(intent: IntentSnapshot) -> None:
    """幂等补写进程中断的终态，不重复结算 intent 或通知。"""
    from axile.server.execution.intents import PROCESS_INTERRUPTED_REASON
    from axile.server.execution.records import append_error_execute_record

    await append_error_execute_record(
        account_id=intent.account_id,
        msg=PROCESS_INTERRUPTED_REASON,
        raw_result={
            "status": "FAILED",
            "error": PROCESS_INTERRUPTED_REASON,
            "interrupt_reason": "process_interrupted",
            "outcome": "error",
            "outcome_reason": PROCESS_INTERRUPTED_REASON,
            "task_status": ExecutionTaskStatus.FAILED.value,
            "execution_kind": intent.kind.value,
        },
        execution_id=intent.execution_id,
    )


async def recover_intents_on_startup() -> None:
    """启动时中断 RUNNING/TERMINATING，修复中断终态并续跑 QUEUED."""
    from axile.server.execution.intents import (
        PROCESS_INTERRUPTED_REASON,
        list_intents_by_status,
        list_interrupted_intents_without_record,
    )

    interrupted = await list_intents_by_status(
        ExecutionTaskStatus.RUNNING,
        ExecutionTaskStatus.TERMINATING,
    )
    for intent in interrupted:
        loguru.logger.warning(f"启动中断遗留 execution_id={intent.execution_id} account_id={intent.account_id}")
        await mark_intent_finished(
            intent.execution_id,
            ExecutionTaskStatus.FAILED,
            error=PROCESS_INTERRUPTED_REASON,
            finished_at=now_str(),
        )
        await _append_interrupted_record(intent)
        clear_running_execution(intent.account_id, intent.execution_id)
        clear_queued_execution(intent.account_id, intent.execution_id)

    for intent in await list_interrupted_intents_without_record():
        await _append_interrupted_record(intent)
        clear_running_execution(intent.account_id, intent.execution_id)
        clear_queued_execution(intent.account_id, intent.execution_id)

    queued = await list_intents_by_status(ExecutionTaskStatus.QUEUED)
    accounts: set[int] = set()
    for intent in queued:
        account = await get_account_snapshot(intent.account_id)
        if account is None:
            await mark_intent_finished(
                intent.execution_id,
                ExecutionTaskStatus.FAILED,
                error="账户不存在",
            )
            continue
        from axile.server.execution.intents import _ensure_memory_queued

        _ensure_memory_queued(account, intent)
        accounts.add(intent.account_id)
        sync_account_live(intent.account_id)
    for account_id in accounts:
        wake_account_dispatcher(account_id)


async def shutdown_dispatchers(*, grace_seconds: float = 2.0) -> None:
    """关停时取消在途 dispatcher/执行 task（best-effort）."""
    from axile.domain.execution import ExecutionTerminateMode
    from axile.server.execution.registry import iter_running_account_executions, request_execution_termination

    with _dispatch_guard:
        tasks = list(_dispatch_tasks.values())
    # 向 RUNNING 发协作式终止；QUEUED 保持以便下次启动续跑。
    running_ids = iter_running_account_executions()
    for account_id, execution_id in running_ids:
        request_execution_termination(
            execution_id,
            reason="server_shutdown",
            mode=ExecutionTerminateMode.GRACEFUL,
        )
        _ = account_id
    if not tasks:
        return
    _done, pending = await asyncio.wait(set(tasks), timeout=grace_seconds)
    for task in pending:
        task.cancel()
