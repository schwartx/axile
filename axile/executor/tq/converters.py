"""TqSdk 快照到 Axile 统一模型的纯转换函数."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from axile.common.trade_channel import TradeChannel
from axile.executor.algorithms.utils.clock import clock_now
from axile.executor.constants.order_status import OrderStatus
from axile.executor.instrument_catalog_records import EXCHANGES, normalize_catalog_symbol
from axile.executor.models.unified_account_assets import Position, PositionDirection, UnifiedAccountAssets
from axile.executor.models.unified_order import OrderDirection, OrderType, TradeRecord, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData
from axile.executor.tq.symbols import TQSymbolResolver


def _value(row: object, name: str, default: Any = "") -> Any:
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def _float(row: object, name: str) -> float:
    try:
        value = float(_value(row, name, 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def _iso_from_nano(value: object) -> str:
    try:
        return datetime.fromtimestamp(int(value) / 1_000_000_000).isoformat()
    except (TypeError, ValueError, OSError):
        return datetime.now().isoformat()


def _quote_time(value: object) -> tuple[str, int]:
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
            return parsed.isoformat(), int(parsed.timestamp() * 1000)
        except ValueError:
            pass
    update_time = _iso_from_nano(value)
    try:
        timestamp = int(int(value) / 1_000_000)
    except (TypeError, ValueError):
        timestamp = int(datetime.fromisoformat(update_time).timestamp() * 1000)
    return update_time, timestamp


def quote_to_unified(row: object, resolver: TQSymbolResolver) -> UnifiedPriceData:
    """转换天勤行情快照."""
    local = str(_value(row, "instrument_id", "") or _value(row, "symbol", ""))
    exchange = str(_value(row, "exchange_id", "") or "")
    full_symbol = f"{exchange}.{local}" if exchange and "." not in local else local
    timestamp_nano = _value(row, "datetime", 0)
    update_time, timestamp = _quote_time(timestamp_nano)
    bid = _float(row, "bid_price1")
    ask = _float(row, "ask_price1")
    last = _float(row, "last_price")
    return UnifiedPriceData(
        symbol=resolver.to_axile(full_symbol),
        last_price=last,
        bid_price=bid,
        ask_price=ask,
        bid_volume=_float(row, "bid_volume1"),
        ask_volume=_float(row, "ask_volume1"),
        volume=_float(row, "volume"),
        turnover=_float(row, "amount"),
        timestamp=timestamp,
        update_time=update_time,
        book_valid=bid > 0 and ask > 0,
        **{f"bid_price_{level}": _float(row, f"bid_price{level}") for level in range(2, 6)},
        **{f"ask_price_{level}": _float(row, f"ask_price{level}") for level in range(2, 6)},
        **{f"bid_volume_{level}": _float(row, f"bid_volume{level}") for level in range(2, 6)},
        **{f"ask_volume_{level}": _float(row, f"ask_volume{level}") for level in range(2, 6)},
        extra={
            "tq_symbol": full_symbol,
            "exchange_id": str(_value(row, "exchange_id", "") or ""),
            "price_tick": _float(row, "price_tick"),
            "volume_multiple": _float(row, "volume_multiple"),
        },
    )


def _order_status(row: object) -> str:
    status = str(_value(row, "status", "") or "")
    volume = _float(row, "volume_orign")
    left = _float(row, "volume_left")
    filled = max(0.0, volume - left)
    if status == "ALIVE":
        return OrderStatus.PARTIALLY_FILLED if filled else OrderStatus.PENDING
    if left <= 0 and volume > 0:
        return OrderStatus.FILLED
    message = str(_value(row, "last_msg", "") or "")
    if any(word in message for word in ("拒绝", "失败", "错误", "不合法")):
        return OrderStatus.REJECTED
    return OrderStatus.CANCELED


def order_to_unified(row: object, resolver: TQSymbolResolver) -> UnifiedOrder:
    """转换天勤订单快照."""
    exchange = str(_value(row, "exchange_id", "") or "")
    local = str(_value(row, "instrument_id", "") or "")
    full_symbol = f"{exchange}.{local}" if exchange and "." not in local else local
    symbol = (
        normalize_catalog_symbol(full_symbol).split(".", 1)[-1]
        if exchange in EXCHANGES
        else resolver.to_axile(full_symbol)
    )
    volume = _float(row, "volume_orign")
    left = _float(row, "volume_left")
    return UnifiedOrder.create(
        order_id=str(_value(row, "order_id", "") or ""),
        symbol=symbol,
        direction=OrderDirection.BUY.value if _value(row, "direction") == "BUY" else OrderDirection.SELL.value,
        order_type=OrderType.MARKET.value if _value(row, "price_type") == "ANY" else OrderType.LIMIT.value,
        volume=volume,
        price=_float(row, "limit_price"),
        channel_type=TradeChannel.TQ,
        status=_order_status(row),
        filled_volume=max(0.0, volume - left),
        avg_price=_float(row, "trade_price"),
        insert_date_time=_value(row, "insert_date_time", 0),
        exchange_id=exchange,
        offset=str(_value(row, "offset", "") or ""),
        last_msg=str(_value(row, "last_msg", "") or ""),
        tq_symbol=full_symbol,
    )


def trade_to_unified(row: object, resolver: TQSymbolResolver) -> TradeRecord:
    """转换天勤成交快照."""
    exchange = str(_value(row, "exchange_id", "") or "")
    local = str(_value(row, "instrument_id", "") or "")
    full_symbol = f"{exchange}.{local}" if exchange and "." not in local else local
    return TradeRecord.create(
        trade_id=str(_value(row, "trade_id", "") or _value(row, "exchange_trade_id", "")),
        symbol=resolver.to_axile(full_symbol),
        order_id=str(_value(row, "order_id", "") or ""),
        trade_time=_iso_from_nano(_value(row, "trade_date_time", 0)),
        trade_volume=_float(row, "volume"),
        trade_price=_float(row, "price"),
        exchange_id=exchange,
        direction=str(_value(row, "direction", "") or ""),
        offset=str(_value(row, "offset", "") or ""),
        tq_symbol=full_symbol,
    )


def account_to_unified(
    account: object,
    position_rows: dict[str, object],
    resolver: TQSymbolResolver,
    quotes: dict[str, dict[str, object]] | None = None,
) -> UnifiedAccountAssets:
    """聚合天勤资金与持仓快照，并按最新成交价计算持仓货值."""
    positions: list[Position] = []
    quotes = quotes or {}
    for full_symbol, row in position_rows.items():
        symbol = resolver.to_axile(str(full_symbol))
        quote = quotes.get(full_symbol)
        price = _positive_number(_value(quote, "last_price", None)) if quote is not None else None
        multiplier = _positive_number(_value(quote, "volume_multiple", None)) if quote is not None else None
        quote_time = _value(quote, "datetime", None) if quote is not None else None
        reason = (
            "missing_quote"
            if quote is None
            else "invalid_price"
            if price is None
            else "invalid_multiplier"
            if multiplier is None
            else None
        )
        long_td = _float(row, "pos_long_today")
        long_yd = _float(row, "pos_long_his")
        short_td = _float(row, "pos_short_today")
        short_yd = _float(row, "pos_short_his")
        long_total = long_td + long_yd
        short_total = short_td + short_yd
        common_extra = {
            "long_td": long_td,
            "long_yd": long_yd,
            "short_td": short_td,
            "short_yd": short_yd,
            "long_total": long_total,
            "short_total": short_total,
            "net_position": long_total - short_total,
            "tq_symbol": full_symbol,
        }
        if long_total:
            frozen = _float(row, "volume_long_frozen")
            value = _position_value(long_total, price, multiplier)
            positions.append(
                Position(
                    symbol=symbol,
                    volume=long_total,
                    available_volume=max(0.0, long_total - frozen),
                    market_value=value,
                    direction=PositionDirection.LONG,
                    avg_price=_float(row, "open_price_long") or None,
                    extra={
                        **common_extra,
                        "frozen": frozen,
                        "position_cost": _float(row, "position_cost_long"),
                        "market_value_provenance": _valuation_provenance(price, multiplier, quote_time, reason, value),
                    },
                )
            )
        if short_total:
            frozen = _float(row, "volume_short_frozen")
            value = _position_value(short_total, price, multiplier)
            positions.append(
                Position(
                    symbol=symbol,
                    volume=short_total,
                    available_volume=max(0.0, short_total - frozen),
                    market_value=value,
                    direction=PositionDirection.SHORT,
                    avg_price=_float(row, "open_price_short") or None,
                    extra={
                        **common_extra,
                        "frozen": frozen,
                        "position_cost": _float(row, "position_cost_short"),
                        "market_value_provenance": _valuation_provenance(price, multiplier, quote_time, reason, value),
                    },
                )
            )
    return UnifiedAccountAssets(
        available_cash=_float(account, "available"),
        total_asset=_float(account, "balance"),
        market_value=_total_value(positions),
        positions=positions,
        extra={"account_id": str(_value(account, "account_id", "") or "")},
    )


def _positive_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _position_value(volume: float, price: float | None, multiplier: float | None) -> float | None:
    if price is None or multiplier is None:
        return None
    value = volume * price * multiplier
    return value if math.isfinite(value) else None


def _total_value(positions: list[Position]) -> float | None:
    if any(position.market_value is None for position in positions):
        return None
    total = sum(position.market_value for position in positions if position.market_value is not None)
    return total if math.isfinite(total) else None


def _quote_is_reference(quote_time: object) -> bool:
    try:
        if isinstance(quote_time, (int, float)):
            timestamp = float(quote_time) / 1_000_000_000
        elif isinstance(quote_time, str):
            timestamp = datetime.fromisoformat(quote_time).timestamp()
        else:
            return True
        return clock_now().timestamp() - timestamp > 300
    except (OverflowError, OSError, TypeError, ValueError):
        return True


def _valuation_provenance(
    price: float | None, multiplier: float | None, quote_time: object, reason: str | None, value: float | None
) -> dict[str, object]:
    if value is None:
        return {
            "source": "unavailable",
            "reason": reason or "calculation_overflow",
            "price": price,
            "multiplier": multiplier,
            "quote_time": quote_time,
        }
    return {
        "source": "tq_last_price",
        "price": price,
        "multiplier": multiplier,
        "quote_time": quote_time,
        "price_status": "reference" if _quote_is_reference(quote_time) else "latest",
    }


__all__ = ["account_to_unified", "order_to_unified", "quote_to_unified", "trade_to_unified"]
