"""连接状态的变更通知；只在订阅建立或状态变化时读取快照。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from threading import RLock
from uuid import uuid4

from axile.server.connection_runtime import public_session_views
from axile.server.native_store import native_operation

type Change = tuple[int, set[int] | None]


class ConnectionEventHub:
    """合并线程上报，队列满时合并变更账户，保留全部待发送状态。"""

    def __init__(self) -> None:
        self.stream_id = uuid4().hex
        self._version = 0
        self._lock = RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue[Change]] = set()
        self._deadlines: dict[int, asyncio.TimerHandle] = {}

    @property
    def version(self) -> int:
        """返回订阅快照使用的变更水位。"""
        with self._lock:
            return self._version

    def changed(self, account_id: int | None = None) -> None:
        """提交变更后唤醒订阅，不从通知线程访问数据库。"""
        with self._lock:
            self._version += 1
            if self._loop is not None and not self._loop.is_closed():
                change = (self._version, {account_id} if account_id is not None else None)
                for queue in self._subscribers:
                    self._loop.call_soon_threadsafe(self._deliver, queue, change)

    @staticmethod
    def _deliver(queue: asyncio.Queue[Change], change: Change) -> None:
        version, ids = change
        if queue.full():
            previous_version, previous_ids = queue.get_nowait()
            version = max(version, previous_version)
            ids = None if ids is None or previous_ids is None else ids | previous_ids
        queue.put_nowait((version, ids))

    def sampled(self, account_id: int, seconds: float) -> None:
        """心跳只延后一次性过期通知，不发送周期快照或访问柜台。"""
        with self._lock:
            if self._subscribers and self._loop is not None and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._arm, account_id, seconds)

    def _arm(self, account_id: int, seconds: float) -> None:
        if timer := self._deadlines.pop(account_id, None):
            timer.cancel()
        if self._subscribers and self._loop is not None:
            self._deadlines[account_id] = self._loop.call_later(max(0, seconds), self._expired, account_id)

    def _expired(self, account_id: int) -> None:
        self._deadlines.pop(account_id, None)
        self.changed(account_id)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[Change]]:
        """先订阅再读取快照，期间到达的变更留在有界队列。"""
        from axile.server.connection_runtime import session_deadlines

        queue: asyncio.Queue[Change] = asyncio.Queue(maxsize=1)
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._subscribers.add(queue)
        for account_id, seconds in session_deadlines().items():
            self._arm(account_id, seconds)
        try:
            yield queue
        finally:
            with self._lock:
                self._subscribers.discard(queue)
                if not self._subscribers:
                    for timer in self._deadlines.values():
                        timer.cancel()
                    self._deadlines.clear()
                    self._loop = None


connection_hub = ConnectionEventHub()


async def connection_frames(account_ids: set[int] | None = None) -> list[dict[str, object]]:
    """只读取账户与运行态缓存，绝不初始化执行器或探测交易连接。"""
    accounts = await native_operation(
        "connection_accounts",
        {
            "account_ids": sorted(account_ids) if account_ids is not None else None,
            "live_sessions": public_session_views(),
        },
    )
    return [
        {
            "account_id": value["id"],
            "connection": value["connection"],
        }
        for value in accounts
    ]
