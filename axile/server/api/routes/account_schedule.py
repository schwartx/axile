"""账户排程预览与统一活动流接口。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-not-found]
from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import AwareDatetime, BaseModel, Field
from starlette.concurrency import run_in_threadpool

from axile.channels import get_channel
from axile.common.trade_channel import TradeChannel
from axile.executor.algorithms.utils.clock import clock_now
from axile.executor.trading_calendar import SHINNY_COVERAGE_END, SHINNY_COVERAGE_START
from axile.server._native import _core
from axile.server.core.db import analysis_store
from axile.server.cron import SCHEDULER_TIMEZONE, is_blank_cron_expr, parse_cron_expr
from axile.server.db.models.account_settings import SupplementSettings
from axile.server.supplement_plan import plan_supplements
from axile.server.trading_calendar import (
    CalendarDecisionStatus,
    CalendarSkipReason,
    CalendarUnavailableReason,
    evaluate_channel_calendar_moment,
)

router = APIRouter()


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


@router.get("/{account_id}/activity/symbols")
async def account_activity_symbols(
    account_id: int,
    since: str,
    until: str,
    symbol: Annotated[str | None, Query()] = None,
) -> Response:
    """从原生读快照返回品种汇总及所选品种成交。"""
    try:
        content = await run_in_threadpool(
            _core.activity_symbols, analysis_store(), account_id, since, until, symbol or None
        )
        return Response(content=content, media_type="application/json")
    except _core.NativeError as error:
        if error.args[0] == "ACCOUNT_NOT_FOUND":
            raise HTTPException(404, detail="账户不存在") from error
        raise HTTPException(503, detail={"code": error.args[0], "message": error.args[1]}) from error


@router.get("/{account_id}/activity")
async def account_activity(
    account_id: int,
    skip: int = Query(0, ge=0, le=2**63 - 1),
    limit: int = Query(100, ge=1, le=500),
    since: Annotated[str | None, Query()] = None,
    until: Annotated[str | None, Query()] = None,
) -> Response:
    """从同一原生读快照返回执行与休市混排页。"""
    try:
        content = await run_in_threadpool(_core.journal, analysis_store(), account_id, skip, limit, since, until)
        return Response(content=content, media_type="application/json")
    except _core.NativeError as error:
        if error.args[0] == "ACCOUNT_NOT_FOUND":
            raise HTTPException(404, detail="账户不存在") from error
        raise HTTPException(503, detail={"code": error.args[0], "message": error.args[1]}) from error


__all__ = [
    "SchedulePreviewCalendar",
    "SchedulePreviewItem",
    "SchedulePreviewRequest",
    "SchedulePreviewResponse",
    "account_activity",
    "account_activity_symbols",
    "router",
]
