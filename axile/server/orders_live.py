"""主服务委托镜像与版本通知；IPC 接收线程不访问数据库或柜台。"""

from __future__ import annotations

from copy import deepcopy
from threading import RLock
from typing import Any

from axile.executor.algorithms.utils.clock import clock_monotonic
from axile.server.connection_events import ConnectionEventHub
from axile.server.connection_state import ConnectionBinding, connection_config_key

orders_hub = ConnectionEventHub()
_lock = RLock()
_frames: dict[int, tuple[ConnectionBinding, dict[str, Any], float]] = {}


def consume_orders_snapshot(frame: dict[str, Any] | None) -> None:
    """只接收当前实例的快照；重复采样只续期，不制造订单更新。"""
    if not frame:
        return
    from axile.server.connection_runtime import matches_session

    binding = ConnectionBinding(**frame["binding"])
    if not matches_session(binding):
        return
    with _lock:
        if not matches_session(binding):
            return
        previous = _frames.get(binding.account_id)
        if frame.get("heartbeat"):
            if previous is None or previous[0] != binding:
                return
            if frame.get("generation", 0) != previous[1].get("generation", 0):
                return
            value = previous[1]
            changed = False
        else:
            value = frame["snapshot"]
            if previous and previous[0] == binding:
                generation, previous_generation = value.get("generation", 0), previous[1].get("generation", 0)
                if (
                    generation < previous_generation
                    or generation == previous_generation
                    and value["version"] < previous[1]["version"]
                ):
                    return
            changed = previous is None or previous[0] != binding or previous[1] != value
        recovered = previous is not None and clock_monotonic() - previous[2] > 5
        _frames[binding.account_id] = binding, deepcopy(value), clock_monotonic()
    orders_hub.sampled(binding.account_id, 5.01)
    if changed or recovered:
        orders_hub.changed(binding.account_id)


def read_orders_snapshot(account: Any) -> dict[str, Any] | None:
    """读取镜像；连接、配置或采样失效时保留数据并撤销可信状态。"""
    from axile.server.connection_runtime import current_session, matches_session

    key = connection_config_key(account.trade_channel, account.account_config)
    with _lock:
        cached = _frames.get(account.id)
        if cached is None or cached[0].config_key != key:
            return None
        binding, value, sampled = cached
        result = deepcopy(value)
    current = current_session(account.id, key)
    stale = not matches_session(binding) or current.status != "ready" or clock_monotonic() - sampled > 5
    if stale:
        result.update(complete=False, error=current.message or "账户委托同步已中断，状态待确认")
    return result


async def orders_frames(account_ids: set[int] | None = None) -> list[dict[str, Any]]:
    """组装 REST 与 SSE 共用的公开快照，不触发任何渠道请求。"""
    from sqlalchemy import select
    from sqlmodel import col

    from axile.server.account_orders import query_account_orders
    from axile.server.core.db import SessionLocal
    from axile.server.db.models.account import Account

    statement = select(Account)
    if account_ids is not None:
        statement = statement.where(col(Account.id).in_(account_ids))
    async with SessionLocal() as session:
        accounts = (await session.execute(statement)).scalars().all()
        return [
            {
                "account_id": account.id,
                "snapshot": (await query_account_orders(account.snapshot())).model_dump(mode="json"),
            }
            for account in accounts
        ]


def forget_orders(account_id: int) -> None:
    """账户删除时清理镜像和回执，避免 ID 复用继承旧状态。"""
    from axile.server.account_orders import _states

    with _lock:
        _frames.pop(account_id, None)
    _states.pop(account_id, None)
    orders_hub.changed(account_id)


def reset_orders() -> None:
    """服务会话重置时丢弃旧进程镜像，重新等待真实基线。"""
    from axile.server.account_orders import _states

    with _lock:
        _frames.clear()
    _states.clear()
    orders_hub.changed()
