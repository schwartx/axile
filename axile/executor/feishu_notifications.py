"""执行器飞书通知辅助函数."""

from __future__ import annotations

import queue
import threading
from typing import Protocol, TypedDict

import loguru
from pydantic_core import to_jsonable_python

from axile.common.default_account_notification import DEFAULT_ACCOUNT_NOTIFICATION_CODE
from axile.common.notification_context import AccountNotificationExecution
from axile.common.notification_function import NotificationFunctionResult, run_notification_function
from axile.executor.algorithms.utils import clock_now
from axile.executor.models.unified_account_assets import Position, UnifiedAccountAssets, is_degraded_snapshot_source
from axile.executor.models.unified_order import TradeRecord, UnifiedOrder
from axile.executor.models.unified_output import UnifiedStandardOutput

type ObjectDict = dict[str, object]


class FormattedFeishuPosition(TypedDict):
    """飞书持仓卡片使用的持仓信息."""

    symbol: str
    direction: str
    market_value: float | None
    volume: float
    avg_price: float


class LoggerLike(Protocol):
    """通知辅助函数使用的最小日志接口."""

    def info(self, message: object, *args: object, **kwargs: object) -> None:
        """记录普通通知日志."""

    def error(self, message: object, *args: object, **kwargs: object) -> None:
        """记录错误通知日志."""


class FeishuNotificationSource(Protocol):
    """飞书通知需要的最小执行器视图."""

    logger: LoggerLike

    def _get_account_mark(self) -> str: ...

    def _get_operation_display(self, order: UnifiedOrder) -> str: ...


def format_position_for_feishu(position: Position) -> FormattedFeishuPosition:
    """格式化持仓数据为飞书通知格式."""
    return {
        "symbol": position.symbol,
        "direction": position.direction.value if hasattr(position.direction, "value") else str(position.direction),
        "market_value": float(position.market_value) if position.market_value is not None else None,
        "volume": float(position.volume),
        "avg_price": float(position.avg_price) if position.avg_price is not None else 0,
    }


def _build_order_lookup(orders: list[UnifiedOrder]) -> dict[tuple[str, str], UnifiedOrder]:
    """按 (symbol, order_id) 建立订单查找表."""
    lookup: dict[tuple[str, str], UnifiedOrder] = {}
    for order in orders:
        lookup[(order.symbol, order.order_id)] = order
    return lookup


def format_trade_for_feishu(
    source: FeishuNotificationSource,
    trade: TradeRecord,
    order_lookup: dict[tuple[str, str], UnifiedOrder],
) -> dict[str, str]:
    """格式化单笔成交为飞书通知格式."""
    order = order_lookup.get((trade.symbol, trade.order_id))
    operate = source._get_operation_display(order) if order is not None else "-"
    return {
        "symbol": trade.symbol,
        "dt": trade.trade_time,
        "operate": operate,
        "volume": f"{trade.trade_volume:.4f}",
        "price": f"{trade.trade_price:.4f}",
        "order_id": trade.order_id,
        "trade_id": trade.trade_id,
    }


def _redact_sensitive(value: object) -> object:
    """递归移除普通策略配置中疑似凭据的字段."""
    if isinstance(value, dict):
        return {
            key: _redact_sensitive(item)
            for key, item in value.items()
            if not any(marker in key.lower() for marker in ("key", "secret", "token", "password", "credential"))
        }
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


def _select_notification_assets(
    source: FeishuNotificationSource,
    output: UnifiedStandardOutput,
    notification_snapshot: dict[str, object] | None,
) -> UnifiedAccountAssets | None:
    """优先使用本次可信资产，否则使用与账户页面同源的备用快照。"""
    current = output.account_assets
    if not is_degraded_snapshot_source(current.source):
        return current
    if notification_snapshot is None:
        return None
    try:
        assets = UnifiedAccountAssets.model_validate(notification_snapshot["assets"])
    except Exception as exc:
        source.logger.error(f"飞书通知备用资产快照无效: {exc}")
        return None
    if is_degraded_snapshot_source(assets.source):
        return None
    source.logger.info(
        f"飞书通知使用备用资产快照: id={notification_snapshot.get('id')}, "
        f"created_at={notification_snapshot.get('created_at')}"
    )
    return assets


