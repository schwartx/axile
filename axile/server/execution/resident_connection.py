"""账户连接宿主：缓存执行器、合并连接准备，并隔离只读请求与故障恢复。"""

from __future__ import annotations

import json
import math
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial

from loguru import logger

from axile.executor.algorithms.utils.clock import clock_monotonic, clock_now_iso
from axile.executor.connection_state import CONNECTION_ERROR_SCOPE, ConnectionObservation, ConnectionStatus
from axile.server.db.models.account import AccountContext

_RECONNECT_COOLDOWN_SECONDS = 30.0


class AccountConnectionUnavailable(ConnectionError):
    """连接暂不可用；只读调用不得据此重新登录或重放交易请求。"""

    error_scope = CONNECTION_ERROR_SCOPE

    def __init__(self, message: str, *, cause: BaseException | None = None, retry_after: float = 0) -> None:
        super().__init__(message)
        self.execution_error = message
        self.retry_after = max(0, math.ceil(retry_after))
        self.technical_detail = str(cause) if cause is not None else message
        self.formatted_traceback = "".join(traceback.format_exception(cause)) if cause is not None else None


@dataclass
class ResidentConnectionState:
    """跨请求保留连接；请求级 guard、算法和审计由 worker 单独管理。"""

    executor: object | None = None
    account_id: int | None = None
    config_signature: str | None = None
    resource_lock: threading.RLock = field(default_factory=threading.RLock)
    connection_attempted: bool = False
    connected_once: bool = False
    connection_error: BaseException | None = None
    next_connect_at: float = 0
    disconnected: bool = False
    connection_observer: Callable[[ConnectionObservation], None] | None = None
    attempt_revision: int = 0
    reconciled_revision: object | None = None


def executor_config_signature(account: AccountContext) -> str:
    """配置复用键包含渠道，避免相同字段跨渠道误用旧执行器。"""
    return json.dumps([str(account.trade_channel), account.account_config], sort_keys=True, ensure_ascii=False)


def _observe(state: ResidentConnectionState, status: ConnectionStatus, message: str | None = None) -> None:
    logger.bind(account_id=state.account_id).info("账户会话状态更新 status={}", status)
    if state.connection_observer is not None:
        labels = {
            "connecting": "正在准备",
            "recovering": "正在恢复",
            "cooldown": "暂不可用",
            "disconnected": "已断开连接",
        }
        evidence = None
        source = getattr(state.connection_error, "_connection_observation", None)
        presentation = source.presentation if isinstance(source, ConnectionObservation) else None
        label = source.label if isinstance(source, ConnectionObservation) and source.label else labels.get(status)
        if status == "cooldown" and state.connection_error is not None:
            evidence = dict(source.error or {}) if isinstance(source, ConnectionObservation) else {}
            trace = "".join(traceback.format_exception(state.connection_error))
            evidence.setdefault("technical_detail", str(state.connection_error))
            evidence.setdefault("traceback", trace)
            if trace != evidence["traceback"]:
                evidence["propagation_traceback"] = trace
            evidence["retry_after_seconds"] = max(0, math.ceil(state.next_connect_at - clock_monotonic()))
        state.connection_observer(
            ConnectionObservation(status, clock_now_iso(), message, evidence, label, presentation)
        )


def _unavailable(state: ResidentConnectionState) -> AccountConnectionUnavailable:
    remaining = max(0, state.next_connect_at - clock_monotonic())
    if state.disconnected:
        message = "账户连接已主动断开，请重新连接；当前委托状态待确认"
    elif remaining > 0:
        message = f"账户连接恢复冷却中，{math.ceil(remaining)} 秒后可重试；当前委托状态待确认"
    else:
        message = "账户连接不可用，请重新连接；当前委托状态待确认"
    return AccountConnectionUnavailable(message, cause=state.connection_error, retry_after=remaining)


def _failed(state: ResidentConnectionState, error: BaseException) -> None:
    state.attempt_revision += 1
    state.connection_attempted = True
    state.connection_error = error
    state.next_connect_at = clock_monotonic() + _RECONNECT_COOLDOWN_SECONDS
    _observe(state, "cooldown", str(getattr(error, "execution_error", None) or "账户连接准备失败"))


def _check_attempt(state: ResidentConnectionState, *, allow_recovery: bool, force: bool) -> None:
    if force:
        return
    if state.disconnected or (state.connection_attempted and not allow_recovery):
        raise _unavailable(state) from state.connection_error
    if state.connection_error is not None and clock_monotonic() < state.next_connect_at:
        raise _unavailable(state) from state.connection_error


