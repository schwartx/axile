"""账户排程跳过记录。"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import Field

from axile.server.db.models.base import Model

type ScheduleSkipReason = Literal[
    "CALENDAR.CLOSED",
    "CALENDAR.NO_NIGHT_SESSION",
    "CALENDAR.SESSION_CLOSED",
    "CALENDAR.UNAVAILABLE",
    "BUSY",
]


class ActivityExecutionRecord(Model):
    """活动流里的执行摘要，不含 raw_input / 成交 / tick。"""

    id: int | None = None
    execution_id: str | None = None
    created_at: str
    is_success: int
    status: str | None = None
    task_status: str | None = None
    error: str | None = None
    outcome: str | None = None
    outcome_reason: str | None = None
    reason_code: str | None = None
    execution_kind: str | None = None
    symbol_results: dict[str, dict[str, Any]] = Field(default_factory=dict)
    total_asset: float | None = None
    summary: dict[str, Any] | None
    duration_sec: float | None = None
    trade_count: int | None = None


class ExecutionActivity(Model):
    """账户活动流中的执行记录。"""

    kind: Literal["execution"] = "execution"
    occurred_at: str
    record: ActivityExecutionRecord


class ScheduleSkipActivity(Model):
    """账户活动流中的休市跳过记录。"""

    kind: Literal["schedule_skip"] = "schedule_skip"
    occurred_at: str
    id: int
    channel: str
    calendar_id: str
    calendar_day: date
    calendar_label: str
    reason_code: ScheduleSkipReason = "CALENDAR.CLOSED"


__all__ = [
    "ActivityExecutionRecord",
    "ExecutionActivity",
    "ScheduleSkipActivity",
]
