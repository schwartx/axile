"""独立目录管道：共享查询与初始化进度复用同一连接。"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from typing import Any
from uuid import uuid4

from axile.executor.algorithms.utils.clock import clock_monotonic, get_default_clock
from axile.executor.instrument_catalog import CatalogError, InstrumentCatalog, Query
from axile.executor.instrument_catalog_records import Instrument


@dataclass
class _Response:
    ready: threading.Event = field(default_factory=threading.Event)
    payload: dict[str, Any] = field(default_factory=dict)


class _CatalogPipe:
    """允许双向请求交错，超时响应按 ID 丢弃，不污染下一次请求。"""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self._send_lock = threading.Lock()
        self._lock = threading.Lock()
        self._pending: dict[str, _Response] = {}
        self._closed = threading.Event()
        self.thread = threading.Thread(target=self._receive, daemon=True, name="instrument-catalog-ipc")

    def _send(self, message: dict) -> None:
        with self._send_lock:
            if self._closed.is_set():
                raise CatalogError("合约目录管道已关闭")
            self.connection.send(message)

    def _request(
        self, op: str, timeout: float | None, checkpoint: Callable[[], None] | None = None, **payload: Any
    ) -> Any:
        request_id, response = uuid4().hex, _Response()
        with self._lock:
            self._pending[request_id] = response
        try:
            self._send({"id": request_id, "op": op, "timeout": timeout, **payload})
            deadline = None if timeout is None else clock_monotonic() + timeout
            while not response.ready.is_set():
                if checkpoint is not None:
                    checkpoint()
                if self._closed.is_set():
                    raise CatalogError("合约目录来源已退出")
                remaining = 0.05 if deadline is None else deadline - clock_monotonic()
                if remaining <= 0:
                    raise CatalogError("等待合约资料超时", "CATALOG.TIMEOUT")
                get_default_clock().event_wait(response.ready, min(remaining, 0.05))
            if response.payload.get("error"):
                raise CatalogError(
                    response.payload["error"], response.payload.get("reason_code", "CATALOG.UNAVAILABLE")
                )
            return response.payload.get("result")
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def _receive(self) -> None:
        try:
            while not self._closed.is_set():
                message = self.connection.recv()
                if "reply" in message:
                    with self._lock:
                        response = self._pending.get(message["reply"])
                        if response is not None:
                            response.payload = message
                            response.ready.set()
                elif message["op"] in {"bind", "unbind", "begin", "end", "progress"}:
                    self._handle(message)
                else:
                    threading.Thread(
                        target=self._handle, args=(message,), daemon=True, name="instrument-catalog-request"
                    ).start()
        except (EOFError, OSError):
            pass
        finally:
            self._closed.set()
            self._disconnected()

    def _handle(self, message: dict) -> None:
        try:
            result = self._dispatch(message)
            response = {"reply": message["id"], "result": result}
        except Exception as exc:
            response = {
                "reply": message["id"],
                "error": str(exc),
                "reason_code": getattr(exc, "reason_code", "CATALOG.UNAVAILABLE"),
            }
        try:
            self._send(response)
        except (OSError, CatalogError):
            pass

    def _dispatch(self, message: dict) -> Any:
        raise NotImplementedError

    def _disconnected(self) -> None:
        pass

    def stop(self) -> None:
        """关闭管道并使所有在途请求立即观察到退出。"""
        self._closed.set()
        self._disconnected()
        with self._send_lock:
            if not self.connection.closed:
                self.connection.close()


class CatalogSession(_CatalogPipe):
    """主服务目录代理，当前 worker 可作为查询来源。"""

    def __init__(self, connection: Connection, catalog: InstrumentCatalog, *, enabled: bool = True) -> None:
        super().__init__(connection)
        self.catalog = catalog
        self.owner = uuid4().hex
        self.source: str | None = None
        self._query: Query | None = None
        self.enabled = enabled
        self.channel = self.last_execution = ""
        self._source_lock = threading.RLock()
        self.request_id = ""
        self.active = False
        self.updated = self.started = self.finished = 0.0
        self.thread.start()

    def snapshot(self, request_id: str) -> tuple[bool, float, float, float]:
        """进度只作用于对应请求，后续业务不得继承旧请求的时间额度。"""
        with self._lock:
            if self.request_id != request_id:
                return False, 0.0, 0.0, 0.0
            return self.active, self.updated, self.started, self.finished

    def close(self) -> None:
        """与 worker 生命周期同步关闭。"""
        self.stop()

    def configure(self, *, channel: str, enabled: bool, last_execution: str) -> None:
        """暂停只退出公共刷新来源，执行仍能用自己的已登录会话补齐。"""
        with self._source_lock:
            self.channel, self.enabled, self.last_execution = channel, enabled, last_execution
            self._register_source()

    def _register_source(self) -> None:
        if self.source is None or self._query is None:
            return
        if not self._closed.is_set():
            self.catalog.register(
                self.source, self._query, channel=self.channel, last_execution=self.last_execution, enabled=self.enabled
            )
        else:
            self.catalog.unregister(self.source)

    def _dispatch(self, message: dict) -> Any:
        op = message["op"]
        if op in {"begin", "end", "progress"}:
            with self._lock:
                if op == "begin":
                    self.request_id = message["request_id"]
                    self.started, self.finished, self.active = clock_monotonic(), 0.0, True
                elif message["request_id"] != self.request_id or not self.active:
                    return None
                elif op == "end":
                    self.finished, self.active = clock_monotonic(), False
                self.updated = clock_monotonic()
            return None
        if op == "bind":
            epoch = message["epoch"]

            def query(symbols: list[str] | None, timeout: float | None) -> list[Instrument]:
                rows = self._request("query", timeout, symbols=symbols, epoch=epoch)
                return [Instrument.from_wire(row) for row in rows]

            with self._source_lock:
                self._disconnected()
                if self._closed.is_set():
                    raise CatalogError("合约目录管道已关闭")
                self.source = f"{self.owner}:{epoch}"
                self._query = query
                self.channel = message.get("channel") or self.channel
                self._register_source()
                if self._closed.is_set():
                    self._disconnected()
            return None
        if op == "unbind":
            self._disconnected()
            return None
        if op == "ensure":
            rows = self.catalog.ensure(
                message["symbols"],
                self._check_alive,
                message["timeout"],
                require_specs=message.get("require_specs", False),
                source_name=self.source,
            )
            return {symbol: row.wire() for symbol, row in rows.items()}
        raise CatalogError(f"未知目录操作: {op}")

    def _check_alive(self) -> None:
        if self._closed.is_set():
            raise CatalogError("合约资料等待者已退出")

    def _disconnected(self) -> None:
        with self._source_lock:
            if self.source is not None:
                self.catalog.unregister(self.source)
                self.source = None
                self._query = None


class RemoteCatalogProvider(_CatalogPipe):
    """worker 客户端；主线程等待资料时仍能响应主服务的查询请求。"""

    def __init__(self, connection: Connection) -> None:
        super().__init__(connection)
        self.query: Query | None = None
        self.epoch = self.request_id = ""
        self.thread.start()

    def bind(self, query: Query, *, channel: str = "") -> None:
        """注册当前会话，旧会话请求由独立尝试标识隔离。"""
        with self._lock:
            self.query = query
            self.epoch = uuid4().hex
        self._request("bind", 5.0, epoch=self.epoch, channel=channel)

    def ensure(
        self,
        symbols: list[str],
        checkpoint: Callable[[], None] | None = None,
        timeout: float | None = None,
        *,
        require_specs: bool = False,
    ) -> Mapping[str, Instrument]:
        """同步等待本次所需资料，取消不取消其他共享等待者。"""
        if timeout is None and not require_specs:
            timeout = 15.0
        rows = self._request("ensure", timeout, checkpoint, symbols=symbols, require_specs=require_specs)
        return {symbol: Instrument.from_wire(row) for symbol, row in rows.items()}

    def begin(self) -> None:
        """标记当前请求进入初始化。"""
        self._request("begin", 5.0, request_id=self.request_id)

    def end(self) -> None:
        """结束初始化计时，恢复业务响应预算。"""
        self._request("end", 5.0, request_id=self.request_id)

    def progress(self, phase: str) -> None:
        """只发送实际进展，目录轮询不延长看门狗。"""
        self._request("progress", 5.0, request_id=self.request_id, phase=phase)

    def close(self) -> None:
        """仅注销当前执行器，保留 worker 管道供新会话复用。"""
        with self._lock:
            self.query = None
        if not self._closed.is_set():
            self._request("unbind", 5.0)

    def _dispatch(self, message: dict) -> Any:
        with self._lock:
            query = self.query
            if message["op"] != "query" or message["epoch"] != self.epoch or query is None:
                raise CatalogError("合约查询来源会话已失效")
        return [row.wire() for row in query(message["symbols"], message["timeout"])]

    @property
    def failed(self) -> bool:
        """管道断开后要求重建 worker；普通请求超时可安全复用带 ID 的管道。"""
        return self._closed.is_set()