def resolve_resident_executor(
    state: ResidentConnectionState,
    account: AccountContext,
    *,
    create: Callable[[], object],
    close: Callable[[object], None],
    allow_recovery: bool = False,
    force: bool = False,
) -> object:
    """只按配置解析缓存对象，不通过健康检查隐式重建执行器。

    Notes
    -----
    调用方必须持有 ``resource_lock``；构造失败也保留冷却状态，避免页面刷新
    在缓存为空时绕过连接限流。配置变化才清空上一份配置的故障与主动断开状态。
    """
    signature = executor_config_signature(account)
    if state.config_signature != signature or state.account_id != account.id:
        if state.executor is not None:
            close(state.executor)
        state.executor = None
        state.account_id, state.config_signature = account.id, signature
        state.connection_attempted = state.connected_once = state.disconnected = False
        state.connection_error, state.next_connect_at = None, 0
    if state.executor is not None:
        return state.executor
    _check_attempt(state, allow_recovery=allow_recovery, force=force)
    if force:
        state.disconnected = False
    # 新实例不能继承旧会话的核对结果，即使渠道在构造时已经连接成功。
    state.connected_once = False
    state.reconciled_revision = None
    _observe(state, "recovering" if state.connection_attempted else "connecting")
    try:
        state.executor = create()
    except Exception as exc:
        _failed(state, exc)
        raise
    return state.executor


def _connected(executor: object) -> bool:
    if getattr(executor, "_requires_connection_initialization", False):
        return False
    verify = getattr(executor, "_verify_connection", None)
    return not callable(verify) or bool(verify())


def reuse_resident_connection(state: ResidentConnectionState, account: AccountContext) -> object:
    """查询只复用已核对的当前会话，不构造执行器、不登录或恢复连接。"""
    with state.resource_lock:
        if (
            state.account_id != account.id
            or state.config_signature != executor_config_signature(account)
            or state.executor is None
            or not state.connected_once
            or state.disconnected
            or state.connection_error is not None
        ):
            raise _unavailable(state) from state.connection_error
        try:
            connected = _connected(state.executor)
        except Exception as exc:
            _failed(state, exc)
            raise _unavailable(state) from exc
        if not connected:
            _failed(state, ConnectionError("账户交易连接已断开"))
            raise _unavailable(state) from state.connection_error
        return state.executor


def reconcile_resident_executor(executor: object) -> None:
    """复用统一核对，并保留常驻账户的终态委托确认。"""
    from axile.executor.connection_reconciliation import reconcile_account_connection
    from axile.server.account_orders_runtime import terminal_order

    reconcile_account_connection(executor, terminal_order)


def _progress_connection_ready(state: ResidentConnectionState) -> bool:
    """进度计划读取同一份渠道状态；探测失败沿用宿主的故障记录。"""
    if state.executor is None:
        return False
    try:
        return _connected(state.executor)
    except Exception as exc:
        _failed(state, exc)
        raise


def ensure_resident_connection(
    state: ResidentConnectionState,
    account: AccountContext,
    *,
    create: Callable[[], object],
    close: Callable[[object], None],
    initialize: Callable[[object], None],
    prepare: Callable[[object], None] = lambda _executor: None,
    reconcile: Callable[[object], None] = reconcile_resident_executor,
    allow_recovery: bool = False,
    force: bool = False,
) -> object:
    """建立过程覆盖构造、渠道登录和账户核对；健康会话复用不重置进度。"""
    from axile.executor.connection_progress import (
        ConnectionProgressTracker,
        connection_progress_scope,
        connection_step_error,
    )
    from axile.executor.connection_state import current_connection_observer

    observed_attempt = state.attempt_revision
    with state.resource_lock:
        if force and state.attempt_revision != observed_attempt:
            force = False
        ensure = partial(
            _ensure_resident_connection,
            state,
            account,
            create=create,
            close=close,
            initialize=initialize,
            prepare=prepare,
            reconcile=reconcile,
            allow_recovery=allow_recovery,
            force=force,
        )
        unchanged = state.account_id == account.id and state.config_signature == executor_config_signature(account)
        connected = unchanged and _progress_connection_ready(state)
        healthy = unchanged and state.connected_once and state.connection_error is None and state.executor is not None
        if healthy and connected and state.reconciled_revision == getattr(state.executor, "connection_revision", None):
            return ensure()
        observer = current_connection_observer()
        monitor = getattr(state.executor, "_connection_monitor", None) if unchanged else None
        tracker = getattr(monitor, "progress", None)
        native_recovered = connected
        if tracker is None or tracker.run.status != "running" or not native_recovered:
            tracker = ConnectionProgressTracker(
                str(account.trade_channel),
                "recovery" if native_recovered else "connect",
                observer=lambda data: (
                    observer(ConnectionObservation("connecting", clock_now_iso(), kind="progress", progress=data))
                    if observer
                    else None
                ),
            )
        with connection_progress_scope(tracker):
            if native_recovered:
                tracker.step("recover_connection", "running")
                tracker.step("recover_connection", "passed")
            try:
                executor = ensure()
                tracker.step("ready", "running")
                tracker.step("ready", "passed")
                tracker.finish()
                return executor
            except Exception as exc:
                tracker.finish("failed", connection_step_error(exc))
                raise