def _legacy_template_variables(
    source: FeishuNotificationSource,
    output: UnifiedStandardOutput,
    account_assets: UnifiedAccountAssets | None,
) -> dict[str, object]:
    """构造与既有飞书模板完全兼容的变量集合."""
    account_mark = source._get_account_mark()
    total_assets = float(account_assets.total_asset) if account_assets is not None else None
    target_volume = output.target_volume
    positions: list[dict[str, str]] = []
    for position in account_assets.positions if account_assets is not None else []:
        formatted_pos = format_position_for_feishu(position)
        value = formatted_pos["market_value"]
        if value is not None and value <= 0:
            continue
        positions.append(
            {
                "symbol": formatted_pos["symbol"],
                "direction": formatted_pos["direction"],
                "market_value": f"{value:.2f}" if value is not None else "暂无有效行情",
                "volume": f"{formatted_pos['volume']:.4f}",
                "target_volume": f"{target_volume.get(formatted_pos['symbol'], 0):.4f}",
                "rate": f"{value / total_assets:.2%}"
                if value is not None and total_assets is not None and total_assets > 0
                else "—",
            }
        )
    order_lookup = _build_order_lookup(output.orders)
    return {
        "account_mark": account_mark,
        "dt": clock_now().strftime("%Y-%m-%d %H:%M:%S"),
        "algorithm": str(output.inputs.algorithm.get("method", "Unknown")) if output.inputs else "Unknown",
        "total_assets": f"{total_assets:.2f}" if total_assets is not None else "未获取",
        "available_cash": f"{float(account_assets.available_cash):.2f}" if account_assets is not None else "未获取",
        "market_value": (
            f"{account_assets.market_value:.2f}"
            if account_assets is not None and account_assets.market_value is not None
            else ("暂无有效行情" if account_assets is not None else "未获取")
        ),
        "positions": positions
        if account_assets is not None
        else [
            {
                "symbol": "未获取",
                "direction": "—",
                "market_value": "未获取",
                "volume": "—",
                "target_volume": "—",
                "rate": "—",
            }
        ],
        "trades": [format_trade_for_feishu(source, trade, order_lookup) for trade in output.trades],
    }


def _structured_template_variables(
    source: FeishuNotificationSource,
    output: UnifiedStandardOutput,
    legacy: dict[str, object],
    assets: UnifiedAccountAssets | None,
) -> dict[str, object]:
    """构造跨渠道且不含连接信息的执行通知变量."""
    inputs = output.inputs
    audit_value = inputs.extra.get("audit") if inputs else None
    audit: dict[str, object] = audit_value if isinstance(audit_value, dict) else {}
    total_asset = float(assets.total_asset) if assets is not None else None
    orders = [order.model_dump(mode="json", exclude={"extra", "update_timestamp"}) for order in output.orders]
    trades = [trade.model_dump(mode="json", exclude={"extra"}) for trade in output.trades]
    positions = [
        {
            **position.model_dump(mode="json", exclude={"extra"}),
            **(
                {"valuation_reference": True, "valuation_source": position.extra["valuation_source"]}
                if position.extra.get("valuation_source") == "ctp_previous_asset_snapshot"
                else {}
            ),
            "target_volume": output.target_volume.get(position.symbol, 0),
            "target_weight": inputs.curr_target.get(position.symbol) if inputs else None,
            "rate": (
                float(position.market_value) / total_asset
                if position.market_value is not None and total_asset is not None and total_asset > 0
                else None
            ),
        }
        for position in (assets.positions if assets is not None else [])
    ]
    symbols: list[dict[str, object]] = []
    for symbol, result in output.symbol_results.items():
        symbols.append(
            {
                "symbol": symbol,
                "algorithm": result.algorithm,
                "status": str(result.status),
                "success": result.success,
                "error": result.error,
                "outcome": str(result.outcome),
                "outcome_reason": result.outcome_reason,
                "reason_code": result.reason_code,
                "target_volume": result.target_volume,
                "sizing": result.sizing.model_dump(mode="json") if result.sizing else None,
                "first_tick": result.first_tick.model_dump(mode="json", exclude={"extra"})
                if result.first_tick
                else None,
                "order_count": len(result.orders),
                "trade_count": len(result.trades),
            }
        )
    account = dict(inputs.feishu_account) if inputs else {}
    account["mark"] = source._get_account_mark()
    status = str(output.status)
    execution_id = audit.get("execution_id")
    kind = audit.get("execution_kind")
    trigger_source = audit.get("trigger_source")
    execution: AccountNotificationExecution = {
        "id": execution_id if isinstance(execution_id, str) else None,
        "kind": kind if isinstance(kind, str) else None,
        "trigger_source": trigger_source if isinstance(trigger_source, str) else None,
        "notified_at": str(legacy["dt"]),
        "execution_time": output.execution_time,
        "status": status,
        "success": output.success,
        "error": output.error,
        "outcome": str(output.outcome),
        "outcome_reason": output.outcome_reason,
        "reason_code": output.reason_code,
        "channel_type": str(output.channel_type),
        "is_test": bool(audit.get("is_test", False)),
    }
    structured = {
        "schema_version": 1,
        "account": account,
        "execution": execution,
        "strategy": {
            "algorithm": _redact_sensitive(to_jsonable_python(inputs.algorithm)) if inputs else {},
            "symbol_algorithms": _redact_sensitive(to_jsonable_python(inputs.symbol_algorithms)) if inputs else {},
            "trade_rules": _redact_sensitive(to_jsonable_python(inputs.trade_rules)) if inputs else {},
            "forbidden_symbols": list(inputs.forbidden_symbols) if inputs else [],
            "risk_symbols": list(inputs.risk_symbols) if inputs else [],
        },
        "assets": {
            "total_assets": total_asset,
            "available_cash": float(assets.available_cash) if assets is not None else None,
            "market_value": float(assets.market_value)
            if assets is not None and assets.market_value is not None
            else None,
            "currency": assets.currency if assets is not None else None,
            "update_time": assets.update_time if assets is not None else None,
            "source": assets.source if assets is not None else "unavailable",
            "cash_rate": float(assets.available_cash) / total_asset
            if assets is not None and total_asset is not None and total_asset > 0
            else None,
            "position_rate": (
                float(assets.market_value) / total_asset
                if assets is not None
                and assets.market_value is not None
                and total_asset is not None
                and total_asset > 0
                else None
            ),
        },
        "targets": {
            "current": dict(inputs.curr_target) if inputs else {},
            "previous": dict(inputs.last_target) if inputs else {},
            "target_volume": output.target_volume,
        },
        "positions": positions if assets is not None else None,
        "orders": orders,
        "trades": trades,
        "symbols": symbols,
        "summary": {
            "symbol_count": len(output.symbol_results),
            "position_count": len(positions) if assets is not None else None,
            "order_count": len(orders),
            "filled_order_count": len(output.get_filled_orders()),
            "active_order_count": len(output.get_active_orders()),
            "trade_count": len(trades),
            "trade_value": sum(float(trade.trade_value) for trade in output.trades),
            "succeeded_symbol_count": sum(1 for result in output.symbol_results.values() if result.success),
            "failed_symbol_count": sum(1 for result in output.symbol_results.values() if not result.success),
        },
    }
    return {**legacy, **structured}


