"""Execution cost projection, preserving the historical UI's unknown-value rules."""

import math
from collections import defaultdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from axile.executor.models.unified_account_assets import is_degraded_snapshot_source

SHANGHAI = ZoneInfo("Asia/Shanghai")


def timestamp(value: str) -> float:
    """Interpret historical naive timestamps as Shanghai time, in milliseconds."""
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=SHANGHAI).timestamp() * 1000 if parsed.tzinfo is None else parsed.timestamp() * 1000


def number(value: Any) -> float | None:
    """Match the UI's finite numeric string handling without treating bool as money."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except ValueError:
        return None


def positive(value: Any) -> float | None:
    """Return a strictly positive finite value."""
    result = number(value)
    return result if result is not None and result > 0 else None


def mapping(value: Any) -> dict:
    """Normalize optional historical objects."""
    return value if isinstance(value, dict) else {}


def sequence(value: Any) -> list:
    """Normalize optional historical arrays."""
    return value if isinstance(value, list) else []


def side_of(value: Any) -> str:
    """Only explicit buy/sell directions are known."""
    value = value.lower() if isinstance(value, str) else None
    return value if value in ("buy", "sell") else "none"


def _trade(raw: Any, symbol: str, result: dict, orders: dict, created_at: str) -> dict:
    trade = mapping(raw)
    extra, tick = mapping(trade.get("extra")), mapping(result.get("first_tick"))
    bid, ask = positive(tick.get("bid_price")), positive(tick.get("ask_price"))
    mid = (bid + ask) / 2 if bid is not None and ask is not None and ask >= bid else None
    reference = mid if mid is not None else positive(tick.get("last_price"))
    multiplier = positive(mapping(result.get("sizing")).get("unit_multiplier"))
    price, quantity = positive(trade.get("trade_price")), positive(trade.get("trade_volume"))
    side = orders.get(str(trade.get("order_id")), "none")
    if side == "none":
        side = side_of(extra.get("direction"))
    value = price * quantity * multiplier if price and quantity and multiplier else None
    signed_difference = (
        (price - reference) * (1 if side == "buy" else -1) if price and reference and side != "none" else None
    )
    try:
        time = timestamp(trade["trade_time"])
        estimated = False
    except (KeyError, ValueError, TypeError):
        time, estimated = timestamp(created_at), True
    currency = extra.get("commission_asset")
    return {
        "symbol": symbol,
        "day": datetime.fromtimestamp(time / 1000, SHANGHAI).date().isoformat(),
        "time": time,
        "timeEstimated": estimated,
        "side": side,
        "quantity": quantity,
        "price": price,
        "reference": reference,
        "referenceSource": "mid" if mid is not None else "last" if reference else None,
        "value": value,
        "cost": signed_difference * quantity * multiplier
        if quantity is not None and multiplier is not None and signed_difference is not None
        else None,
        "lossBp": signed_difference / reference * 1e4
        if signed_difference is not None and reference is not None
        else None,
        "fee": number(extra.get("commission")),
        "feeCurrency": currency if isinstance(currency, str) and currency.strip() else None,
    }


def execution_list_payload(payload: dict) -> dict:
    """裁剪新旧绩效投影的逐单证据，只保留列表状态、结论与统计。"""
    record = mapping(payload.get("record"))
    raw = mapping(record.get("raw_result"))
    results = mapping(raw.get("symbol_results"))
    compact = {
        key: raw[key]
        for key in (
            "status",
            "task_status",
            "error",
            "outcome",
            "outcome_reason",
            "reason_code",
            "msg",
            "execution_kind",
        )
        if key in raw
    }
    if isinstance(raw.get("symbol_results"), dict):
        compact["symbol_results"] = {
            symbol: {
                key: result[key]
                for key in ("status", "error", "outcome", "outcome_reason", "reason_code")
                if key in result
            }
            for symbol, value in results.items()
            for result in [mapping(value)]
        }
    trade_count = payload.get("tradeCount")
    if trade_count is None and any(isinstance(mapping(value).get("trades"), list) for value in results.values()):
        trade_count = sum(len(sequence(mapping(value).get("trades"))) for value in results.values())
    return {**payload, "record": {**record, "raw_result": compact}, "tradeCount": trade_count}


def project_execution(record) -> tuple[dict, list[dict]]:
    """Project one explicitly supplied canonical result into compact analysis facts."""
    raw = mapping(record.result)
    results = {symbol: mapping(value) for symbol, value in mapping(raw.get("symbol_results")).items()}
    trades = []
    has_fill = False
    for symbol, result in results.items():
        orders = {
            str(mapping(order).get("order_id")): side_of(mapping(order).get("direction"))
            for order in sequence(result.get("orders"))
        }
        trades.extend(
            _trade(trade, symbol, result, orders, record.created_at) for trade in sequence(result.get("trades"))
        )
        has_fill |= any(
            (number(mapping(order).get("filled_volume")) or 0) > 0 for order in sequence(result.get("orders"))
        )
    noop = (
        record.is_success == 1
        and not trades
        and not has_fill
        and (
            raw.get("status") == "NOOP"
            or bool(results)
            and all(result.get("status") == "NOOP" for result in results.values())
        )
    )
    raw = dict(raw)
    duration = number(raw.get("execution_time"))
    payload = {
        "key": str(record.id),
        "record": {
            "id": record.id,
            "execution_id": record.execution_id,
            "created_at": record.created_at,
            "is_success": record.is_success,
            "raw_result": {
                key: raw[key]
                for key in (
                    "status",
                    "task_status",
                    "error",
                    "outcome",
                    "outcome_reason",
                    "reason_code",
                    "msg",
                    "execution_kind",
                    "symbol_results",
                )
                if key in raw
            },
        },
        "noop": noop,
        "tradeCount": len(trades),
        "durationSec": duration if duration is not None and duration >= 0 else None,
        "symbolCount": len({trade["symbol"] for trade in trades if trade["quantity"] is not None}),
        "positionCount": position_count(raw.get("account_assets")),
        "positions": position_preview(raw.get("account_assets")),
        "transactions": transaction_preview(trades, results),
        "reason": str(raw.get("error") or "")[:300],
        "attempts": attempted_trades(results)
        if record.is_success != 1 or raw.get("status") == "PARTIAL" or raw.get("task_status") == "TERMINATED"
        else [],
    }
    return execution_list_payload(payload), trades


def attempted_trades(results: dict) -> list[dict]:
    """Keep planned and filled net quantities for failed or partial executions."""
    output = []
    for symbol, result in results.items():
        sizing = mapping(result.get("sizing"))
        before, target = number(sizing.get("current_quantity")), number(sizing.get("target_quantity"))
        orders = result.get("orders")
        filled = []
        for raw in sequence(orders):
            order = mapping(raw)
            quantity, side = number(order.get("filled_volume")), side_of(order.get("direction"))
            filled.append(quantity * (1 if side == "buy" else -1) if quantity is not None and side != "none" else None)
        output.append(
            {
                "symbol": symbol,
                "planned": target - before if target is not None and before is not None else None,
                "filled": sum(filled) if isinstance(orders, list) and all(n is not None for n in filled) else None,
                "reason": str(result.get("error") or "")[:200],
            }
        )
    return output


def position_preview(value: Any) -> list[dict] | None:
    """Keep explicit positions for chart inspection; unavailable is not flat."""
    assets = mapping(value)
    if position_count(value) is None:
        return None
    return [
        {
            "symbol": str(position.get("symbol", "")),
            "quantity": number(position.get("volume")),
            "direction": str(position.get("direction", "")),
        }
        for raw in assets["positions"]
        if (position := mapping(raw)) and number(position.get("volume")) != 0
    ]


def transaction_preview(trades: list[dict], results: dict) -> list[dict]:
    """Aggregate each symbol and side separately, keeping real fill times and unknowns."""
    groups = defaultdict(list)
    for trade in trades:
        if trade["quantity"] is not None:
            groups[(trade["symbol"], trade["side"])].append(trade)
    output = []
    for (symbol, side), fills in groups.items():
        quantity = sum(fill["quantity"] for fill in fills)
        priced = all(fill["price"] is not None for fill in fills)
        refs = {fill["reference"] for fill in fills}
        sizing = mapping(results.get(symbol, {}).get("sizing"))
        output.append(
            {
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "price": sum(fill["quantity"] * fill["price"] for fill in fills) / quantity if priced else None,
                "reference": next(iter(refs)) if len(refs) == 1 else None,
                "referenceSource": fills[0]["referenceSource"],
                "time": min(fill["time"] for fill in fills),
                "endTime": max(fill["time"] for fill in fills),
                "timeEstimated": any(fill["timeEstimated"] for fill in fills),
                "before": number(sizing.get("current_quantity")),
                "target": number(sizing.get("target_quantity")),
                "summary": summarize(fills),
            }
        )
    return output


def position_count(value: Any) -> int | None:
    """Unknown or degraded snapshots must never be classified as flat."""
    assets = mapping(value)
    positions = assets.get("positions")
    if is_degraded_snapshot_source(assets.get("source")) or not isinstance(positions, list):
        return None
    quantities = [number(mapping(position).get("volume")) for position in positions]
    if any(quantity is None for quantity in quantities):
        return None
    return sum(quantity != 0 for quantity in quantities)


def summarize(trades: list[dict]) -> dict:
    """Use amount-weighted loss BP, separate fee currencies and partial coverage."""
    valued = [trade for trade in trades if trade["value"] is not None]
    valid = [trade for trade in valued if trade["cost"] is not None]
    value = sum(trade["value"] for trade in valued)
    covered_value = sum(trade["value"] for trade in valid)
    fees: dict[str, float] = defaultdict(float)
    fee_covered = 0
    for trade in trades:
        if trade["fee"] is not None and trade["feeCurrency"]:
            fees[trade["feeCurrency"]] += trade["fee"]
            fee_covered += 1
    return {
        "value": value if valued else None,
        "cost": sum(trade["cost"] for trade in valid) if valid else None,
        "lossBp": sum(trade["lossBp"] * trade["value"] for trade in valid) / covered_value
        if covered_value > 0
        else None,
        "coverage": covered_value / value if len(valued) == len(trades) and value > 0 else None,
        "covered": len(valid),
        "count": len(trades),
        "amountComplete": len(valued) == len(trades),
        "fees": dict(fees),
        "feeCovered": fee_covered,
    }


def daily_costs(trades: list[dict]) -> dict[str, dict]:
    """Group trades by their actual Shanghai date, including cross-day fills."""
    groups = defaultdict(list)
    for trade in trades:
        groups[trade["day"]].append(trade)
    return {day: summarize(group) for day, group in groups.items()}
