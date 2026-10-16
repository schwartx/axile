"""观察连接生命周期与实际账户访问，不根据后续业务处理推断连通性。"""

from __future__ import annotations

import threading
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Literal

from loguru import logger

from axile.executor.algorithms.utils.clock import clock_now_iso
from axile.executor.connection_presentation import PresentationInput, presentation_snapshot
from axile.executor.connection_progress import (
    ConnectionProgressTracker,
    ConnectionStepUpdate,
    connection_step_error,
    current_connection_progress,
)

CONNECTION_ERROR_SCOPE = "account_connection"
type ConnectionStatus = Literal["ok", "error", "connecting", "recovering", "cooldown", "disconnected"]


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

    status: ConnectionStatus
    observed_at: str
    message: str | None = None
    error: dict[str, object] | None = None
    label: str | None = None
    presentation: dict[str, object] | None = None
    kind: Literal["connection", "access", "session", "progress"] = "connection"

    progress: dict[str, object] | None = None

    def __post_init__(self) -> None:
        """旧位置参数保持原义，正常结果清除故障内容。"""
        label = self.label.strip() if isinstance(self.label, str) else None
        object.__setattr__(self, "label", (label or None) if self.status != "ok" else None)
        object.__setattr__(
            self,
            "presentation",
            presentation_snapshot(self.presentation) if self.status in {"error", "cooldown"} else None,
        )
        if self.status == "ok":
            object.__setattr__(self, "message", None)
            object.__setattr__(self, "error", None)


def _failure_observation(error: BaseException) -> ConnectionObservation | None:
    """只沿同一异常及显式 cause 查找，不继承 monitor 的历史结果。"""
    seen: set[int] = set()
    while id(error) not in seen:
        seen.add(id(error))
        observation = getattr(error, "_connection_observation", None)
        if isinstance(observation, ConnectionObservation):
            return observation
        if error.__cause__ is None:
            break
        error = error.__cause__
    return None


def preserve_connection_failure(error: BaseException, source: BaseException) -> None:
    """包装同一故障时显式保留其观察和完整原始证据。"""
    observation = _failure_observation(source)
    if observation is not None:
        setattr(error, "_connection_observation", observation)
        setattr(error, "error_scope", CONNECTION_ERROR_SCOPE)


def _with_propagation_traceback(observation: ConnectionObservation, error: BaseException) -> ConnectionObservation:
    """首次 raise 前上报的错误在传播后补全调用栈，不丢弃原始 SDK 证据。"""
    if error.__traceback__ is None:
        return observation
    trace = "".join(traceback.format_exception(error))
    details = dict(observation.error or {})
    if trace in (details.get("traceback"), details.get("propagation_traceback")):
        return observation
    return replace(observation, error={**details, "propagation_traceback": trace})


def _has_exception_frames(error: BaseException) -> bool:
    """检查实际参与格式化的异常链，避免把异常摘要当成调用栈。"""
    seen: set[int] = set()
    while id(error) not in seen:
        seen.add(id(error))
        if error.__traceback__ is not None:
            return True
        cause = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
        if cause is None:
            break
        error = cause
    return False


def _failure_evidence(error: BaseException, message: str, raw: str) -> dict[str, object]:
    """保存原始证据；未抛出的回调异常另存上报现场，不伪造异常栈。"""
    formatted = getattr(error, "formatted_traceback", None)
    details: dict[str, object] = {
        "message": message,
        "technical_detail": raw,
        "traceback": formatted or "".join(traceback.format_exception(error)),
        "exception_type": type(error).__name__,
        "error_scope": CONNECTION_ERROR_SCOPE,
    }
    if not formatted and not _has_exception_frames(error):
        details["traceback_unavailable_reason"] = "该异常在首次上报时尚未抛出，没有异常调用栈。"
        # 去掉本函数和 failed 的内部帧，末帧定位到渠道实际的上报处。
        details["reporting_stack"] = "".join(traceback.format_stack()[:-2])
    for key in ("reason_code", "error_id", "status", "status_code", "return_code", "broker_message"):
        value = getattr(error, key, None)
        if isinstance(value, (str, int)):
            details[key] = value
    return details


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
        self.progress = current_connection_progress()
        self._latest: ConnectionObservation | None = None
        self._active = True
        self._failure_revision = 0

    def bind(self, observer: ConnectionObserver) -> None:
        """绑定观察者并回放最新结果，不伪造新的确认时间。"""
        with self._lock:
            self._observer = observer
            self.progress = current_connection_progress() or self.progress
            if self._active and self._latest is not None:
                self._notify(self._latest)

    def step(self, step: str, status: ConnectionStepUpdate, details: dict | None = None) -> None:
        """渠道共用的阶段上报入口；记录器在连接前由宿主注入。"""
        with self._lock:
            if not self._active:
                return
            self.progress = self.progress or current_connection_progress()
            tracker = self.progress
        if tracker is not None:
            tracker.step(step, status, details)

    def begin_recovery(self) -> None:
        """已有会话断线后开启真实恢复过程，不重演首次登录步骤。"""
        with self._lock:
            if not self._active:
                return
            tracker = self.progress
            if tracker is None or (tracker.run.status == "running" and tracker.run.purpose == "recovery"):
                return
            observer = self._observer
            self.progress = ConnectionProgressTracker(
                tracker.run.channel,
                "recovery",
                lambda data: (
                    observer(ConnectionObservation("recovering", clock_now_iso(), kind="progress", progress=data))
                    if observer
                    else None
                ),
            )
            self.progress.step("recover_connection", "running")
            self._publish(ConnectionObservation("recovering", clock_now_iso(), "正在恢复账户通道"))

    def close(self) -> None:
        """主动释放后忽略迟到回调，保留最后观察。"""
        with self._lock:
            self._active = False

    @contextmanager
    def connecting(self, *, label: str | None = None, presentation: PresentationInput = None) -> Iterator[None]:
        """仅包围实际连接建立代码，业务查询和转换不进入此边界。"""
        try:
            yield
        except Exception as exc:
            self.failed(exc, label=label, presentation=presentation)
            raise

    def succeeded(self) -> None:
        """渠道明确完成连接、认证或实际账户访问时调用。"""
        if self.progress is not None and self.progress.run.purpose == "recovery":
            self.step("recover_connection", "passed")
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
                self._publish(ConnectionObservation("ok", clock_now_iso(), kind="access"))

    def failed(self, error: BaseException, *, label: str | None = None, presentation: PresentationInput = None) -> None:
        """连接故障发生处标记归属并固定完整异常链。"""
        if not self._active:
            return
        if self.progress is not None:
            self.progress.finish("failed", connection_step_error(error))
        error.error_scope = CONNECTION_ERROR_SCOPE  # type: ignore[attr-defined]
        previous = _failure_observation(error)
        if previous is not None:
            # 外层连接边界的通用文案不能覆盖内层已确认的故障。
            previous = _with_propagation_traceback(previous, error)
            setattr(error, "_connection_observation", previous)
            self._publish(previous)
            return
        raw = str(getattr(error, "technical_detail", None) or error)
        message = str(getattr(error, "execution_error", None) or raw or type(error).__name__)
        details = _failure_evidence(error, message, raw)
        observation = ConnectionObservation(
            "error", clock_now_iso(), message, details, label, presentation_snapshot(presentation)
        )
        setattr(error, "_connection_observation", observation)
        self._publish(observation)

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
