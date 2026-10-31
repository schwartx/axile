"""CTP 回放旁路：只提取允许字段，不复用交易模型的缺省推断。"""

from __future__ import annotations

import math
from collections import OrderedDict
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from axile.executor import replay as recording
from axile.executor.algorithms.utils.clock import clock_now_us
from axile.executor.ctp.converters import _time, stable_order_id
from axile.executor.ctp.quote_time import resolve_quote_time
from axile.executor.ctp_product_sessions import get_ctp_product_sessions
from axile.executor.trading_calendar import CHINA_CALENDAR_ID

_DIRECTION = {"0": "BUY", "1": "SELL"}
_OFFSET = {
    "0": "OPEN",
    "1": "CLOSE",
    "2": "CLOSE",
    "3": "CLOSETODAY",
    "4": "CLOSEYESTERDAY",
    "5": "CLOSE",
    "6": "CLOSE",
}
_STATUS = {
    "0": "filled",
    "1": "partially_filled",
    "2": "canceled",
    "3": "working",
    "4": "canceled",
    "5": "canceled",
    "a": "pending",
    "b": "trigger_wait",
    "c": "activated",
}
_IDENTITY = {
    "TradingDay": "trading_day",
    "FrontID": "front_id",
    "SessionID": "session_id",
    "OrderRef": "order_ref",
    "ExchangeID": "exchange_id",
    "OrderSysID": "order_sys_id",
    "TradeID": "trade_id",
    "RequestID": "request_id",
}
_ENUM_ATTRIBUTES = {
    "Direction": "native_direction",
    "OrderStatus": "native_status",
    "CombOffsetFlag": "native_comb_offset_flag",
    "OffsetFlag": "native_offset_flag",
    "PosiDirection": "native_position_direction",
}


class Fields:
    """记录字段缺失与无效值，保留合法零量。"""

    def __init__(self, row: object) -> None:
        self.row = row
        self.quality: dict[str, str] = {}
        self.attributes: dict[str, str | int] = {}

    def value(self, name: str) -> object:
        """读取原始字段并记录存在性。"""
        if isinstance(self.row, Mapping):
            value = self.row.get(name)
        else:
            value = getattr(self.row, name, None)
        self.quality[name] = "missing" if value is None or value == "" else "known"
        if (
            name in _ENUM_ATTRIBUTES
            and isinstance(value, str | int)
            and not isinstance(value, bool)
            and value != ""
            and len(str(value)) <= 512
        ):
            self.attributes[_ENUM_ATTRIBUTES[name]] = value
        return value

    def text(self, name: str) -> str | None:
        """读取有界文本，不把对象 repr 当原生值。"""
        value = self.value(name)
        if value is None or value == "":
            return None
        if not isinstance(value, str | int) or isinstance(value, bool) or len(str(value)) > 512:
            self.quality[name] = "invalid"
            return None
        return str(value).strip() or None

    def number(self, name: str) -> float | None:
        """读取有限数值，保留真实零并拒绝哨兵。"""
        value = self.value(name)
        if value is None or value == "":
            return None
        try:
            number = float(value)
        except (ValueError, TypeError, OverflowError):
            number = math.inf
        if isinstance(value, bool) or not math.isfinite(number) or abs(number) >= 1.7976931348623157e308:
            self.quality[name] = "invalid"
            return None
        return number

    def enum(self, name: str, values: Mapping[str, str]) -> str | None:
        """仅转换有明确定义的原生枚举。"""
        value = self.text(name)
        if value is not None and value not in values:
            self.quality[name] = "invalid"
        return values.get(value) if value is not None else None


def capture(owner: object, topic: str, build: Callable[[], dict[str, object]]) -> None:
    """只在开启时构造观察；规范化故障也不能改变交易返回。"""
    if not recording.replay_enabled(owner):
        return
    try:
        recording.observe(owner, topic, build())
    except Exception:  # noqa: BLE001 - 旁路规范化也必须隔离于交易
        recording.observe(
            owner,
            "/connection",
            {
                "source": "ctp.replay",
                "status": "error",
                "reason": "normalization_failed",
                "available": False,
                "connected": False,
                "synchronized": False,
            },
        )


def _base(fields: Fields, source: str) -> dict[str, object]:
    identity = {key: value for native, key in _IDENTITY.items() if (value := fields.text(native)) is not None}
    return {
        "source": source,
        "attributes": fields.attributes,
        "native_identity": identity,
        "field_quality": fields.quality,
    }


