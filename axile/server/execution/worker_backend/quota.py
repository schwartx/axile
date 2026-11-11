"""报单放行额度的进程间确认和异常退出补写。"""

import asyncio
from multiprocessing.connection import Connection
from multiprocessing.context import SpawnProcess
from threading import Lock, Thread

from axile.executor.account_control.models import AccountControlCounterDeltaWrite
from axile.server.account_control.store import AccountControlStore


class QuotaClient:
    """worker 只等待主进程确认收到计数。"""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self._lock = Lock()

    def __call__(self, instance_id: str, deltas: list[AccountControlCounterDeltaWrite]) -> None:
        """确认待记额度，或在空列表时清除已提交的会话。"""
        with self._lock:
            try:
                self.connection.send((instance_id, deltas))
                if not self.connection.poll(5) or self.connection.recv() is not True:
                    raise TimeoutError("主进程未确认报单额度")
            except (EOFError, OSError, ValueError) as exc:
                # 连接超时后可能还有旧确认在途，关闭后才能避免误用。
                self.connection.close()
                raise RuntimeError("报单额度同步失败") from exc


class QuotaMirror:
    """主进程按会话和原有增量键保存各桶的累计量。"""

    def __init__(self, connection: Connection, account_id: int, process: SpawnProcess) -> None:
        self._connection = connection
        self._account_id = account_id
        self.process = process
        self._pending: dict[str, dict[str, AccountControlCounterDeltaWrite]] = {}
        # shortcut: 主进程内存镜像不覆盖整机崩溃，需要该保证时升级为持久化确认。
        self._recovery_lock = Lock()
        self._thread = Thread(target=self._receive, name=f"quota-mirror-{account_id}", daemon=True)
        self._thread.start()

    def _receive(self) -> None:
        try:
            while True:
                instance_id, deltas = self._connection.recv()
                if deltas:
                    if any(delta.account_id != self._account_id for delta in deltas):
                        self._connection.send(False)
                        return
                    pending = self._pending.setdefault(instance_id, {})
                    pending.update((delta.delta_uid, delta) for delta in deltas)
                else:
                    self._pending.pop(instance_id, None)
                self._connection.send(True)
        except (EOFError, OSError):
            pass
        finally:
            self._connection.close()

    def recover(self) -> bool:
        """额度通道结束后检查收尾；未落库的计数须等进程退出才补写。"""
        with self._recovery_lock:
            self._thread.join(timeout=1)
            if self._thread.is_alive():
                return False
            deltas = [delta for pending in self._pending.values() for delta in pending.values()]
            # 正常关闭额度管道后，空副本允许旧进程继续刷盘录制。
            if deltas and self.process.is_alive():
                return False
            if deltas:
                asyncio.run(_flush_quota(deltas))
                self._pending.clear()
            return True


async def _flush_quota(deltas: list[AccountControlCounterDeltaWrite]) -> None:
    await AccountControlStore().flush_execution_records(counter_deltas=deltas, events=[])
