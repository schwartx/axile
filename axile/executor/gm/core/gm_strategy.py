# -*- coding: utf-8 -*-
"""
Axile GM Callback Bridge Strategy.

这是策略桥接器使用的固定策略文件，用于接收掘金的回调事件。
由 GMStrategyBridge 动态调用。
"""

# pyright: reportAttributeAccessIssue=false, reportUndefinedVariable=false, reportCallIssue=false, reportArgumentType=false

import queue
import time
from datetime import datetime
from typing import TypeAlias

from gm.api import *  # type: ignore  # noqa: F401, F403
from gm.api._errors import GmError
from gm.csdk.c_sdk import TickLikeDict2  # type: ignore
from gm.model import DictLikeAccountStatus, DictLikeExecRpt, DictLikeOrder  # type: ignore
from gm.model.storage import Context  # type: ignore

from axile.common.trade_channel import TradeChannel
from axile.executor.gm.common import (
    AccConnectionState,
    _build_gm_trade_id,
    convert_gm_order_type_to_type,
    convert_gm_side_to_direction,
    convert_gm_status_to_string,
    from_gm_price,
)
from axile.executor.gm.connection import (
    GMAccountNotFoundError,
    GMAccountStartupError,
    report_account_status,
    report_gm_failure,
    report_runtime_failure,
)
from axile.executor.gm.core.api_bridge import GMApiBridge, GMBridgeRequestPayload, GMSubscribeSymbolsRequest
from axile.executor.gm.core.bridge_context import GMStrategyRuntimeContext, get_gm_strategy_runtime_context
from axile.executor.gm.core.callback_dispatcher import GMRuntimeLogLevel
from axile.executor.models.unified_order import TradeRecord, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData

_StartupDetailValue: TypeAlias = str | int | float | bool | None


def _get_bridge_context() -> GMStrategyRuntimeContext | None:
    """返回当前 GM bridge 已注册的运行时上下文."""
    return get_gm_strategy_runtime_context()


def _get_bridge_stats() -> dict[str, int] | None:
    """返回 bridge 共享统计信息."""
    context = _get_bridge_context()
    if context is None:
        return None
    return context.stats


def _update_startup_phase(phase: str, **details: _StartupDetailValue) -> None:
    """记录 GM bridge startup phase，供主线程超时诊断使用."""
    context = _get_bridge_context()
    if context is None:
        return
    state = context.startup_state

    entry: dict[str, _StartupDetailValue] = {"phase": phase, "ts": time.time()}
    if details:
        entry.update(details)

    history = state.setdefault("history", [])
    if isinstance(history, list):
        history.append(entry)
    state["phase"] = phase
    state["last"] = entry


def _increment_error_stat() -> None:
    """增加 bridge 错误统计计数."""
    stats = _get_bridge_stats()
    if stats:
        stats["errors"] += 1


def _emit_runtime_log(level: GMRuntimeLogLevel, message: str) -> None:
    """
    输出 GM runtime 日志.

    Parameters
    ----------
    level : GMRuntimeLogLevel
        日志级别。
    message : str
        日志正文。

    Notes
    -----
    仅 ``error`` 级别保留 ``print`` 输出；其余级别静默，
    与旧分发器可用时的可观测行为一致。
    """
    if level == "error":
        print(message, flush=True)


def _convert_order_to_unified(order: DictLikeOrder) -> UnifiedOrder:
    """将GM订单对象转换为统一订单模型."""
    create_time = order.created_at
    if isinstance(create_time, datetime):
        create_time = create_time.isoformat()

    actionable_order_id = str(getattr(order, "cl_ord_id", None) or getattr(order, "order_id", "") or "")
    unified_order = UnifiedOrder.create(
        order_id=actionable_order_id,
        symbol=str(order.symbol or ""),
        direction=convert_gm_side_to_direction(int(order.side)).value,
        order_type=convert_gm_order_type_to_type(int(order.order_type)).value,
        volume=float(order.volume or 0),
        price=float(order.price or 0),
        channel_type=TradeChannel.GM,
        status=convert_gm_status_to_string(int(order.status)),
        create_time=create_time,
        update_time=create_time,
        raw_order_data=dict(order) if hasattr(order, "__iter__") else {},  # pyright: ignore[reportUnknownArgumentType]
    )
    unified_order.extra.update(
        {
            "account_id": getattr(order, "account_id", None),
            "cl_ord_id": getattr(order, "cl_ord_id", None),
            "exchange_order_id": getattr(order, "order_id", None),
        }
    )
    return unified_order


