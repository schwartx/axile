"""账户记录的字段合并与可验证成交均价。"""

from copy import deepcopy
from math import isclose, isfinite

from axile.executor.models.unified_order import TradeRecord, UnifiedOrder


def merge_trade(previous: TradeRecord | None, incoming: TradeRecord) -> TradeRecord:
    """同一笔成交合并查询和回报信息，缺失值不覆盖已知字段。"""
    result = incoming.model_copy(deep=True)
    if previous is None:
        return result
    extra = deepcopy(previous.extra)
    for key, value in result.extra.items():
        previous_value = extra.get(key)
        if isinstance(value, dict) and isinstance(previous_value, dict):
            extra[key] = {**previous_value, **{k: v for k, v in value.items() if v is not None and v != ""}}
        elif value is not None and value != "":
            extra[key] = value
    result.extra = extra
    if not result.order_id:
        result.order_id = previous.order_id
    if not result.trade_time:
        result.trade_time = previous.trade_time
    return result


def merge_order(previous: UnifiedOrder | None, incoming: UnifiedOrder) -> UnifiedOrder:
    """保留未携带的扩展信息；均价只对应同一累计成交量。"""
    result = incoming.model_copy(deep=True)
    if previous is None:
        return result
    extra = deepcopy(previous.extra)
    if incoming.filled_volume != previous.filled_volume:
        extra.pop("avg_price_source", None)
        for key in ("filled_amount", "cum_quote"):
            extra.pop(key, None)
            raw = extra.get("raw_order_data")
            if isinstance(raw, dict):
                raw.pop(key, None)
    for key, value in result.extra.items():
        previous_value = extra.get(key)
        if isinstance(value, dict) and isinstance(previous_value, dict):
            extra[key] = {**previous_value, **{k: v for k, v in value.items() if v is not None}}
        elif value is not None and value != "":
            extra[key] = value
    result.extra = extra
    if result.avg_price > 0 and incoming.extra.get("avg_price_source") != "trades":
        result.extra.pop("avg_price_source", None)
    if result.filled_volume == previous.filled_volume and result.avg_price == 0:
        result.avg_price = previous.avg_price
    return result


def with_trade_average(order: UnifiedOrder, trades: list[TradeRecord]) -> UnifiedOrder:
    """仅用完整且唯一关联的逐笔成交补齐缺失均价，不修改原订单。"""
    result = order.model_copy(deep=True)
    if result.extra.get("avg_price_source") == "trades":
        result.avg_price = 0
        result.extra.pop("avg_price_source", None)
    if result.filled_volume <= 0 or result.avg_price > 0 and isfinite(result.avg_price):
        return result
    rows = [
        trade
        for trade in trades
        if trade.symbol == order.symbol
        and trade.order_id == order.order_id
        and trade.extra.get("trading_day") == order.extra.get("trading_day")
        and trade.extra.get("exchange_id") == order.extra.get("exchange_id")
    ]
    if not rows or any(not isfinite(t.trade_price) or t.trade_price <= 0 for t in rows):
        return result
    volume = sum(t.trade_volume for t in rows)
    if not isclose(volume, order.filled_volume, rel_tol=1e-9, abs_tol=1e-9):
        return result
    result.avg_price = sum(t.trade_price * t.trade_volume for t in rows) / volume
    result.extra["avg_price_source"] = "trades"
    return result
