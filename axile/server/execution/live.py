"""
执行实时态广播中枢（进程内 pub/sub + 阶段进度镜像）.

Notes
-----
主服务中的镜像与 registry 同处一个 ASGI 进程。账户 worker 的真实阶段经既有
监控 IPC 采样后合入此镜像；审计事件继续保留原有持久化与广播入口。任务终态
由主服务生命周期结算，worker 的进度采样不能释放运行槽位。

多 ASGI 进程部署仍不受支持，镜像与 registry 均为主服务进程内状态。

线程安全：事件可能经 ``append_execution_event_sync`` 在**独立线程 + 独立事件
循环**上产生（见 :func:`axile.server.execution_audit._run_coroutine_sync`），故
:meth:`publish` 只做「加锁更新进度镜像 + ``call_soon_threadsafe`` 回投主循环」，
真正向订阅者队列投递发生在主循环线程内。
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import uuid4

from axile.domain.execution import ExecutionEventType
from axile.executor.algorithms.utils.clock import clock_monotonic
from axile.server.connection_state import ConnectionBinding
from axile.server.db.models import now_str

# 阶段序：索引越大越靠后，用于「单调不回退」。对应前端五步阶段条
# 触发 → 冻结输入 → 算目标 → 下单 → 收口。
PHASE_ORDER: tuple[str, ...] = ("triggered", "snapshot", "planning", "executing", "settling")
_PHASE_INDEX: dict[str, int] = {name: i for i, name in enumerate(PHASE_ORDER)}

# 运行态状态标记。
STATUS_RUNNING = "running"
STATUS_QUEUED = "queued"
STATUS_TERMINATING = "terminating"
_LIVE_STATUS: frozenset[str] = frozenset({STATUS_QUEUED, STATUS_RUNNING, STATUS_TERMINATING})
_TERMINAL_STATUS: frozenset[str] = frozenset({"done", "failed", "terminated"})

# 事件类型 → 阶段标签；未列出者（如终止请求/确认）不推进阶段，仅作为事件转发。
_PHASE_BY_EVENT: dict[ExecutionEventType, str] = {
    ExecutionEventType.EXECUTION_STARTED: "triggered",
    ExecutionEventType.INPUT_SNAPSHOTTED: "snapshot",
    ExecutionEventType.TARGET_COMPUTED: "planning",
    ExecutionEventType.SYMBOL_DECISION_MADE: "executing",
    ExecutionEventType.SYMBOL_SKIPPED: "executing",
    ExecutionEventType.ORDER_SUBMITTED: "executing",
    ExecutionEventType.ORDER_ACKNOWLEDGED: "executing",
    ExecutionEventType.ORDER_TERMINAL: "executing",
    ExecutionEventType.EXECUTION_COMPLETED: "settling",
}

# 终态事件 → 终态状态。
_TERMINAL_STATUS_BY_EVENT: dict[ExecutionEventType, str] = {
    ExecutionEventType.EXECUTION_COMPLETED: "done",
    ExecutionEventType.EXECUTION_FAILED: "failed",
    ExecutionEventType.EXECUTION_TERMINATED: "terminated",
}

# 终态项在进度镜像中保留多久（秒）再逐出，给「刚连上」的订阅者留一小段可见窗口。
_EVICT_DELAY_SEC = 5.0
# 每订阅者队列容量；满了改发权威全量快照（见 _deliver）。
_QUEUE_MAXSIZE = 256


def _later_phase(current: str, incoming: str | None) -> str:
    """返回两个阶段中更靠后的一个（单调不回退）；``incoming`` 为空时保持 ``current``。"""
    if incoming is None:
        return current
    return incoming if _PHASE_INDEX.get(incoming, -1) > _PHASE_INDEX.get(current, -1) else current


@dataclass
class _Progress:
    """某账户当前执行的进度镜像项。"""

    execution_id: str
    account_id: int
    kind: str | None
    phase: str
    status: str
    updated_at: str
    pending_execution_id: str | None = None
    pending_kind: str | None = None
    sampled_at: float | None = None
    worker_version: int = -1
    worker_instance: str | None = None


class ExecutionLiveHub:
    """
    进程内执行实时态广播中枢.

    Attributes
    ----------
    该类不对外暴露内部字段，统一经方法访问；进度镜像与订阅者集合分别以
    ``threading.Lock`` 保护写入，队列投递只在绑定的主事件循环上进行。
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue[dict[str, object]]] = set()
        self._progress: dict[int, _Progress] = {}
        self._lock = threading.Lock()
        self._stream_id = uuid4().hex
        self._version = 0
        self._progress_expiry: dict[int, asyncio.TimerHandle] = {}

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """绑定主事件循环（应在服务启动 lifespan 内调用）。"""
        self._loop = loop

    def publish(
        self,
        *,
        execution_id: str,
        account_id: int,
        event_type: ExecutionEventType | str,
        kind: str | None = None,
        symbol: str | None = None,
        seq: int = 0,
    ) -> None:
        """
        依据一条执行事件推进账户阶段并向订阅者广播.

        线程安全：可从任意线程/事件循环调用（见模块 docstring）。

        Parameters
        ----------
        execution_id : str
            执行链路标识。
        account_id : int
            账户 ID。
        event_type : ExecutionEventType | str
            触发本次推进的事件类型。
        kind : str | None
            执行种类（rebalance/clear 等），用于前端文案。
        symbol : str | None
            事件关联品种（可空）。
        seq : int
            事件序号。
        """
        et = event_type if isinstance(event_type, ExecutionEventType) else ExecutionEventType(event_type)
        phase = _PHASE_BY_EVENT.get(et)
        terminal_status = _TERMINAL_STATUS_BY_EVENT.get(et)

        with self._lock:
            cur = self._progress.get(account_id)
            if cur is None or cur.execution_id != execution_id:
                # 新账户或换了一条执行：以本事件为起点重置。
                eff_phase = phase or "triggered"
                eff_status = terminal_status or STATUS_RUNNING
            else:
                eff_phase = _later_phase(cur.phase, phase)
                eff_status = terminal_status or cur.status
            prog = _Progress(
                execution_id=execution_id,
                account_id=account_id,
                kind=kind if kind is not None else (cur.kind if cur else None),
                phase=eff_phase,
                status=eff_status,
                updated_at=now_str(),
                pending_execution_id=None if cur is None else cur.pending_execution_id,
                pending_kind=None if cur is None else cur.pending_kind,
            )
            # 上一张票的迟到终态仍可广播，但不能覆盖下一张票的权威镜像。
            if terminal_status is None or cur is None or cur.execution_id == execution_id:
                self._progress[account_id] = prog
            self._broadcast_locked(
                {
                    "type": "event",
                    "account_id": account_id,
                    "execution_id": execution_id,
                    "kind": prog.kind,
                    "phase": prog.phase,
                    "status": prog.status,
                    "event_type": et.value,
                    "symbol": symbol,
                    "seq": seq,
                    "ts": prog.updated_at,
                }
            )
        if terminal_status is not None:
            self._schedule_evict(account_id, execution_id)

    def publish_slots(
        self,
        *,
        account_id: int,
        execution_id: str,
        kind: str | None,
        status: str,
        phase: str | None,
        pending_execution_id: str | None,
        pending_kind: str | None,
    ) -> None:
        """由 intent 层推送账户 headline + pending（入队当下即可被 SSE 看见）."""
        with self._lock:
            cur = self._progress.get(account_id)
            keep_phase = (
                cur.phase
                if cur is not None and cur.execution_id == execution_id and cur.phase not in {"queued", ""}
                else (phase or "triggered")
            )
            if status == STATUS_QUEUED:
                keep_phase = "queued"
            prog = _Progress(
                execution_id=execution_id,
                account_id=account_id,
                kind=kind,
                phase=keep_phase,
                status=status,
                updated_at=cur.updated_at if cur is not None and cur.execution_id == execution_id else now_str(),
                sampled_at=cur.sampled_at
                if cur is not None and cur.execution_id == execution_id
                else clock_monotonic(),
                worker_version=cur.worker_version if cur is not None and cur.execution_id == execution_id else -1,
                worker_instance=cur.worker_instance if cur is not None and cur.execution_id == execution_id else None,
                pending_execution_id=pending_execution_id,
                pending_kind=pending_kind,
            )
            self._progress[account_id] = prog
            self._broadcast_locked({"type": "event", **self._as_dict(prog)})

    def consume_progress(self, binding: ConnectionBinding, frame: dict[str, object]) -> None:
        """接受当前实例的采样；阶段不回退，任务状态仍由父进程结算。"""
        from axile.server.connection_runtime import matches_session

        phase, version = frame.get("phase"), frame.get("version")
        if phase not in PHASE_ORDER or type(version) is not int or version < 0:
            return
        if not isinstance(frame.get("changed_at"), str) or not matches_session(binding):
            return
        with self._lock:
            cur = self._progress.get(binding.account_id)
            if (
                cur is None
                or cur.execution_id != frame.get("execution_id")
                or cur.status not in {STATUS_RUNNING, STATUS_TERMINATING}
                or not matches_session(binding)
            ):
                return
            if cur.worker_instance == binding.instance_id and version < cur.worker_version:
                return
            stale = cur.sampled_at is not None and clock_monotonic() - cur.sampled_at > 5
            next_phase = _later_phase(cur.phase, str(phase))
            changed = next_phase != cur.phase or cur.worker_instance != binding.instance_id or stale
            if next_phase != cur.phase:
                cur.updated_at = str(frame["changed_at"])
            cur.phase, cur.sampled_at = next_phase, clock_monotonic()
            cur.worker_version, cur.worker_instance = version, binding.instance_id
            if changed:
                self._broadcast_locked({"type": "event", **self._as_dict(cur)})
            loop = self._loop
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(self._arm_progress_expiry, binding.account_id, cur.execution_id)

    def _arm_progress_expiry(self, account_id: int, execution_id: str) -> None:
        if timer := self._progress_expiry.pop(account_id, None):
            timer.cancel()
        if self._loop is not None:
            self._progress_expiry[account_id] = self._loop.call_later(
                5.01, self._expire_progress, account_id, execution_id
            )

    def _expire_progress(self, account_id: int, execution_id: str) -> None:
        self._progress_expiry.pop(account_id, None)
        with self._lock:
            cur = self._progress.get(account_id)
            if (
                cur is not None
                and cur.execution_id == execution_id
                and cur.status in _LIVE_STATUS
                and cur.sampled_at is not None
                and clock_monotonic() - cur.sampled_at > 5
            ):
                self._broadcast_locked({"type": "event", **self._as_dict(cur)})

    def execution_progress(self, account_id: int, execution_id: str) -> dict[str, object] | None:
        """读取指定执行的实时阶段，不把下一笔执行的阶段返回给历史页面。"""
        value = self.progress_for(account_id)
        if value is None or value["execution_id"] != execution_id:
            return None
        return {"phase": value["phase"], "updated_at": value["ts"], "stale": value["progress_stale"]}

    def snapshot(self) -> list[dict[str, object]]:
        """返回当前所有仍在运行或排队的账户进度（SSE 首帧 / 冷渲染用）。"""
        with self._lock:
            return [self._as_dict(p) for p in self._progress.values() if p.status in _LIVE_STATUS]

    def progress_for(self, account_id: int) -> dict[str, object] | None:
        """返回某账户的当前进度镜像（含刚结束的短窗口）；无则 ``None``。"""
        with self._lock:
            prog = self._progress.get(account_id)
            return self._as_dict(prog) if prog is not None else None

    def snapshot_envelope(self) -> dict[str, object]:
        """在同一把锁内读取全量及版本，供初连、重连与背压恢复使用。"""
        with self._lock:
            return {
                "type": "snapshot",
                "stream_id": self._stream_id,
                "version": self._version,
                "data": [self._as_dict(p) for p in self._progress.values() if p.status in _LIVE_STATUS],
            }

    def clear_live_account(self, account_id: int) -> None:
        """账户槽位已空时立刻摘掉进度镜像，并向订阅者发终态帧."""
        with self._lock:
            cur = self._progress.pop(account_id, None)
            if cur is None:
                return
            self._broadcast_locked(
                {
                    "type": "event",
                    "account_id": account_id,
                    "execution_id": cur.execution_id,
                    "kind": cur.kind,
                    "phase": cur.phase,
                    "status": "done",
                    "pending_execution_id": None,
                    "pending_kind": None,
                    "ts": now_str(),
                }
            )

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[dict[str, object]]]:
        """
        订阅广播帧.

        必须在主事件循环（SSE 处理协程）内使用；退出时自动退订。
        """
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    # ---- 内部 ----

    @staticmethod
    def _as_dict(prog: _Progress) -> dict[str, object]:
        return {
            "account_id": prog.account_id,
            "execution_id": prog.execution_id,
            "kind": prog.kind,
            "phase": prog.phase,
            "status": prog.status,
            "pending_execution_id": prog.pending_execution_id,
            "pending_kind": prog.pending_kind,
            "ts": prog.updated_at,
            "progress_stale": prog.sampled_at is not None and clock_monotonic() - prog.sampled_at > 5,
        }

    def _fanout(self, frame: dict[str, object]) -> None:
        """把一帧投递到主循环线程再分发给订阅者（跨线程安全）。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._deliver, frame)

    def _broadcast_locked(self, frame: dict[str, object]) -> None:
        """锁内分配版本并安排投递，保持跨线程发布与快照水位的顺序。"""
        self._version += 1
        frame.update(stream_id=self._stream_id, version=self._version)
        self._fanout(frame)

    def _deliver(self, frame: dict[str, object]) -> None:
        """在主循环投递；队列满时用全量快照替换积压，恢复全部账户状态。"""
        for queue in self._subscribers:
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(self.snapshot_envelope())

    def _schedule_evict(self, account_id: int, execution_id: str) -> None:
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(lambda: loop.call_later(_EVICT_DELAY_SEC, self._evict, account_id, execution_id))

    def _evict(self, account_id: int, execution_id: str) -> None:
        with self._lock:
            cur = self._progress.get(account_id)
            if cur is not None and cur.execution_id == execution_id:
                del self._progress[account_id]


def consume_worker_progress(frame: dict[str, object] | None) -> None:
    """监控入口只接收带实际实例身份的小快照，不访问数据库或渠道。"""
    if not isinstance(frame, dict):
        return
    binding, snapshot = frame.get("binding"), frame.get("snapshot")
    if not isinstance(binding, dict) or not isinstance(snapshot, dict):
        return
    try:
        identity = ConnectionBinding(**binding)
    except (TypeError, ValueError):
        return
    live_hub.consume_progress(identity, snapshot)


# 模块级单例。
live_hub = ExecutionLiveHub()
