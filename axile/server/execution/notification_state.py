"""后台通知线程使用独立同步连接保存成功摘要。"""

from functools import lru_cache

from sqlalchemy import Engine, create_engine, event, literal, select
from sqlalchemy.dialects.sqlite import insert
from sqlmodel import col

from axile.common.config import settings
from axile.server.db.models.account_notification import AccountNotificationState
from axile.server.db.models.system_notification import SystemNotificationState, SystemNotificationStatePublic


@lru_cache(maxsize=1)
def _notification_engine() -> Engine:
    """线程与 worker 各自创建连接池，不复用 asyncio session。"""
    engine = create_engine(str(settings.sqlalchemy_database_uri).replace("sqlite+aiosqlite:", "sqlite:"))

    @event.listens_for(engine, "connect")
    def configure_connection(connection, _record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    return engine


def record_system_notification_result(result: SystemNotificationStatePublic) -> None:
    """保存最近完成的真实告警；试跑由调用方隔离，不进入此函数。"""
    if result.finished_at is None:
        raise ValueError("真实告警结果必须包含完成时间")
    values = result.model_dump()
    statement = insert(SystemNotificationState).values(id=1, **values)
    statement = statement.on_conflict_do_update(
        index_elements=["id"],
        set_=values,
        where=col(SystemNotificationState.finished_at).is_(None)
        | (col(SystemNotificationState.finished_at) < result.finished_at),
    )
    with _notification_engine().begin() as connection:
        connection.execute(statement)


def get_system_notification_result() -> SystemNotificationStatePublic:
    """读取持久化摘要；从未发送时返回空结果。"""
    with _notification_engine().connect() as connection:
        row = (
            connection.execute(select(SystemNotificationState).where(col(SystemNotificationState.id) == 1))
            .mappings()
            .first()
        )
    return SystemNotificationStatePublic.model_validate(dict(row)) if row else SystemNotificationStatePublic()


def record_notification_result(
    account_id: int,
    execution_id: str | None,
    finished_at: str,
    ok: bool,
    error: str | None,
    event_type: str = "execution.finished",
) -> None:
    """保存最近完成的通知结果；失败保留此前成功摘要。"""
    from axile.server.db.models import Account

    values = {
        "last_attempt_event_type": event_type,
        "last_attempt_at": finished_at,
        "last_attempt_execution_id": execution_id,
        "last_attempt_ok": ok,
        "last_attempt_error": error,
    }
    if ok:
        values.update(last_success_at=finished_at, execution_id=execution_id)
    columns = ["account_id", *values]
    statement = insert(AccountNotificationState).from_select(
        columns,
        select(col(Account.id), *(literal(value) for value in values.values())).where(col(Account.id) == account_id),
    )
    statement = statement.on_conflict_do_update(
        index_elements=["account_id"],
        set_=values,
        where=(col(AccountNotificationState.last_attempt_at).is_(None))
        | (col(AccountNotificationState.last_attempt_at) < finished_at),
    )
    with _notification_engine().begin() as connection:
        connection.execute(statement)