def _source_time(fields: Fields, day: str, clock: str, *, millis: str | None = None) -> dict[str, object]:
    native_day, native_clock = fields.text(day), fields.text(clock)
    fraction = fields.number(millis) if millis else 0
    if fraction is not None and (not float(fraction).is_integer() or not 0 <= fraction <= 999):
        fields.quality[millis] = "invalid"
        fraction = None
    parsed = _time(native_day or "", native_clock or "", int(fraction) if fraction is not None else -1)
    timestamp = _unix_us(parsed)
    return {
        "source_time_us": timestamp,
        "source_precision": "millisecond" if millis else "second",
        "source_times": {
            "date": native_day,
            "clock": native_clock,
            "millisecond": fraction if millis else None,
            "timezone": "Asia/Shanghai",
        },
    }


def _unix_us(value: str) -> int | None:
    moment = datetime.fromisoformat(value) if value else None
    if moment is not None:
        delta = moment.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
        timestamp = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    else:
        timestamp = None
    return timestamp


def _quote_time(owner: object, f: Fields) -> dict[str, object]:
    """复用夜盘纯解析规则，不改交易行情缓存或日志水位。"""
    result = _source_time(f, "ActionDay", "UpdateTime", millis="UpdateMillisec")
    symbol = f.text("InstrumentID")
    instrument = getattr(owner, "_instruments", {}).get(symbol)
    sessions = ()
    if getattr(instrument, "ProductClass", None) == "1":
        sessions = get_ctp_product_sessions(getattr(instrument, "ExchangeID", ""), getattr(instrument, "ProductID", ""))
    calendar = getattr(owner, "_trading_calendar", None)
    calendar_id = getattr(owner, "_channel_calendar_id", None) or CHINA_CALENDAR_ID
    if calendar is None:
        calendar = getattr(owner, "_quote_calendar", None)
        calendar_id = CHINA_CALENDAR_ID
    native = result["source_times"]
    value = _time(
        native["date"] or "",
        native["clock"] or "",
        int(native["millisecond"]) if native["millisecond"] is not None else -1,
    )
    value, status, reason = resolve_quote_time(
        value,
        getattr(owner, "_trading_day", ""),
        sessions,
        lambda day: calendar.is_open(calendar_id, day) if calendar is not None else None,
    )
    result.update(source_time_us=_unix_us(value), source_time_quality=status)
    result["source_times"] = {**native, "resolved_time": value or None, "resolution": reason or status}
    return result


def _order_id(fields: Fields) -> str | None:
    day, front, session, ref = (fields.text(k) for k in ("TradingDay", "FrontID", "SessionID", "OrderRef"))
    if day is None or front is None or session is None or ref is None:
        return None
    try:
        return stable_order_id(day, int(front), int(session), ref)
    except ValueError:
        return None


def order(row: object, *, source: str = "ctp.order_return") -> dict[str, object]:
    """从原始报单保留未知方向、状态、时间和真实原生身份。"""
    f = Fields(row)
    data = _base(f, source)
    status = f.enum("OrderStatus", _STATUS) or "unknown"
    quantity, filled = f.number("VolumeTotalOriginal"), f.number("VolumeTraded")
    data.update(
        symbol=f.text("InstrumentID"),
        order_id=_order_id(f),
        direction=f.enum("Direction", _DIRECTION),
        offset=f.enum("CombOffsetFlag", _OFFSET),
        status=status,
        price=f.number("LimitPrice"),
        quantity=quantity,
        filled_quantity=filled,
        remaining_quantity=max(0.0, quantity - filled) if quantity is not None and filled is not None else None,
        confirmed_book=status in {"working", "partially_filled"} if status != "unknown" else None,
    )
    data.update(_source_time(f, "InsertDate", "InsertTime"))
    data["attributes"].update(
        {
            key: value
            for native, key in {
                "CombHedgeFlag": "hedge_flag",
                "OrderPriceType": "native_order_type",
                "TimeCondition": "time_condition",
                "VolumeCondition": "volume_condition",
                "OrderSubmitStatus": "submit_status",
                "StatusMsg": "status_msg",
            }.items()
            if (value := f.text(native)) is not None
        }
    )
    return data