def build_execute_results_feishu_card(
    source: FeishuNotificationSource,
    output: UnifiedStandardOutput,
    *,
    template_id: str = "AAqRUQhyOM90g",
    notification_snapshot: dict[str, object] | None = None,
) -> ObjectDict:
    """按账户配置构造可直接发送的执行结果卡片."""
    assets = _select_notification_assets(source, output, notification_snapshot)
    return {
        "type": "template",
        "data": {
            "template_id": template_id,
            "template_variable": _legacy_template_variables(source, output, assets),
        },
    }


def build_execution_notification_context(
    source: FeishuNotificationSource,
    output: UnifiedStandardOutput,
    notification_snapshot: dict[str, object] | None = None,
    *,
    is_test: bool = False,
) -> dict[str, object]:
    """为渠道无关的通知函数构造脱敏执行快照。"""
    assets = _select_notification_assets(source, output, notification_snapshot)
    legacy = _legacy_template_variables(source, output, assets)
    context = _structured_template_variables(source, output, legacy, assets)
    context.pop("schema_version", None)
    for key in legacy:
        if key not in {"positions", "trades"}:
            context.pop(key, None)
    context["default_feishu_variables"] = legacy
    context["event_type"] = "execution.finished"
    context["is_test"] = is_test
    execution = context.get("execution")
    if isinstance(execution, dict):
        execution["is_test"] = is_test
        context["event_id"] = f"execution:{execution.get('id')}" if execution.get("id") else None
    return to_jsonable_python(context)


def _record_notification_result(
    source: FeishuNotificationSource, output: UnifiedStandardOutput, ok: bool, error: str | None
) -> None:
    """记录后台通知结果，存储失败不影响执行器。"""
    callback = getattr(source, "_notification_result_callback", None)
    if not callable(callback):
        return
    audit = output.inputs.extra.get("audit", {}) if output.inputs else {}
    execution_id = audit.get("execution_id") if isinstance(audit, dict) else None
    try:
        callback(execution_id, clock_now().isoformat(), ok, error)
    except Exception:  # noqa: BLE001 - 状态落库不影响通知结果
        loguru.logger.exception("保存通知结果失败 execution_id={}", execution_id)


