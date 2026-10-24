"""按已确认原生报撤调用产生持久化到值预警，与交易额度独立。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from axile.executor.algorithms.utils.clock import clock_now_ms
from axile.server.native_store import native_operation
from axile.server.order_submissions import submission_database


class SubmissionMonitorUpdate(BaseModel):
    """两个自然日调用阈值；空值禁用，正整数启用。"""

    model_config = ConfigDict(extra="forbid")

    order_limit: int | None = Field(default=None, strict=True, ge=1, le=1_000_000_000)
    combined_limit: int | None = Field(default=None, strict=True, ge=1, le=1_000_000_000)


@dataclass(slots=True, kw_only=True)
class SubmissionMonitorSettings:
    """保存的配置及版本；时区沿用保存时的账户风控时区。"""

    order_limit: int | None = None
    combined_limit: int | None = None
    timezone: str = "Asia/Shanghai"
    revision: int = 0
    updated_at_ms: int | None = None


@dataclass(slots=True, kw_only=True)
class SubmissionWarning:
    """记录触发当时的数字，不用后续快照替换历史事实。"""

    event_uid: str
    account_id: int
    metric: Literal["order_calls", "combined_calls"]
    value: int
    limit: int
    control_date: str
    timezone: str
    occurred_at_ms: int
    revision: int
    submission_uid: str | None = None
    execution_id: str | None = None
    source: str


@dataclass(slots=True, kw_only=True)
class SubmissionMetric:
    """仅在接入范围内判断是否达到阈值；未知调用不推断为零。"""

    metric: Literal["order_calls", "combined_calls"]
    value: int
    unknown: int
    limit: int | None
    state: Literal["disabled", "below", "unknown", "reached"]


@dataclass(slots=True, kw_only=True)
class SubmissionMonitorPublic:
    """配置、当前自然日状态和触发事实共用同一统计口径。"""

    settings: SubmissionMonitorSettings
    control_date: str
    scope: str = "instrumented_ctp_calls"
    sampled_at_ms: int = field(default_factory=clock_now_ms)
    metrics: list[SubmissionMetric]
    warnings: list[SubmissionWarning]


async def save_submission_monitor(account_id: int, update: SubmissionMonitorUpdate, timezone: str) -> dict:
    """保存后立即重评估；重复保存相同配置不制造新版本或重复预警。"""
    receipt = await native_operation(
        "save_submission_monitor",
        {"account_id": account_id, **update.model_dump(), "timezone": timezone, "now_ms": clock_now_ms()},
        database=submission_database(),
    )
    if receipt["changed"]:
        logger.info(
            "原生报撤监测配置更新 account_id={} previous={} current={}",
            account_id,
            receipt["previous"],
            receipt["current"],
        )
    return receipt["monitor"]
