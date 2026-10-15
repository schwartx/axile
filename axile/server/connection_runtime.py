"""主进程的当前会话真源；读取只使用本地存活信息与监控缓存。"""

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import RLock

from axile.executor.algorithms.utils.clock import clock_monotonic, clock_now_iso
from axile.executor.connection_state import ConnectionObservation, ConnectionStatus
from axile.server.db.models.account_connection import AccountConnectionPublic, AccountSessionStatus

_STALE_SECONDS = 5.0
_lock = RLock()
_STATUS: dict[ConnectionStatus, AccountSessionStatus] = {
    "ok": "ready",
    "error": "unavailable",
    "cooldown": "unavailable",
    "connecting": "connecting",
    "recovering": "recovering",
    "disconnected": "disconnected",
}


@dataclass
class _LiveSession:
    config_key: str
    instance_id: str
    alive: Callable[[], bool]
    observation: ConnectionObservation | None = None
    sequence: int = 0
    sampled: float = field(default_factory=clock_monotonic)
    sampled_at: str | None = None
    ended: bool = False
    retry_deadline: float = 0
    view_status: AccountSessionStatus | None = None
    updated_at: str | None = None


_sessions: dict[int, _LiveSession] = {}


def register_session(binding, alive: Callable[[], bool]) -> None:
    """新实例接管当前状态，历史记录不能代替新实例的就绪报告。"""
    with _lock:
        _sessions[binding.account_id] = _LiveSession(binding.config_key, binding.instance_id, alive)
    from axile.server.connection_events import connection_hub

    connection_hub.changed(binding.account_id)
    from axile.server.orders_live import orders_hub

    orders_hub.changed(binding.account_id)
    connection_hub.sampled(binding.account_id, _STALE_SECONDS + 0.01)


def observe_session(binding, sequence: int, observation: ConnectionObservation | None) -> None:
    """心跳只刷新采样时间，不能伪造新的渠道访问成功时间。"""
    with _lock:
        session = _sessions.get(binding.account_id)
        if (
            session is None
            or session.ended
            or session.instance_id != binding.instance_id
            or session.config_key != binding.config_key
        ):
            return
        recovered = clock_monotonic() - session.sampled > _STALE_SECONDS
        changed = sequence > session.sequence
        session.sampled, session.sampled_at = clock_monotonic(), clock_now_iso()
        if sequence > session.sequence:
            session.sequence = sequence
            if observation != session.observation:
                session.updated_at = clock_now_iso()
                seconds = (observation.error or {}).get("retry_after_seconds", 0) if observation else 0
                session.retry_deadline = clock_monotonic() + (
                    float(seconds) if isinstance(seconds, (int, float)) else 0
                )
            session.observation = observation
    from axile.server.connection_events import connection_hub

    connection_hub.sampled(binding.account_id, _STALE_SECONDS + 0.01)
    if recovered or changed:
        connection_hub.changed(binding.account_id)
        from axile.server.orders_live import orders_hub

        orders_hub.changed(binding.account_id)


def session_deadlines() -> dict[int, float]:
    """为新订阅恢复一次性过期通知，不安排周期采样。"""
    with _lock:
        now = clock_monotonic()
        return {
            account_id: max(0, session.sampled + _STALE_SECONDS + 0.01 - now)
            for account_id, session in _sessions.items()
            if not session.ended
        }


def end_session(binding, message: str, *, failed: bool = False) -> bool:
    """会话结束保留原因；旧实例不能结束已经替换的新实例。"""
    with _lock:
        session = _sessions.get(binding.account_id)
        if session is None or session.instance_id != binding.instance_id or session.ended:
            return False
        session.ended = True
        if failed:
            session.retry_deadline = clock_monotonic() + 30
        session.observation = ConnectionObservation("error" if failed else "disconnected", clock_now_iso(), message)
    from axile.server.connection_events import connection_hub

    connection_hub.changed(binding.account_id)
    from axile.server.orders_live import orders_hub

    orders_hub.changed(binding.account_id)
    return True


def clear_sessions() -> None:
    """服务启动时重置进程内会话，旧数据库观察只作为历史。"""
    with _lock:
        _sessions.clear()
    from axile.server.connection_events import connection_hub

    connection_hub.changed()
    from axile.server.orders_live import reset_orders

    reset_orders()


def forget_session(account_id: int) -> None:
    """账户删除后清除本地状态，避免数据库复用 ID 时继承旧会话。"""
    with _lock:
        _sessions.pop(account_id, None)
    from axile.server.connection_events import connection_hub

    connection_hub.changed(account_id)
    from axile.server.orders_live import forget_orders

    forget_orders(account_id)


def current_session(account_id: int, config_key: str) -> AccountConnectionPublic:
    """返回有存活依据的当前状态；没有实例时返回未建立。"""
    with _lock:
        session = _sessions.get(account_id)
        if session is None or session.config_key != config_key:
            return AccountConnectionPublic()
        observation = session.observation
        alive = not session.ended and session.alive()
        if not alive:
            status = "unavailable" if observation and observation.status == "error" else "disconnected"
            message = observation.message if session.ended and observation else "账户执行器已退出，会话已结束"
        elif clock_monotonic() - session.sampled > _STALE_SECONDS:
            status, message = "unknown", "执行器监控已过期，当前会话状态待确认"
        elif observation is None:
            status, message = "uninitialized", "执行器尚未建立账户会话"
        else:
            status = _STATUS[observation.status]
            message = observation.message
        if status != session.view_status:
            session.view_status, session.updated_at = status, clock_now_iso()
        return AccountConnectionPublic(
            status=status,
            message=message,
            worker_alive=alive,
            observed_at=session.updated_at,
            sampled_at=session.sampled_at,
            label=observation.label if observation and status == "unavailable" else None,
            retry_after_seconds=max(0, math.ceil(session.retry_deadline - clock_monotonic()))
            if status == "unavailable"
            else 0,
        )


def matches_session(binding) -> bool:
    """确认 IPC 来源仍是当前存活实例，拒绝替换后的迟到帧。"""
    with _lock:
        session = _sessions.get(binding.account_id)
        return bool(
            session
            and not session.ended
            and session.instance_id == binding.instance_id
            and session.config_key == binding.config_key
            and session.alive()
        )
