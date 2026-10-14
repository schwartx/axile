"""观察连接生命周期与实际账户访问，不根据后续业务处理推断连通性。"""

from __future__ import annotations

import threading
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

from loguru import logger

from axile.executor.algorithms.utils.clock import clock_now_iso

CONNECTION_ERROR_SCOPE = "account_connection"


def connection_error_scope(error: BaseException) -> str | None:
    """沿显式异常链保留归属，不解释异常文本。"""
    seen: set[int] = set()
    while id(error) not in seen:
        seen.add(id(error))
        if getattr(error, "error_scope", None) == CONNECTION_ERROR_SCOPE:
            return CONNECTION_ERROR_SCOPE
        cause = error.__cause__ or error.__context__
        if cause is None:
            break
        error = cause
    return None


@dataclass(frozen=True)
class ConnectionObservation:
    """一条明确的连接结果；正常结果不携带此前错误。"""

    status: Literal["ok", "error"]
    observed_at: str
    message: str | None = None
    error: dict[str, object] | None = None


ConnectionObserver = Callable[[ConnectionObservation], None]
_observer: ContextVar[ConnectionObserver | None] = ContextVar("account_connection_observer", default=None)


def current_connection_observer() -> ConnectionObserver | None:
    """返回构造当前执行器时绑定的宿主观察者。"""
    return _observer.get()


@contextmanager
def connection_observer_scope(observer: ConnectionObserver) -> Iterator[None]:
    """把宿主观察者显式传给构造中的执行器；后台线程使用捕获后的引用。"""
    token = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)


class ConnectionMonitor:
    """只保存最新观察，序列化回调并隔离宿主记录失败。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._observer = current_connection_observer()
        self._latest: ConnectionObservation | None = None
        self._active = True
        self._failure_revision = 0

    def bind(self, observer: ConnectionObserver) -> None:
        """绑定观察者并回放最新结果，不伪造新的确认时间。"""
        with self._lock:
            self._observer = observer
            if self._active and self._latest is not None:
                self._notify(self._latest)

    def close(self) -> None:
        """主动释放后忽略迟到回调，保留最后观察。"""
        with self._lock:
            self._active = False

    @contextmanager
    def connecting(self) -> Iterator[None]:
        """仅包围实际连接建立代码，业务查询和转换不进入此边界。"""
        try:
            yield
        except Exception as exc:
            self.failed(exc)
            raise

    def succeeded(self) -> None:
        """渠道明确完成连接、认证或实际账户访问时调用。"""
        self._publish(ConnectionObservation("ok", clock_now_iso()))

    @contextmanager
    def accessing(self) -> Iterator[None]:
        """实际账户访问成功即恢复；失败分类仍由渠道负责，业务转换放在边界外。"""
        with self._lock:
            revision = self._failure_revision
        yield
        with self._lock:
            # 查询期间新收到的断线优先，迟到的成功响应不能覆盖它。
            if revision == self._failure_revision:
                self.succeeded()

    def failed(self, error: BaseException) -> None:
        """连接故障发生处标记归属并固定完整异常链。"""
        error.error_scope = CONNECTION_ERROR_SCOPE  # type: ignore[attr-defined]
        raw = str(getattr(error, "technical_detail", None) or error)
        message = str(getattr(error, "execution_error", None) or raw or type(error).__name__)
        details: dict[str, object] = {
            "message": message,
            "technical_detail": raw,
            "traceback": getattr(error, "formatted_traceback", None) or "".join(traceback.format_exception(error)),
            "exception_type": type(error).__name__,
            "error_scope": CONNECTION_ERROR_SCOPE,
        }
        for key in ("reason_code", "error_id", "status", "status_code", "return_code", "broker_message"):
            value = getattr(error, key, None)
            if isinstance(value, (str, int)):
                details[key] = value
        self._publish(ConnectionObservation("error", clock_now_iso(), message, details))

    def _publish(self, observation: ConnectionObservation) -> None:
        with self._lock:
            if self._active:
                if observation.status == "error":
                    self._failure_revision += 1
                self._latest = observation
                self._notify(observation)

    def _notify(self, observation: ConnectionObservation) -> None:
        if self._observer is not None:
            try:
                self._observer(observation)
            except Exception:
                logger.exception("账户连接状态记录失败，保留原始业务结果")


def connection_monitor(owner: object) -> ConnectionMonitor:
    """为执行器及绕过构造的测试实例提供统一记录器。"""
    monitor = getattr(owner, "_connection_monitor", None)
    if monitor is None:
        monitor = ConnectionMonitor()
        setattr(owner, "_connection_monitor", monitor)
    return monitor
