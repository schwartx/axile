"""账户真源到 scheduler / worker 的对齐：短事务、外部调用、条件回写。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

from loguru import logger
from sqlalchemy import literal, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import col, select

from axile.common.trade_channel import TradeChannel
from axile.server.api.routes.account_support import _apply_account_job
from axile.server.core.db import SessionLocal
from axile.server.core.scheduler import Scheduler
from axile.server.db.models import Account
from axile.server.db.models.account import AccountSnapshot
from axile.server.db.models.account_runtime_sync import AccountRuntimeSync, AccountRuntimeSyncAttempt
from axile.server.db.models.base import now_str
from axile.server.db.models.execution import ExecuteRecord
from axile.server.error_details import exception_details
from axile.server.execution.ctp_channels import reconcile_china_channel_account
from axile.server.execution.runtime_locks import account_runtime_lock
from axile.server.execution.worker_backend.manager import WorkerBackendExecutionError
from axile.server.repositories import get_latest_portfolio_id_by_account_id


class _ScheduledAccountSkipped(Exception):
    """盘前候选在取得锁后已删除、停用或切换渠道。"""


@dataclass(frozen=True)
class _RuntimeTarget:
    """一次对齐使用的独立快照，不携带数据库会话或懒加载关系。"""

    account: AccountSnapshot
    revision: int
    reset_worker: bool
    portfolio_id: int | None
    last_execution: str = ""


async def enqueue_account_runtime_sync(
    session: AsyncSession,
    account_id: int,
    *,
    reset_worker: bool,
    create_request_key: str | None = None,
) -> AccountRuntimeSync:
    """在账户写事务内原子登记目标版本，保留尚未执行的 worker 重建要求。"""
    initial = AccountRuntimeSync(
        account_id=account_id, reset_worker=reset_worker, create_request_key=create_request_key
    )
    statement = insert(AccountRuntimeSync).values(**initial.model_dump(exclude={"id"}))
    statement = statement.on_conflict_do_update(
        index_elements=["account_id"],
        set_={
            "revision": col(AccountRuntimeSync.revision) + 1,
            "status": "pending",
            "reset_worker": col(AccountRuntimeSync.reset_worker) | reset_worker,
            "last_error": None,
            "requested_at": now_str(),
            "synchronized_at": None,
        },
    ).returning(AccountRuntimeSync)
    sync = await session.scalar(statement.execution_options(populate_existing=True))
    assert sync is not None
    return sync


async def _begin_attempt(
    account_id: int,
    factory: async_sessionmaker[AsyncSession],
    *,
    scheduled: bool = False,
) -> _RuntimeTarget:
    """提交尝试记录后关闭会话，外部调用只能消费返回的快照。"""
    async with factory() as session, session.begin():
        # 先取得短写事务，再读取一致的账户/版本快照，避免 WAL 读事务升级失败。
        initial = AccountRuntimeSync(account_id=account_id).model_dump(exclude={"id"})
        await session.execute(
            insert(AccountRuntimeSync)
            .from_select(
                list(initial),
                select(*(literal(value) for value in initial.values()))
                .select_from(Account)
                .where(Account.id == account_id),
            )
            .on_conflict_do_nothing(index_elements=["account_id"])
        )
        account = await session.get(Account, account_id)
        if scheduled and (
            account is None
            or not account.is_started
            or account.trade_channel not in {TradeChannel.CTP, TradeChannel.TQ}
        ):
            # 回滚本事务，包括上方可能插入的首次同步记录。
            raise _ScheduledAccountSkipped
        if account is None:
            raise LookupError(f"账户不存在: {account_id}")
        sync = await session.scalar(
            update(AccountRuntimeSync)
            .where(col(AccountRuntimeSync.account_id) == account_id)
            .values(
                attempts=col(AccountRuntimeSync.attempts) + 1,
                last_attempt_at=now_str(),
                status="pending",
                last_error=None,
                synchronized_at=None,
            )
            .returning(AccountRuntimeSync)
        )
        assert sync is not None
        portfolio_id = await get_latest_portfolio_id_by_account_id(session, account_id)
        last_execution = await session.scalar(
            select(ExecuteRecord.created_at)
            .where(ExecuteRecord.account_id == account_id)
            .order_by(col(ExecuteRecord.id).desc())
            .limit(1)
        )
        return _RuntimeTarget(account.snapshot(), sync.revision, sync.reset_worker, portfolio_id, last_execution or "")


async def _finish_attempt(
    account_id: int,
    revision: int,
    error: str | None,
    factory: async_sessionmaker[AsyncSession],
    *,
    status: Literal["pending", "synchronized", "failed"],
) -> AccountRuntimeSync:
    """审计每次尝试；成功与失败都只能更新自己对应的目标版本。"""
    async with factory() as session, session.begin():
        session.add(
            AccountRuntimeSyncAttempt(
                account_id=account_id, revision=revision, succeeded=status == "synchronized", error=error
            )
        )
        values: dict[str, object] = {"status": status, "last_error": error}
        if status == "synchronized":
            values.update(status="synchronized", reset_worker=False, synchronized_at=now_str())
        await session.execute(
            update(AccountRuntimeSync)
            .where(col(AccountRuntimeSync.account_id) == account_id, col(AccountRuntimeSync.revision) == revision)
            .values(**values)
        )
        sync = await session.scalar(select(AccountRuntimeSync).where(AccountRuntimeSync.account_id == account_id))
        assert sync is not None
        # 返回独立对象，即使调用方设置 expire_on_commit=True 也不依赖会话。
        return AccountRuntimeSync(**sync.model_dump())


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
    factory: async_sessionmaker[AsyncSession],
    mode: Literal["startup", "day", "night"] | None = None,
) -> AccountRuntimeSync:
    """锁内收敛最新目标；外部调用前关闭事务，回写仅影响对应 revision。"""
    while True:
        target = await _begin_attempt(account_id, factory, scheduled=mode is not None)
        try:
            await _apply_account_job(sched, target.account, target.portfolio_id)
            status, error = await _prepare_runtime_target(target, mode)
        except Exception as exc:  # noqa: BLE001 - 外部状态失败不能回滚账户真源
            status = "failed"
            error = _runtime_error_text(exc)
            logger.exception("账户运行态对齐失败 account_id={} revision={}", account_id, target.revision)
        sync = await _finish_attempt(account_id, target.revision, error, factory, status=status)
        if sync.revision == target.revision:
            return sync


async def reconcile_account_runtime(
    account_id: int, sched: Scheduler, *, session_factory: async_sessionmaker[AsyncSession] | None = None
) -> AccountRuntimeSync:
    """串行对齐并收敛到最新版本；取消及柜台未初始化保留 pending，交由既有入口重试。"""
    async with account_runtime_lock(account_id):
        return await _reconcile_locked(account_id, sched, session_factory or SessionLocal)


async def reconcile_scheduled_account(
    account_id: int,
    sched: Scheduler,
    mode: Literal["startup", "day", "night"],
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> AccountRuntimeSync | None:
    """盘前准备复用完整对齐，锁内重新验证候选并保留天勤盘前重建语义。"""
    async with account_runtime_lock(account_id):
        try:
            return await _reconcile_locked(account_id, sched, session_factory or SessionLocal, mode)
        except _ScheduledAccountSkipped:
            return None


async def recover_account_runtime_on_startup(
    sched: Scheduler, *, session_factory: async_sessionmaker[AsyncSession] | None = None
) -> None:
    """并发恢复独立账户；慢连接只占用本账户任务，不串行阻塞其他账户。"""
    factory = session_factory or SessionLocal
    async with factory() as session:
        account_ids = list((await session.scalars(select(Account.id))).all())

    async def recover(account_id: int) -> None:
        try:
            await reconcile_account_runtime(account_id, sched, session_factory=factory)
        except Exception:  # noqa: BLE001 - 单账户故障不阻断其他账户恢复
            logger.exception("启动恢复账户运行态失败 account_id={}", account_id)

    # gather 保留各任务的生命周期；取消总任务时同时取消各账户恢复。
    await asyncio.gather(*(recover(account_id) for account_id in account_ids if account_id is not None))
