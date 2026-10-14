"""
GM 策略桥接运行时上下文.

集中管理 ``GMStrategyBridge`` 与 ``gm_strategy.py`` 之间共享的运行态对象。
当前实现按单 bridge 单上下文建模，不再维护线程局部或账户级隔离。
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeAlias

from axile.executor.connection_state import ConnectionMonitor

if TYPE_CHECKING:
    from axile.executor.gm.core.strategy_bridge import GMBridgeEventSink, GMBridgeRequest

GMStartupHistoryEntry: TypeAlias = dict[str, object]
GMStartupStateValue: TypeAlias = str | int | float | bool | None | GMStartupHistoryEntry | list[GMStartupHistoryEntry]


@dataclass(slots=True)
class GMStrategyRuntimeContext:
    """
    表示当前 GM 策略文件可见的运行时上下文.

    Attributes
    ----------
    dispatcher : GMBridgeEventSink
        用于分发订单、成交和行情事件的桥接 sink。
    ready_event : threading.Event
        策略初始化完成后通知主线程的事件对象。
    stop_event : threading.Event
        主线程请求停止时设置的事件对象。
    stats : dict[str, int]
        bridge 线程共享的统计信息。
    subscribe_symbols : list[str]
        当前 bridge 已订阅的行情标的列表。
    request_queue : queue.Queue[GMBridgeRequest]
        主线程提交给 GM runtime 执行的请求队列。
    startup_state : dict[str, GMStartupStateValue]
        启动阶段诊断状态。
    runtime_stop_requested : threading.Event
        标记是否已请求 GM runtime 软停止。
    runtime_stop_lock : threading.Lock
        协调软停止请求的互斥锁。
    timer_id : int
        GM bridge 请求定时器 ID；未创建时为 ``0``。
    """

    dispatcher: GMBridgeEventSink
    ready_event: threading.Event
    stop_event: threading.Event
    stats: dict[str, int]
    subscribe_symbols: list[str]
    request_queue: queue.Queue[GMBridgeRequest]
    startup_state: dict[str, GMStartupStateValue]
    runtime_stop_requested: threading.Event
    runtime_stop_lock: threading.Lock
    timer_id: int = 0
    account_id: str = ""
    connection_monitor: ConnectionMonitor | None = None
    strategy_initialized: bool = False
    account_checked: bool = False
    startup_failed: bool = False
    startup_failure_handler: Callable[[Exception], None] | None = None


# 当前设计固定为“一账户一 worker 进程，一进程一条 GM bridge”。
# 因此这里使用进程内模块级单例上下文即可，不需要线程局部或多 bridge 隔离。
_active_context: GMStrategyRuntimeContext | None = None


def install_gm_strategy_runtime_context(
    *,
    dispatcher: GMBridgeEventSink,
    ready_event: threading.Event,
    stop_event: threading.Event,
    stats: dict[str, int],
    subscribe_symbols: list[str],
    request_queue: queue.Queue[GMBridgeRequest],
    startup_state: dict[str, GMStartupStateValue],
    runtime_stop_requested: threading.Event,
    runtime_stop_lock: threading.Lock,
    account_id: str = "",
    connection_monitor: ConnectionMonitor | None = None,
    startup_failure_handler: Callable[[Exception], None] | None = None,
) -> None:
    """
    注册当前 bridge 线程对应的 GM 策略运行时上下文.

    Parameters
    ----------
    dispatcher : GMBridgeEventSink
        用于分发订单、成交和行情事件的桥接 sink。
    ready_event : threading.Event
        策略初始化完成后通知主线程的事件对象。
    stop_event : threading.Event
        主线程请求停止时设置的事件对象。
    stats : dict[str, int]
        bridge 线程共享的统计信息。
    subscribe_symbols : list[str]
        当前 bridge 已订阅的行情标的列表。
    request_queue : queue.Queue[GMBridgeRequest]
        主线程提交给 GM runtime 执行的请求队列。
    startup_state : dict[str, GMStartupStateValue]
        启动阶段诊断状态。
    runtime_stop_requested : threading.Event
        标记是否已请求 GM runtime 软停止。
    runtime_stop_lock : threading.Lock
        协调软停止请求的互斥锁。

    """
    global _active_context

    active_context = GMStrategyRuntimeContext(
        dispatcher=dispatcher,
        ready_event=ready_event,
        stop_event=stop_event,
        stats=stats,
        subscribe_symbols=subscribe_symbols,
        request_queue=request_queue,
        startup_state=startup_state,
        runtime_stop_requested=runtime_stop_requested,
        runtime_stop_lock=runtime_stop_lock,
        account_id=account_id,
        connection_monitor=connection_monitor,
        startup_failure_handler=startup_failure_handler,
    )
    _active_context = active_context


def get_gm_strategy_runtime_context() -> GMStrategyRuntimeContext | None:
    """
    返回当前已注册的 GM 策略运行时上下文.

    Returns
    -------
    GMStrategyRuntimeContext | None
        当前已注册的 bridge 上下文；未注册时返回 ``None``。
    """
    return _active_context


def clear_gm_strategy_runtime_context() -> None:
    """清空当前 GM 策略运行时上下文."""
    global _active_context
    _active_context = None
