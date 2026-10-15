"""当前进程内 guard 的只读注册表，独立于执行器及柜台连接。"""

from __future__ import annotations

from collections import deque
from threading import Lock, RLock
from typing import Any
from weakref import WeakKeyDictionary

from loguru import logger

from axile.executor.account_control.guard import AccountControlGuard

_lock = RLock()
_guards: dict[int, AccountControlGuard] = {}
_final: dict[int, dict[str, Any]] = {}
_updates: dict[int, deque[dict[str, Any]]] = {}
_operation_locks: WeakKeyDictionary[AccountControlGuard, Lock] = WeakKeyDictionary()


def register(guard: AccountControlGuard) -> None:
    """绑定新实例，旧实例的收尾不能撤销此绑定。"""
    if guard.account_id is None:
        return

    account_id = guard.account_id

    def outcome(event: Any) -> None:
        with _lock:
            data = event.model_dump(mode="json")
            _updates.setdefault(account_id, deque(maxlen=100)).append(data)
            final = _final.get(account_id)
            if final is not None:
                final["records"] = [data if row["event_uid"] == data["event_uid"] else row for row in final["records"]]

    guard.monitor_outcome_sink = outcome
    with _lock:
        _guards[guard.account_id] = guard
        _final.pop(guard.account_id, None)


def finish(guard: AccountControlGuard, *, committed: bool) -> None:
    """保留最后已知值，落库确认随最终快照一并发布。"""
    if guard.account_id is None:
        return
    try:
        frame = guard.monitor_snapshot()
    except Exception:
        logger.exception("流控最终监控快照失败，已提交计数不受影响")
        return
    frame.update(status="idle" if committed else "incomplete", committed=committed)
    with _lock:
        if _guards.get(guard.account_id) is guard:
            _guards.pop(guard.account_id, None)
            _final[guard.account_id] = frame


def snapshot(account_id: int) -> dict[str, Any] | None:
    """只复制 guard 状态，不创建执行器。"""
    with _lock:
        guard = _guards.get(account_id)
        final = _final.get(account_id)
        updates = list(_updates.get(account_id, ()))
    if guard is not None:
        frame = guard.monitor_snapshot()
    elif final is not None:
        frame = dict(final)
    else:
        return None
    frame["outcome_updates"] = updates
    return frame


def account_ids() -> list[int]:
    """列出本进程已有监控来源，不创建运行时。"""
    with _lock:
        return list(_guards.keys() | _final.keys())


def active_guard(account_id: int) -> AccountControlGuard | None:
    """供同账户人工委托操作共用正在运行的额度，不创建第二份基线。"""
    with _lock:
        return _guards.get(account_id)


def operation_lock(guard: AccountControlGuard) -> Lock:
    """人工操作与最终刷盘共用锁，避免收尾后才产生未计入的报撤次数。"""
    with _lock:
        return _operation_locks.setdefault(guard, Lock())
