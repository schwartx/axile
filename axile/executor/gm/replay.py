"""掘金回放旁路：保留实际源观察，不借用交易转换的缺省值。"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from axile.common.gm_symbols import GM_SYMBOL_RESOLVER
from axile.executor import replay as recording
from axile.executor.quote_quality import quote_observation

_MISSING = object()
_NON_TRADE_EXEC_TYPES = frozenset({"1", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "18", "19"})
_STATUS = {
    "1": "working",
    "2": "partially_filled",
    "3": "filled",
    "5": "canceled",
    "8": "rejected",
    "10": "pending",
    "12": "expired",
    "15": "trigger_wait",
    "16": "activated",
}
_IDENTITY = ("cl_ord_id", "order_id", "ex_ord_id", "exec_id")
_ATTRIBUTES = (
    "side",
    "status",
    "order_type",
    "order_style",
    "order_business",
    "position_effect",
    "position_side",
    "ord_rej_reason",
    "ord_rej_reason_detail",
    "order_duration",
    "order_qualifier",
    "stop_price",
    "filled_amount",
    "filled_vwap",
    "exec_type",
    "commission",
    "cost",
    "vwap",
    "vwap_diluted",
    "vwap_open",
    "market_value",
    "available",
    "available_now",
    "available_today",
    "order_frozen",
    "order_frozen_today",
    "target_volume",
    "target_value",
    "target_percent",
    "price",
    "volume",
)


class Fields:
    """仅提取有限数值及有界原生标量，同时保存字段质量。"""

    def __init__(self, row: object) -> None:
        self.row = row
        self.quality: dict[str, str] = {}

    def value(self, name: str) -> object:
        """读取字典及 SDK 对象共有字段，不调用业务 getter。"""
        value = self.row.get(name) if isinstance(self.row, Mapping) else getattr(self.row, name, None)
        self.quality[name] = "missing" if value is None or value == "" else "known"
        return value

    def scalar(self, name: str) -> str | float | int | None:
        """不保存 SDK 对象、非有限数字、布尔枚举及超长文本。"""
        value = self.value(name)
        if value is None or value == "":
            return None
        if isinstance(value, str) and len(value) <= 512:
            return value
        if type(value) in {int, float} and math.isfinite(value) and abs(value) <= 2**53 - 1:
            return value
        self.quality[name] = "invalid"
        return None

    def text(self, name: str) -> str | None:
        """将确知的原生标识转换为文本。"""
        value = self.scalar(name)
        return str(value) if value is not None else None

    def number(self, name: str) -> float | None:
        """读取有限数值，合法零量不当成缺失。"""
        value = self.value(name)
        if value is None or value == "":
            return None
        try:
            result = float(value)
        except (TypeError, ValueError, OverflowError):
            result = math.inf
        if isinstance(value, bool) or not math.isfinite(result) or abs(result) > 2**53 - 1:
            self.quality[name] = "invalid"
            return None
        return result

    def enum(self, name: str, choices: Mapping[str, str]) -> str | None:
        """不把未知方向、状态或业务类型映射为默认值。"""
        key = self.text(name)
        if key is not None and key not in choices:
            self.quality[name] = "invalid"
        return choices.get(key) if key is not None else None


def capture(owner: object, topic: str, build: Callable[[], dict[str, object]], *, receipt: object = _MISSING) -> None:
    """规范化与交付均隔离于交易路径，并沿用入口录制身份。"""
    if receipt is _MISSING:
        receipt = recording.capture_receipt(owner)
    if not isinstance(receipt, recording.ReplayReceipt):
        return
    try:
        data = build()
        if topic == "/trade" and data.get("execution_type") == "report":
            topic = "/command"
            data.update(operation="execution_report", phase="native_report", command_id=None)
        recording.observe_receipt(receipt, topic, data)
    except Exception:  # noqa: BLE001 - 旁路失败不能改变 SDK 返回或交易异常
        recording.observe_receipt(
            receipt,
            "/connection",
            {
                "source": "gm.replay",
                "available": False,
                "connected": False,
                "synchronized": False,
                "reason": "normalization_failed",
            },
        )


def _base(f: Fields, source: str) -> dict[str, object]:
    return {
        "source": source,
        "field_quality": f.quality,
        "native_identity": {key: value for key in _IDENTITY if (value := f.text(key)) is not None},
        "attributes": {key: value for key in _ATTRIBUTES if (value := f.scalar(key)) is not None},
    }


def _canonical_quality(data: dict[str, object], *fields: str) -> None:
    """通用字段的质量随转换结果更新，原生同名字段的缺失不能遮蔽别名值。"""
    quality = data["field_quality"]
    for name in fields:
        if name in quality:
            quality.setdefault(f"native_{name}", quality[name])
        quality[name] = "known" if data.get(name) is not None else "missing"


def _symbol(f: Fields) -> str | None:
    value = f.text("symbol")
    return GM_SYMBOL_RESOLVER.to_axile(value) if value else None


def symbol(row: object) -> str:
    """返回可用于行情 topic 的代码，缺失时不造默认品种。"""
    return _symbol(Fields(row)) or ""


def _time(f: Fields, name: str) -> int | None:
    value = f.value(name)
    try:
        moment = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(moment, datetime) or moment.tzinfo is None:
            raise ValueError("missing_timezone")
        delta = moment.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
        return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    except (TypeError, ValueError, OverflowError):
        if value is not None:
            f.quality[name] = "invalid"
        return None


def order(row: object, *, source: str = "gm.order_return") -> dict[str, object]:
    """在统一订单转换前保存订单状态及真实原生身份。"""
    f = Fields(row)
    data = _base(f, source)
    status = f.enum("status", _STATUS) or "unknown"
    quantity, filled = f.number("volume"), f.number("filled_volume")
    data.update(
        symbol=_symbol(f),
        order_id=f.text("cl_ord_id") or f.text("order_id"),
        direction=f.enum("side", {"1": "BUY", "2": "SELL"}),
        order_type=f.enum("order_type", {"1": "LIMIT", "2": "MARKET"}),
        offset=f.enum("position_effect", {"1": "OPEN", "2": "CLOSE", "3": "CLOSETODAY", "4": "CLOSEYESTERDAY"}),
        status=status,
        price=f.number("price"),
        quantity=quantity,
        filled_quantity=filled,
        remaining_quantity=max(0.0, quantity - filled) if quantity is not None and filled is not None else None,
        average_price=f.number("filled_vwap"),
        confirmed_book=(
            status in {"working", "partially_filled"}
            and f.text("order_type") == "1"
            and f.number("stop_price") in {None, 0}
            and f.quality.get("stop_price") != "invalid"
        )
        if status != "unknown"
        else None,
        source_time_us=_time(f, "updated_at"),
        create_time_us=_time(f, "created_at"),
        trigger_price=f.number("stop_price"),
    )
    _canonical_quality(data, "order_id")
    return data


def trade(owner: object, row: object, *, source: str = "gm.execution_report") -> dict[str, object]:
    """缺少可靠成交号时保留弱身份，不使用合成 ID 强去重。"""
    f = Fields(row)
    data = _base(f, source)
    identity, symbol = f.text("exec_id"), _symbol(f)
    account = getattr(owner, "_replay_account_id", None)
    order_id = f.text("cl_ord_id") or f.text("order_id")
    exec_type = f.text("exec_type")
    physical_trade = exec_type == "15"
    stable = physical_trade and bool(identity and symbol and type(account) is int)
    data.update(
        symbol=symbol,
        trade_id=identity,
        trade_key=f"gm:{account}:{symbol}:{identity}" if stable else None,
        identity_quality="stable" if stable else "weak",
        order_id=order_id,
        association="confirmed" if order_id else "pending",
        direction=f.enum("side", {"1": "BUY", "2": "SELL"}),
        offset=f.enum("position_effect", {"1": "OPEN", "2": "CLOSE", "3": "CLOSETODAY", "4": "CLOSEYESTERDAY"}),
        price=f.number("price") if physical_trade else None,
        quantity=f.number("volume") if physical_trade else None,
        execution_type="trade" if physical_trade else "report" if exec_type in _NON_TRADE_EXEC_TYPES else "unknown",
        source_time_us=_time(f, "created_at"),
    )
    _canonical_quality(data, "order_id")
    return data


def quote(row: object, *, source: str = "gm.tick") -> dict[str, object]:
    """保存真实档位与采样时间，不把补零或最近量变成连续市场成交。"""
    f = Fields(row)
    data = _base(f, source)
    quotes = f.value("quotes")
    sides: dict[str, list[dict[str, float]]] = {"bids": [], "asks": []}
    for label, native in (("bids", "bid"), ("asks", "ask")):
        for row_quote in quotes if isinstance(quotes, list | tuple) else []:
            level = Fields(row_quote)
            price, quantity = level.number(f"{native}_p"), level.number(f"{native}_v")
            if price is not None and price > 0 and quantity is not None and quantity > 0:
                sides[label].append({"price": price, "quantity": quantity})
            else:
                break
    bids, asks = sides["bids"], sides["asks"]
    source_time = _time(f, "created_at")
    data.update(
        symbol=_symbol(f),
        source_time_us=source_time,
        **sides,
        source_precision="microsecond",
        delivery_method="callback" if source == "gm.tick" else "query",
        available_depth={"bid": len(bids), "ask": len(asks)},
        depth_coverage={"kind": "top_n", "bid": len(bids), "ask": len(asks), "unlisted": "unknown"},
        **quote_observation(bids, asks),
        last_price=f.number("price"),
        last_price_kind="trade" if f.quality.get("price") == "known" else "unknown",
        volume=f.number("last_volume"),
        volume_kind="last",
    )
    _canonical_quality(data, "volume")
    return data


def _position(row: object, source: str) -> dict[str, object]:
    f = Fields(row)
    data = _base(f, source)
    available_field = "available_now" if f.value("available_now") is not None else "available"
    data.update(
        symbol=_symbol(f),
        position_side=f.enum("side", {"1": "LONG", "2": "SHORT"}),
        quantity=f.number("volume"),
        today_quantity=f.number("volume_today"),
        available_quantity=f.number(available_field),
        frozen_quantity=f.number("order_frozen"),
        position_basis={"status": "unverified", "proof": None},
        source_time_us=_time(f, "updated_at"),
    )
    _canonical_quality(data, "position_side")
    return data


def account_observation(
    cash: object, positions: list[object] | None, *, source: str = "gm.query_account"
) -> dict[str, object]:
    """采正常账户查询，保留可卖与冻结变化但不承诺成交原子对齐。"""
    f = Fields(cash)
    data = _base(f, source)
    rows = [_position(row, source) for row in positions or []]
    data.update(
        positions=rows,
        scope={"kind": "account", "symbols": []},
        scope_complete=positions is not None
        and all(row["symbol"] and row["position_side"] and row["quantity"] is not None for row in rows),
        position_basis={"status": "unverified", "proof": None},
        assets={"balance": f.number("nav"), "available_cash": f.number("available"), "frozen_margin": None},
    )
    return data


def command(
    operation: str,
    kwargs: Mapping[str, object],
    *,
    phase: str,
    command_id: str | None,
    result: object = None,
    error_type: str | None = None,
) -> dict[str, object]:
    """只保存实际报撤参数的允许字段，不复制账户、token 或 SDK 对象。"""
    data = order(kwargs, source="gm.native_request")
    params = {
        key: value
        for key in ("symbol", "volume", "side", "order_type", "position_effect", "price")
        if (value := Fields(kwargs).scalar(key)) is not None
    }
    cancel_rows = kwargs.get("wait_cancel_orders")
    if isinstance(cancel_rows, list):
        params["wait_cancel_orders"] = [
            {key: value for key in ("cl_ord_id", "order_id") if (value := Fields(row).text(key)) is not None}
            for row in cancel_rows
        ]
        if len(cancel_rows) == 1:
            data["order_id"] = Fields(cancel_rows[0]).text("cl_ord_id") or Fields(cancel_rows[0]).text("order_id")
    data.update(
        operation="place_order" if operation == "order_volume" else "cancel_order",
        phase=phase,
        command_id=command_id,
        actual_parameters=params,
        error_type=error_type,
    )
    rows = result if isinstance(result, list | tuple) else []
    if rows:
        f = Fields(rows[0])
        data["order_id"] = f.text("cl_ord_id") or f.text("order_id")
    _canonical_quality(data, "order_id")
    return data


def order_set(rows: list[object], token: dict[str, object] | None, *, synchronized: bool) -> dict[str, object]:
    """完整集合的缺席判断必须有查询发起前的覆盖证明。"""
    return {
        "source": "gm.query_orders",
        "scope": {"kind": "account", "symbols": []},
        "scope_complete": token is not None,
        "synchronized": synchronized,
        "sync_generation": token.get("sync_generation") if token else None,
        "query_basis": token.get("query_basis") if token else None,
        "orders": [order(row, source="gm.query_orders") for row in rows],
    }
