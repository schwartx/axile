"""为执行器与独立脚本提供共用的持久化报单记录入口。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from axile.common.config import settings
from axile.executor.order_submissions import OrderSubmissionFact, OrderSubmissionRecorder
from axile.server.db.models.order_submission import OrderSubmission


def create_order_submission_recorder(account_id: int, *, source: str = "script") -> OrderSubmissionRecorder:
    """使用服务数据库创建 CTP 记录器，供原生脚本和主程序共同调用。

    Notes
    -----
    脚本须使用同一个服务数据库及账户 ID；本函数不连接柜台、不发送订单，
    也不代替脚本接入发送前的账户控制。数据库须先完成迁移。
    """
    if account_id < 1:
        raise ValueError("account_id 必须为正整数")
    database = submission_database()
    return OrderSubmissionRecorder(account_id, lambda fact: _save_fact(database, fact), source=source)


def submission_database() -> str:
    """返回共用服务数据库；不允许静默写入另一份临时数据库。"""
    url = make_url(str(settings.sqlalchemy_database_uri))
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise ValueError("原生报单记录需要持久化 SQLite 服务数据库")
    return url.database


def _save_fact(database: str, fact: OrderSubmissionFact) -> None:
    """独立短事务可从 SDK 回调线程调用；旧回报不得覆盖更高版本事实。"""
    with closing(sqlite3.connect(database, timeout=5)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            "INSERT INTO order_submission (submission_uid, account_id, occurred_at_ms, revision, data) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(submission_uid) DO UPDATE SET "
            "occurred_at_ms=excluded.occurred_at_ms, revision=excluded.revision, data=excluded.data "
            "WHERE excluded.revision > order_submission.revision",
            (fact.submission_uid, fact.account_id, fact.occurred_at_ms, fact.revision, fact.model_dump_json()),
        )

        from axile.server.submission_monitor import record_submission_warnings

        record_submission_warnings(connection, fact.account_id, fact.occurred_at_ms, fact=fact)


class OrderSubmissionSummary(BaseModel):
    """仅汇总已接入入口的调用事实；不保证覆盖柜台全部订单。"""

    scope: str = "instrumented_ctp_calls"
    recorded: int = 0
    sync_accepted: int = 0
    sync_rejected: int = 0
    unknown: int = 0
    broker_acknowledged: int = 0
    broker_rejected: int = 0
    cancel_recorded: int = 0
    cancel_sync_accepted: int = 0
    cancel_sync_rejected: int = 0
    cancel_unknown: int = 0

    @property
    def order_calls(self) -> int:
        """已确认调用次数，包含同步拒绝，不把待确认事实当作已发送。"""
        return self.sync_accepted + self.sync_rejected

    @property
    def combined_calls(self) -> int:
        """报单与撤单调用合计，不等于撤单成功数或柜台委托条数。"""
        return self.order_calls + self.cancel_sync_accepted + self.cancel_sync_rejected


class OrderSubmissionPage(BaseModel):
    """最近报单事实及整个自然日的汇总。"""

    control_date: str
    timezone: str
    summary: OrderSubmissionSummary
    data: list[OrderSubmissionFact]


def submission_window(control_date: str, timezone: str) -> tuple[int, int]:
    """按账户时区计算自然日，正确处理夏令时跨日。"""
    day = date.fromisoformat(control_date)
    tz = ZoneInfo(timezone)
    start = datetime.combine(day, datetime.min.time(), tz)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tz)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


async def query_order_submission_summary(
    session: AsyncSession, account_id: int, control_date: str, timezone: str
) -> OrderSubmissionSummary:
    """从发送时间汇总，不受业务放行时间、guard 切换或分页影响。"""
    start, end = submission_window(control_date, timezone)
    send = col(OrderSubmission.data)["send_status"].as_string()
    broker = col(OrderSubmission.data)["broker_status"].as_string()
    operation = func.coalesce(col(OrderSubmission.data)["operation"].as_string(), "place_order")
    statement = (
        select(operation, send, broker, func.count())
        .where(
            OrderSubmission.account_id == account_id,
            OrderSubmission.occurred_at_ms >= start,
            OrderSubmission.occurred_at_ms < end,
        )
        .group_by(operation, send, broker)
    )
    summary = OrderSubmissionSummary()
    for operation, send_status, broker_status, count in (await session.execute(statement)).all():
        if operation == "cancel_order":
            summary.cancel_recorded += count
            if send_status == "accepted":
                summary.cancel_sync_accepted += count
            elif send_status == "rejected":
                summary.cancel_sync_rejected += count
            else:
                summary.cancel_unknown += count
            continue
        summary.recorded += count
        if send_status == "accepted":
            summary.sync_accepted += count
        elif send_status == "rejected":
            summary.sync_rejected += count
        else:
            summary.unknown += count
        if broker_status == "acknowledged":
            summary.broker_acknowledged += count
        elif broker_status == "rejected":
            summary.broker_rejected += count
    return summary


async def query_order_submissions(
    session: AsyncSession, account_id: int, control_date: str, timezone: str, *, limit: int = 100
) -> list[OrderSubmissionFact]:
    """返回指定自然日的最近调用流水；分页限制不影响汇总。"""
    start, end = submission_window(control_date, timezone)
    statement = (
        select(OrderSubmission.data)
        .where(
            OrderSubmission.account_id == account_id,
            OrderSubmission.occurred_at_ms >= start,
            OrderSubmission.occurred_at_ms < end,
        )
        .order_by(col(OrderSubmission.occurred_at_ms).desc(), OrderSubmission.submission_uid)
        .limit(limit)
    )
    return [OrderSubmissionFact.model_validate(row) for row in (await session.execute(statement)).scalars().all()]
