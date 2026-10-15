"""管理 worker 内执行器缓存与请求级运行时准备.

该模块把“可跨请求复用的执行器实例”和“只能按请求重建的审计/
账户控制上下文”明确拆开，避免复用连接时把上一条命令的运行时
状态意外带入下一条命令。
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from functools import partial
from typing import cast

from loguru import logger

from axile.common.trade_channel import TradeChannel
from axile.executor.abstract_executor.base import AbstractExecutor
from axile.executor.connection_state import connection_observer_scope
from axile.executor.termination import ExecutionTerminationController
from axile.server.connection_state import ConnectionBinding, ConnectionCollector
from axile.server.db.models.account import AccountSnapshot
from axile.server.execution.audit_sink import build_server_execution_audit_sink
from axile.server.execution.execution_account_control import (
    build_account_control_guard,
    flush_account_control_records,
)
from axile.server.execution.factory import create_executor_instance, initialize_executor_instance
from axile.server.execution.resident_connection import (
    ResidentConnectionState,
    ensure_resident_connection,
    executor_config_signature,
    reuse_resident_connection,
)
from axile.server.execution.worker_backend.catalog import RemoteCatalogProvider
from axile.server.execution.worker_backend.protocol import WorkerTerminationSignal


@dataclass
class _WorkerBackendState(ResidentConnectionState):
    """保存多进程 worker 内可复用的执行器缓存.

    Notes
    -----
    这里缓存的是执行器实例及其复用判定键，不缓存请求级审计上下文
    或账户控制 guard。
    """

    request_active: bool = False
    catalog_provider: RemoteCatalogProvider | None = None
    requires_worker_restart: bool = False
    connection_collector: ConnectionCollector = field(default_factory=ConnectionCollector)
    termination_lock: threading.Lock = field(default_factory=threading.Lock)
    active_execution_id: str | None = None
    termination_event: threading.Event = field(default_factory=threading.Event)
    termination_reason: str | None = None
    termination_mode: str | None = None
    pending_terminations: dict[str, WorkerTerminationSignal] = field(default_factory=dict)
    orders_recovery_enabled: bool = False
    orders_account: AccountSnapshot | None = None
    orders_retry_at: float = 0
    orders_recovery_thread: threading.Thread | None = None
    orders_last_sample: tuple | None = None
    orders_binding: ConnectionBinding | None = None
    orders_executor: object | None = None
    orders_generation: int = 0


def _request_worker_termination(state: _WorkerBackendState, signal: WorkerTerminationSignal) -> None:
    """把控制线程收到的终止信号投递给当前或即将启动的 execution."""
    with state.termination_lock:
        if state.active_execution_id != signal.execution_id:
            state.pending_terminations[signal.execution_id] = signal
            return
        state.termination_reason = signal.reason
        state.termination_mode = signal.mode
        state.termination_event.set()


def _activate_worker_termination(
    state: _WorkerBackendState,
    execution_id: str | None,
) -> ExecutionTerminationController | None:
    """为 worker 当前请求创建可由控制管道唤醒的终止控制器."""
    if execution_id is None:
        return None
    with state.termination_lock:
        state.active_execution_id = execution_id
        state.termination_event.clear()
        state.termination_reason = None
        state.termination_mode = None
        pending = state.pending_terminations.pop(execution_id, None)
        if pending is not None:
            state.termination_reason = pending.reason
            state.termination_mode = pending.mode
            state.termination_event.set()

    def _reason() -> str | None:
        with state.termination_lock:
            return state.termination_reason

    def _mode() -> str | None:
        with state.termination_lock:
            return state.termination_mode

    return ExecutionTerminationController(
        cancel_event=state.termination_event,
        reason_provider=_reason,
        mode_provider=_mode,
    )


def _clear_worker_termination(state: _WorkerBackendState, execution_id: str | None) -> None:
    """清除已完成请求的终止上下文，避免污染常驻 worker 的下一次请求."""
    if execution_id is None:
        return
    with state.termination_lock:
        if state.active_execution_id != execution_id:
            return
        state.active_execution_id = None
        state.termination_event.clear()
        state.termination_reason = None
        state.termination_mode = None


def _close_executor(executor: object | None) -> None:
    """清理请求级状态并释放缓存执行器。"""
    if executor is None:
        return
    monitor = getattr(executor, "_connection_monitor", None)
    if monitor is not None:
        monitor.close()
    finalize_error = None
    try:
        _finalize_executor(executor)
    except Exception as exc:
        finalize_error = exc
    try:
        stop = getattr(executor, "stop", None)
        close = getattr(executor, "close", None)
        if callable(stop):
            stop()
        elif callable(close):
            close()
    except Exception:
        if finalize_error is not None:
            logger.opt(exception=finalize_error).warning("审计收尾失败，连接释放也失败")
        raise
    if finalize_error is not None:
        raise finalize_error


def _close_state_executor(state: _WorkerBackendState) -> None:
    """清理失败时标记整个 worker 不可复用，由主进程终止兜底。"""
    try:
        _close_executor(state.executor)
    except Exception:
        state.requires_worker_restart = True
        raise


def _config_signature(account: AccountSnapshot) -> str:
    """生成账户配置的稳定签名，用于判定执行器是否可复用.

    Parameters
    ----------
    account : Account
        待生成配置签名的账户对象。

    Returns
    -------
    str
        仅由账户配置字段决定的稳定签名。
    """
    return executor_config_signature(account)


def _prepare_executor(
    *,
    executor: object,
    account: AccountSnapshot,
    execution_id: str | None,
    audit_context: dict[str, object],
    termination_controller: ExecutionTerminationController | None = None,
) -> None:
    """
    为当前请求准备执行器的审计与账户控制运行时。

    Parameters
    ----------
    executor : object
        当前请求要执行的执行器实例。
    account : Account
        关联账户对象。
    execution_id : str | None
        当前执行标识。
    audit_context : dict[str, object]
        需要绑定到执行器的审计上下文。

    Notes
    -----
    即使执行器实例被缓存复用，请求级 audit sink、账户控制 guard
    和 runtime 准备步骤也必须逐次重绑。
    """
    # 这里每次请求都重新绑定 audit / guard，即使执行器实例被复用，请求级上下文也不能复用。
    set_audit_context = getattr(executor, "set_audit_context", None)
    if callable(set_audit_context):
        set_audit_context(audit_context)

    set_audit_sink = getattr(executor, "set_audit_sink", None)
    if callable(set_audit_sink):
        set_audit_sink(build_server_execution_audit_sink())

    guard = asyncio.run(build_account_control_guard(account, execution_id))
    set_account_control_guard = getattr(executor, "set_account_control_guard", None)
    if callable(set_account_control_guard):
        set_account_control_guard(guard)

    set_termination_controller = getattr(executor, "set_termination_controller", None)
    if callable(set_termination_controller):
        set_termination_controller(termination_controller)

    prepare_execution_runtime = getattr(executor, "prepare_execution_runtime", None)
    if callable(prepare_execution_runtime):
        prepare_execution_runtime()


def _resolve_prepared_executor(
    *,
    state: _WorkerBackendState,
    account: AccountSnapshot,
    execution_id: str | None,
    audit_context: dict[str, object],
    termination_controller: ExecutionTerminationController | None = None,
    allow_recovery: bool = True,
    force_reconnect: bool = False,
    allow_connect: bool = True,
) -> object:
    """
    解析并准备本次请求要使用的执行器实例。

    Parameters
    ----------
    state : _WorkerBackendState
        worker 当前缓存状态。
    account : Account
        本次请求绑定的账户对象。
    execution_id : str | None
        当前执行标识。
    audit_context : dict[str, object]
        需要绑定到执行器的审计上下文。

    Returns
    -------
    object
        已完成请求级准备的执行器实例。
    """

    def prepare(executor: object) -> None:
        _configure_executor_resources(state, account, executor)
        _prepare_executor(
            executor=executor,
            account=account,
            execution_id=execution_id,
            audit_context=audit_context,
            termination_controller=termination_controller,
        )

    def initialize(executor: object) -> None:
        _initialize_worker_connection(state, executor)

    if state.orders_account is None or _config_signature(state.orders_account) != _config_signature(account):
        state.orders_recovery_enabled = False
        state.orders_account = account
        state.orders_retry_at = 0
    with connection_observer_scope(state.connection_collector.observe):
        state.connection_observer = state.connection_collector.observe_session
        if state.connection_collector.needs_reconciliation:
            state.connected_once = False
        if not allow_connect:
            with state.resource_lock:
                executor = reuse_resident_connection(state, account)
                prepare(executor)
                return executor
        executor = ensure_resident_connection(
            state,
            account,
            create=lambda: create_executor_instance(account, initialize=False),
            close=lambda _executor: _close_state_executor(state),
            initialize=initialize,
            prepare=prepare,
            allow_recovery=allow_recovery,
            force=force_reconnect,
        )
        state.orders_recovery_enabled = True
        state.orders_account = account
        state.orders_binding = state.connection_collector.binding
        if state.orders_executor is not executor:
            state.orders_generation += 1
            state.orders_executor = executor
        return executor


def _configure_executor_resources(state: _WorkerBackendState, account: AccountSnapshot, executor: object) -> None:
    """交易和人工查询共用目录资源；只绑定能力，不启动连接。"""
    if account.trade_channel == TradeChannel.CTP:
        from axile.server.execution.catalog_control import catalog_control_scope

        setattr(executor, "_catalog_control_factory", partial(catalog_control_scope, account))
    if state.catalog_provider is not None:
        set_instruments = getattr(executor, "set_instrument_catalog_provider", None)
        if callable(set_instruments):
            set_instruments(state.catalog_provider)
    monitor = getattr(executor, "_connection_monitor", None)
    if monitor is not None:
        collector = state.connection_collector

        def observe(observation):
            collector.observe(observation)
            book = getattr(executor, "account_orders", None)
            if observation.status == "error" and book is not None:
                book.invalidate("账户连接异常，委托状态待确认")

        monitor.bind(observe)


def _initialize_worker_connection(state: _WorkerBackendState, executor: object) -> None:
    """在已有账户控制上下文中初始化渠道，并保持目录 IPC 的开始/结束配对。"""
    catalog = state.catalog_provider if callable(getattr(executor, "set_instrument_catalog_provider", None)) else None
    end_attempted = False
    try:
        if catalog is not None:
            catalog.begin()
        initialize_executor_instance(cast(AbstractExecutor, executor))
        if catalog is not None:
            end_attempted = True
            catalog.end()
    except Exception:
        if catalog is not None and not end_attempted:
            try:
                catalog.end()
            except Exception as notification_error:
                logger.opt(exception=notification_error).warning("初始化结束通知发送失败，保留原始异常")
        raise


def _finalize_executor(executor: object | None) -> None:
    """
    清理执行器上的运行时与账户控制残留状态。

    Parameters
    ----------
    executor : object | None
        当前需要清理的执行器实例；为空时忽略。

    Notes
    -----
    账户控制记录必须先落库，再清理执行器 runtime，否则这次请求
    期间累积的 guard 状态会在清理后丢失。
    """
    if executor is None:
        return

    # 先刷盘，再逐项解除绑定；刷盘或单个 setter 失败也必须清理其他请求状态。
    error: Exception | None = None
    try:
        asyncio.run(flush_account_control_records(executor))
    except Exception as exc:
        error = exc
    request_bindings = (
        ("set_account_control_guard", None),
        ("set_termination_controller", None),
        ("set_audit_sink", None),
        ("set_audit_context", {}),
    )
    for setter_name, value in request_bindings:
        try:
            setter = getattr(executor, setter_name, None)
            if callable(setter):
                setter(value)
        except Exception as exc:
            error = error or exc
    try:
        clear_execution_runtime = getattr(executor, "clear_execution_runtime", None)
        if callable(clear_execution_runtime):
            clear_execution_runtime()
    except Exception as exc:
        error = error or exc
    if error is not None:
        raise error
