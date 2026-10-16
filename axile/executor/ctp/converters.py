"""OpenCTP 原生对象到统一模型的纯转换函数。"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from openctp_ctp import thosttraderapi as td

from axile.common.trade_channel import TradeChannel
from axile.executor.algorithms.utils.clock import clock_now
from axile.executor.constants.order_status import OrderStatus
from axile.executor.ctp.combination import split_combination_position
from axile.executor.models.unified_account_assets import Position, PositionDirection, UnifiedAccountAssets
from axile.executor.models.unified_order import OrderDirection, OrderType, TradeRecord, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData


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


def _price(row: object, name: str) -> float:
    """CTP 价格缺失使用 DBL_MAX；与数量等数值字段分开清洗。"""
    value = _float(row, name)
    return value if 0 < value < 1.7976931348623157e308 else 0.0


_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _time(day: str, clock: str, millisec: int = 0) -> str:
    """解释 CTP 自然日期与本地时刻；未知时间返回空串，禁止回退当前时间。"""
    if len(day) != 8 or not day.isdigit() or len(clock) != 8 or not 0 <= millisec <= 999:
        return ""
    try:
        parsed = datetime.strptime(f"{day} {clock}", "%Y%m%d %H:%M:%S")
    except ValueError:
        return ""
    return parsed.replace(tzinfo=_SHANGHAI, microsecond=millisec * 1000).isoformat()


def _time_evidence(day: str, clock: str, source: str, event_time: str) -> dict[str, str]:
    """保留日期来源及解析状态，供审计区分未知时间。"""
    return {
        "event_date": day,
        "event_clock": clock,
        "event_time_source": source,
        "event_time_status": "native" if event_time else "unknown",
    }


def stable_order_id(trading_day: str, front_id: int, session_id: int, order_ref: str) -> str:
    """构造不随交易所系统编号变化的订单标识。"""
    return f"{trading_day}:{front_id}:{session_id}:{order_ref}"


_ORDER_STATUS = {
    td.THOST_FTDC_OST_AllTraded: OrderStatus.FILLED,
    td.THOST_FTDC_OST_PartTradedQueueing: OrderStatus.PARTIALLY_FILLED,
    td.THOST_FTDC_OST_PartTradedNotQueueing: OrderStatus.CANCELED,
    td.THOST_FTDC_OST_NoTradeQueueing: OrderStatus.PENDING,
    td.THOST_FTDC_OST_NoTradeNotQueueing: OrderStatus.CANCELED,
    td.THOST_FTDC_OST_Canceled: OrderStatus.CANCELED,
    td.THOST_FTDC_OST_Unknown: OrderStatus.SUBMITTED,
    td.THOST_FTDC_OST_NotTouched: OrderStatus.SUBMITTED,
    td.THOST_FTDC_OST_Touched: OrderStatus.SUBMITTED,
}


def order_to_unified(row: object, *, trading_day: str, front_id: int, session_id: int) -> UnifiedOrder:
    """转换原生报单回报或查询帧。"""
    row_front = int(_value(row, "FrontID", front_id) or front_id)
    row_session = int(_value(row, "SessionID", session_id) or session_id)
    order_ref = str(_value(row, "OrderRef", "") or "")
    day = str(_value(row, "TradingDay", trading_day) or trading_day)
    volume = _float(row, "VolumeTotalOriginal")
    traded = _float(row, "VolumeTraded")
    direction = OrderDirection.BUY if _value(row, "Direction") == td.THOST_FTDC_D_Buy else OrderDirection.SELL
    price_type = OrderType.LIMIT if _value(row, "OrderPriceType") == td.THOST_FTDC_OPT_LimitPrice else OrderType.MARKET
    insert_time = str(_value(row, "InsertTime", "") or "")
    insert_day = str(_value(row, "InsertDate", "") or "")
    create_time = _time(insert_day, insert_time)
    return UnifiedOrder.create(
        order_id=stable_order_id(day, row_front, row_session, order_ref),
        symbol=str(_value(row, "InstrumentID", "") or ""),
        direction=direction.value,
        order_type=price_type.value,
        volume=volume,
        price=_price(row, "LimitPrice"),
        channel_type=TradeChannel.CTP,
        status=_ORDER_STATUS.get(_value(row, "OrderStatus"), OrderStatus.REJECTED),
        filled_volume=traded,
        avg_price=0.0,
        create_time=create_time,
        update_time=clock_now().astimezone(_SHANGHAI).isoformat(),
        **{
            key: _value(row, native, None)
            for key, native in {
                "hedge_flag": "CombHedgeFlag",
                "native_order_type": "OrderPriceType",
                "time_condition": "TimeCondition",
                "gtd_date": "GTDDate",
                "volume_condition": "VolumeCondition",
                "min_volume": "MinVolume",
                "contingent_condition": "ContingentCondition",
                "stop_price": "StopPrice",
                "submit_status": "OrderSubmitStatus",
                "active_time": "ActiveTime",
                "suspend_time": "SuspendTime",
                "native_update_time": "UpdateTime",
                "cancel_time": "CancelTime",
                "order_local_id": "OrderLocalID",
                "force_close_reason": "ForceCloseReason",
            }.items()
        },
        trading_day=day,
        **_time_evidence(insert_day, insert_time, "InsertDate", create_time),
        order_ref=order_ref,
        front_id=row_front,
        session_id=row_session,
        exchange_id=str(_value(row, "ExchangeID", "") or ""),
        order_sys_id=str(_value(row, "OrderSysID", "") or "").strip(),
        offset_flag=str(_value(row, "CombOffsetFlag", "") or "")[:1],
        status_msg=str(_value(row, "StatusMsg", "") or ""),
    )


def trade_to_unified(
    row: object, *, trading_day: str, front_id: int, session_id: int, resolved_order_id: str = ""
) -> TradeRecord:
    """转换原生成交帧并保持稳定订单关联。"""
    day = str(_value(row, "TradingDay", trading_day) or trading_day)
    order_ref = str(_value(row, "OrderRef", "") or "")
    price = _price(row, "Price")
    volume = _float(row, "Volume")
    trade_day = str(_value(row, "TradeDate", "") or "")
    trade_clock = str(_value(row, "TradeTime", "") or "")
    trade_time = _time(trade_day, trade_clock)
    return TradeRecord.create(
        trade_id=str(_value(row, "TradeID", "") or ""),
        symbol=str(_value(row, "InstrumentID", "") or ""),
        order_id=resolved_order_id,
        trade_time=trade_time,
        trading_day=day,
        **_time_evidence(trade_day, trade_clock, "TradeDate", trade_time),
        trade_volume=volume,
        trade_price=price,
        order_ref=order_ref,
        exchange_id=str(_value(row, "ExchangeID", "") or ""),
        order_sys_id=str(_value(row, "OrderSysID", "") or "").strip(),
        direction=str(_value(row, "Direction", "") or ""),
        offset_flag=str(_value(row, "OffsetFlag", "") or ""),
        hedge_flag=str(_value(row, "HedgeFlag", "") or ""),
    ).model_copy(update={"order_id": resolved_order_id})


def quote_to_unified(row: object) -> UnifiedPriceData:
    """转换原生深度行情帧。"""
    day = str(_value(row, "ActionDay", "") or "")
    clock = str(_value(row, "UpdateTime", "") or "")
    try:
        millisec = int(_value(row, "UpdateMillisec", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        millisec = -1
    update_time = _time(day, clock, millisec)
    timestamp = int(datetime.fromisoformat(update_time).timestamp() * 1000) if update_time else 0
    bid = _price(row, "BidPrice1")
    ask = _price(row, "AskPrice1")
    return UnifiedPriceData(
        symbol=str(_value(row, "InstrumentID", "") or ""),
        last_price=_price(row, "LastPrice"),
        bid_price=bid,
        ask_price=ask,
        bid_volume=_float(row, "BidVolume1"),
        ask_volume=_float(row, "AskVolume1"),
        volume=_float(row, "Volume"),
        turnover=_float(row, "Turnover"),
        timestamp=timestamp,
        update_time=update_time,
        book_valid=0 < bid <= ask and _float(row, "BidVolume1") > 0 and _float(row, "AskVolume1") > 0,
        **{f"bid_price_{level}": _price(row, f"BidPrice{level}") for level in range(2, 6)},
        **{f"ask_price_{level}": _price(row, f"AskPrice{level}") for level in range(2, 6)},
        **{f"bid_volume_{level}": _float(row, f"BidVolume{level}") for level in range(2, 6)},
        **{f"ask_volume_{level}": _float(row, f"AskVolume{level}") for level in range(2, 6)},
        extra={
            "lower_limit_price": _price(row, "LowerLimitPrice"),
            "upper_limit_price": _price(row, "UpperLimitPrice"),
            "exchange_id": str(_value(row, "ExchangeID", "") or ""),
            "trading_day": str(_value(row, "TradingDay", "") or ""),
            "event_millisec": _value(row, "UpdateMillisec", 0),
            **_time_evidence(day, clock, "ActionDay", update_time),
        },
    )


def account_to_unified(
    account: object,
    position_rows: list[object],
    instruments: Mapping[str, object],
    *,
    quotes: Mapping[str, UnifiedPriceData] | None = None,
    quote_errors: Mapping[str, str] | None = None,
    progress: Callable[[], None] | None = None,
) -> UnifiedAccountAssets:
    """聚合资金与原生持仓帧为统一账户快照。"""
    expanded: list[object] = []
    for row in position_rows:
        split = split_combination_position(row)
        expanded.extend(split if split is not None else [row])
        if progress is not None:
            progress()
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in expanded:
        symbol = str(_value(row, "InstrumentID", "") or "")
        native_direction = str(_value(row, "PosiDirection", "") or "")
        direction = PositionDirection.LONG if native_direction == td.THOST_FTDC_PD_Long else PositionDirection.SHORT
        key = (symbol, direction.value)
        volume = _position_quantity(row, "Position")
        if volume == 0:
            continue
        group = groups.setdefault(
            key, {"volume": 0.0, "today": 0.0, "yesterday": 0.0, "frozen": 0.0, "cost": 0.0, "origin": None}
        )
        today = _position_quantity(row, "TodayPosition")
        if today > volume:
            raise ValueError(f"{symbol}: 今仓与总持仓不一致")
        # YdPosition 是昨结持仓，平昨后不会代表当前剩余昨仓。
        # 每条记录按当前 Position 拆分，避免跨今昨记录重复累计昨结数量。
        group["today"] += today
        group["yesterday"] += volume - today
        group["volume"] += volume
        group["frozen"] += (
            _float(row, "LongFrozen") if direction is PositionDirection.LONG else _float(row, "ShortFrozen")
        )
        group["cost"] += _float(row, "PositionCost")
        group["origin"] = _value(row, "combination_origin", group["origin"])
        if progress is not None:
            progress()
    positions: list[Position] = []
    for (symbol, direction), group in groups.items():
        instrument = instruments.get(symbol)
        multiplier = _float(instrument, "VolumeMultiple")
        volume = group["volume"]
        quote = quotes.get(symbol) if quotes else None
        reason = (quote_errors or {}).get(symbol)
        if multiplier <= 0:
            reason = "missing_multiplier"
        elif quote is None and reason is None:
            reason = "missing_quote"
        elif quote is not None and _price({"LastPrice": quote.last_price}, "LastPrice") <= 0:
            reason = "invalid_last_price"
        market_value = volume * quote.last_price * multiplier if reason is None and quote is not None else None
        avg_price = group["cost"] / (volume * multiplier) if volume and multiplier > 0 else None
        extra = {
            "long_td" if direction == PositionDirection.LONG.value else "short_td": group["today"],
            "long_yd" if direction == PositionDirection.LONG.value else "short_yd": group["yesterday"],
            "frozen": group["frozen"],
            "position_cost": group["cost"],
            "volume_multiple": multiplier if multiplier > 0 else None,
            "last_price": quote.last_price if quote is not None else None,
            "quote_timestamp": quote.timestamp if quote is not None else None,
            "quote_update_time": quote.update_time if quote is not None else None,
            "valuation_status": "unavailable" if reason else "priced",
            "valuation_source": None if reason else quote.extra.get("valuation_source", "ctp_last_price"),
            "valuation_reference": quote.extra.get("valuation_reference", False) if quote is not None else False,
        }
        if reason:
            extra["valuation_reason"] = reason
        if group["origin"]:
            extra["combination_origin"] = group["origin"]
        positions.append(
            Position(
                symbol=symbol,
                volume=volume,
                available_volume=max(0.0, volume - group["frozen"]),
                market_value=market_value,
                direction=direction,
                avg_price=avg_price,
                extra=extra,
            )
        )
        if progress is not None:
            progress()
    return UnifiedAccountAssets(
        available_cash=_float(account, "Available"),
        total_asset=_float(account, "Balance"),
        market_value=sum(position.market_value for position in positions)
        if all(position.market_value is not None for position in positions)
        else None,
        positions=positions,
        extra={
            "account_id": str(_value(account, "AccountID", "") or ""),
            "trading_day": str(_value(account, "TradingDay", "") or ""),
        },
    )


def _position_quantity(row: object, name: str) -> float:
    """拒绝损坏的持仓数量，避免通用数值转换把异常静默变成零仓。"""
    raw = _value(row, name, 0)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"{name}: 持仓数量必须是非负整数")
    value = float(raw)
    if not math.isfinite(value) or value < 0 or not value.is_integer():
        raise ValueError(f"{name}: 持仓数量必须是非负整数")
    return value


__all__ = ["account_to_unified", "order_to_unified", "quote_to_unified", "stable_order_id", "trade_to_unified"]