def _convert_execrpt_to_trade(execrpt: DictLikeExecRpt) -> TradeRecord:
    """将GM执行回报转换为统一成交记录."""
    trade_time = execrpt.created_at
    if isinstance(trade_time, datetime):
        trade_time = trade_time.isoformat()

    return TradeRecord.create(
        trade_id=_build_gm_trade_id(
            exec_id=getattr(execrpt, "exec_id", None),
            cl_ord_id=getattr(execrpt, "cl_ord_id", None),
            exchange_order_id=getattr(execrpt, "order_id", None),
            symbol=getattr(execrpt, "symbol", None),
            trade_time=trade_time,
            trade_volume=float(getattr(execrpt, "volume", 0) or 0),
            trade_price=float(getattr(execrpt, "price", 0) or 0),
        ),
        symbol=str(getattr(execrpt, "symbol", "") or ""),
        order_id=str(getattr(execrpt, "cl_ord_id", None) or getattr(execrpt, "order_id", None) or ""),
        trade_time=str(trade_time) if trade_time else "",
        trade_volume=float(getattr(execrpt, "volume", 0) or 0),
        trade_price=float(getattr(execrpt, "price", 0) or 0),
        extra={
            "channel_type": TradeChannel.GM,
            "account_id": getattr(execrpt, "account_id", None),
            "symbol": str(getattr(execrpt, "symbol", "") or ""),
            "cl_ord_id": getattr(execrpt, "cl_ord_id", None),
            "exchange_order_id": getattr(execrpt, "order_id", None),
            "raw_trade_data": (dict(execrpt) if hasattr(execrpt, "__iter__") else {}),  # pyright: ignore[reportUnknownArgumentType]
        },
    )


def _convert_tick_to_unified(tick: TickLikeDict2) -> UnifiedPriceData:
    """将GM tick数据转换为统一价格数据."""
    quotes = getattr(tick, "quotes", []) or []
    bid_prices: list[float] = [q.bid_p for q in quotes if hasattr(q, "bid_p") and q.bid_p > 0]
    ask_prices: list[float] = [q.ask_p for q in quotes if hasattr(q, "ask_p") and q.ask_p > 0]
    bid_volumes: list[float] = [q.bid_v for q in quotes if hasattr(q, "bid_p") and q.bid_p > 0]
    ask_volumes: list[float] = [q.ask_v for q in quotes if hasattr(q, "ask_p") and q.ask_p > 0]

    created_at = getattr(tick, "created_at", None)
    if isinstance(created_at, datetime):
        dt_str = created_at.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        timestamp_ms = int(created_at.timestamp() * 1000)
    else:
        dt_str = str(created_at) if created_at else ""
        timestamp_ms = 0

    return from_gm_price(
        {
            "symbol": str(getattr(tick, "symbol", "")),
            "price": float(getattr(tick, "price", 0)),
            "bid_price": bid_prices,
            "ask_price": ask_prices,
            "bid_volume": bid_volumes,
            "ask_volume": ask_volumes,
            "volume": float(getattr(tick, "last_volume", 0)),
            "timestamp": timestamp_ms,
            "dt": dt_str,
        },
        include_raw_data=False,
    )


def init(_context: Context) -> None:
    """策略初始化."""
    _update_startup_phase("init_entered")
    context = _get_bridge_context()
    timer_result = timer(_process_bridge_requests, period=1, start_delay=0)  # noqa: F405
    timer_status = int(timer_result.get("status", 0))
    if timer_status != 0:
        raise RuntimeError(f"GM bridge 请求定时器启动失败: status={timer_status}")
    timer_id = int(timer_result.get("timer_id", 0))
    if context is not None:
        context.timer_id = timer_id
    _update_startup_phase("timer_started", timer_id=timer_id)

    # 订阅行情（如果有配置）
    subscribe_symbols = [] if context is None else list(context.subscribe_symbols)
    if subscribe_symbols:
        for symbol in subscribe_symbols:
            subscribe(symbols=symbol, frequency="tick")  # noqa: F405
            _emit_runtime_log("info", f"订阅行情: {symbol}")
    _update_startup_phase("subscriptions_ready", subscribed_count=len(subscribe_symbols or []))

    # init 回调可能发生在 SDK 账户加载之前；在事件泵的首次定时回调中验证账户。
    if context is not None:
        context.strategy_initialized = True
        _update_startup_phase("account_validation_pending")

    # 记录初始化
    _emit_runtime_log("info", "策略初始化完成，等待交易账户登录确认")


