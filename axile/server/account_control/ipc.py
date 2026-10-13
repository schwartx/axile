"""独立监控管道，业务请求和终止控制不等待此通路。"""

from __future__ import annotations

from collections.abc import Callable
from multiprocessing.connection import Connection
from threading import Event, Lock, Thread
from typing import Any

from loguru import logger

from axile.executor.algorithms.utils.clock import clock_now_ms
from axile.server.account_control import providers

_remotes: dict[int, RemoteMonitor] = {}
_lock = Lock()


def serve(connection: Connection, account_id: int) -> None:
    """Worker 监控线程只读取当前进程的 guard。"""
    try:
        while connection.recv() is not None:
            try:
                frame = providers.snapshot(account_id)
            except Exception:
                logger.exception("worker 流控快照读取失败")
                frame = {"monitor_error": True}
            connection.send(frame)
    except (EOFError, OSError):
        pass
    finally:
        connection.close()


class RemoteMonitor:
    """每个 worker 一个采样线程；超时后等待原响应，不发送第二个请求。"""

    def __init__(
        self, connection: Connection, account_id: int, process_alive: Callable[[], bool] | None = None
    ) -> None:
        self.connection = connection
        self.account_id = account_id
        self.process_alive = process_alive
        self.stop = Event()
        self.frame: dict[str, Any] | None = None
        self.sampled_at_ms = 0
        self.failed = False
        self.thread = Thread(target=self._run, name=f"flow-monitor-{account_id}", daemon=True)
        with _lock:
            _remotes[account_id] = self
        self.thread.start()

    def _run(self) -> None:
        try:
            while not self.stop.is_set():
                self.connection.send(True)
                while not self.connection.poll(0.2):
                    if self.stop.is_set():
                        return
                frame = self.connection.recv()
                if frame is None or not frame.get("monitor_error"):
                    self.frame = frame
                    self.sampled_at_ms = clock_now_ms()
                self.stop.wait(1)
        except (EOFError, OSError):
            self.failed = True
        finally:
            self.connection.close()

    def close(self) -> None:
        """停止监控线程，独立于业务管道释放。"""
        self.failed = True
        self.stop.set()
        self.thread.join(timeout=0.3)


def sample(account_id: int) -> tuple[dict[str, Any] | None, int, bool] | None:
    """读取最近采样缓存，不进行 IPC 等待。"""
    with _lock:
        remote = _remotes.get(account_id)
    if remote is None:
        return None
    dead = remote.failed if remote.process_alive is None else not remote.process_alive()
    return remote.frame, remote.sampled_at_ms, dead