def _ensure_resident_connection(
    state: ResidentConnectionState,
    account: AccountContext,
    *,
    create: Callable[[], object],
    close: Callable[[object], None],
    initialize: Callable[[object], None],
    prepare: Callable[[object], None] = lambda _executor: None,
    reconcile: Callable[[object], None] = reconcile_resident_executor,
    allow_recovery: bool = False,
    force: bool = False,
) -> object:
    """由连接操作或交易准备建立会话；普通查询使用 reuse_resident_connection。"""
    observed_attempt = state.attempt_revision
    with state.resource_lock:
        # 同时抵达的显式重连共用前一次结果；等待锁不构成再次绕过冷却的授权。
        if force and state.attempt_revision != observed_attempt:
            force = False
        had_executor = state.executor is not None
        executor = resolve_resident_executor(
            state, account, create=create, close=close, allow_recovery=allow_recovery, force=force
        )
        try:
            connected = _connected(executor)
        except Exception as exc:
            _failed(state, exc)
            raise
        if connected:
            if state.connection_error is not None and not state.connected_once:
                _check_attempt(state, allow_recovery=allow_recovery, force=force)
            try:
                prepare(executor)
            except Exception as exc:
                if not state.connected_once:
                    _failed(state, exc)
                raise
            if (
                not state.connected_once
                or state.connection_error is not None
                or state.reconciled_revision != getattr(executor, "connection_revision", None)
            ):
                _confirm_ready(state, executor, reconcile)
            return executor
        if state.connected_once and state.connection_error is None:
            _failed(state, ConnectionError("账户交易连接已断开"))
        _check_attempt(state, allow_recovery=allow_recovery, force=force)
        if had_executor:
            _observe(state, "recovering" if state.connection_attempted else "connecting")
        try:
            state.connected_once = False
            if state.connection_attempted and had_executor:
                close(executor)
                state.executor = None
                state.executor = executor = create()
            state.disconnected = False
            state.connection_attempted = True
            prepare(executor)
            if not _connected(executor):
                initialize(executor)
            if not _connected(executor):
                raise ConnectionError("账户连接初始化结束，但交易通道尚未就绪")
            _confirm_ready(state, executor, reconcile)
            return executor
        except Exception as exc:
            _failed(state, exc)
            raise


def _confirm_ready(state: ResidentConnectionState, executor: object, reconcile: Callable[[object], None]) -> None:
    try:
        reconcile(executor)
    except Exception as exc:
        _failed(state, exc)
        raise
    state.connection_attempted = state.connected_once = True
    state.attempt_revision += 1
    state.disconnected = False
    state.reconciled_revision = getattr(executor, "connection_revision", None)
    state.connection_error, state.next_connect_at = None, 0
    _observe(state, "ok")


def disconnect_resident_executor(state: ResidentConnectionState, *, close: Callable[[object], None]) -> None:
    """主动断开后禁止读请求重连；显式重连和配置变化可以解除此状态。"""
    with state.resource_lock:
        if state.executor is not None:
            try:
                close(state.executor)
            except Exception as exc:
                _failed(state, exc)
                raise
        state.executor = None
        state.connected_once = False
        state.reconciled_revision = None
        state.disconnected = state.connection_attempted = True
        state.connection_error, state.next_connect_at = None, 0
        _observe(state, "disconnected", "账户连接已主动断开")
