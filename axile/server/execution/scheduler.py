"""账户执行任务的调度辅助函数."""

from typing import Union, cast

import loguru
from apscheduler.job import Job  # type: ignore[import-not-found]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-not-found]

from axile.channels.gm_sessions import GMSessionSettings, gm_phase, gm_session_context
from axile.executor.algorithms.utils.clock import clock_now
from axile.server.core.db import SessionLocal
from axile.server.core.log_config import execution_log_context
from axile.server.core.scheduler import Scheduler
from axile.server.cron import SCHEDULER_TIMEZONE, combine_cron_triggers, is_blank_cron_expr
from axile.server.db.models import Account, ScheduleSkip
from axile.server.db.models.account import AccountContext
from axile.server.repositories import get_latest_portfolio_id_by_account_id
from axile.server.trading_calendar import (
    CalendarDecisionStatus,
    evaluate_channel_calendar_moment,
    schedule_windows,
)


async def execute_scheduled_rebalance(account_id: int, scheduled_at: str | None = None) -> None:
    """在进入执行链路前按北京时间和渠道日历判断 Cron 触发。"""
    from datetime import datetime

    triggered_at = datetime.fromisoformat(scheduled_at) if scheduled_at else clock_now(tz=SCHEDULER_TIMEZONE)
    async with SessionLocal() as session:
        account = await session.get(Account, account_id)
        if account is None or not account.is_started or is_blank_cron_expr(account.cron_expr):
            return
        channel = account.trade_channel

    try:
        decision = evaluate_channel_calendar_moment(
            channel, triggered_at, windows=schedule_windows(channel, getattr(account, "gm_sessions", None))
        )
    except Exception:  # noqa: BLE001 - 调度层日历故障必须 fail-closed
        loguru.logger.bind(account_id=account_id, channel=str(channel)).exception("交易日历判断失败，跳过排程")
        decision = None

    if (
        scheduled_at
        and str(channel) == "gm"
        and decision is not None
        and decision.status == CalendarDecisionStatus.AVAILABLE_OPEN
    ):
        now = clock_now(tz=SCHEDULER_TIMEZONE)
        if now.date() != triggered_at.date() or gm_phase(now) != gm_phase(triggered_at):
            decision = decision.model_copy(
                update={"status": CalendarDecisionStatus.AVAILABLE_CLOSED, "reason_code": "CALENDAR.SESSION_CLOSED"}
            )

    context = {
        "account_id": account_id,
        "channel": str(channel),
        "calendar_id": decision.calendar_id if decision else None,
        "calendar_day": (decision.day if decision else triggered_at.date()).isoformat(),
    }
    account_logger = loguru.logger.bind(**context)
    if decision is None or decision.status in {
        CalendarDecisionStatus.AVAILABLE_CLOSED,
        CalendarDecisionStatus.UNAVAILABLE,
    }:
        reason_code = (
            decision.reason_code or "CALENDAR.CLOSED"
            if decision is not None and decision.status is CalendarDecisionStatus.AVAILABLE_CLOSED
            else "CALENDAR.UNAVAILABLE"
        )
        calendar_id = decision.calendar_id if decision is not None else ""
        calendar_day = decision.day if decision is not None else triggered_at.date()
        calendar_label = decision.label if decision is not None and decision.label else ""
        try:
            async with SessionLocal() as session:
                session.add(
                    ScheduleSkip(
                        account_id=account_id,
                        channel=str(channel),
                        triggered_at=triggered_at.isoformat(),
                        calendar_id=calendar_id,
                        calendar_day=calendar_day,
                        calendar_label=calendar_label,
                        reason_code=reason_code,
                    )
                )
                await session.commit()
        except Exception:  # noqa: BLE001 - 审计写入失败不能改变休市决策
            account_logger.exception("休市跳过记录写入失败")
        account_logger.info("排程跳过", reason_code=reason_code)
        return

    if getattr(account, "supplement", None) is not None:
        from axile.server.execution.supplements import create_group

        await create_group(account_id, triggered_at)
        return

    from axile.domain.execution import ExecutionKind
    from axile.server.execution.intents import submit_intent

    result = await submit_intent(
        account_id,
        ExecutionKind.REBALANCE,
        "scheduler",
        payload={
            "gm_session": gm_session_context((account.gm_sessions or GMSessionSettings()).model_dump(), triggered_at)
        }
        if str(channel) == "gm"
        else None,
        on_conflict="skip",
    )
    if result.outcome == "skipped_busy":
        try:
            async with SessionLocal() as session:
                session.add(
                    ScheduleSkip(
                        account_id=account_id,
                        channel=str(channel),
                        triggered_at=triggered_at.isoformat(),
                        calendar_id=decision.calendar_id if decision is not None else "",
                        calendar_day=(decision.day if decision else triggered_at.date()),
                        calendar_label=decision.label if decision is not None and decision.label else "",
                        reason_code="BUSY",
                    )
                )
                await session.commit()
        except Exception:  # noqa: BLE001 - 审计写入失败不能改变 busy 决策
            account_logger.exception("BUSY 跳过记录写入失败")
        account_logger.info("排程因已有执行在途跳过")
        return