def _apply_account_startup_status(context: GMStrategyRuntimeContext, status: dict) -> None:
    """只有已登录账户可以打开就绪门，首次断开快照不作为最终失败原因。"""
    state = status.get("state", 0)
    context.startup_state["account_state"] = state
    _update_startup_phase("account_status_checked", account_state=state)
    if state == AccConnectionState.LOGGEDIN:
        if context.connection_monitor is not None:
            context.connection_monitor.succeeded()
        if context.strategy_initialized:
            context.account_was_ready = True
            context.ready_event.set()
            _update_startup_phase("ready_signaled", account_state=state)
    elif state == AccConnectionState.ERROR:
        error = GMAccountStartupError(context.account_id, status)
        if context.connection_monitor is not None:
            report_account_status(context.connection_monitor, error, context.account_id, status)
        raise error


def _fail_account_startup(context: GMStrategyRuntimeContext, error: Exception) -> None:
    """把 timer/callback 中的验证失败明确送回启动线程，不依赖 SDK 传播回调异常。"""
    context.startup_failed = True
    if context.connection_monitor is not None:
        context.connection_monitor.failed(error)
    _update_startup_phase("account_validation_failed", error=str(error))
    if context.startup_failure_handler is not None:
        context.startup_failure_handler(error)
    _emit_runtime_log("error", str(error))
    _stop_bridge_timer()
    _request_soft_runtime_stop()


def _check_account_startup(context: GMStrategyRuntimeContext) -> bool:
    """读取初始状态一次，连接/登录中的后续结果由已有账户回调推进。"""
    if context.startup_failed:
        return False
    if not context.account_id or context.ready_event.is_set():
        return True
    if not context.strategy_initialized or context.account_checked:
        return False
    context.account_checked = True
    try:
        status = GMApiBridge.get_account_status(context.account_id)
        if status is None:
            error = GMAccountNotFoundError(context.account_id)
            if context.connection_monitor is not None:
                report_runtime_failure(context.connection_monitor, error)
            raise error
        if context.account_was_ready and status.get("state") != AccConnectionState.LOGGEDIN:
            return False
        _apply_account_startup_status(context, status)
    except Exception as error:
        if context.connection_monitor is not None and getattr(error, "status", None) in {1000, 1026}:
            report_runtime_failure(context.connection_monitor, error)
        if context.connection_monitor is not None:
            report_gm_failure(
                context.connection_monitor,
                error,
                "账户状态读取失败",
                "初始交易账户状态读取过程报告异常。",
                facts={"目标账户": context.account_id, "失败阶段": "初始账户状态读取"},
                steps=("检查当前 GM 服务及目标账户的访问权限。",),
            )
        _fail_account_startup(context, error)
    return context.ready_event.is_set()


def on_tick(_context: Context, tick: TickLikeDict2) -> None:
    """
    Tick 数据推送事件.

    当订阅的标的有新的 tick 数据时触发此回调。
    """
    if _is_stopped():
        return

    try:
        context = _get_bridge_context()
        dispatcher = None if context is None else context.dispatcher
        stats = _get_bridge_stats()

        if stats:
            stats["tick_received"] += 1

        if dispatcher:
            unified_price = _convert_tick_to_unified(tick)
            dispatcher.dispatch_price_data(unified_price)

    except Exception as e:
        _emit_runtime_log("error", f"处理 tick 回调出错: {e}")
        stats = _get_bridge_stats()
        if stats:
            stats["errors"] += 1


def on_order_status(_context: Context, order: DictLikeOrder) -> None:
    """
    委托状态更新事件.

    当委托状态变化时（已报、部成、已成、撤单等）触发此回调。
    """
    if _is_stopped():
        return

    try:
        context = _get_bridge_context()
        dispatcher = None if context is None else context.dispatcher
        stats = _get_bridge_stats()

        if stats:
            stats["order_status_received"] += 1

        if dispatcher:
            unified_order = _convert_order_to_unified(order)
            dispatcher.dispatch_order_update(unified_order)

    except Exception as e:
        _emit_runtime_log("error", f"处理订单状态回调出错: {e}")
        stats = _get_bridge_stats()
        if stats:
            stats["errors"] += 1


