"""从显式业务事实重建通知展示，保留已提供的错误与原生时间。"""

from copy import deepcopy
from typing import Any, cast

from axile.executor.feishu_notifications import FeishuNotificationSource, build_execution_notification_context
from axile.executor.models.unified_output import UnifiedStandardOutput
from axile.server.execution.legacy_compat import normalize_legacy_result


def _fill_missing_result_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """补齐历史展示字段；显式业务事实不被归一化文案覆盖。"""
    normalized = cast("dict[str, Any]", normalize_legacy_result(payload))
    for key, value in payload.items():
        if key == "symbol_results" and isinstance(value, dict):
            normalized[key] = {
                symbol: _fill_missing_result_fields(item) if isinstance(item, dict) else item
                for symbol, item in value.items()
            }
        else:
            normalized[key] = value
    return normalized


def _preserve_order_update_times(context: dict[str, Any], payload: dict[str, Any]) -> None:
    """通知使用持久化更新时间，避免统一模型用当前时间重建计算属性。"""
    symbols = payload.get("symbol_results")
    if not isinstance(symbols, dict):
        return
    source_orders = [
        order
        for result in symbols.values()
        if isinstance(result, dict)
        for order in result.get("orders", [])
        if isinstance(order, dict)
    ]
    for source, target in zip(source_orders, context["orders"], strict=True):
        if "update_time" in source:
            target["update_time"] = deepcopy(source["update_time"])
        else:
            target.pop("update_time", None)


def build_persisted_notification_context(
    source: FeishuNotificationSource,
    result: dict[str, Any],
    persisted_input: object,
    account_config: object,
) -> dict[str, Any]:
    """复用唯一事实读出的结果，不连接渠道或将展示补全写回业务事实。"""
    payload = _fill_missing_result_fields(deepcopy(result))
    if "inputs" not in payload:
        payload["inputs"] = deepcopy(persisted_input)
    inputs = payload.get("inputs")
    if isinstance(inputs, dict):
        payload["inputs"] = {**inputs, "account_config": account_config}
    output = UnifiedStandardOutput.model_validate(payload)
    context = build_execution_notification_context(source, output)
    _preserve_order_update_times(context, payload)
    return context