def trade(owner: object, row: object, *, source: str, order_id: str | None = None) -> dict[str, object]:
    """保留成交源观察，缺账号命名空间时不制造稳定去重键。"""
    f = Fields(row)
    data = _base(f, source)
    account = getattr(owner, "_replay_account_id", None)
    symbol, day, exchange, identity = (f.text(k) for k in ("InstrumentID", "TradingDay", "ExchangeID", "TradeID"))
    stable = isinstance(account, int) and not isinstance(account, bool) and all((symbol, day, exchange, identity))
    data.update(
        symbol=symbol,
        trade_id=identity,
        trade_key=f"ctp:{account}:{day}:{exchange}:{symbol}:{identity}" if stable else None,
        identity_quality="stable" if stable else "weak" if identity else "unknown",
        order_id=order_id,
        association="confirmed" if order_id else "pending",
        direction=f.enum("Direction", _DIRECTION),
        offset=f.enum("OffsetFlag", _OFFSET),
        price=f.number("Price"),
        quantity=f.number("Volume"),
    )
    data.update(_source_time(f, "TradeDate", "TradeTime"))
    return data


def quote(owner: object, row: object) -> dict[str, object]:
    """提取真实价量档位；缺档和无效哨兵不补成零挂量。"""
    f = Fields(row)
    data = _base(f, "ctp.depth_return")
    sides: dict[str, list[dict[str, float]]] = {}
    for label, native in (("bids", "Bid"), ("asks", "Ask")):
        levels = []
        for index in range(1, 6):
            price, quantity = f.number(f"{native}Price{index}"), f.number(f"{native}Volume{index}")
            if price is not None and price > 0 and quantity is not None and quantity > 0:
                levels.append({"price": price, "quantity": quantity})
        sides[label] = levels
    data.update(_quote_time(owner, f))
    bids, asks = sides["bids"], sides["asks"]
    source_time = data["source_time_us"]
    config = getattr(owner, "account_config", None)
    age = getattr(config, "quote_max_age_seconds", 0)
    valid_until = (
        source_time + int(age * 1_000_000)
        if isinstance(source_time, int) and isinstance(age, int | float) and age > 0
        else None
    )
    data.update(
        symbol=f.text("InstrumentID"),
        **sides,
        available_depth={"bid": len(bids), "ask": len(asks)},
        depth_coverage={"kind": "top_n", "bid": len(bids), "ask": len(asks), "unlisted": "unknown"},
        book_valid=bool(
            bids
            and asks
            and bids[0]["price"] <= asks[0]["price"]
            and valid_until is not None
            and isinstance(source_time, int)
            and source_time <= clock_now_us() <= valid_until
        ),
        valid_until_us=valid_until,
        last_price=f.number("LastPrice"),
        last_price_kind="trade" if f.quality.get("LastPrice") == "known" else "unknown",
        volume=f.number("Volume"),
        volume_kind="cumulative",
    )
    return data


def account_observation(accounts: list[object], positions: list[object], *, source: str) -> dict[str, object]:
    """正常查询结果不承诺资产与成交的业务原子对齐。"""
    groups: dict[tuple[str | None, str | None], list[dict[str, object]]] = {}
    rows = []
    for row in positions:
        f = Fields(row)
        position = _base(f, source)
        position.update(
            symbol=f.text("InstrumentID"),
            position_side=f.enum("PosiDirection", {"2": "LONG", "3": "SHORT"}),
            quantity=f.number("Position"),
            today_quantity=f.number("TodayPosition"),
            long_frozen=f.number("LongFrozen"),
            short_frozen=f.number("ShortFrozen"),
            position_basis={"status": "unverified", "proof": None},
        )
        if position["symbol"] is None or position["position_side"] is None:
            rows.append(position)
        else:
            groups.setdefault((position["symbol"], position["position_side"]), []).append(position)
    rows.extend(_sum_positions(group) for group in groups.values())
    f = Fields(accounts[-1] if accounts else {})
    data = _base(f, source)
    data.update(
        scope={"kind": "account", "symbols": []},
        scope_complete=bool(accounts)
        and all(
            row["symbol"] is not None and row["position_side"] is not None and row["quantity"] is not None
            for row in rows
        ),
        positions=rows,
        position_basis={"status": "unverified", "proof": None},
        assets={
            "balance": f.number("Balance"),
            "available_cash": f.number("Available"),
            "frozen_margin": f.number("FrozenMargin"),
        },
    )
    return data


def _sum_positions(rows: list[dict[str, object]]) -> dict[str, object]:
    """合并同侧今昨分项；任一分项缺值时对应总量仍未知。"""
    result = dict(rows[0])
    quality = dict(result["field_quality"])
    for field in ("quantity", "today_quantity", "long_frozen", "short_frozen"):
        values = [row[field] for row in rows]
        known = all(value is not None for value in values)
        result[field] = sum(values) if known else None
        quality[field] = "known" if known else "missing"
    result["field_quality"] = quality
    return result


