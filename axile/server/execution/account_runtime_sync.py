"""账户真源到 scheduler / worker 的对齐：短事务、外部调用、条件回写。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal

from loguru import logger

from axile.common.trade_channel import TradeChannel
from axile.server._native import _core
from axile.server.api.routes.account_support import _apply_account_job
from axile.server.core.scheduler import Scheduler
from axile.server.db.models.account import AccountSnapshot
from axile.server.db.models.base import now_str
from axile.server.error_details import exception_details
from axile.server.execution.ctp_channels import reconcile_china_channel_account
from axile.server.execution.runtime_locks import account_runtime_lock
from axile.server.execution.worker_backend.manager import WorkerBackendExecutionError
from axile.server.native_store import native_operation


class _ScheduledAccountSkipped(Exception):
    """盘前候选在取得锁后已删除、停用或切换渠道。"""


@dataclass(frozen=True)
class _RuntimeTarget:
    """一次对齐使用的独立快照，不携带数据库会话或懒加载关系。"""

    account: AccountSnapshot
    revision: int
    reset_worker: bool
    portfolio_id: int | None
    attempt_id: int
    last_execution: str = ""


async def _begin_attempt(
    account_id: int,
    *,
    scheduled: bool = False,
) -> _RuntimeTarget:
    """一次原生短事务取得独立快照并提交尝试，返回后才接触外部运行态。"""
    try:
        value = await native_operation(
            "runtime_sync_begin_attempt", {"account_id": account_id, "scheduled": scheduled, "now": now_str()}
        )
    except _core.NativeError as exc:
        if exc.args[0] == "ACCOUNT_NOT_FOUND":
            raise LookupError(f"账户不存在: {account_id}") from exc
        raise
    if value is None:
        raise _ScheduledAccountSkipped
    return _RuntimeTarget(
        account=AccountSnapshot.model_validate(value["account"]),
        revision=value["revision"],
        reset_worker=value["reset_worker"],
        portfolio_id=value["portfolio_id"],
        attempt_id=value["attempt_id"],
        last_execution=value["last_execution"],
    )


async def _finish_attempt(
    account_id: int,
    revision: int,
    attempt_id: int,
    error: str | None,
    *,
    status: Literal["pending", "synchronized", "failed"],
) -> dict[str, Any]:
    """审计每次尝试；成功与失败都只能更新自己对应的目标版本。"""
    value = await native_operation(
        "runtime_sync_finish_attempt",
        {
            "account_id": account_id,
            "revision": revision,
            "attempt_id": attempt_id,
            "status": status,
            "error": error,
            "now": now_str(),
        },
    )
    return value


def _runtime_error_text(exc: Exception) -> str:
    """沿用既有文本存储完整原文和 traceback，不改变同步状态分类。"""
    details = exception_details(exc)
    summary = getattr(exc, "execution_error", None)
    parts = [summary, details["technical_detail"], details["traceback"]]
    return "\n\n".join(dict.fromkeys(str(part) for part in parts if part))


async def _prepare_runtime_target(
    target: _RuntimeTarget,
    mode: Literal["startup", "day", "night"] | None,
) -> tuple[Literal["pending", "synchronized", "failed"], str | None]:
    """仅在渠道准备边界识别明确的柜台未初始化；scheduler 错误不能降级。"""
    reset = target.reset_worker or (target.account.trade_channel == TradeChannel.TQ and mode in {"day", "night"})
    try:
        await reconcile_china_channel_account(target.account, reset=reset, last_execution=target.last_execution)
    except WorkerBackendExecutionError as exc:
        if target.account.trade_channel != TradeChannel.CTP or exc.reason_code != "CTP_NOT_INITIALIZED":
            raise
        logger.warning(
            "账户通道暂未就绪 account_id={} revision={} reason_code={}: {}",
            target.account.id,
            target.revision,
            exc.reason_code,
            exc.execution_error,
        )
        return "pending", _runtime_error_text(exc)
    return "synchronized", None


async def _reconcile_locked(
    account_id: int,
    sched: Scheduler,
    mode: Literal["startup", "day", "night"] | None = None,
) -> dict[str, Any]:
    """锁内收敛最新目标；外部调用前关闭事务，回写仅影响对应 revision。"""
    while True:
        target = await _begin_attempt(account_id, scheduled=mode is not None)
        try:
            await _apply_account_job(sched, target.account, target.portfolio_id)
            status, error = await _prepare_runtime_target(target, mode)
        except Exception as exc:  # noqa: BLE001 - 外部状态失败不能回滚账户真源
            status = "failed"
            error = _runtime_error_text(exc)
            logger.exception("账户运行态对齐失败 account_id={} revision={}", account_id, target.revision)
        sync = await _finish_attempt(account_id, target.revision, target.attempt_id, error, status=status)
        if sync["revision"] == target.revision:
            return sync


async def reconcile_account_runtime(account_id: int, sched: Scheduler) -> dict[str, Any]:
    """串行对齐并收敛到最新版本；取消及柜台未初始化保留 pending，交由既有入口重试。"""
    async with account_runtime_lock(account_id):
        return await _reconcile_locked(account_id, sched)


async def reconcile_scheduled_account(
    account_id: int,
    sched: Scheduler,
    mode: Literal["startup", "day", "night"],
) -> dict[str, Any] | None:
    """盘前准备复用完整对齐，锁内重新验证候选并保留天勤盘前重建语义。"""
    async with account_runtime_lock(account_id):
        try:
            return await _reconcile_locked(account_id, sched, mode)
        except _ScheduledAccountSkipped:
            return None


async def recover_account_runtime_on_startup(sched: Scheduler) -> None:
    """并发恢复独立账户；慢连接只占用本账户任务，不串行阻塞其他账户。"""
    account_ids = await native_operation("runtime_sync_scan", {})

    async def recover(account_id: int) -> None:
        try:
            await reconcile_account_runtime(account_id, sched)
        except Exception:  # noqa: BLE001 - 单账户故障不阻断其他账户恢复
            logger.exception("启动恢复账户运行态失败 account_id={}", account_id)

    # gather 保留各任务的生命周期；取消总任务时同时取消各账户恢复。
    await asyncio.gather(*(recover(account_id) for account_id in account_ids if account_id is not None))
