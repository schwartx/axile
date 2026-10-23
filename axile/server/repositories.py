"""账户、组合绑定与执行记录的原生读取入口。"""

from axile.server.db.models import Account, AccountSnapshot, ExecuteRecord
from axile.server.db.models.base import now_str
from axile.server.native_store import native_operation


async def get_account(account_id: int) -> Account | None:
    """一次读取账户配置与持久化运行摘要，不携带数据库会话。"""
    value = await native_operation("account_get", {"account_id": account_id})
    return Account.model_validate(value) if value is not None else None


async def get_account_snapshot(account_id: int) -> AccountSnapshot | None:
    """为执行编排读取普通账户快照。"""
    value = await native_operation("account_get", {"account_id": account_id})
    return AccountSnapshot.model_validate(value) if value is not None else None


async def stop_account_for_clear_positions(account_id: int) -> AccountSnapshot:
    """停止自动调度并取消补发，提交后返回清仓使用的账户快照。"""
    value = await native_operation("account_stop_for_clear", {"account_id": account_id, "now": now_str()})
    from axile.server.execution.notification_outbox import wake_notifications
    from axile.server.execution.supplements import reap_cancelled_queue

    await reap_cancelled_queue(account_id)
    wake_notifications(account_id)
    return AccountSnapshot.model_validate(value)


async def get_latest_account_id_by_portfolio_id(portfolio_id: int) -> int | None:
    """获取最近绑定且当前仍指向该组合的账户 ID。"""
    return await native_operation("portfolio_latest_account", {"portfolio_id": portfolio_id})


async def get_latest_portfolio_id_by_account_id(account_id: int) -> int | None:
    """获取账户最后一条绑定历史所指向的组合。"""
    return await native_operation("account_latest_portfolio", {"account_id": account_id})


async def get_latest_success_execute_record_by_account_id(account_id: int | None) -> ExecuteRecord | None:
    """读取账户最近一次成功执行的紧凑头记录。"""
    if account_id is None:
        return None
    rows = await native_operation(
        "recent_execution_records", {"account_id": account_id, "limit": 1, "success_only": True}
    )
    return ExecuteRecord.model_validate(rows[0]) if rows else None


async def get_recent_execute_records_by_account_id(account_id: int | None, limit: int = 20) -> list[ExecuteRecord]:
    """按 ID 倒序读取账户最近执行的紧凑头记录。"""
    if account_id is None:
        return []
    rows = await native_operation("recent_execution_records", {"account_id": account_id, "limit": limit})
    return [ExecuteRecord.model_validate(row) for row in rows]
