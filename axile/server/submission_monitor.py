"""按已确认原生报撤调用产生持久化到值预警，与交易额度独立。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from axile.executor.algorithms.utils.clock import clock_now_ms
from axile.executor.order_submissions import OrderSubmissionFact
from axile.server.order_submissions import (
    submission_database,
    submission_window,
)


class SubmissionMonitorUpdate(BaseModel):
    """两个自然日调用阈值；空值禁用，正整数启用。"""

    model_config = ConfigDict(extra="forbid")

    order_limit: int | None = Field(default=None, strict=True, ge=1, le=1_000_000_000)
    combined_limit: int | None = Field(default=None, strict=True, ge=1, le=1_000_000_000)


class SubmissionMonitorSettings(SubmissionMonitorUpdate):
    """保存的配置及版本；时区沿用保存时的账户风控时区。"""

    timezone: str = "Asia/Shanghai"
    revision: int = 0
    updated_at_ms: int | None = None


class SubmissionWarning(BaseModel):
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


class SubmissionMetric(BaseModel):
    """仅在接入范围内判断是否达到阈值；未知调用不推断为零。"""

    metric: Literal["order_calls", "combined_calls"]
    value: int
    unknown: int
    limit: int | None
    state: Literal["disabled", "below", "unknown", "reached"]


class SubmissionMonitorPublic(BaseModel):
    """配置、当前自然日状态和触发事实共用同一统计口径。"""

    settings: SubmissionMonitorSettings
    control_date: str
    scope: str = "instrumented_ctp_calls"
    sampled_at_ms: int = Field(default_factory=clock_now_ms)
    metrics: list[SubmissionMetric]
    warnings: list[SubmissionWarning]


def _day(timestamp: int, timezone: str) -> str:
    return datetime.fromtimestamp(timestamp / 1000, ZoneInfo(timezone)).date().isoformat()


def record_submission_warnings(
    connection: sqlite3.Connection,
    account_id: int,
    timestamp: int,
    *,
    fact: OrderSubmissionFact | None = None,
) -> None:
    """在调用事实的同一写事务内去重预警，并发及回报重放不会重复触发。"""
    row = connection.execute("SELECT data FROM order_submission_monitor WHERE account_id=?", (account_id,)).fetchone()
    if row is None:
        return
    config = SubmissionMonitorSettings.model_validate_json(row[0])
    day = _day(timestamp, config.timezone)
    if config.updated_at_ms is not None and day < _day(config.updated_at_ms, config.timezone):
        # 新配置不追溯应用于上一个自然日的迟到回报。
        return
    start, end = submission_window(day, config.timezone)
    rows = connection.execute(
        "SELECT coalesce(json_extract(data, '$.operation'), 'place_order'), count(*) "
        "FROM order_submission WHERE account_id=? AND occurred_at_ms>=? AND occurred_at_ms<? "
        "AND json_extract(data, '$.send_status') IN ('accepted', 'rejected') GROUP BY 1",
        (account_id, start, end),
    ).fetchall()
    counts = dict(rows)
    orders = counts.get("place_order", 0)
    metrics: tuple[tuple[Literal["order_calls", "combined_calls"], int, int | None], ...] = (
        ("order_calls", orders, config.order_limit),
        ("combined_calls", orders + counts.get("cancel_order", 0), config.combined_limit),
    )
    for metric, value, limit in metrics:
        if limit is None or value < limit:
            continue
        uid = f"{account_id}:{config.revision}:{day}:{metric}"
        warning = SubmissionWarning(
            event_uid=uid,
            account_id=account_id,
            metric=metric,
            value=value,
            limit=limit,
            control_date=day,
            timezone=config.timezone,
            occurred_at_ms=clock_now_ms(),
            revision=config.revision,
            submission_uid=None if fact is None else fact.submission_uid,
            execution_id=None if fact is None else fact.execution_id,
            source="configuration" if fact is None else fact.source,
        )
        connection.execute(
            "INSERT INTO order_submission_warning (event_uid, account_id, occurred_at_ms, data) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(event_uid) DO NOTHING",
            (uid, account_id, warning.occurred_at_ms, warning.model_dump_json()),
        )


def save_submission_monitor(account_id: int, update: SubmissionMonitorUpdate, timezone: str) -> None:
    """保存后立即重评估；重复保存相同配置不制造新版本或重复预警。"""
    with closing(sqlite3.connect(submission_database(), timeout=5)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT data FROM order_submission_monitor WHERE account_id=?", (account_id,)
        ).fetchone()
        old = (
            SubmissionMonitorSettings(timezone=timezone)
            if row is None
            else SubmissionMonitorSettings.model_validate_json(row[0])
        )
        if row is not None and (old.order_limit, old.combined_limit, old.timezone) == (
            update.order_limit,
            update.combined_limit,
            timezone,
        ):
            return
        now = clock_now_ms()
        config = SubmissionMonitorSettings(
            **update.model_dump(), timezone=timezone, revision=old.revision + 1, updated_at_ms=now
        )
        connection.execute(
            "INSERT INTO order_submission_monitor (account_id, data) VALUES (?, ?) "
            "ON CONFLICT(account_id) DO UPDATE SET data=excluded.data",
            (account_id, config.model_dump_json()),
        )
        record_submission_warnings(connection, account_id, now)
        logger.info(
            "原生报撤监测配置更新 account_id={} previous={} current={}",
            account_id,
            old.model_dump(),
            config.model_dump(),
        )
