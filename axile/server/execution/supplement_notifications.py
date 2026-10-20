"""服务端终态统一产生补发执行通知，包括未进入执行器的失败。"""

from copy import deepcopy
from typing import Any, cast

import loguru
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from axile.domain.execution import ExecutionKind, ExecutionTaskStatus
from axile.executor.feishu_notifications import LoggerLike
from axile.executor.models.unified_order import OrderDirection, UnifiedOrder
from axile.executor.session_closed import COMMON_SESSION_CLOSED, session_closed_scope
from axile.server.business_facts.notification_context import compact_record_notification_context
from axile.server.business_facts.repository import read_execution_detail, read_execution_result
from axile.server.core.db import SessionLocal
from axile.server.db.models import Account, ExecuteRecord
from axile.server.db.models.execution_intent import ExecutionIntent
from axile.server.db.models.supplement import NotificationEvent, SupplementGroup, SupplementStep
from axile.server.execution.notification_records import build_persisted_notification_context
from axile.server.execution.supplements import (
    cancel_groups_in_session,
    settle_groups,
    step_is_valid,
    supplement_context,
)


class NotificationView:
    """重建通知快照所需的无渠道视图。"""

    logger: LoggerLike = cast("LoggerLike", loguru.logger)

    def __init__(self, account: Account) -> None:
        self.account = account

    def _get_account_mark(self) -> str:
        return self.account.name

    def _get_operation_display(self, order: UnifiedOrder) -> str:
        return "买入" if order.direction == OrderDirection.BUY else "卖出"


async def execution_steps(session: AsyncSession, execution_id: str) -> list[SupplementStep]:
    """读取执行承接的全部触发关联。"""
    return list(
        (await session.scalars(select(SupplementStep).where(SupplementStep.execution_id == execution_id))).all()
    )


async def validate_execution_requests(execution_id: str) -> bool:
    """只撤销失效补发，保留共享执行的独立请求。"""
    async with SessionLocal() as session:
        steps = await execution_steps(session, execution_id)
        if not steps:
            return True
        row = (await session.scalars(select(ExecutionIntent).where(ExecutionIntent.execution_id == execution_id))).one()
        valid = bool(row.payload.get("independent_request"))
        invalid_groups = set()
        for step in steps:
            if step.status != "cancelled" and await step_is_valid(session, step):
                valid = True
                step.status = "queued"
            else:
                step.status = "cancelled"
                group = await session.get(SupplementGroup, step.group_id)
                if group is not None and group.status == "active":
                    invalid_groups.add(group.id)
            session.add(step)
        for group_id in invalid_groups:
            await cancel_groups_in_session(session, row.account_id, "schedule_interrupted", group_id=group_id)
        await session.commit()
    from axile.server.execution.notification_outbox import wake_notifications

    wake_notifications(row.account_id)
    return valid


async def _execution_context(session: AsyncSession, row: ExecutionIntent, account: Account) -> dict[str, Any]:
    """优先沿用标准执行快照；早期失败仍有完整结构与空资产。"""
    record = await session.scalar(select(ExecuteRecord).where(ExecuteRecord.execution_id == row.execution_id))
    context: dict[str, Any] = {}
    detail = await read_execution_detail(session, record) if record is not None else None
    result = detail["result"] if detail is not None else None
    if isinstance(result, dict):
        extra = result.get("extra")
        captured = extra.get("server_notification_context") if isinstance(extra, dict) else None
        if isinstance(captured, dict):
            context = deepcopy(captured)
    if not context and isinstance(result, dict) and result:
        try:
            context = build_persisted_notification_context(
                NotificationView(account),
                result,
                detail["input"] if detail is not None else None,
                account.account_config,
            )
        except ValueError:
            loguru.logger.debug("执行无完整标准输出 execution_id={}", row.execution_id)
    if not context:
        context = {
            "account": {"id": account.id, "name": account.name, "mark": account.name},
            "execution": {},
            "strategy": {},
            "assets": {},
            "targets": {},
            "positions": None,
            "orders": [],
            "trades": [],
            "symbols": [],
            "summary": {
                "symbol_count": 0,
                "position_count": None,
                "order_count": 0,
                "filled_order_count": 0,
                "active_order_count": 0,
                "trade_count": 0,
                "trade_value": 0.0,
                "succeeded_symbol_count": 0,
                "failed_symbol_count": 0,
            },
            "default_feishu_variables": {
                "account_mark": account.name,
                "dt": row.finished_at,
                "algorithm": row.algorithm or "Unknown",
                "total_assets": "未获取",
                "available_cash": "未获取",
                "market_value": "未获取",
                "positions": [],
                "trades": [],
            },
        }
    execution = context["execution"]
    execution.update(
        id=row.execution_id,
        kind=ExecutionKind(row.kind).value,
        trigger_source=row.trigger_source,
        status=execution.get("status", ExecutionTaskStatus(row.status).value),
        success=execution.get("success", row.status == ExecutionTaskStatus.SUCCEEDED),
        error=row.error or execution.get("error"),
        notified_at=row.finished_at,
        is_test=False,
        execution_time=execution.get("execution_time", 0.0),
        channel_type=str(account.trade_channel),
        outcome_reason=execution.get("outcome_reason", row.error),
        outcome=execution.get("outcome"),
        reason_code=execution.get("reason_code"),
    )
    context.update(event_id=f"execution:{row.execution_id}", event_type="execution.finished")
    return context


async def finish_supplement_execution(session: AsyncSession, row: ExecutionIntent) -> None:
    """执行完成事件先入 outbox，再在同事务内完成取消组。"""
    steps = await execution_steps(session, row.execution_id)
    if not steps:
        return
    account = await session.get(Account, row.account_id)
    if account is None:
        return
    ran = row.started_at is not None or row.status == ExecutionTaskStatus.FAILED
    for step in steps:
        if step.status != "cancelled":
            step.status = "finished"
            session.add(step)
    await session.flush()
    # 完成组状态后再拍执行通知，取消组仍保留 cancelling 供静默示例识别。
    groups = {}
    for step in steps:
        group = await session.get(SupplementGroup, step.group_id)
        if group is not None:
            groups[group.id] = group
    from axile.server.execution.supplements import steps_for_group

    for group in groups.values():
        all_steps = await steps_for_group(session, group.id)
        if group.status == "active" and all(step.status in {"finished", "cancelled"} for step in all_steps):
            group.status = "completed"
            session.add(group)
    if ran:
        event_id = f"execution:{row.execution_id}"
        event = await session.scalar(select(NotificationEvent).where(NotificationEvent.id == event_id))
        if event is None:
            context = await _execution_context(session, row, account)
            context["execution"]["supplements"] = [
                supplement_context(groups[step.group_id], step) for step in steps if step.status != "cancelled"
            ]
            event = NotificationEvent(
                id=event_id, account_id=row.account_id, event_type="execution.finished", context=context
            )
            session.add(event)
        await session.flush()
        record = await session.scalar(select(ExecuteRecord).where(ExecuteRecord.execution_id == row.execution_id))
        if record is not None:
            await compact_record_notification_context(session, record, event)
    await session.flush()
    if row.status == ExecutionTaskStatus.SKIPPED:
        record = await session.scalar(select(ExecuteRecord).where(ExecuteRecord.execution_id == row.execution_id))
        if record is not None and session_closed_scope(await read_execution_result(session, record)) == "all":
            for group in groups.values():
                await cancel_groups_in_session(session, row.account_id, COMMON_SESSION_CLOSED, group_id=group.id)
    await settle_groups(session, row.account_id)
