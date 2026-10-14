"""
执行实时态广播中枢（进程内 pub/sub + 阶段进度镜像）.

Notes
-----
本模块把「账户当前正在跑哪个 execution、跑到哪个阶段」做成可被读接口与 SSE
共享的单一真源。事件经 :func:`axile.server.execution_audit.append_execution_event`
这一唯一漏斗流入 :meth:`ExecutionLiveHub.publish`，据此推进阶段并向所有订阅者广播。

**单进程单 worker 假设**：进度镜像与订阅者队列均为内存态，与
:mod:`axile.server.execution.registry` 的并发锁同处一进程才成立。一旦部署为多
worker，本模块与那把并发锁会一并失效，须迁移到 Redis pub/sub + DB running 标志。

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
    ExecutionEventType.EXECUTION_FAILED: "settling",
    ExecutionEventType.EXECUTION_TERMINATED: "settling",
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


def phase_of(event_type: ExecutionEventType | str) -> str | None:
    """
    把执行事件类型映射到阶段标签.

    Parameters
    ----------
    event_type : ExecutionEventType | str
        执行事件类型（枚举或其字符串值）。

    Returns
    -------
    str | None
        对应阶段标签（``PHASE_ORDER`` 之一）；不推进阶段的事件返回 ``None``。
    """
    et = event_type if isinstance(event_type, ExecutionEventType) else ExecutionEventType(event_type)
    return _PHASE_BY_EVENT.get(et)


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
                updated_at=now_str(),
                pending_execution_id=pending_execution_id,
                pending_kind=pending_kind,
            )
            self._progress[account_id] = prog
            self._broadcast_locked({"type": "event", **self._as_dict(prog)})

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
        }

    def _fanout(self, frame: dict[str, object]) -> None:
        """把一帧投递到主循环线程再分发给订阅者（跨线程安全）。"""
        loop = self._loop
        if loop is None:
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


# 模块级单例。
live_hub = ExecutionLiveHub()
