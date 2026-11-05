"""GM 执行器共享的转换与状态辅助."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from enum import IntEnum
from typing import Any

from gm.api import (  # type: ignore  # pyright: ignore[reportUnknownVariableType]
    ExecType_Trade,
    OrderSide_Buy,
    OrderSide_Sell,
    OrderType_Limit,
)
from loguru import logger

from axile.channels.gm_sessions import AUCTION_PHASES, SHANGHAI, gm_phase
from axile.common.gm_symbols import GM_SYMBOL_RESOLVER
from axile.common.trade_channel import TradeChannel
from axile.executor.constants.order_status import OrderStatus
from axile.executor.models.unified_order import (
    OrderDirection,
    OrderType,
    TradeRecord,
    UnifiedOrder,
)
from axile.executor.models.unified_price import UnifiedPriceData


def from_gm_price(
    tick: dict[str, Any],
    channel_type: TradeChannel = TradeChannel.GM,
    include_raw_data: bool = True,  # noqa: FBT001, FBT002
) -> UnifiedPriceData:
    """转换 GM tick；竞价参考价使用买卖一一致的虚拟匹配价，原成交价保留在 extra。"""
    bid_prices: list[Any] = tick["bid_price"] if tick["bid_price"] else []
    ask_prices: list[Any] = tick["ask_price"] if tick["ask_price"] else []
    bid_volumes: list[Any] = tick["bid_volume"] if tick["bid_volume"] else []
    ask_volumes: list[Any] = tick["ask_volume"] if tick["ask_volume"] else []
    gm_symbol = str(tick["symbol"])
    extra: dict[str, object] = {"channel_type": channel_type, "gm_symbol": gm_symbol}
    last_price = float(tick["price"])
    if tick["timestamp"] > 0 and gm_phase(datetime.fromtimestamp(tick["timestamp"] / 1000, SHANGHAI)) in AUCTION_PHASES:
        bid = float(bid_prices[0]) if bid_prices else 0.0
        ask = float(ask_prices[0]) if ask_prices else 0.0
        extra.update(gm_last_trade_price=last_price, price_basis="auction_indicative")
        # 未成交时 price 无效；没有明确匹配价则留零，让定量校验阻断。
        last_price = bid if math.isfinite(bid) and bid > 0 and bid == ask else 0.0
    if include_raw_data:
        extra["raw_data"] = tick

    return UnifiedPriceData.model_construct(
        symbol=GM_SYMBOL_RESOLVER.to_axile(gm_symbol),
        last_price=last_price,
        bids=[(float(price), float(quantity)) for price, quantity in zip(bid_prices, bid_volumes) if float(price) > 0],
        asks=[(float(price), float(quantity)) for price, quantity in zip(ask_prices, ask_volumes) if float(price) > 0],
        volume=float(tick["volume"]),
        turnover=0.0,
        timestamp=tick["timestamp"],
        update_time=tick["dt"],
        extra=extra,
    )


def convert_gm_side_to_direction(side: int) -> OrderDirection:
    """将 GM 买卖方向转换为统一方向枚举."""
    if side == OrderSide_Buy:
        return OrderDirection.BUY
    if side == OrderSide_Sell:
        return OrderDirection.SELL
    return OrderDirection.BUY


def convert_gm_order_type_to_type(order_type: int) -> OrderType:
    """将 GM 订单类型转换为统一订单类型枚举."""
    if order_type == OrderType_Limit:
        return OrderType.LIMIT
    return OrderType.MARKET


def convert_gm_status_to_string(status: int) -> str:
    """将 GM 订单状态转换为中文状态字符串."""
    status_map = {
        1: OrderStatus.SUBMITTED,
        2: OrderStatus.PARTIALLY_FILLED,
        3: OrderStatus.FILLED,
        5: OrderStatus.CANCELED,
        8: OrderStatus.REJECTED,
        10: "待报",
        12: OrderStatus.EXPIRED,
        15: "待触发",
        16: "已触发",
    }
    return status_map.get(status, "未知")


def _build_gm_trade_id(
    *,
    exec_id: object,
    cl_ord_id: object,
    exchange_order_id: object,
    symbol: object,
    trade_time: object,
    trade_volume: float,
    trade_price: float,
) -> str:
    """为 GM 成交记录构造稳定且尽量不冲突的 trade_id."""
    if exec_id:
        return str(exec_id)

    identity_payload = {
        "cl_ord_id": str(cl_ord_id or ""),
        "exchange_order_id": str(exchange_order_id or ""),
        "symbol": str(symbol or ""),
        "trade_time": str(trade_time or ""),
        "trade_volume": float(trade_volume),
        "trade_price": float(trade_price),
    }
    payload = json.dumps(identity_payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
    return f"gm-fallback:{digest}"


def convert_gm_trade_to_trade_record(trade: dict[str, Any]) -> TradeRecord | None:
    """仅将有效成交回报转换为成交；其他回报保留在渠道日志中。"""
    if trade.get("exec_type") != ExecType_Trade:
        logger.warning("GM 非成交执行回报: {}", trade)
        return None
    try:
        trade_volume = float(trade.get("volume", 0))
        trade_price = float(trade.get("price", 0))
    except (TypeError, ValueError, OverflowError):
        logger.error("GM 成交回报数量或价格非法: {}", trade)
        return None
    if any(isinstance(trade.get(key), bool) for key in ("volume", "price")) or not (
        math.isfinite(trade_volume) and trade_volume > 0 and math.isfinite(trade_price) and trade_price > 0
    ):
        logger.error("GM 成交回报数量或价格非法: {}", trade)
        return None
    trade_time = trade.get("created_at")
    if isinstance(trade_time, datetime):
        trade_time = trade_time.isoformat()

    gm_symbol = str(trade.get("symbol", ""))
    return TradeRecord.create(
        trade_id=_build_gm_trade_id(
            exec_id=trade.get("exec_id"),
            cl_ord_id=trade.get("cl_ord_id"),
            exchange_order_id=trade.get("order_id"),
            symbol=trade.get("symbol"),
            trade_time=trade_time,
            trade_volume=trade_volume,
            trade_price=trade_price,
        ),
        symbol=GM_SYMBOL_RESOLVER.to_axile(gm_symbol),
        order_id=str(trade.get("cl_ord_id") or trade.get("order_id") or ""),
        trade_time=trade_time or "",
        trade_volume=trade_volume,
        trade_price=trade_price,
        extra={
            "channel_type": TradeChannel.GM,
            "account_id": trade.get("account_id"),
            "symbol": trade.get("symbol", ""),
            "gm_symbol": gm_symbol,
            "cl_ord_id": trade.get("cl_ord_id"),
            "exchange_order_id": trade.get("order_id"),
            "raw_trade_data": trade,
        },
    )


def _get_gm_actionable_order_id(order: dict[str, Any]) -> str:
    """返回 GM 通用层使用的可操作订单主键."""
    return str(order.get("cl_ord_id") or order.get("order_id") or "")


def convert_gm_order_to_unified(order: dict[str, Any]) -> UnifiedOrder:
    """将 GM 订单数据转换为统一订单模型."""
    create_time = order.get("created_at")
    if isinstance(create_time, datetime):
        create_time = create_time.isoformat()

    actionable_order_id = _get_gm_actionable_order_id(order)

    gm_symbol = str(order.get("symbol", ""))
    unified_order = UnifiedOrder.create(
        order_id=actionable_order_id,
        symbol=GM_SYMBOL_RESOLVER.to_axile(gm_symbol),
        direction=convert_gm_side_to_direction(order.get("side", 0)).value,
        order_type=convert_gm_order_type_to_type(order.get("order_type", 0)).value,
        volume=float(order.get("volume", 0)),
        price=float(order.get("price", 0)),
        channel_type=TradeChannel.GM,
        status=convert_gm_status_to_string(order.get("status", 0)),
        filled_volume=float(order.get("filled_volume") or 0),
        avg_price=float(order.get("filled_vwap") or 0),
        create_time=create_time,
        update_time=(
            order["updated_at"].isoformat()
            if isinstance(order.get("updated_at"), datetime)
            else order.get("updated_at")
        ),
        raw_order_data=order,
    )

    unified_order.extra.update(
        {
            "account_id": order.get("account_id"),
            "gm_symbol": gm_symbol,
            "cl_ord_id": order.get("cl_ord_id"),
            "exchange_order_id": order.get("order_id"),
            "order_style": order.get("order_style"),
            "order_business": order.get("order_business"),
            "position_effect": order.get("position_effect"),
            "position_side": order.get("position_side"),
            "ord_rej_reason": order.get("ord_rej_reason"),
            "ord_rej_reason_detail": order.get("ord_rej_reason_detail"),
        }
    )

    return unified_order


class AccConnectionState(IntEnum):
    """账户连接状态枚举."""

    CONNECTING = 1
    CONNECTED = 2
    LOGGEDIN = 3
    DISCONNECTING = 4
    DISCONNECTED = 5
    ERROR = 6

    def __str__(self) -> str:
        """返回账户连接状态的中文描述."""
        return {
            AccConnectionState.CONNECTING: "连接中",
            AccConnectionState.CONNECTED: "已连接",
            AccConnectionState.LOGGEDIN: "已登录",
            AccConnectionState.DISCONNECTING: "断开中",
            AccConnectionState.DISCONNECTED: "已断开",
            AccConnectionState.ERROR: "错误",
        }.get(self, "未知")


class AccountStatusError(Exception):
    """账户状态异常错误."""


__all__ = [
    "AccConnectionState",
    "AccountStatusError",
    "convert_gm_order_to_unified",
    "convert_gm_order_type_to_type",
    "convert_gm_side_to_direction",
    "convert_gm_status_to_string",
    "convert_gm_trade_to_trade_record",
    "from_gm_price",
]