def on_execution_report(_context: Context, execrpt: DictLikeExecRpt) -> None:
    """
    委托执行回报事件.

    当订单产生新的成交时触发此回调。
    """
    if _is_stopped():
        return

    try:
        context = _get_bridge_context()
        dispatcher = None if context is None else context.dispatcher
        stats = _get_bridge_stats()

        if stats:
            stats["execution_report_received"] += 1

        if dispatcher:
            trade_record = _convert_execrpt_to_trade(execrpt)
            dispatcher.dispatch_trade_record(trade_record)

    except Exception as e:
        _emit_runtime_log("error", f"处理成交回报出错: {e}")
        stats = _get_bridge_stats()
        if stats:
            stats["errors"] += 1


def on_account_status(_context: Context, account: DictLikeAccountStatus) -> None:
    """交易账户状态更新事件."""
    if _is_stopped():
        return
    _emit_runtime_log("info", f"账户状态更新: {account}")
    context = _get_bridge_context()
    if context is None or context.connection_monitor is None or account.get("account_id") != context.account_id:
        return
    if getattr(context, "startup_failed", False):
        return
    status = account.get("status") or {}
    if (
        getattr(context, "strategy_initialized", False)
        and not context.ready_event.is_set()
        and not context.account_was_ready
    ):
        try:
            _apply_account_startup_status(context, status)
        except Exception as error:
            _fail_account_startup(context, error)
        return
    state = status.get("state")
    if state == AccConnectionState.LOGGEDIN:
        if context.strategy_initialized:
            context.ready_event.set()
        context.connection_monitor.succeeded()
    elif state in {AccConnectionState.ERROR, AccConnectionState.DISCONNECTED}:
        if context.ready_event.is_set():
            context.stats["connection_revision"] = context.stats.get("connection_revision", 0) + 1
        context.ready_event.clear()
        report_account_status(
            context.connection_monitor, RuntimeError(f"账户连接异常: {status}"), context.account_id, status
        )


def on_error(_context: Context, code: str, info: str | bytes) -> None:
    """将 SDK 明确的鉴权错误送回启动线程，其他错误只保留日志。"""
    if _is_stopped():
        return
    _emit_runtime_log("error", f"错误: code={code}, info={info}")
    _increment_error_stat()
    try:
        status = int(code)
    except (TypeError, ValueError):
        return
    if status not in {1000, 1026}:
        return
    context = _get_bridge_context()
    if context is None or context.startup_failed:
        return
    message = info.decode("utf-8", errors="replace") if isinstance(info, bytes) else info
    error = GmError(status, message, "on_error")
    if context.connection_monitor is not None:
        report_runtime_failure(context.connection_monitor, error)
    if not context.account_was_ready:
        _fail_account_startup(context, error)
        return
    if context.ready_event.is_set():
        context.stats["connection_revision"] = context.stats.get("connection_revision", 0) + 1
    context.ready_event.clear()


def on_trade_data_connected(_context: Context) -> None:
    """交易通道网络连接成功事件."""
    if _is_stopped():
        return
    _update_startup_phase("trade_connected")
    _emit_runtime_log("info", "交易通道已连接")
    context = _get_bridge_context()
    if context is not None and context.account_was_ready:
        context.account_checked = False


def on_trade_data_disconnected(_context: Context) -> None:
    """交易通道网络连接断开事件."""
    if _is_stopped():
        return
    _emit_runtime_log("warning", "交易通道已断开")
    context = _get_bridge_context()
    if context is not None and context.connection_monitor is not None:
        if context.ready_event.is_set():
            context.stats["connection_revision"] = context.stats.get("connection_revision", 0) + 1
        context.ready_event.clear()
        report_gm_failure(
            context.connection_monitor,
            ConnectionError("交易通道已断开"),
            "交易通道断开",
            "已收到 GM 交易数据通道断开回调。",
            facts={"断开对象": "交易数据通道", "证据": "on_trade_data_disconnected"},
            facts_title="断线证据",
            steps_title="交易服务检查",
            steps=("检查 GM 交易服务和网络访问情况。", "该回调无法确认本地终端进程是否退出。"),
        )


