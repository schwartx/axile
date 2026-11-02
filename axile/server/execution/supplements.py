"""独立补发状态机；账户锁串行化触发、取消与开跑准入。"""

import asyncio
import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from axile.domain.execution import ExecutionKind, ExecutionTaskStatus
from axile.executor.algorithms.utils.clock import clock_now
from axile.server.core.db import SessionLocal
from axile.server.core.scheduler import scheduler
from axile.server.cron import (
    SCHEDULER_TIMEZONE,
    combine_cron_triggers,
    effective_triggers,
    is_blank_cron_expr,
)
from axile.server.db.models import Account
from axile.server.db.models.execution_intent import ExecutionIntent
from axile.server.db.models.supplement import NotificationEvent, SupplementGroup, SupplementStep
from axile.server.supplement_plan import plan_supplements
from axile.server.trading_calendar import CalendarDecisionStatus, evaluate_channel_calendar_moment, schedule_windows

_locks: dict[int, asyncio.Lock] = {}


class SupplementNotRunnable(ValueError):
    """补发需求在准入之前被取消或过期。"""


def account_lock(account_id: int) -> asyncio.Lock:
    """账户控制面的同进程串行化锁；数据库唯一键仍负责最终去重。"""
    return _locks.setdefault(account_id, asyncio.Lock())


def fingerprint(account: Account) -> str:
    """仅调度身份、渠道和绑定参与组去重。"""
    value = [
        account.cron_expr,
        account.supplement.model_dump(),
        str(account.trade_channel),
        account.portfolio_id,
    ]
    if account.gm_sessions is not None:
        value.append(account.gm_sessions.model_dump())
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


async def steps_for_group(session: AsyncSession, group_id: str) -> list[SupplementStep]:
    """按计划顺序读取步骤。"""
    return list(
        (
            await session.scalars(
                select(SupplementStep).where(SupplementStep.group_id == group_id).order_by(col(SupplementStep.index))
            )
        ).all()
    )


async def create_group(account_id: int, base: datetime) -> None:
    """去重并持久化一次有效基础触发，然后安排未来步骤。"""
    async with account_lock(account_id), SessionLocal() as session:
        account = await session.get(Account, account_id)
        if account is None or not account.is_started or not account.supplement or not account.portfolio_id:
            return
        if is_blank_cron_expr(account.cron_expr):
            return
        trigger = combine_cron_triggers(
            effective_triggers(account.cron_expr, account.trade_channel, getattr(account, "gm_sessions", None))
        )
        if trigger.get_next_fire_time(None, base) != base:
            return
        digest = fingerprint(account)
        existing = await session.scalar(
            select(SupplementGroup).where(
                SupplementGroup.account_id == account_id,
                SupplementGroup.fingerprint == digest,
                SupplementGroup.base_scheduled_at == base.isoformat(),
            )
        )
        if existing is not None:
            return
        planned, expires = plan_supplements(
            account.trade_channel, account.cron_expr, account.supplement, base, account.gm_sessions
        )
        group = SupplementGroup(
            account_id=account_id,
            fingerprint=digest,
            base_scheduled_at=base.isoformat(),
            expires_at=expires.isoformat(),
            configured_count=account.supplement.count,
            effective_count=len(planned) - 1,
        )
        session.add(group)
        await session.flush()
        steps = [
            SupplementStep(group_id=group.id, index=index, scheduled_at=when.isoformat())
            for index, when in enumerate(planned)
        ]
        session.add_all(steps)
        await session.commit()
    schedule_expiration(group)
    for step in steps[1:]:
        schedule_step(step)
    await fire_step(steps[0].id)


def schedule_expiration(group: SupplementGroup) -> None:
    """到期检查遗漏唤醒；已开跑执行继续按原有语义收尾。"""
    scheduler.add_job(
        expire_group,
        trigger="date",
        run_date=datetime.fromisoformat(group.expires_at),
        args=[group.id],
        id=f"supplement-expiry:{group.id}",
        replace_existing=True,
        misfire_grace_time=None,
    )


