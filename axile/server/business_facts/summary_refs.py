"""冻结摘要指标与业务事实引用；不保存逐字段 JSON 投影、顺序或哈希。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from math import isfinite
from typing import Any

REFERENCE_KEY = "business_facts_ref"


class BusinessFactReferenceError(ValueError):
    """审计引用缺失、越权或与冻结事实不一致。"""


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _converted(value: float | None, conversion: object) -> float | None:
    if value is None or not isinstance(conversion, dict):
        return value
    rate = _number(conversion.get("rate"))
    return value * rate if rate is not None and isfinite(rate) and rate > 0 else None


def _trade(trade: dict[str, Any]) -> dict[str, object]:
    extra = trade.get("extra")
    extra = extra if isinstance(extra, dict) else {}
    conversion = trade.get("value_conversion")
    conversion = conversion if isinstance(conversion, dict) else {}
    asset = extra.get("commission_asset")
    native_value = _number(trade.get("trade_value"))
    return {
        "price": _number(trade.get("trade_price")),
        "volume": _number(trade.get("trade_volume")),
        "value": _converted(native_value, trade.get("value_conversion")),
        "native_value": native_value,
        "price_currency": conversion.get("price_currency"),
        "value_currency": conversion.get("value_currency"),
        "time": trade.get("trade_time"),
        "fee": _number(extra.get("commission")),
        "fee_asset": asset if isinstance(asset, str) and asset else None,
    }


def _order(order: dict[str, Any], trades: dict[str, list[dict[str, object]]]) -> dict[str, object]:
    order_id = str(order.get("order_id") or "")
    direction = str(order.get("direction") or "").upper()
    side = (
        "sell"
        if "SELL" in direction or "卖" in direction
        else "buy"
        if "BUY" in direction or "买" in direction
        else "none"
    )
    extra = order.get("extra")
    extra = extra if isinstance(extra, dict) else {}
    client = extra.get("client_order_id")
    order_type = str(order.get("order_type") or "")
    return {
        "order_id": order_id,
        "side": side,
        "order_type": order_type.rsplit(".", 1)[-1] if "." in order_type else order_type,
        "price": _number(order.get("price")),
        "avg_price": _number(order.get("avg_price")),
        "value_conversion": order.get("value_conversion"),
        "volume": _number(order.get("volume")),
        "filled_volume": _number(order.get("filled_volume")),
        "status": str(order.get("status") or ""),
        "client_order_id": client if isinstance(client, str) else None,
        "trades": trades.get(order_id, []),
    }


def _orders_tree(symbol: object) -> list[dict[str, object]]:
    """为审计详情生成订单与成交视图；不作为第二份存储事实。"""
    if not isinstance(symbol, Mapping):
        raise BusinessFactReferenceError("摘要订单引用的品种结果不是对象")
    raw_orders = symbol.get("orders")
    raw_trades = symbol.get("trades")
    trades: dict[str, list[dict[str, object]]] = {}
    for trade in raw_trades if isinstance(raw_trades, list) else []:
        if isinstance(trade, dict):
            trades.setdefault(str(trade.get("order_id") or ""), []).append(_trade(trade))
    return (
        [_order(order, trades) for order in raw_orders if isinstance(order, dict)]
        if isinstance(raw_orders, list)
        else []
    )


def validate_summary_reference(reference: Mapping[str, object]) -> None:
    """只校验不可变执行身份与业务视图版本。"""
    record_id = reference.get("record_id")
    if not isinstance(record_id, int) or isinstance(record_id, bool) or record_id < 1:
        raise BusinessFactReferenceError("摘要引用缺少终态记录身份")
    if type(reference.get("projection_version")) is not int or reference["projection_version"] != 2:
        raise BusinessFactReferenceError("摘要业务视图版本无效")


def compact_summary_content(content: object, result: object, *, record_id: int) -> dict[str, object] | None:
    """摘要保留独有指标和差异证据，相同明细引用已冻结业务事实。"""
    if not isinstance(content, Mapping) or not isinstance(result, Mapping) or REFERENCE_KEY in content:
        return None
    symbols = result.get("symbol_results")
    observed = content.get("symbol_results")
    if not isinstance(symbols, Mapping) or not isinstance(observed, Mapping):
        return None
    compact = dict(content)
    heavy = {"orders", "trades", "sizing", "first_tick", "account_assets", "memory", "technical_detail"}
    kept = {}
    for symbol, row in observed.items():
        fact = symbols.get(symbol)
        kept[symbol] = (
            {
                key: value
                for key, value in row.items()
                if key not in heavy or not isinstance(fact, Mapping) or key not in fact or fact[key] != value
            }
            if isinstance(row, Mapping)
            else row
        )
    compact["symbol_results"] = kept
    compact[REFERENCE_KEY] = {"version": 1, "view": "result", "record_id": record_id, "projection_version": 2}
    return compact


def hydrate_summary_content(
    content: Mapping[str, object], result: object, *, include_reference: bool = True
) -> dict[str, object]:
    """按冻结执行读取业务详情；摘要独有指标及差异证据优先。"""
    reference = content.get(REFERENCE_KEY)
    if not isinstance(reference, Mapping) or reference.get("view") != "result":
        raise BusinessFactReferenceError("执行摘要缺少结果引用")
    validate_summary_reference(reference)
    symbols = result.get("symbol_results") if isinstance(result, Mapping) else None
    if not isinstance(symbols, Mapping):
        raise BusinessFactReferenceError("摘要缺少冻结品种事实")
    hydrated = deepcopy(dict(content))
    kept = hydrated.get("symbol_results")
    if isinstance(kept, Mapping):
        merged = {}
        for symbol, evidence in kept.items():
            if symbol not in symbols:
                raise BusinessFactReferenceError("摘要品种事实缺失")
            fact = symbols[symbol]
            merged[symbol] = (
                {**fact, **evidence} if isinstance(fact, Mapping) and isinstance(evidence, Mapping) else evidence
            )
        hydrated["symbol_results"] = merged
    if not include_reference:
        hydrated.pop(REFERENCE_KEY, None)
    return hydrated
