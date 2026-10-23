"""国内常驻账户通道的启动与交易日前准备."""

from __future__ import annotations

import asyncio
from typing import Literal

from loguru import logger

from axile.common.trade_channel import TradeChannel
from axile.server.core.scheduler import Scheduler
from axile.server.db.models.account import AccountContext
from axile.server.execution.worker_backend.manager import get_worker_backend_manager
from axile.server.native_store import native_operation

CHINA_NIGHT_PREPARE_JOB_ID = "china-night-session-prepare"
CHINA_DAY_PREPARE_JOB_ID = "china-day-session-prepare"
_MAX_PREPARE_CONCURRENCY = 4


async def _started_china_channel_accounts() -> list[int]:
    """批量取得已启用的 CTP 与天勤账户 ID，锁内再读取完整配置。"""
    return await native_operation("runtime_sync_scan", {"started_china_only": True})


async def _prepare_accounts(
    accounts: list[int],
    mode: Literal["startup", "day", "night"],
) -> None:
    """并发准备账户；天勤在日夜盘窗口先强制重建 worker."""
    # 上层对齐依赖本模块的渠道操作；延迟导入仅用于定时编排入口。
    from axile.server.core.scheduler import scheduler
    from axile.server.execution.account_runtime_sync import reconcile_scheduled_account

    semaphore = asyncio.Semaphore(_MAX_PREPARE_CONCURRENCY)

    async def prepare(account_id: int) -> None:
        async with semaphore:
            try:
                sync = await reconcile_scheduled_account(account_id, scheduler, mode)
                if sync is not None and sync.status == "synchronized":
                    logger.info("通道准备完成 account_id={} mode={}", account_id, mode)
            except Exception:  # noqa: BLE001 - 单账户失败不得阻断其他账户
                logger.exception("通道准备失败 account_id={} mode={}", account_id, mode)

    await asyncio.gather(*(prepare(account) for account in accounts))


async def prepare_china_channel_accounts(
    mode: Literal["startup", "day", "night"],
) -> None:
    """准备全部已启用的国内常驻执行渠道."""
    from axile.server.execution.catalog_preheat import arm_catalog_preheat

    arm_catalog_preheat()
    await _prepare_accounts(await _started_china_channel_accounts(), mode)


async def reconcile_china_channel_account(
    account: AccountContext, *, reset: bool = False, last_execution: str = ""
) -> None:
    """对齐 CTP 或天勤账户的常驻 worker."""
    if account.id is None:
        return
    manager = get_worker_backend_manager()
    manager.configure_catalog_account(account, last_execution=last_execution)
    if reset:
        await manager.drop_account(int(account.id))
    if account.trade_channel not in {TradeChannel.CTP, TradeChannel.TQ} or not account.is_started:
        return
    await manager.prepare_account(account)


async def drop_account_worker(account_id: int) -> None:
    """关闭账户可能持有的常驻 Worker。"""
    await get_worker_backend_manager().drop_account(account_id)


def register_china_channel_jobs(scheduler: Scheduler) -> None:
    """注册国内常驻渠道的夜盘前与日盘前准备任务."""
    common = {
        "trigger": "cron",
        "replace_existing": True,
        "max_instances": 1,
        "coalesce": True,
        "misfire_grace_time": 1800,
    }
    _ = scheduler.add_job(
        prepare_china_channel_accounts,
        hour=20,
        minute=30,
        id=CHINA_NIGHT_PREPARE_JOB_ID,
        args=["night"],
        **common,
    )
    _ = scheduler.add_job(
        prepare_china_channel_accounts,
        hour=8,
        minute=30,
        id=CHINA_DAY_PREPARE_JOB_ID,
        args=["day"],
        **common,
    )


__all__ = [
    "drop_account_worker",
    "prepare_china_channel_accounts",
    "reconcile_china_channel_account",
    "register_china_channel_jobs",
]