async def expire_group(group_id: str) -> None:
    """仍有未执行步骤时按排程中断结束，避免末次丢失后永久挂起。"""
    async with SessionLocal() as session:
        group = await session.get(SupplementGroup, group_id)
        if group is None or group.status != "active":
            return
        steps = await steps_for_group(session, group_id)
        unfinished = False
        for step in steps:
            if step.status == "pending":
                unfinished = True
            elif step.status == "queued":
                intent = await session.scalar(
                    select(ExecutionIntent).where(ExecutionIntent.execution_id == step.execution_id)
                )
                unfinished |= intent is None or intent.status == ExecutionTaskStatus.QUEUED
    if unfinished:
        await cancel_groups(group.account_id, "schedule_interrupted", group_id=group.id)


def schedule_step(step: SupplementStep) -> None:
    """调度器只保存唤醒信号，持久化步骤才是执行依据。"""
    scheduler.add_job(
        fire_step,
        trigger="date",
        run_date=datetime.fromisoformat(step.scheduled_at),
        args=[step.id],
        id=f"supplement:{step.id}",
        replace_existing=True,
        misfire_grace_time=1,
    )


async def step_is_valid(session: AsyncSession, step: SupplementStep, *, pending: bool = False) -> bool:
    """入队与开跑均重新校验组、账户、配置及有效时间。"""
    group = await session.get(SupplementGroup, step.group_id)
    if group is None or group.status != "active":
        return False
    account = await session.get(Account, group.account_id)
    now = clock_now(tz=SCHEDULER_TIMEZONE)
    if account is None or not account.is_started or not account.supplement or not account.portfolio_id:
        return False
    if fingerprint(account) != group.fingerprint or now >= datetime.fromisoformat(group.expires_at):
        return False
    if pending and step.status != "pending":
        return False
    decision = evaluate_channel_calendar_moment(
        account.trade_channel, now, windows=schedule_windows(account.trade_channel, account.gm_sessions)
    )
    return decision.status not in {CalendarDecisionStatus.AVAILABLE_CLOSED, CalendarDecisionStatus.UNAVAILABLE}


async def fire_step(step_id: str) -> None:
    """实际唤醒时间不用于推导后续步骤；过期触发只结束旧组。"""
    from axile.server.execution.intents import submit_intent

    async with SessionLocal() as session:
        step = await session.get(SupplementStep, step_id)
        group = None if step is None else await session.get(SupplementGroup, step.group_id)
        if step is None or group is None or step.status != "pending" or group.status != "active":
            return
        valid = await step_is_valid(session, step, pending=True)
    if not valid:
        await cancel_groups(group.account_id, "schedule_interrupted", group_id=group.id)
        return
    try:
        result = await submit_intent(
            group.account_id,
            ExecutionKind.REBALANCE,
            "scheduler",
            payload={"supplement_step_id": step_id},
            on_conflict="skip",
        )
    except SupplementNotRunnable:
        await cancel_groups(group.account_id, "schedule_interrupted", group_id=group.id)
        return

    if result.outcome == "skipped_busy":
        await cancel_groups(group.account_id, "schedule_interrupted", group_id=group.id)


async def attach_step(session: AsyncSession, execution: ExecutionIntent, step_id: str | None) -> None:
    """在 intent 写入/合并事务中记录触发关联。"""
    if step_id is None:
        execution.payload = (
            {**execution.payload, "independent_request": True}
            if execution.kind == ExecutionKind.REBALANCE
            else execution.payload
        )
        return
    step = await session.get(SupplementStep, step_id)
    if step is None or not await step_is_valid(session, step, pending=True):
        raise SupplementNotRunnable("补发需求已取消或过期")
    step.execution_id = execution.execution_id
    step.status = "queued"
    session.add(step)