def command(
    owner: object, name: str, row: object, rid: int, *, phase: str, result: int | None = None
) -> dict[str, object]:
    """将原生报撤调用关联到公共 command，不保存请求凭据。"""
    data = order(row, source="ctp.native_request")
    data.update(
        command_id=recording.current_command_id(),
        operation="cancel_order" if name == "ReqOrderAction" else "place_order",
        phase=phase,
        request_id=rid,
        return_code=result,
    )
    identity = dict(data["native_identity"])
    identity["request_id"] = str(rid)
    if name == "ReqOrderInsert":
        data["order_id"] = stable_order_id(
            getattr(owner, "_trading_day", ""),
            getattr(owner, "_front_id", 0),
            getattr(owner, "_session_id", 0),
            identity.get("order_ref", ""),
        )
    data["native_identity"] = identity
    return data


def link_cancel_request(owner: object, request_id: int, symbol: str, order_id: str) -> None:
    """在原生调用前保存本次录制内的精确撤单请求关联。"""
    observer = getattr(owner, "_replay_observer", None)
    command_id = recording.current_command_id()
    generation = getattr(owner, "_replay_sync_generation", None)
    if observer is None or not command_id or type(generation) is not int:
        return
    try:
        with getattr(owner, "_lock"):
            links = getattr(owner, "_replay_cancel_request_links", None)
            if links is None:
                links = OrderedDict()
                setattr(owner, "_replay_cancel_request_links", links)
            if links:
                previous = next(iter(links.values()))
                if previous[0] is not observer or previous[1] != generation:
                    links.clear()
            links[request_id] = (observer, generation, command_id, symbol, order_id)
            while len(links) > 512:
                links.popitem(last=False)
    except Exception:  # noqa: BLE001 - 回放关联失败不能阻止撤单请求
        recording.observe(
            owner, "/connection", {"source": "ctp.replay", "available": False, "reason": "cancel_link_failed"}
        )


def begin_query(owner: object, symbol: str | None) -> dict[str, object] | None:
    """记录查询发起前缀，不能以业务订单缓存版本替代。"""
    if not recording.replay_enabled(owner):
        return None
    try:
        return recording.begin_query(owner, [symbol] if symbol else [])
    except Exception:  # noqa: BLE001 - 查询观察不能改变柜台请求
        return None


def order_set(
    rows: list[object], symbol: str | None, *, synchronized: bool, token: dict[str, object] | None = None
) -> dict[str, object]:
    """无可证明查询起点时保留集合，不凭缺席清掉并发新单。"""
    return {
        "source": "ctp.query_orders",
        "scope": {"kind": "symbols" if symbol else "account", "symbols": [symbol] if symbol else []},
        "scope_complete": token is not None,
        "synchronized": synchronized,
        "sync_generation": token.get("sync_generation") if token else None,
        "query_basis": token.get("query_basis") if token else None,
        "orders": [order(row, source="ctp.query_orders") for row in rows],
        "attributes": {},
        "native_identity": {},
        "field_quality": {},
    }


def rejection(
    row: object, info: object, *, source: str, request_id: int | None = None, order_id: str | None = None
) -> dict[str, object]:
    """只在真实 ErrorID 非零时确认拒绝，不填造方向或价格。"""
    data = order(row, source=source)
    error = Fields(info)
    code = error.number("ErrorID")
    data.update(
        status="rejected" if code is not None and code != 0 else "unknown",
        confirmed_book=False if code is not None and code != 0 else None,
        error_id=code,
        request_id=request_id,
        command_id=recording.current_command_id(),
    )
    if order_id is not None:
        data["order_id"] = order_id
    return data


def cancel_rejection(owner: object, row: object, info: object, request_id: int | None = None) -> dict[str, object]:
    """仅按同录制、同代次的原生请求 ID 确认撤单失败。"""
    rid = request_id if request_id is not None else Fields(row).value("RequestID")
    rid = rid if type(rid) is int and rid > 0 else None
    data = rejection(row, info, source="ctp.cancel_error", request_id=rid)
    data["command_id"] = None
    if rid is None or data["error_id"] is None or data["error_id"] == 0:
        return data
    links = getattr(owner, "_replay_cancel_request_links", None)
    if links is None:
        return data
    with getattr(owner, "_lock"):
        link = links.get(rid)
        if (
            link is None
            or link[0] is not getattr(owner, "_replay_observer", None)
            or link[1] != getattr(owner, "_replay_sync_generation", None)
        ):
            return data
    data.update(
        command_id=link[2],
        symbol=link[3],
        order_id=link[4],
        operation="cancel_order",
        phase="native_error",
        success=False,
        accepted=False,
    )
    return data
