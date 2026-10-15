"""账户流控共享缓存与 SSE 广播；运行增量始终由实例快照整体替换。"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import select, update
from sqlmodel import col

from axile.executor.account_control.context import control_context_kind
from axile.executor.account_control.guard import AccountControlGuard, resolve_control_window
from axile.executor.account_control.models import AccountControlContextKind
from axile.executor.account_control.monitor import policy_fingerprint
from axile.executor.account_control.presets import resolve_account_control_policy
from axile.executor.algorithms.utils.clock import clock_now_ms
from axile.server.account_control import providers
from axile.server.account_control.ipc import sample
from axile.server.account_control.schema import AccountControlMonitorPublic
from axile.server.account_control.store import AccountControlStore
from axile.server.account_control.warnings import enqueue_quota_warnings, wake_pending_quota_warnings
from axile.server.core.db import SessionLocal
from axile.server.db.models.account import Account
from axile.server.db.models.account_control import AccountControlEvent
from axile.server.db.models.account_control_session import AccountControlSession


def _affected(session: AccountControlSession, control_date: str) -> bool:
    tz = ZoneInfo(session.timezone)
    start = datetime.fromtimestamp(session.started_at_ms / 1000, tz).date().isoformat()
    end = datetime.fromtimestamp((session.finished_at_ms or clock_now_ms()) / 1000, tz).date().isoformat()
    return start <= control_date <= end


async def recover_sessions() -> None:
    """启动时把上个服务进程未提交的会话标记为不完整。"""
    async with SessionLocal() as db:
        await db.execute(
            update(AccountControlSession)
            .where(col(AccountControlSession.status) == "active")
            .values(status="incomplete", finished_at_ms=clock_now_ms())
        )
        await db.commit()


class AccountControlMonitor:
    """服务进程单一采样任务；订阅者只读取缓存，不触发柜台访问。"""

    def __init__(self) -> None:
        self.stream_id = uuid4().hex
        self.version = 0
        self._published_at_ms: int | None = None
        self.frames: dict[int, dict[str, Any]] = {}
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self.task: asyncio.Task[None] | None = None
        self._local_tasks: dict[int, asyncio.Task[Any]] = {}
        self._sources: dict[int, list[tuple[Any, Any, bool]]] = {}
        self._outcomes: dict[str, str] = {}
        self._history: dict[int, tuple[object, AccountControlGuard, list[dict[str, Any]]]] = {}
        self._refresh_lock = asyncio.Lock()
        self._queued_warnings: dict[tuple[str, str], None] = {}

    def start(self) -> None:
        """启动进程唯一采样任务。"""
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run())

    async def close(self) -> None:
        """停止采样，不影响交易运行时。"""
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception:
                logger.exception("账户流控监控采样失败，交易执行不受影响")
            await asyncio.sleep(1)

    def envelope(self, data: list[dict[str, Any]], *, kind: str = "snapshot") -> dict[str, Any]:
        """用流会话与版本标识一帧。"""
        return {"stream_id": self.stream_id, "version": self.version, "kind": kind, "data": data}

    def snapshot(self) -> dict[str, Any]:
        """返回最近一次完整采样及其版本，不为订阅者单独触发采样。"""
        stale = self._published_at_ms is None or clock_now_ms() - self._published_at_ms > 5000
        data = [{**frame, "stale": True} if stale else frame for frame in self.frames.values()]
        return self.envelope(data)

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        """注册有界订阅，在取得首帧之前开始接收变化。"""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=16)
        self.subscribers.add(queue)
        try:
            yield queue
        finally:
            self.subscribers.discard(queue)

    def _publish(self, changed: list[dict[str, Any]], removed: list[int]) -> None:
        self.version += 1
        self._published_at_ms = clock_now_ms()
        frame = self.envelope(changed, kind="update")
        frame["removed"] = removed
        frame["freshness"] = [
            {"account_id": f["account_id"], "sampled_at_ms": f["sampled_at_ms"], "stale": f["stale"]}
            for f in self.frames.values()
        ]
        for queue in self.subscribers:
            if queue.full():
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(self.snapshot())
            else:
                queue.put_nowait(frame)

    async def refresh(self) -> None:
        """串行发布采样，防止重连和后台刷新竞争。"""
        async with self._refresh_lock:
            await self._refresh()

    async def _refresh(self) -> None:
        async with SessionLocal() as db:
            accounts = (await db.execute(select(Account))).scalars().all()
            sessions = (await db.execute(select(AccountControlSession))).scalars().all()
            await asyncio.gather(*(self._read_source(a.id) for a in accounts if a.id is not None))
            changed = []
            frames = {}
            present = set()
            for account in accounts:
                assert account.id is not None
                present.add(account.id)
                related = [s for s in sessions if s.account_id == account.id]
                frame = AccountControlMonitorPublic.model_validate(
                    await self._account_frame(db, account, related)
                ).model_dump(mode="json")
                await self._enqueue_warnings(db, account, related)
                previous = self.frames.get(account.id)
                if self._content(previous) != self._content(frame):
                    changed.append(frame)
                frames[account.id] = frame
            removed = [key for key in self.frames if key not in present]
            for key in removed:
                self._history.pop(key, None)
            await wake_pending_quota_warnings(db)
        # 整轮采样完成后同步提交，快照不会读到跨版本的半轮账户数据。
        self.frames = frames
        self._publish(changed, removed)

    async def _enqueue_warnings(self, db: Any, account: Account, sessions: list[AccountControlSession]) -> None:
        """采样冻结的触发事实，不从页面用量或当前保存配置推测预警。"""
        from axile.executor.account_control.warnings import AccountControlQuotaWarning

        assert account.id is not None
        pending = {}
        known_instances = {session.instance_id for session in sessions}
        for frame, _, _ in self._sources.get(account.id, []):
            if frame is None or frame.get("instance_id") not in known_instances:
                continue
            for raw in frame.get("quota_warnings", []):
                warning = AccountControlQuotaWarning.model_validate(raw)
                key = warning.event_id, frame["instance_id"]
                if key not in self._queued_warnings:
                    pending[key] = raw
        if not pending:
            return
        try:
            await enqueue_quota_warnings(db, account, pending.values())
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("运行中额度预警入队失败，等待下次采样")
        else:
            self._queued_warnings.update(dict.fromkeys(pending))
            while len(self._queued_warnings) > 2048:
                self._queued_warnings.pop(next(iter(self._queued_warnings)))

    @staticmethod
    def _content(frame: dict[str, Any] | None) -> object:
        if frame is None:
            return None
        return {key: value for key, value in frame.items() if key not in {"seq", "sampled_at_ms", "outcome_updates"}}

    async def _read_source(self, account_id: int) -> None:
        sources = []
        remote = sample(account_id)
        if remote is not None:
            sources.append(remote)
        if account_id in providers.account_ids():
            task = self._local_tasks.get(account_id)
            if task is None:
                task = asyncio.create_task(asyncio.to_thread(providers.snapshot, account_id))
                self._local_tasks[account_id] = task
            try:
                frame = await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
            except Exception:
                if task.done():
                    self._local_tasks.pop(account_id, None)
            else:
                self._local_tasks.pop(account_id, None)
                sources.insert(0, (frame, clock_now_ms(), False))
        self._sources[account_id] = sources

    async def _account_frame(self, db: Any, account: Account, sessions: list[AccountControlSession]) -> dict[str, Any]:
        assert account.id is not None
        policy = resolve_account_control_policy(account.account_control_preset, account.account_control_override)
        window = resolve_control_window(policy.timezone)
        # 目录查询可与交易并行；它的新会话不能接管交易实例或构成重复执行。
        execution_sessions = [
            session
            for session in sessions
            if control_context_kind(session.context_kind, session.execution_id)
            != AccountControlContextKind.ACCOUNT_OPERATION
        ]
        latest = max(execution_sessions, key=lambda s: s.id or 0, default=None)
        sources = self._sources.get(account.id, [])
        valid = [
            source
            for source in sources
            if source[0] is not None
            and (latest is None or source[0]["instance_id"] == latest.instance_id or source[0].get("incomplete", False))
        ]
        live, sampled, failed = valid[0] if valid else (None, None, any(source[2] for source in sources))
        if live is not None:
            closed = {s.instance_id for s in sessions if s.status != "active"}
            updates = [
                event
                for event in live.get("outcome_updates", [])
                if any(f":{instance}:" in event["event_uid"] for instance in closed)
            ]
            await self._update_outcomes(db, account.id, updates)
        incomplete = any(s.status == "incomplete" and _affected(s, window.control_date) for s in sessions)
        incomplete = incomplete or sum(s.status == "active" for s in execution_sessions) > 1
        if (
            (failed or (live is not None and live["status"] == "incomplete"))
            and latest is not None
            and latest.status == "active"
        ):
            latest.status = "incomplete"
            latest.finished_at_ms = clock_now_ms()
            await db.commit()
            incomplete = True
        active = (latest is not None and latest.status == "active") or (
            live is not None and live["status"] == "running" and live.get("incomplete", False)
        )
        if live is not None and (active or live.get("committed") is False):
            frame = dict(live)
            frame["sampled_at_ms"] = sampled
        elif active:
            frame = dict(self.frames.get(account.id) or await self._historical(db, account, policy, sessions))
            frame.update(status="unknown", sampled_at_ms=frame.get("sampled_at_ms"))
        else:
            frame = await self._historical(db, account, policy, sessions)
            previous = live or self.frames.get(account.id)
            if (
                incomplete
                and previous is not None
                and previous.get("control_date") == window.control_date
                and previous.get("instance_id") is not None
            ):
                frame = dict(previous)
                frame.update(status="incomplete", waiting=[], waiting_total=0)
        frame.pop("outcome_updates", None)
        now = clock_now_ms()
        frame.update(
            saved_policy=policy.model_dump(mode="json"),
            saved_fingerprint=policy_fingerprint(policy.model_dump(mode="json")),
            incomplete=incomplete or frame.get("incomplete", False) or frame["status"] == "incomplete",
            stale=bool(frame["status"] == "incomplete" or (active and (not sampled or now - sampled > 5000))),
            pending_policy=active
            and frame.get("actual_fingerprint") != policy_fingerprint(policy.model_dump(mode="json")),
        )
        if not active:
            frame["actual_policy"] = None
            frame["actual_fingerprint"] = None
        return frame

    async def _update_outcomes(self, db: Any, account_id: int, events: list[dict[str, Any]]) -> None:
        changed = False
        latest_events = {event["event_uid"]: event for event in events}
        for uid, event in latest_events.items():
            signature = str((event["outcome"], event["metadata"]))
            if self._outcomes.get(uid) == signature:
                continue
            result = await db.execute(
                update(AccountControlEvent)
                .where(col(AccountControlEvent.event_uid) == uid, col(AccountControlEvent.account_id) == account_id)
                .values(outcome=event["outcome"], metadata_=event["metadata"])
            )
            if result.rowcount:
                self._outcomes[uid] = signature
                changed = True
        if changed:
            await db.commit()
            self._history.pop(account_id, None)
        while len(self._outcomes) > 1000:
            self._outcomes.pop(next(iter(self._outcomes)))

    async def _historical(
        self, db: Any, account: Account, policy: Any, sessions: list[AccountControlSession]
    ) -> dict[str, Any]:
        assert account.id is not None
        window = resolve_control_window(policy.timezone)
        signature = (
            policy_fingerprint(policy.model_dump(mode="json")),
            window.minute_bucket_start,
            tuple((s.instance_id, s.status) for s in sessions),
        )
        cached = self._history.get(account.id)
        if cached is not None and cached[0] == signature:
            guard, records = cached[1:]
        else:
            store = AccountControlStore(db)
            baseline = await store.load_daily_counters(account.id, window.control_date)
            recent = await store.load_recent_allowed_timestamps(account.id)
            guard = AccountControlGuard(
                account_id=account.id,
                execution_id=None,
                channel=account.trade_channel,
                policy=policy,
                baseline=baseline,
                recent_allowed_snapshot=recent,
            )
            events = (
                (
                    await db.execute(
                        select(AccountControlEvent)
                        .where(col(AccountControlEvent.account_id) == account.id)
                        .order_by(col(AccountControlEvent.occurred_at_ms).desc(), col(AccountControlEvent.id).desc())
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )
            records = [
                {
                    "event_uid": event.event_uid,
                    "occurred_at_ms": event.occurred_at_ms,
                    "operation": event.operation,
                    "symbol": event.symbol,
                    "decision": event.decision.value,
                    "outcome": event.outcome,
                    "metadata": event.metadata_,
                }
                for event in events
            ]
            self._history[account.id] = signature, guard, records
        # 仅缓存持久化输入；间隔用量和预计等待随每次采样的当前时间重算。
        frame = guard.monitor_snapshot()
        frame.update(
            status="idle",
            instance_id=None,
            execution_id=None,
            seq=0,
            sampled_at_ms=clock_now_ms(),
            actual_policy=None,
            actual_fingerprint=None,
            records=records,
        )
        return frame


monitor = AccountControlMonitor()