async def cancel_groups_in_session(
    session: AsyncSession, account_id: int, reason: str, *, group_id: str | None = None
) -> bool:
    """先持久化 cancelling 与撤销步骤；调用方负责提交及唤醒 outbox。"""
    query = select(SupplementGroup).where(SupplementGroup.account_id == account_id, SupplementGroup.status == "active")
    if group_id:
        query = query.where(SupplementGroup.id == group_id)
    groups = list((await session.scalars(query)).all())
    for group in groups:
        group.status = "cancelling"
        group.end_reason = reason
        group.cancel_requested_at = clock_now().isoformat()
        session.add(group)
        for step in await steps_for_group(session, group.id):
            if step.status == "pending":
                step.status = "cancelled"
            elif step.status == "queued":
                intent = await session.scalar(
                    select(ExecutionIntent).where(ExecutionIntent.execution_id == step.execution_id)
                )
                if intent is not None and intent.status == ExecutionTaskStatus.QUEUED:
                    step.status = "cancelled"
            session.add(step)
    await session.flush()
    await revoke_cancelled_intents(session, account_id, reason)
    await settle_groups(session, account_id)
    return bool(groups)


async def revoke_cancelled_intents(session: AsyncSession, account_id: int, reason: str) -> None:
    """没有有效请求的补发队列票在步骤撤销事务内结束。"""
    queued = list(
        (
            await session.scalars(
                select(ExecutionIntent).where(
                    ExecutionIntent.account_id == account_id, ExecutionIntent.status == ExecutionTaskStatus.QUEUED
                )
            )
        ).all()
    )
    for intent in queued:
        if intent.payload.get("independent_request"):
            continue
        related = list(
            (
                await session.scalars(select(SupplementStep).where(SupplementStep.execution_id == intent.execution_id))
            ).all()
        )
        if related and all(step.status == "cancelled" for step in related):
            intent.status = ExecutionTaskStatus.TERMINATED
            intent.finished_at = clock_now().isoformat()
            intent.cancel_requested_at = intent.finished_at
            intent.cancel_reason = reason
            session.add(intent)


async def cancel_groups(account_id: int, reason: str, *, group_id: str | None = None) -> bool:
    """取消剩余需求；重复操作不生成第二个取消事件。"""
    async with account_lock(account_id):
        return await cancel_groups_locked(account_id, reason, group_id=group_id)


async def cancel_groups_locked(account_id: int, reason: str, *, group_id: str | None = None) -> bool:
    """调用方持有账户锁时提交取消，再处理内存和通知副作用。"""
    async with SessionLocal() as session:
        changed = await cancel_groups_in_session(session, account_id, reason, group_id=group_id)
        await session.commit()
    from axile.server.execution.notification_outbox import wake_notifications

    await reap_cancelled_queue(account_id)
    wake_notifications(account_id)
    return changed


def supplement_context(group: SupplementGroup, step: SupplementStep) -> dict[str, Any]:
    """通知使用的步骤身份及组终态。"""
    return {
        "group_id": group.id,
        "base_scheduled_at": group.base_scheduled_at,
        "scheduled_at": step.scheduled_at,
        "index": step.index,
        "configured_count": group.configured_count,
        "effective_count": group.effective_count,
        "is_last": step.index == group.effective_count,
        "group_status": group.status,
        "end_reason": group.end_reason,
    }


