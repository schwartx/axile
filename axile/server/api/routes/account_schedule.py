"""账户排程预览与统一活动流接口。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal, cast

from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-not-found]
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import literal, union_all
from sqlalchemy import select as sql_select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, desc, func, select

from axile.channels import get_channel
from axile.common.trade_channel import TradeChannel
from axile.executor.algorithms.utils.clock import clock_now
from axile.executor.trading_calendar import SHINNY_COVERAGE_END, SHINNY_COVERAGE_START
from axile.server.api.business_execution import (
    ExecuteRecordSummaryPublic,
    projected_trade,
    read_execution_summaries,
    read_projected_trades,
)
from axile.server.api.deps import SessionDep
from axile.server.api.routes.account_support import _get_account_or_404
from axile.server.cron import SCHEDULER_TIMEZONE, is_blank_cron_expr, parse_cron_expr
from axile.server.db.models import (
    AccountActivityListPublic,
    ExecuteRecord,
    ExecutionActivity,
    ScheduleSkip,
    ScheduleSkipActivity,
)
from axile.server.db.models.account_settings import SupplementSettings
from axile.server.db.models.analysis_source import source_trade
from axile.server.db.models.performance import CostSummary
from axile.server.db.models.schedule import (
    ActivityExecutionRecord,
    ActivitySymbolListPublic,
    ActivitySymbolRowPublic,
    ScheduleSkipReason,
)
from axile.server.performance_costs import summarize
from axile.server.supplement_plan import plan_supplements
from axile.server.trading_calendar import (
    CalendarDecisionStatus,
    CalendarSkipReason,
    CalendarUnavailableReason,
    evaluate_channel_calendar_moment,
)

router = APIRouter()


def _time_clauses(column, since: str | None, until: str | None):
    """把可选时间窗编成列比较；``since`` 含、``until`` 不含。"""
    clauses = []
    if since:
        clauses.append(column >= since)
    if until:
        clauses.append(column < until)
    return clauses


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _compact_execution_activity(
    row: ExecuteRecord, summary: ExecuteRecordSummaryPublic, trades: list[dict], total: float | None
) -> ExecutionActivity:
    """列表只发布类型化头字段、摘要数字和结果权益。"""
    duration = row.execution_time
    costs = CostSummary.model_validate(summarize(trades)) if summary.trade_count is not None else None
    return ExecutionActivity(
        occurred_at=row.created_at,
        record=ActivityExecutionRecord(
            id=row.id,
            execution_id=row.execution_id,
            created_at=row.created_at,
            is_success=row.is_success,
            status=_text_or_none(row.display_status),
            task_status=_text_or_none(row.task_status),
            error=_text_or_none(row.display_error),
            outcome=_text_or_none(row.display_outcome),
            outcome_reason=_text_or_none(row.display_outcome_reason),
            reason_code=_text_or_none(row.reason_code),
            execution_kind=_text_or_none(row.execution_kind),
            symbol_results=summary.symbol_results or {},
            total_asset=total,
            summary=costs,
            duration_sec=duration if duration is not None and duration >= 0 else None,
            trade_count=summary.trade_count,
        ),
    )


class SchedulePreviewRequest(BaseModel):
    """未来原始 Cron 触发点的只读预览请求。"""

    trade_channel: TradeChannel
    cron_expr: str
    supplement: SupplementSettings | None = None
    scheduled_ats: list[AwareDatetime] | None = Field(
        default=None, min_length=1, max_length=100, description="仅重算指定基础触发的补发摘要，不推进预览游标"
    )
    after: AwareDatetime | None = None
    limit: int = Field(default=5, ge=1, le=100)


class SchedulePreviewCalendar(BaseModel):
    """排程预览对应渠道的轻量日历摘要。"""

    requirement: Literal["required", "not_required"]
    availability: Literal["available", "unavailable", "not_required"]
    unavailable_reason: CalendarUnavailableReason | None = None
    calendar_id: str | None = None
    label: str | None = None
    coverage_start: date | None = None
    coverage_end: date | None = None


class SchedulePreviewItem(BaseModel):
    """单个未来 Cron 触发点及其轻量日历动作。"""

    scheduled_at: datetime
    base_scheduled_at: datetime | None = None
    index: int = 0
    effective_count: int = 0
    is_last: bool = True
    calendar_day: date
    calendar_status: CalendarDecisionStatus
    action: Literal["execute", "skip"]
    unavailable_reason: CalendarUnavailableReason | None = None
    calendar_id: str | None = None
    label: str | None = None
    reason_code: CalendarSkipReason | None = None


class SchedulePreviewResponse(BaseModel):
    """账户排程预览响应。"""

    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    evaluated_at: datetime
    calendar: SchedulePreviewCalendar
    items: list[SchedulePreviewItem]
    next_cursor: datetime | None = None
    has_more: bool = False


def _field_error(field: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=[{"type": "value_error", "loc": ["body", field], "msg": message, "input": None}],
    )


def _next_schedule_times(
    triggers: list[CronTrigger],
    *,
    start: datetime,
    limit: int,
    exclusive: bool = False,
) -> list[datetime]:
    """合并多个 CronTrigger，按北京时间返回去重后的未来触发点。"""
    next_values = [trigger.get_next_fire_time(None, start) for trigger in triggers]
    if exclusive:
        for index, value in enumerate(next_values):
            while value is not None and value <= start:
                value = triggers[index].get_next_fire_time(value, value)
            next_values[index] = value
    result: list[datetime] = []
    while len(result) < limit:
        candidates = [value for value in next_values if value is not None]
        if not candidates:
            break
        current = min(candidates)
        result.append(current)
        for index, value in enumerate(next_values):
            if value == current:
                next_values[index] = triggers[index].get_next_fire_time(current, current)
    return result


def _calendar_summary(channel: TradeChannel, current: datetime) -> SchedulePreviewCalendar:
    """返回渠道当前使用的 Shinny 日历摘要。"""
    declaration = get_channel(str(channel)).descriptor.calendar
    if declaration is None:
        return SchedulePreviewCalendar(requirement="not_required", availability="not_required")
    decision = evaluate_channel_calendar_moment(channel, current)
    return SchedulePreviewCalendar(
        requirement="required",
        availability=("unavailable" if decision.status is CalendarDecisionStatus.UNAVAILABLE else "available"),
        unavailable_reason=decision.unavailable_reason,
        calendar_id=declaration.calendar_id,
        label=declaration.label,
        coverage_start=SHINNY_COVERAGE_START,
        coverage_end=SHINNY_COVERAGE_END,
    )


def _selected_base_preview(payload: SchedulePreviewRequest, base: datetime) -> SchedulePreviewItem:
    """只重算已有基础触发的补发摘要，保持原时间点及日历动作。"""
    local_time = base.astimezone(SCHEDULER_TIMEZONE)
    decision = evaluate_channel_calendar_moment(payload.trade_channel, local_time)
    skipped = decision.status in {CalendarDecisionStatus.AVAILABLE_CLOSED, CalendarDecisionStatus.UNAVAILABLE}
    points = [local_time]
    if payload.supplement is not None and not skipped:
        points, _ = plan_supplements(payload.trade_channel, payload.cron_expr, payload.supplement, local_time)
    return SchedulePreviewItem(
        scheduled_at=local_time,
        base_scheduled_at=local_time,
        effective_count=len(points) - 1,
        is_last=len(points) == 1,
        calendar_day=decision.day,
        calendar_status=decision.status,
        action="skip" if skipped else "execute",
        unavailable_reason=decision.unavailable_reason,
        calendar_id=decision.calendar_id,
        label=decision.label,
        reason_code=decision.reason_code,
    )


@router.post("/schedule-preview", response_model=SchedulePreviewResponse)
async def schedule_preview(payload: SchedulePreviewRequest) -> SchedulePreviewResponse:
    """按时间游标只读预览未来 Cron 触发点及其交易日历动作。"""
    try:
        get_channel(str(payload.trade_channel))
    except KeyError as exc:
        raise _field_error("trade_channel", str(exc)) from exc

    evaluated_at = clock_now(tz=SCHEDULER_TIMEZONE)
    calendar = _calendar_summary(payload.trade_channel, evaluated_at)
    if is_blank_cron_expr(payload.cron_expr):
        return SchedulePreviewResponse(evaluated_at=evaluated_at, calendar=calendar, items=[])
    try:
        triggers = parse_cron_expr(payload.cron_expr)
    except ValueError as exc:
        raise _field_error("cron_expr", str(exc)) from exc

    if payload.scheduled_ats is not None:
        bases = sorted({value.astimezone(SCHEDULER_TIMEZONE) for value in payload.scheduled_ats})
        return SchedulePreviewResponse(
            evaluated_at=evaluated_at,
            calendar=calendar,
            items=[_selected_base_preview(payload, base) for base in bases],
        )

    start = payload.after.astimezone(SCHEDULER_TIMEZONE) if payload.after is not None else evaluated_at
    scheduled = _next_schedule_times(
        triggers,
        start=start,
        limit=payload.limit + 1,
        exclusive=payload.after is not None,
    )
    metadata = {}
    if payload.supplement:
        # 包含游标所在旧组的未来补发，再合并基础触发；旧组不从 cron 反推。
        from datetime import timedelta

        lookback = start - timedelta(minutes=payload.supplement.count * payload.supplement.interval_minutes)
        bases = _next_schedule_times(
            triggers, start=lookback, limit=(payload.supplement.count + 1) * (payload.limit + 2)
        )
        expanded = []
        for base in bases:
            decision = evaluate_channel_calendar_moment(payload.trade_channel, base)
            points = [base]
            if decision.status not in {CalendarDecisionStatus.AVAILABLE_CLOSED, CalendarDecisionStatus.UNAVAILABLE}:
                points, _ = plan_supplements(payload.trade_channel, payload.cron_expr, payload.supplement, base)
            for index, point in enumerate(points):
                if point > start or (payload.after is None and point == start):
                    expanded.append(point)
                    metadata[point] = {
                        "base_scheduled_at": base,
                        "index": index,
                        "effective_count": len(points) - 1,
                        "is_last": index == len(points) - 1,
                    }
        scheduled = sorted(set(expanded))[: payload.limit + 1]
    has_more = len(scheduled) > payload.limit
    page = scheduled[: payload.limit]
    items = []
    for scheduled_at in page:
        local_time = scheduled_at.astimezone(SCHEDULER_TIMEZONE)
        decision = evaluate_channel_calendar_moment(payload.trade_channel, local_time)
        items.append(
            SchedulePreviewItem(
                scheduled_at=local_time,
                **metadata.get(scheduled_at, {}),
                calendar_day=decision.day,
                calendar_status=decision.status,
                action=(
                    "skip"
                    if decision.status in {CalendarDecisionStatus.AVAILABLE_CLOSED, CalendarDecisionStatus.UNAVAILABLE}
                    else "execute"
                ),
                unavailable_reason=decision.unavailable_reason,
                calendar_id=decision.calendar_id,
                label=decision.label,
                reason_code=decision.reason_code,
            )
        )
    return SchedulePreviewResponse(
        evaluated_at=evaluated_at,
        calendar=calendar,
        items=items,
        next_cursor=items[-1].scheduled_at if items else None,
        has_more=has_more,
    )


@router.get("/{account_id}/activity/symbols", response_model=ActivitySymbolListPublic)
async def account_activity_symbols(
    session: SessionDep,
    account_id: int,
    since: str,
    until: str,
    symbol: Annotated[str | None, Query()] = None,
) -> ActivitySymbolListPublic:
    """实时按品种汇总；时间窗必填。"""
    await _get_account_or_404(session, account_id)
    statement = (
        sql_select(source_trade, col(ExecuteRecord.execution_id))
        .join(ExecuteRecord, col(ExecuteRecord.id) == source_trade.c.record_id)
        .where(
            col(ExecuteRecord.account_id) == account_id,
            *_time_clauses(col(ExecuteRecord.created_at), since, until),
        )
        .order_by(desc(col(ExecuteRecord.created_at)), desc(col(ExecuteRecord.id)), source_trade.c.trade_index)
        .execution_options(yield_per=100)
    )
    if symbol is not None:
        statement = statement.where(source_trade.c.symbol == symbol)
    rows = await session.stream(statement)
    grouped: dict[str, list[dict]] = {}
    try:
        async for row in rows.mappings():
            trade = {**projected_trade(row), "record_id": row["record_id"], "execution_id": row["execution_id"]}
            grouped.setdefault(str(trade["symbol"]), []).append(trade)
    finally:
        await rows.close()
    data = [
        ActivitySymbolRowPublic(
            symbol=name,
            summary=CostSummary.model_validate(summarize(fills)),
            last_time=int(max(fill["time"] for fill in fills)),
            n_trades=len(fills),
            trades=fills if symbol else [],
        )
        for name, fills in grouped.items()
    ]
    return ActivitySymbolListPublic(data=data, count=len(data))


@router.get("/{account_id}/activity", response_model=AccountActivityListPublic)
async def account_activity(
    session: SessionDep,
    account_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    since: Annotated[str | None, Query()] = None,
    until: Annotated[str | None, Query()] = None,
) -> AccountActivityListPublic:
    """合并执行与休市记录，按发生时间倒序分页。"""
    await _get_account_or_404(session, account_id)
    # SQLite 的旧式事务模式下 SELECT 不会主动开启读事务；SAVEPOINT
    # 让页头、总数和详情使用同一快照，同时保留调用者已有的事务。
    async with session.begin_nested():
        return await _read_activity_page(session, account_id, skip, limit, since, until)


async def _read_activity_page(
    session: AsyncSession,
    account_id: int,
    skip: int,
    limit: int,
    since: str | None,
    until: str | None,
) -> AccountActivityListPublic:
    """只合并标量页头，随后读取当前页需要的执行与休市详情。"""
    execution_where = [
        col(ExecuteRecord.account_id) == account_id,
        *_time_clauses(col(ExecuteRecord.created_at), since, until),
    ]
    skip_where = [
        col(ScheduleSkip.account_id) == account_id,
        *_time_clauses(col(ScheduleSkip.triggered_at), since, until),
    ]
    execution_count = await session.scalar(select(func.count()).select_from(ExecuteRecord).where(*execution_where))
    skip_count = await session.scalar(select(func.count()).select_from(ScheduleSkip).where(*skip_where))
    headers = union_all(
        select(
            col(ExecuteRecord.created_at).label("occurred_at"),
            col(ExecuteRecord.id).label("id"),
            literal(0).label("kind"),
        ).where(*execution_where),
        select(col(ScheduleSkip.triggered_at), col(ScheduleSkip.id), literal(1)).where(*skip_where),
    ).subquery()
    # 原 Python 稳定排序在时间和 ID 都相同时先放执行记录。
    page = (
        await session.execute(
            sql_select(headers)
            .order_by(headers.c.occurred_at.desc(), headers.c.id.desc(), headers.c.kind)
            .offset(skip)
            .limit(limit)
        )
    ).all()
    execution_ids = [row.id for row in page if row.kind == 0]
    skip_ids = [row.id for row in page if row.kind == 1]
    execution_rows = (
        (await session.execute(select(ExecuteRecord).where(col(ExecuteRecord.id).in_(execution_ids)))).scalars().all()
    )
    skip_rows = (await session.execute(select(ScheduleSkip).where(col(ScheduleSkip.id).in_(skip_ids)))).scalars().all()

    summaries = await read_execution_summaries(session, execution_rows)
    trades = await read_projected_trades(session, execution_ids)
    from axile.server.business_facts.assets import read_result_asset_totals

    totals = await read_result_asset_totals(session, execution_ids)
    activities: dict[tuple[int, int], ExecutionActivity | ScheduleSkipActivity] = {
        (0, cast(int, row.id)): _compact_execution_activity(
            row, summaries[cast(int, row.id)], trades.get(cast(int, row.id), []), totals.get(cast(int, row.id))
        )
        for row in execution_rows
    }
    activities.update(
        (
            (1, cast(int, row.id)),
            ScheduleSkipActivity(
                occurred_at=row.triggered_at,
                id=cast(int, row.id),
                channel=row.channel,
                calendar_id=row.calendar_id,
                calendar_day=row.calendar_day,
                calendar_label=row.calendar_label,
                reason_code=cast("ScheduleSkipReason", row.reason_code),
            ),
        )
        for row in skip_rows
    )
    return AccountActivityListPublic(
        data=[activities[(row.kind, row.id)] for row in page],
        count=int(execution_count or 0) + int(skip_count or 0),
    )


__all__ = [
    "SchedulePreviewCalendar",
    "SchedulePreviewItem",
    "SchedulePreviewRequest",
    "SchedulePreviewResponse",
    "account_activity",
    "account_activity_symbols",
    "router",
]
