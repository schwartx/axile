"""执行器飞书通知辅助函数."""

from __future__ import annotations

import json
import queue
import threading
from typing import Protocol

import loguru

from axile.common.default_account_notification import DEFAULT_ACCOUNT_NOTIFICATION_CODE
from axile.common.notification_function import NotificationFunctionResult, run_notification_function
from axile.executor.algorithms.utils import clock_now
from axile.executor.models.unified_account_assets import UnifiedAccountAssets, is_degraded_snapshot_source
from axile.executor.models.unified_order import UnifiedOrder
from axile.executor.models.unified_output import UnifiedStandardOutput

type ObjectDict = dict[str, object]


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


def build_execute_results_feishu_card(
    source: FeishuNotificationSource,
    output: UnifiedStandardOutput,
    *,
    template_id: str = "AAqRUQhyOM90g",
    notification_snapshot: dict[str, object] | None = None,
) -> ObjectDict:
    """按账户配置构造可直接发送的执行结果卡片."""
    context = build_execution_notification_context(source, output, notification_snapshot)
    return {
        "type": "template",
        "data": {
            "template_id": template_id,
            "template_variable": context["default_feishu_variables"],
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
    from axile.server._native import _core

    assets = _select_notification_assets(source, output, notification_snapshot)
    result = output.model_dump(
        mode="json",
        exclude={
            "memory": True,
            "extra": True,
            "account_assets": True,
            "inputs": {"account_config", "extra", "feishu_key", "execution_notification_code"},
            "symbol_results": {
                "__all__": {
                    "memory": True,
                    "first_tick": {"extra"},
                    "orders": {"__all__": {"extra"}},
                    "trades": {"__all__": {"extra"}},
                }
            },
        },
    )
    result["account_assets"] = (assets if assets is not None else output.account_assets).model_dump(
        mode="json", exclude={"extra": True, "positions": {"__all__": {"extra"}}}
    )
    if output.inputs is not None:
        audit = output.inputs.extra.get("audit")
        audit = audit if isinstance(audit, dict) else {}
        result["inputs"]["extra"] = {
            "audit": {
                key: value
                for key in ("execution_id", "execution_kind", "trigger_source")
                if isinstance(value := audit.get(key), str)
            }
        }
    lookup = {(order.symbol, order.order_id): order for order in output.orders}
    operations = [
        source._get_operation_display(order)
        if (order := lookup.get((trade.symbol, trade.order_id))) is not None
        else "-"
        for trade in output.trades
    ]
    return _core.execution_notification_context(
        json.dumps(
            {
                "result": result,
                "now": clock_now().isoformat(),
                "account_mark": source._get_account_mark(),
                "trade_operations": operations,
                "is_test": is_test,
            },
            ensure_ascii=False,
        )
    )


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