async def settle_groups(session: AsyncSession, account_id: int) -> None:
    """相关执行收尾后完成组；取消标记与 outbox 在同一事务提交。"""
    groups = list(
        (
            await session.scalars(
                select(SupplementGroup).where(
                    SupplementGroup.account_id == account_id, col(SupplementGroup.status).in_(["active", "cancelling"])
                )
            )
        ).all()
    )
    for group in groups:
        steps = await steps_for_group(session, group.id)
        if any(step.status in {"pending", "queued", "running"} for step in steps):
            continue
        if group.status == "active":
            group.status = "completed"
        else:
            group.status = "cancelled"
            event_id = f"supplement:{group.id}:cancelled"
            if await session.scalar(select(NotificationEvent).where(NotificationEvent.id == event_id)) is None:
                last = await session.scalar(
                    select(NotificationEvent)
                    .where(
                        NotificationEvent.account_id == account_id,
                        col(NotificationEvent.id).in_(
                            [f"execution:{step.execution_id}" for step in steps if step.execution_id]
                        ),
                    )
                    .order_by(col(NotificationEvent.sequence).desc())
                    .limit(1)
                )
                snapshot = (
                    last.context
                    if last and any(step.execution_id == last.context.get("execution", {}).get("id") for step in steps)
                    else None
                )
                session.add(
                    NotificationEvent(
                        id=event_id,
                        account_id=account_id,
                        event_type="supplement.cancelled",
                        context={
                            "event_id": event_id,
                            "event_type": "supplement.cancelled",
                            "execution": None,
                            "is_test": False,
                            "supplement": {
                                "group_id": group.id,
                                "base_scheduled_at": group.base_scheduled_at,
                                "reason": group.end_reason,
                                "cancel_requested_at": group.cancel_requested_at,
                                "steps": [
                                    supplement_context(group, step)
                                    | {"status": step.status, "execution_id": step.execution_id}
                                    for step in steps
                                    if step.status == "cancelled"
                                ],
                            },
                            "last_execution": snapshot,
                            "last_execution_at": last.created_at if snapshot and last else None,
                        },
                    )
                )
        session.add(group)


async def recover_supplements() -> None:
    """只恢复未来有效步骤，错过的步骤不补跑，运行中交易不重放。"""
    now = clock_now(tz=SCHEDULER_TIMEZONE)
    async with SessionLocal() as session:
        groups = list(
            (
                await session.scalars(
                    select(SupplementGroup).where(col(SupplementGroup.status).in_(["active", "cancelling"]))
                )
            ).all()
        )
    for group in groups:
        await _recover_group(group, now)


async def _recover_group(group: SupplementGroup, now: datetime) -> None:
    """中间错过的步骤跳过；只有末次丢失或整组到期才取消剩余计划。"""
    async with SessionLocal() as session:
        steps = await steps_for_group(session, group.id)
        last_missed = False
        for step in steps:
            if step.status not in {"pending", "queued"} or datetime.fromisoformat(step.scheduled_at) > now:
                continue
            last_missed |= step.index == group.effective_count
            intent = (
                None
                if not step.execution_id
                else await session.scalar(
                    select(ExecutionIntent).where(ExecutionIntent.execution_id == step.execution_id)
                )
            )
            if intent is None or intent.status == ExecutionTaskStatus.QUEUED:
                step.status = "cancelled"
                session.add(step)
        await session.flush()
        await revoke_cancelled_intents(session, group.account_id, "schedule_interrupted")
        await session.commit()
    if group.status == "active" and (last_missed or datetime.fromisoformat(group.expires_at) <= now):
        await cancel_groups(group.account_id, "schedule_interrupted", group_id=group.id)
    elif group.status == "active":
        schedule_expiration(group)
        for step in steps:
            if step.status == "pending":
                schedule_step(step)
    else:
        async with SessionLocal() as session:
            await settle_groups(session, group.account_id)
            await session.commit()
    await reap_cancelled_queue(group.account_id)


async def reap_cancelled_queue(account_id: int) -> None:
    """提交撤销后才清理内存槽，不能误清仍承接共享请求的队列。"""
    from axile.server.execution.intents import get_intent, sync_account_live
    from axile.server.execution.registry import (
        clear_queued_execution,
        get_queued_execution_id,
        update_execution_task_state,
    )

    execution_id = get_queued_execution_id(account_id)
    if execution_id is None:
        return
    intent = await get_intent(execution_id)
    if intent is not None and intent.status == ExecutionTaskStatus.TERMINATED:
        clear_queued_execution(account_id, execution_id)
        update_execution_task_state(execution_id, status=ExecutionTaskStatus.TERMINATED, finished_at=intent.finished_at)
        sync_account_live(account_id)
