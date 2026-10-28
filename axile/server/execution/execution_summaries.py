"""执行视图的原生调用入口；摘要和对账只在读取时组装。"""

import json
from collections.abc import Mapping
from typing import Any

from axile.server._native import _core


def execution_result_view(
    result: Mapping[str, object], before_account_assets: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """使用 Rust 的同一口径派生详情与证据导出。"""
    return _core.execution_result_view(
        json.dumps({"result": result, "before_account_assets": before_account_assets}, ensure_ascii=False)
    )


def build_symbol_reconciliation(
    result: Mapping[str, object], before_account_assets: Mapping[str, Any] | None
) -> dict[str, Any]:
    """从原生执行视图提取逐品种对账。"""
    return execution_result_view(result, before_account_assets)["reconciliation"]


def count_orders_from_symbol_results(result: dict[str, object]) -> int:
    """统计 worker 完成事件中的订单笔数。"""
    symbols = result.get("symbol_results")
    if not isinstance(symbols, dict):
        return 0
    return sum(
        len(value["orders"])
        for value in symbols.values()
        if isinstance(value, dict) and isinstance(value.get("orders"), list)
    )
