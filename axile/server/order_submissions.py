"""为执行器与独立脚本提供共用的持久化报单记录入口。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from axile.common.config import settings
from axile.executor.algorithms.utils.clock import clock_now_ms
from axile.executor.order_submissions import OrderSubmissionFact, OrderSubmissionRecorder
from axile.server.native_store import native_operation_sync


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
    url = urlsplit(str(settings.sqlalchemy_database_uri))
    database = url.path.removeprefix("/")
    if url.scheme not in {"sqlite", "sqlite+aiosqlite"} or url.netloc or not database or database == ":memory:":
        raise ValueError("原生报单记录需要持久化 SQLite 服务数据库")
    return database


def _save_fact(database: str, fact: OrderSubmissionFact) -> None:
    """同步原生短事务可从 SDK 回调线程调用；提交返回后才允许发送。"""
    native_operation_sync(
        "save_order_submission", {"fact": fact.model_dump(mode="json"), "now_ms": clock_now_ms()}, database=database
    )


@dataclass(slots=True, kw_only=True)
class OrderSubmissionSummary:
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


def submission_window(control_date: str, timezone: str) -> tuple[int, int]:
    """按账户时区计算自然日，正确处理夏令时跨日。"""
    day = date.fromisoformat(control_date)
    tz = ZoneInfo(timezone)
    start = datetime.combine(day, datetime.min.time(), tz)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tz)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)