def dispatch_execution_notification(
    source: FeishuNotificationSource,
    output: UnifiedStandardOutput,
    feishu_key: str | None,
    code: str | None,
    notification_snapshot: dict[str, object] | None,
) -> None:
    """执行账户唯一的通知函数。"""
    if not code:
        return
    audit = output.inputs.extra.get("audit", {}) if output.inputs else {}
    execution_id = audit.get("execution_id") if isinstance(audit, dict) else None
    context = build_execution_notification_context(source, output, notification_snapshot)
    result = (
        NotificationFunctionResult(False, "未配置飞书 Webhook，无法发送通知")
        if code.strip() == DEFAULT_ACCOUNT_NOTIFICATION_CODE.strip() and not feishu_key
        else run_notification_function(code, context, feishu_key=feishu_key)
    )
    if not result.ok:
        source.logger.error(f"执行通知失败: {result.error}")
    loguru.logger.info("执行通知完成 execution_id={} ok={} error={}", execution_id, result.ok, result.error)
    _record_notification_result(source, output, result.ok, result.error)
    if result.ok:
        callback = getattr(source, "_notification_success_callback", None)
        if callable(callback):
            try:
                callback(execution_id, clock_now().isoformat())
            except Exception:  # noqa: BLE001 - 状态落库失败不得改变通知或交易结果
                loguru.logger.exception("保存通知成功状态失败 execution_id={}", execution_id)


# ---- 有界后台通知派发器 ----
#
# 飞书通知是执行尾部的尽力而为副作用。HTTP 请求虽有超时，仍使用固定 worker 与
# 有界队列隔离网络延迟；队列满时丢弃新通知，保证主执行链路不阻塞且资源有界。

_FEISHU_NOTIFY_WORKER_COUNT = 2
"""后台飞书通知 worker 线程数。"""

_FEISHU_NOTIFY_QUEUE_MAXSIZE = 64
"""待发通知队列容量；超出后丢弃最新通知，避免无界堆积。"""

_FeishuNotifyTask = tuple[
    "FeishuNotificationSource", UnifiedStandardOutput, str | None, str | None, dict[str, object] | None
]

_notify_queue: queue.Queue[_FeishuNotifyTask] = queue.Queue(maxsize=_FEISHU_NOTIFY_QUEUE_MAXSIZE)
_notify_workers_started = False
_notify_workers_lock = threading.Lock()


def _feishu_notify_worker_loop() -> None:
    """后台 worker 主循环：串行消费队列并发送飞书通知，异常不退出。"""
    while True:
        source, output, feishu_key, code, notification_snapshot = _notify_queue.get()
        try:
            dispatch_execution_notification(source, output, feishu_key, code, notification_snapshot)
        except Exception as exc:  # noqa: BLE001 - 通知任务异常不得拖垮 worker
            loguru.logger.error(f"执行通知任务异常: {exc}")
            _record_notification_result(source, output, False, f"执行通知任务异常: {exc}")
        finally:
            _notify_queue.task_done()


def _ensure_feishu_notify_workers_started() -> None:
    """惰性启动后台通知 worker 线程（幂等、线程安全）。"""
    global _notify_workers_started
    if _notify_workers_started:
        return
    with _notify_workers_lock:
        if _notify_workers_started:
            return
        for index in range(_FEISHU_NOTIFY_WORKER_COUNT):
            threading.Thread(
                target=_feishu_notify_worker_loop,
                name=f"feishu-notify-{index}",
                daemon=True,
            ).start()
        _notify_workers_started = True


def enqueue_execute_results_to_feishu(
    source: FeishuNotificationSource,
    output: UnifiedStandardOutput,
    feishu_key: str | None = None,
    code: str | None = None,
    notification_snapshot: dict[str, object] | None = None,
) -> None:
    """
    将执行结果飞书通知投递到有界后台队列.

    Parameters
    ----------
    source : FeishuNotificationSource
        提供日志与账户视图的执行器。
    output : UnifiedStandardOutput
        本次执行的统一输出对象。
    feishu_key : str | None, default=None
        飞书群机器人 key；为空时直接跳过。

    Returns
    -------
    None
        该函数仅投递任务，不返回结果。

    Notes
    -----
    尽力而为语义：由固定数量的后台 worker 串行发送。队列满时**丢弃本次通知并记
    warning**，用「丢通知」换取「主执行链路不阻塞、线程与内存有界」。绝不为单次
    执行新建线程。
    """
    if not code:
        return
    _ensure_feishu_notify_workers_started()
    try:
        _notify_queue.put_nowait((source, output, feishu_key, code, notification_snapshot))
    except queue.Full:
        _record_notification_result(source, output, False, "通知队列已满，本次通知未发送")
        loguru.logger.warning(f"执行通知队列已满（maxsize={_FEISHU_NOTIFY_QUEUE_MAXSIZE}），丢弃本次通知以避免无界堆积")
