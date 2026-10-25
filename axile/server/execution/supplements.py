"""补发的日历判断、调度与提交后副作用；持久化由原生业务事务完成。"""

import asyncio
import hashlib
import json
from datetime import datetime
from typing import Any

from axile.domain.execution import ExecutionKind, ExecutionTaskStatus
from axile.executor.algorithms.utils.clock import clock_now
from axile.server.core.scheduler import scheduler
from axile.server.cron import SCHEDULER_TIMEZONE, combine_cron_triggers, is_blank_cron_expr, parse_cron_expr
from axile.server.db.models import AccountSnapshot
from axile.server.db.models.base import new_execution_id
from axile.server.native_store import native_operation
from axile.server.repositories import get_account_snapshot
from axile.server.supplement_plan import plan_supplements
from axile.server.trading_calendar import CalendarDecisionStatus, evaluate_channel_calendar_moment

_locks: dict[int, asyncio.Lock] = {}


class SupplementNotRunnable(ValueError):
    """补发需求在准入之前被取消或过期。"""


def account_lock(account_id: int) -> asyncio.Lock:
    """串行化账户控制面；数据库唯一键负责最终去重。"""
    return _locks.setdefault(account_id, asyncio.Lock())


def fingerprint(account: AccountSnapshot) -> str:
    """仅调度身份、渠道和绑定参与组去重。"""
    assert account.supplement is not None
    value = [account.cron_expr, account.supplement.model_dump(), str(account.trade_channel), account.portfolio_id]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def calendar_context(channel: str | None) -> dict[str, Any]:
    """在事务外读取日历；原生准入仍重验当前账户配置。"""
    now = clock_now(tz=SCHEDULER_TIMEZONE)
    statuses = {} if channel is None else {channel: str(evaluate_channel_calendar_moment(channel, now).status)}
    return {"now": now.isoformat(), "calendar_status_by_channel": statuses}


async def create_group(account_id: int, base: datetime) -> None:
    """规划在事务外完成，原生保存时重验配置并去重。"""
    async with account_lock(account_id):
        account = await get_account_snapshot(account_id)
        if account is None or not account.is_started or not account.supplement or not account.portfolio_id:
            return
        if is_blank_cron_expr(account.cron_expr):
            return
        trigger = combine_cron_triggers(parse_cron_expr(account.cron_expr))
        if trigger.get_next_fire_time(None, base) != base:
            return
        planned, expires = plan_supplements(account.trade_channel, account.cron_expr, account.supplement, base)
        group = {
            "id": new_execution_id(),
            "account_id": account_id,
            "fingerprint": fingerprint(account),
            "base_scheduled_at": base.isoformat(),
            "expires_at": expires.isoformat(),
            "configured_count": account.supplement.count,
            "effective_count": len(planned) - 1,
        }
        steps = [
            {"id": new_execution_id(), "group_id": group["id"], "index": index, "scheduled_at": when.isoformat()}
            for index, when in enumerate(planned)
        ]
        created = await native_operation(
            "supplement_create_group", {"account_id": account_id, "group": group, "steps": steps}
        )
        if created is None:
            return
        group, steps = created["group"], created["steps"]

    schedule_expiration(group)
    for step in steps[1:]:
        schedule_step(step)
    await fire_step(steps[0]["id"])


def schedule_expiration(group: dict[str, Any]) -> None:
    """到期检查遗漏唤醒；已开跑执行继续按原有语义收尾。"""
    scheduler.add_job(
        expire_group,
        trigger="date",
        run_date=datetime.fromisoformat(group["expires_at"]),
        args=[group["id"]],
        id=f"supplement-expiry:{group['id']}",
        replace_existing=True,
        misfire_grace_time=None,
    )


async def expire_group(group_id: str) -> None:
    """仍有未执行步骤时结束旧组，提交后才处理内存及通知。"""
    result = await native_operation("supplement_expire_group", {"group_id": group_id, "now": clock_now().isoformat()})
    if result is not None:
        await _after_cancel(result["account_id"])


def schedule_step(step: dict[str, Any]) -> None:
    """调度器只保存唤醒信号，持久化步骤才是执行依据。"""
    scheduler.add_job(
        fire_step,
        trigger="date",
        run_date=datetime.fromisoformat(step["scheduled_at"]),
        args=[step["id"]],
        id=f"supplement:{step['id']}",
        replace_existing=True,
        misfire_grace_time=1,
    )


async def fire_step(step_id: str) -> None:
    """日历在事务外判断，关联 intent 时原子校验步骤仍然有效。"""
    from axile.server.execution.intents import submit_intent

    snapshot = await native_operation("supplement_step", {"step_id": step_id})
    if snapshot is None or snapshot["step"]["status"] != "pending" or snapshot["group"]["status"] != "active":
        return
    group = snapshot["group"]
    context = calendar_context(snapshot["channel"])
    status = context["calendar_status_by_channel"].get(snapshot["channel"])
    if status in {CalendarDecisionStatus.AVAILABLE_CLOSED, CalendarDecisionStatus.UNAVAILABLE}:
        await cancel_groups(group["account_id"], "schedule_interrupted", group_id=group["id"])
        return
    try:
        result = await submit_intent(
            group["account_id"],
            ExecutionKind.REBALANCE,
            "scheduler",
            payload={"supplement_step_id": step_id},
            on_conflict="skip",
        )
    except SupplementNotRunnable:
        await cancel_groups(group["account_id"], "schedule_interrupted", group_id=group["id"])
        return
    if result.outcome == "skipped_busy":
        await cancel_groups(group["account_id"], "schedule_interrupted", group_id=group["id"])


async def cancel_groups(account_id: int, reason: str, *, group_id: str | None = None) -> bool:
    """取消剩余需求；重复操作不生成第二个取消事件。"""
    async with account_lock(account_id):
        return await cancel_groups_locked(account_id, reason, group_id=group_id)


async def cancel_groups_locked(account_id: int, reason: str, *, group_id: str | None = None) -> bool:
    """调用方持有账户锁；先提交取消，再清理内存队列并唤醒通知。"""
    changed = await native_operation(
        "supplement_cancel_groups",
        {"account_id": account_id, "reason": reason, "group_id": group_id, "now": clock_now().isoformat()},
    )
    await _after_cancel(account_id)
    return changed


async def _after_cancel(account_id: int) -> None:
    """持久化提交成功后才允许消费通知与清理内存槽。"""
    from axile.server.execution.notification_outbox import wake_notifications

    await reap_cancelled_queue(account_id)
    wake_notifications(account_id)


async def recover_supplements() -> None:
    """原子跳过错过的步骤，提交后只恢复未来唤醒。"""
    result = await native_operation("supplement_recover", {"now": clock_now(tz=SCHEDULER_TIMEZONE).isoformat()})
    for item in result["groups"]:
        schedule_expiration(item["group"])
        for step in item["steps"]:
            schedule_step(step)
    for account_id in result["account_ids"]:
        await _after_cancel(account_id)


async def reap_cancelled_queue(account_id: int) -> None:
    """提交撤销后才清理内存槽，保留仍承接共享请求的队列。"""
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
