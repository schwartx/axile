"""连接故障按配置与最近成功访问去重，和状态保存使用同一事务。"""

import hashlib

from sqlalchemy.dialects.sqlite import insert
from sqlmodel import Session

from axile.common.notification_context import CONNECTION_FAILURE_EVENT
from axile.server.db.models.account import Account
from axile.server.db.models.supplement import NotificationEvent


def enqueue_connection_failure(session: Session, binding, state, values: dict) -> str:
    """只持久化业务说明；派发由服务已有的通知轮询唤醒。"""
    epoch = f"{binding.account_id}:{binding.config_key}:{state.last_success_at or 'initial'}"
    event_id = "connection:" + hashlib.sha256(epoch.encode()).hexdigest()
    account = session.get(Account, binding.account_id)
    presentation = values.get("presentation") or {}
    message = presentation.get("summary") or values.get("message") or "账户连接不可用"
    context = {
        "event_id": event_id,
        "event_type": CONNECTION_FAILURE_EVENT,
        "execution_id": None,
        "occurred_at": values["observed_at"],
        "account": {"id": binding.account_id, "name": account.name if account else ""},
        "error": {"type": "AccountConnectionFailure", "message": message, "traceback": ""},
        "is_test": False,
    }
    session.exec(
        insert(NotificationEvent)
        .values(
            id=event_id,
            account_id=binding.account_id,
            event_type=CONNECTION_FAILURE_EVENT,
            created_at=values["observed_at"],
            context=context,
            status="pending",
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )
    return event_id
