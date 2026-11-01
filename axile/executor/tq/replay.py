"""天勤原始快照的回放旁路；不订阅、不查询、不补造 SDK 未交付的变化。"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import datetime
from zoneinfo import ZoneInfo

from axile.executor import replay as recording
from axile.executor.instrument_catalog_records import EXCHANGES, normalize_catalog_symbol

MISSING = object()
_IDENTITIES = ("order_id", "exchange_order_id", "trade_id", "exchange_trade_id", "exchange_id", "instrument_id")
_ORDER_ATTRIBUTES = (
    "status",
    "last_msg",
    "price_type",
    "time_condition",
    "volume_condition",
    "is_online",
    "is_dead",
    "is_error",
)


def number(row: Mapping[str, object], name: str) -> float | None:
    """仅接受有限原生数值，未知值不补零。"""
    value = row.get(name)
    return (
        float(value)
        if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
        else None
    )


def text(row: Mapping[str, object], name: str) -> str | None:
    """只保留有界文本，不记录 SDK 对象或配置。"""
    value = row.get(name)
    return value if isinstance(value, str) and 0 < len(value) <= 512 else None


def symbol(row: Mapping[str, object]) -> str | None:
    """复用统一期货代码语义，不触发按需目录查询。"""
    value = text(row, "instrument_id") or text(row, "symbol")
    if value and "." in value and value.split(".", 1)[0] in EXCHANGES:
        value = value.split(".", 1)[-1]
    return normalize_catalog_symbol(value) if value and "." not in value else value


def _time(value: object) -> int | None:
    if type(value) is int and value > 0:
        return value // 1000 if value // 1000 <= 9_007_199_254_740_991 else None
    if isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            return int(moment.timestamp() * 1_000_000)
        except (ValueError, OverflowError, OSError):
            pass
    return None


def _base(owner: object, row: Mapping[str, object], source: str) -> dict[str, object]:
    config = getattr(owner, "account_config", None)
    mode = getattr(config, "account_mode", None)
    return {
        "source": source,
        "symbol": symbol(row),
        "attributes": {"account_mode": mode} if mode in {"live", "kq", "sim"} else {},
        "native_identity": {key: value for key in _IDENTITIES if (value := text(row, key)) is not None},
        "field_quality": {},
    }


def capture(owner: object, topic: str, build: Callable[[], dict[str, object]], *, receipt: object = MISSING) -> None:
    """始终使用入口凭据，规范化失败不能改变交易结果。"""
    if receipt is MISSING:
        receipt = recording.capture_receipt(owner)
    if not isinstance(receipt, recording.ReplayReceipt):
        return
    try:
        recording.observe_receipt(receipt, topic, build())
    except Exception:  # noqa: BLE001 - 旁路不能影响 owner 事件泵或交易结果
        recording.observe_receipt(
            receipt, "/connection", {"source": "tq.replay", "available": False, "reason": "normalization_failed"}
        )


def begin_query(owner: object, receipt: recording.ReplayReceipt | None, scope: list[str]) -> dict[str, object] | None:
    """排队前的观察者已更换时，不向新录制借覆盖凭据。"""
    if receipt is None or recording.capture_receipt(owner) != receipt:
        return None
    token = recording.begin_query(owner, scope)
    if recording.capture_receipt(owner) != receipt:
        recording.finish_query(owner, token)
        return None
    return token


def event(
    owner: object,
    kind: str,
    row: dict[str, object],
    *,
    receipt: object = MISSING,
    token: dict[str, object] | None = None,
    synchronized: bool = False,
) -> None:
    """在 owner 交付业务监听器之前记录实际观察。"""
    if kind == "quote":
        name = symbol(row)
        if name:
            capture(owner, f"/quote/{name}", lambda: quote(owner, row), receipt=receipt)
    elif kind in {"order", "trade"}:
        build = order if kind == "order" else trade
        capture(owner, f"/{kind}", lambda: build(owner, row), receipt=receipt)
    elif kind == "orders.reset":
        capture(
            owner,
            "/order_set",
            lambda: order_set(owner, row["rows"], synchronized=synchronized, token=token),
            receipt=receipt,
        )
    elif kind == "connection":
        ready = row.get("ready") is True
        capture(
            owner,
            "/connection",
            lambda: {
                "source": "tq.connection",
                "connected": ready,
                "available": ready,
                "synchronized": ready,
                "reason": "synchronized" if ready else "disconnected",
            },
            receipt=receipt,
        )


def order(owner: object, row: Mapping[str, object], *, source: str = "tq.order_snapshot") -> dict[str, object]:
    """保留实际在线、结束与错误事实，缺失状态不推断为卖出或撤单。"""
    data = _base(owner, row, source)
    quantity, left = number(row, "volume_orign"), number(row, "volume_left")
    status = "unknown"
    if row.get("is_error") is True:
        status = "rejected"
    elif row.get("status") == "ALIVE":
        status = "partially_filled" if quantity is not None and left is not None and 0 < left < quantity else "working"
    elif row.get("status") == "FINISHED":
        if quantity is not None and quantity > 0 and left == 0:
            status = "filled"
        elif row.get("is_error") is False and left is not None and left > 0:
            status = "canceled"
    for name in _ORDER_ATTRIBUTES:
        value = row.get(name)
        if isinstance(value, str | bool) and (not isinstance(value, str) or len(value) <= 512):
            data["attributes"]["native_status" if name == "status" else name] = value
    confirmed = None
    if status in {"working", "partially_filled"} and type(row.get("is_online")) is bool:
        confirmed = row["is_online"]
    elif status in {"filled", "canceled", "rejected"}:
        confirmed = False
    data.update(
        order_id=text(row, "order_id"),
        direction=row.get("direction") if row.get("direction") in {"BUY", "SELL"} else None,
        offset=row.get("offset") if row.get("offset") in {"OPEN", "CLOSE", "CLOSETODAY"} else None,
        order_type={"LIMIT": "LIMIT", "ANY": "MARKET"}.get(row.get("price_type")),
        status=status,
        confirmed_book=confirmed,
        price=number(row, "limit_price"),
        quantity=quantity,
        remaining_quantity=left,
        filled_quantity=max(0.0, quantity - left) if quantity is not None and left is not None else None,
        source_time_us=_time(row.get("insert_date_time")),
        source_precision="nanosecond",
    )
    data["field_quality"] = {
        key: "known" if value is not None else "missing"
        for key, value in data.items()
        if key in {"direction", "offset", "price", "quantity", "remaining_quantity", "source_time_us"}
    }
    return data


def trade(owner: object, row: Mapping[str, object], *, source: str = "tq.trade_snapshot") -> dict[str, object]:
    """账户成交不是市场逐笔；无可靠身份时不强去重。"""
    data = _base(owner, row, source)
    tid = text(row, "trade_id")
    account = getattr(owner, "_replay_account_id", None)
    moment = _time(row.get("trade_date_time"))
    stable = tid is not None and type(account) is int
    data.update(
        order_id=text(row, "order_id"),
        trade_id=tid or text(row, "exchange_trade_id"),
        trade_key=f"tq:{account}:{tid}" if stable else None,
        identity_quality="stable" if stable else "weak",
        granularity="individual",
        direction=row.get("direction") if row.get("direction") in {"BUY", "SELL"} else None,
        offset=row.get("offset") if row.get("offset") in {"OPEN", "CLOSE", "CLOSETODAY"} else None,
        price=number(row, "price"),
        quantity=number(row, "volume"),
        source_time_us=moment,
        source_precision="nanosecond",
    )
    return data


def quote(owner: object, row: Mapping[str, object]) -> dict[str, object]:
    """仅记录实际有效档位，不把补零或累计量转成市场成交。"""
    data = _base(owner, row, "tq.quote_snapshot")
    sides = {}
    for side in ("bid", "ask"):
        levels = []
        for level in range(1, 6):
            price, volume = number(row, f"{side}_price{level}"), number(row, f"{side}_volume{level}")
            if price is None or price <= 0 or volume is None or volume < 0:
                break
            levels.append({"price": price, "quantity": volume})
        sides[side] = levels
    data.update(
        bids=sides["bid"],
        asks=sides["ask"],
        available_depth={"bid": len(sides["bid"]), "ask": len(sides["ask"])},
        depth_coverage={"kind": "top_n", "bid": len(sides["bid"]), "ask": len(sides["ask"]), "unlisted": "unknown"},
        book_valid=bool(sides["bid"] and sides["ask"] and sides["bid"][0]["price"] <= sides["ask"][0]["price"]),
        last_price=number(row, "last_price"),
        last_price_kind="trade",
        volume=number(row, "volume"),
        volume_kind="cumulative",
        turnover=number(row, "amount"),
        source_time_us=_time(row.get("datetime")),
        source_precision="nanosecond" if type(row.get("datetime")) is int else "microsecond",
        delivery_method="snapshot_diff",
        intermediate_states="observed_only",
    )
    return data


def order_set(
    owner: object, rows: list[dict[str, object]], *, synchronized: bool, token: dict[str, object] | None
) -> dict[str, object]:
    """完整集合必须携带 owner 冻结前取得的覆盖凭据。"""
    return {
        "source": "tq.order_set",
        "scope": {"kind": "account", "symbols": []},
        "scope_complete": token is not None and synchronized,
        "synchronized": synchronized,
        "sync_generation": token.get("sync_generation") if token else None,
        "query_basis": token.get("query_basis") if token else None,
        "orders": [order(owner, row, source="tq.order_set") for row in rows],
    }


def account(owner: object, assets: dict[str, object], positions: dict[str, dict[str, object]]) -> dict[str, object]:
    """保留正常查询的今昨与冻结数量，不从成交增量推算持仓。"""
    rows = []
    for full_symbol, native in positions.items():
        for side in ("long", "short"):
            row = {**native, "symbol": full_symbol}
            today, yesterday = number(row, f"pos_{side}_today"), number(row, f"pos_{side}_his")
            frozen = number(row, f"volume_{side}_frozen")
            total = today + yesterday if today is not None and yesterday is not None else None
            position = _base(owner, row, "tq.account_query")
            position.update(
                position_side=side.upper(),
                quantity=total,
                today_quantity=today,
                yesterday_quantity=yesterday,
                frozen_quantity=frozen,
                available_quantity=max(0.0, total - frozen) if total is not None and frozen is not None else None,
                position_basis={"status": "unverified", "proof": None},
            )
            rows.append(position)
    return {
        "source": "tq.account_query",
        "scope": {"kind": "account", "symbols": []},
        "scope_complete": all(row["symbol"] is not None and row["quantity"] is not None for row in rows),
        "positions": rows,
        "position_basis": {"status": "unverified", "proof": None},
        "assets": {
            "balance": number(assets, "balance"),
            "available_cash": number(assets, "available"),
            "frozen_margin": number(assets, "frozen_margin"),
        },
    }


def command(
    owner: object,
    receipt: recording.ReplayReceipt | None,
    command_id: str | None,
    operation: str,
    arguments: dict[str, object],
    invoke: Callable[[], object],
) -> object:
    """原生动作复用排队前的命令身份，返回不冒充交易所确认。"""
    base = {
        "source": "tq.native_request",
        "command_id": command_id,
        "operation": operation,
        "actual_parameters": arguments,
        "order_id": arguments.get("order_id"),
        "symbol": symbol(arguments),
    }
    recording.observe_receipt(receipt, "/command", {**base, "phase": "native_request"})
    try:
        result = invoke()
    except BaseException as error:
        recording.observe_receipt(
            receipt, "/command", {**base, "phase": "native_error", "error_type": type(error).__name__}
        )
        raise
    recording.observe_receipt(receipt, "/command", {**base, "phase": "native_returned"})
    return result