def _process_bridge_requests(_context: Context) -> None:
    """在 GM run() 上下文中执行 bridge 请求."""
    if _is_stopped():
        _stop_bridge_timer()
        _request_soft_runtime_stop()
        return

    context = _get_bridge_context()
    if context is None:
        return
    if not _check_account_startup(context):
        return
    request_queue = context.request_queue

    while True:
        try:
            request = request_queue.get_nowait()
        except queue.Empty:
            return

        # 原子地把 Future 从 PENDING 推进到 RUNNING：已被调用方取消时返回 False
        # （取代旧的 `cancelled()` 守卫），成功后调用方的 cancel() 不再生效，
        # 因此下面的 set_result / set_exception 不会与超时取消撞成 InvalidStateError。
        if not request.future.set_running_or_notify_cancel():
            continue

        # deadline 二次校验：调用方超时会 cancel Future，但「已 get_nowait 取出、
        # 尚未 dispatch」这段窗口取消标志覆盖不到。过期请求一律丢弃，绝不下发到
        # SDK——延迟执行一笔已被上层判定失败、且可能已经重试过的下单/撤单，
        # 风险远大于少执行一笔。
        if request.is_expired():
            request.future.set_exception(
                TimeoutError(f"GM bridge 请求已过期，跳过执行: operation={request.request.operation}")
            )
            continue

        try:
            result = _dispatch_bridge_request(request.request)
        except Exception as exc:
            request.future.set_exception(exc)
        else:
            request.future.set_result(result)


def _subscribe_runtime_symbols(symbols: list[str]) -> list[str]:
    """在当前 GM runtime 中补充订阅新的 tick 标的."""
    requested_symbols = list(
        dict.fromkeys(str(symbol or "").strip() for symbol in symbols if str(symbol or "").strip())
    )
    if not requested_symbols:
        return []

    context = _get_bridge_context()
    if context is None:
        return []

    existing_symbols = [str(symbol or "").strip() for symbol in context.subscribe_symbols if str(symbol or "").strip()]
    existing_symbol_set = set(existing_symbols)
    new_symbols = [symbol for symbol in requested_symbols if symbol not in existing_symbol_set]
    if not new_symbols:
        return []

    for symbol in new_symbols:
        subscribe(symbols=symbol, frequency="tick")  # noqa: F405
        _emit_runtime_log("info", f"动态订阅行情: {symbol}")

    context.subscribe_symbols[:] = existing_symbols + new_symbols
    return new_symbols


def _dispatch_bridge_request(request: GMBridgeRequestPayload):
    """分发 bridge 请求到对应的 GM SDK 调用."""
    if isinstance(request, GMSubscribeSymbolsRequest):
        return _subscribe_runtime_symbols(request.symbols)
    return GMApiBridge.execute_request(request)


def _stop_bridge_timer() -> None:
    """停止 bridge 请求定时器."""
    context = _get_bridge_context()
    timer_id = 0 if context is None else context.timer_id
    if timer_id:
        timer_stop(timer_id)  # noqa: F405
        if context is not None:
            context.timer_id = 0


def _request_soft_runtime_stop() -> None:
    """软停止 GM runtime 主循环，避免 gm.api.stop() 的 sys.exit(2) 语义."""
    try:
        from gm.api import basic as gm_basic  # type: ignore
    except Exception:
        return

    context = _get_bridge_context()
    stop_requested = None if context is None else context.runtime_stop_requested
    stop_lock = None if context is None else context.runtime_stop_lock

    unsubscribe_all = getattr(gm_basic, "_py_gmi_unsubscribe_all", None)
    if stop_requested is not None and stop_lock is not None:
        with stop_lock:
            first_request = not bool(stop_requested.is_set())
            stop_requested.set()
            if first_request and callable(unsubscribe_all):
                unsubscribe_all()
    elif callable(unsubscribe_all):
        unsubscribe_all()

    setattr(gm_basic, "running", False)


def _is_stopped() -> bool:
    """检查策略是否已停止."""
    context = _get_bridge_context()
    stop_event = None if context is None else context.stop_event
    return stop_event is not None and stop_event.is_set()


if __name__ == "__main__":
    # 此文件由 GMStrategyBridge 调用
    pass
