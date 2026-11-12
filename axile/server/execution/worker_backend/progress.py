"""当前执行的内存进度；监控线程采样，不等待交易线程。"""

from __future__ import annotations

from dataclasses import asdict
from threading import Lock

from axile.server.connection_state import ConnectionBinding
from axile.server.db.models import now_str
from axile.server.execution.live import PHASE_ORDER


class WorkerExecutionProgress:
    """只保存当前执行的一份快照，不积累事件或产生后台线程。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._frame: dict[str, object] | None = None
        self._version = 0
        self._binding: ConnectionBinding | None = None

    def begin(self, execution_id: str | None, binding: ConnectionBinding | None = None) -> None:
        """请求入口初始化身份，旧执行的迟到回调随后失效。"""
        with self._lock:
            self._binding = binding
            self._version += 1
            self._frame = (
                {"execution_id": execution_id, "version": self._version, "phase": "triggered", "changed_at": now_str()}
                if execution_id
                else None
            )

    def report(self, execution_id: str | None, phase: str) -> None:
        """同一执行只向前推进，非法阶段不进入 IPC。"""
        if phase not in PHASE_ORDER:
            raise ValueError(f"未知执行阶段: {phase}")
        with self._lock:
            frame = self._frame
            if frame is None or frame["execution_id"] != execution_id:
                return
            if PHASE_ORDER.index(phase) <= PHASE_ORDER.index(str(frame["phase"])):
                return
            self._version += 1
            self._frame = {
                "execution_id": execution_id,
                "version": self._version,
                "phase": phase,
                "changed_at": now_str(),
            }

    def snapshot(self) -> dict[str, object] | None:
        """复制小快照；不读取执行器、数据库或渠道。"""
        with self._lock:
            return (
                {"binding": asdict(self._binding), "snapshot": dict(self._frame)}
                if self._frame and self._binding
                else None
            )