async def _scheduled_signal(account_id: int) -> None:
    """补发账户的 APScheduler 唤醒信号；计划时间由提交事件提供。"""


async def create_job(
    sched: Scheduler,
    account: AccountContext,
    triggers: list[CronTrigger],
    logger: "loguru.Logger | None" = None,
    *,
    binding_checked: bool = False,
) -> None:
    """
    为账户创建定时执行任务.

    Parameters
    ----------
    sched : Scheduler
        当前服务使用的调度器实例。
    account : Account
        需要创建定时任务的账户。
    triggers : list[CronTrigger]
        账户配置对应的 Cron 触发器列表。
    logger : loguru.Logger | None, optional
        用于输出日志的 logger；未提供时使用全局 logger。
    binding_checked : bool, default=False
        调用方已在账户快照中确认组合绑定时为真，避免运行态应用阶段重新读库。

    Returns
    -------
    None
        该函数仅创建调度任务，不返回结果。
    """
    if logger is None:
        logger = loguru.logger

    acc_logger = logger.bind(
        **execution_log_context(
            account_id=account.id,
            account_name=account.name,
            channel=account.trade_channel,
        )
    )

    if not account.is_started:
        acc_logger.info("跳过定时任务创建 原因: 账户未启动")
        return

    if not binding_checked:
        async with SessionLocal() as session:
            portfolio_id = await get_latest_portfolio_id_by_account_id(session, cast("int", account.id))
            if portfolio_id is None:
                acc_logger.info("跳过定时任务创建, 原因: 组合未绑定")
                return

    try:
        if getattr(account, "supplement", None) or str(account.trade_channel) == "gm":
            _install_supplement_listener(sched)
        trigger = combine_cron_triggers(triggers)
        next_run_time = trigger.get_next_fire_time(None, clock_now(tz=SCHEDULER_TIMEZONE))  # type: ignore[no-untyped-call]
        sched.add_job(  # type: ignore[no-untyped-call]
            func=_scheduled_signal
            if (getattr(account, "supplement", None) or str(account.trade_channel) == "gm")
            else execute_scheduled_rebalance,
            args=[account.id],
            trigger=trigger,
            id=str(account.id),
            name=f"{account.name}#{account.id}",
            next_run_time=next_run_time,
            max_instances=1,
        )

        acc_logger.info(f"定时任务创建成功 CRON={account.cron_expr}")
    except Exception as e:
        acc_logger.error(
            f"账户定时任务初始化失败 | 错误原因={str(e)}",
            exc_info=True,
        )
        raise


def delete_job(
    sched: Scheduler,
    account_id: int,
    logger: "loguru.Logger | None" = None,
) -> None:
    """
    在账户存在调度任务时删除它.

    Parameters
    ----------
    sched : Scheduler
        当前服务使用的调度器实例。
    account_id : int
        目标账户 ID。
    logger : loguru.Logger | None, optional
        用于输出日志的 logger；未提供时使用全局 logger。

    Returns
    -------
    None
        该函数仅删除调度任务，不返回结果。
    """
    if logger is None:
        logger = loguru.logger

    job: Union[Job, None] = sched.get_job(str(account_id))  # type: ignore[no-untyped-call]
    if job is not None:
        job.remove()  # type: ignore[no-untyped-call]
        logger.bind(**execution_log_context(account_id=account_id)).info("定时任务删除成功")


def _install_supplement_listener(sched: Scheduler) -> None:
    """APScheduler 提交事件携带真实计划时间，不用回调墙钟推导基础触发。"""
    import asyncio

    from apscheduler.events import EVENT_JOB_SUBMITTED

    if getattr(sched, "_supplement_listener_installed", False):
        return

    def submitted(event) -> None:
        job = sched.get_job(event.job_id)
        if job is None or job.func != _scheduled_signal:
            return
        now = clock_now(tz=SCHEDULER_TIMEZONE)
        for planned in event.scheduled_run_times:
            if (now - planned).total_seconds() < 1:
                asyncio.create_task(execute_scheduled_rebalance(int(event.job_id), planned.isoformat()))

    sched.add_listener(submitted, EVENT_JOB_SUBMITTED)
    sched._supplement_listener_installed = True
