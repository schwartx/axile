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

from axile.executor.account_control.guard import AccountControlGuard, resolve_control_window
from axile.executor.account_control.models import (
    AccountControlContextKind,
    EffectiveAccountControlPolicy,
)
from axile.executor.account_control.monitor import policy_fingerprint
from axile.executor.account_control.presets import resolve_account_control_policy
from axile.executor.account_control.warnings import AccountControlQuotaWarning
from axile.executor.algorithms.utils.clock import clock_now_iso, clock_now_ms
from axile.server.account_control import providers
from axile.server.account_control.ipc import sample
from axile.server.account_control.schema import public_frame
from axile.server.account_control.store import snapshots_from_baseline
from axile.server.account_control.warnings import wake_pending_quota_warnings
from axile.server.db.models.account_control_session import AccountControlSession
from axile.server.native_store import native_operation
from axile.server.order_submissions import submission_window


def _affected(session: AccountControlSession, control_date: str) -> bool:
    tz = ZoneInfo(session.timezone)
    start = datetime.fromtimestamp(session.started_at_ms / 1000, tz).date().isoformat()
    end = datetime.fromtimestamp((session.finished_at_ms or clock_now_ms()) / 1000, tz).date().isoformat()
    return start <= control_date <= end


async def recover_sessions() -> None:
    """启动时把上个服务进程未提交的会话标记为不完整。"""
    await native_operation("control_monitor_recover", {"now_ms": clock_now_ms()})


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
        self._history: dict[int, tuple[tuple, AccountControlGuard, list[dict[str, Any]]]] = {}
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
        inputs = await native_operation("control_monitor_inputs", {})
        accounts = inputs["accounts"]
        sessions = [AccountControlSession.model_validate(raw) for raw in inputs["sessions"]]
        await asyncio.gather(*(self._read_source(account["id"]) for account in accounts))
        policies = {account["id"]: self._policy(account) for account in accounts}
        requests = [self._persistence_request(account, policies[account["id"]], sessions) for account in accounts]
        receipt = await native_operation(
            "control_monitor_refresh", {"accounts": requests, "created_at": clock_now_iso()}
        )
        if receipt.get("warning_error"):
            logger.error("运行中额度预警入队失败，等待下次采样: {}", receipt["warning_error"])
        if receipt["warnings_committed"]:
            for request in requests:
                for source in request["warning_sources"]:
                    for raw in source["warnings"]:
                        key = AccountControlQuotaWarning.model_validate(raw).event_id, source["instance_id"]
                        self._queued_warnings[key] = None
            while len(self._queued_warnings) > 2048:
                self._queued_warnings.pop(next(iter(self._queued_warnings)))
        updated = set(receipt["updated_uids"])
        for request in requests:
            for event in request["outcomes"]:
                if event["event_uid"] in updated:
                    self._outcomes[event["event_uid"]] = str((event["outcome"], event["metadata"]))
        while len(self._outcomes) > 1000:
            self._outcomes.pop(next(iter(self._outcomes)))
        states = {state["account_id"]: state for state in receipt["data"]}
        changed = []
        frames = {}
        for account in accounts:
            policy = policies[account["id"]]
            state = states[account["id"]]
            related = [AccountControlSession.model_validate(raw) for raw in state["sessions"]]
            if state["history"] is not None:
                self._historical(account, policy, related, state)
                state["history"] = None
            frame = public_frame(await self._account_frame(account, policy, related, state))
            if self._content(self.frames.get(account["id"])) != self._content(frame):
                changed.append(frame)
            frames[account["id"]] = frame
        removed = [key for key in self.frames if key not in frames]
        for key in removed:
            self._history.pop(key, None)
        await wake_pending_quota_warnings(receipt["pending_accounts"])
        # 整轮采样完成后同步发布，订阅者只看到完整的一轮。
        self.frames = frames
        self._publish(changed, removed)

    @staticmethod
    def _policy(account: dict[str, Any]) -> EffectiveAccountControlPolicy:
        return resolve_account_control_policy(account["account_control_preset"], account["account_control_override"])

    def _live_source(self, account_id: int, sessions: list[AccountControlSession]) -> tuple[Any, Any, bool]:
        execution_sessions = [
            session
            for session in sessions
            if session.effective_context_kind != AccountControlContextKind.ACCOUNT_OPERATION
        ]
        latest = max(execution_sessions, key=lambda session: session.id or 0, default=None)
        sources = self._sources.get(account_id, [])
        valid = [
            source
            for source in sources
            if source[0] is not None
            and (latest is None or source[0]["instance_id"] == latest.instance_id or source[0].get("incomplete", False))
        ]
        return valid[0] if valid else (None, None, any(source[2] for source in sources))

    @staticmethod
    def _history_signature(policy: Any, sessions: list[AccountControlSession]) -> tuple:
        window = resolve_control_window(policy.timezone)
        return (
            policy_fingerprint(policy.model_dump(mode="json")),
            window.minute_bucket_start,
            tuple((session.instance_id, session.status) for session in sessions),
        )

    def _persistence_request(
        self, account: dict[str, Any], policy: EffectiveAccountControlPolicy, sessions: list[AccountControlSession]
    ) -> dict[str, Any]:
        account_id = account["id"]
        sessions = [session for session in sessions if session.account_id == account_id]
        window = resolve_control_window(policy.timezone)
        start, end = submission_window(window.control_date, policy.timezone)
        live, _, failed = self._live_source(account_id, sessions)
        executions = [
            session
            for session in sessions
            if session.effective_context_kind != AccountControlContextKind.ACCOUNT_OPERATION
        ]
        latest = max(executions, key=lambda session: session.id or 0, default=None)
        mark = (
            latest.instance_id
            if latest is not None
            and latest.status == "active"
            and (failed or bool(live and live["status"] == "incomplete"))
            else None
        )
        cached = self._history.get(account_id)
        warning_sources = []
        known = {session.instance_id for session in sessions}
        for source, _, _ in self._sources.get(account_id, []):
            if source is None or source.get("instance_id") not in known:
                continue
            warnings = [
                raw
                for raw in source.get("quota_warnings", [])
                if (AccountControlQuotaWarning.model_validate(raw).event_id, source["instance_id"])
                not in self._queued_warnings
            ]
            if warnings:
                warning_sources.append({"instance_id": source["instance_id"], "warnings": warnings})
        # 迟到结果不受展示来源筛选；优先来源覆盖旧值，同一事件只保留最后结果。
        updates = {
            event["event_uid"]: event
            for source, _, _ in reversed(self._sources.get(account_id, []))
            if source is not None
            for event in source.get("outcome_updates", [])
        }
        outcomes = [
            event
            for uid, event in updates.items()
            if self._outcomes.get(uid) != str((event["outcome"], event["metadata"]))
        ]
        return {
            "account_id": account_id,
            "trade_channel": account["trade_channel"],
            "control_date": window.control_date,
            "timezone": policy.timezone,
            "start_ms": start,
            "end_ms": end,
            "now_ms": clock_now_ms(),
            "active_instance_id": None if live is None else live.get("instance_id"),
            "mark_incomplete": mark,
            "outcomes": outcomes,
            "warning_sources": warning_sources,
            "read_history": cached is None or cached[0] != self._history_signature(policy, sessions),
            "history_sessions": [] if cached is None else cached[0][2],
        }

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

    async def _account_frame(
        self,
        account: dict[str, Any],
        policy: EffectiveAccountControlPolicy,
        sessions: list[AccountControlSession],
        persisted: dict[str, Any],
    ) -> dict[str, Any]:
        account_id = account["id"]
        window = resolve_control_window(policy.timezone)
        execution_sessions = [
            session
            for session in sessions
            if session.effective_context_kind != AccountControlContextKind.ACCOUNT_OPERATION
        ]
        latest = max(execution_sessions, key=lambda session: session.id or 0, default=None)
        live, sampled, failed = self._live_source(account_id, sessions)
        incomplete = any(s.status == "incomplete" and _affected(s, window.control_date) for s in sessions)
        incomplete = incomplete or sum(s.status == "active" for s in execution_sessions) > 1
        active = (latest is not None and latest.status == "active") or (
            live is not None and live["status"] == "running" and live.get("incomplete", False)
        )
        if live is not None and (active or live.get("committed") is False):
            frame = dict(live)
            frame["sampled_at_ms"] = sampled
        elif active:
            frame = dict(self.frames.get(account_id) or self._historical(account, policy, sessions, persisted))
            frame.update(status="unknown", sampled_at_ms=frame.get("sampled_at_ms"))
        else:
            frame = self._historical(account, policy, sessions, persisted)
            previous = live or self.frames.get(account_id)
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
        gaps = [s for s in sessions if s.status == "incomplete" and _affected(s, window.control_date)]
        risk_unknown = persisted["incomplete"]
        risk_unknown = risk_unknown or bool(live and live.get("incomplete", False))
        frame.update(
            history_incomplete=incomplete,
            snapshot_stale=bool(active and (failed or not sampled or now - sampled > 5000)),
            counter_integrity="unknown" if risk_unknown else "known",
            risk_operations_blocked=False,
            incomplete_instances=[
                {
                    "instance_id": s.instance_id,
                    "status": s.status,
                    "context_kind": s.context_kind,
                    "source": s.source,
                    "started_at_ms": s.started_at_ms,
                    "finished_at_ms": s.finished_at_ms,
                    "timezone": s.timezone,
                }
                for s in sorted(gaps, key=lambda s: s.started_at_ms, reverse=True)[:100]
            ],
            incomplete_instances_total=len(gaps),
            counter_recovery="historical_gap_retained" if risk_unknown else None,
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
        if account["trade_channel"] == "ctp":
            frame["order_submissions"] = persisted["order_submissions"]
            frame["submission_monitor"] = persisted["submission_monitor"]
        return frame

    def _historical(
        self, account: dict[str, Any], policy: Any, sessions: list[AccountControlSession], persisted: dict[str, Any]
    ) -> dict[str, Any]:
        account_id = account["id"]
        signature = self._history_signature(policy, sessions)
        cached = self._history.get(account_id)
        raw = persisted["history"]
        if raw is None:
            assert cached is not None and cached[0] == signature
            guard, records = cached[1:]
        else:
            baseline, recent = snapshots_from_baseline(raw)
            guard = AccountControlGuard(
                account_id=account_id,
                execution_id=None,
                channel=account["trade_channel"],
                policy=policy,
                baseline=baseline,
                recent_allowed_snapshot=recent,
            )
            records = raw["records"]
            self._history[account_id] = signature, guard, records
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
