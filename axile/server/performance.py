"""Live observation and equity helpers.

The legacy build_wbt_input/calculate_performance API is retired; production
performance is computed by the native analysis worker, not a Python fallback.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from zoneinfo import ZoneInfo

from axile.server.asset_observations import is_asset_observation
from axile.server.db.models.performance import PerformancePoint

_TIMEZONE = ZoneInfo("Asia/Shanghai")


@dataclass
class Observation:
    """一条去重后的执行快照，仅包含绩效计算所需数据."""

    id: int
    execution_id: str | None
    time: datetime
    asset: float | None
    target: dict[str, float] | None
    prices: dict[str, float]
    target_weight: dict[str, float] | None = None
    sizing_fallback: bool = False
    target_units: dict[str, float] | None = None
    target_quantities: dict[str, float] | None = None


def local_time(value: str) -> datetime:
    """将有时区和历史无时区时间统一为上海本地时间."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(_TIMEZONE).replace(tzinfo=None)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _mapping(value: object) -> dict[str, object]:
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def _mark_price(raw_tick: object) -> float | None:
    """盯市价与柜台对齐取最新价；盘口无效或无最新价回退买卖中间价."""
    tick = _mapping(raw_tick)
    if tick.get("book_valid") is False:
        return None
    last = _number(tick.get("last_price"))
    if last is not None and last > 0:
        return last
    bid, ask = tick.get("bid_price"), tick.get("ask_price")
    bid = _number(bid[0] if isinstance(bid, list) and bid else bid)
    ask = _number(ask[0] if isinstance(ask, list) and ask else ask)
    if bid is None or ask is None or bid <= 0 or ask < bid:
        return None
    return (bid + ask) / 2


def _prices(result: dict[str, object]) -> dict[str, float]:
    raw = result.get("first_ticks", {})
    ticks = dict(_mapping(raw))
    if isinstance(raw, list):
        for value in cast("list[object]", raw):
            tick = _mapping(value)
            symbol = tick.get("symbol")
            if isinstance(symbol, str):
                ticks[symbol] = tick
    for symbol, value in _mapping(result.get("symbol_results")).items():
        item = _mapping(value)
        if item.get("first_tick") is not None:
            ticks[symbol] = item["first_tick"]
    return {
        symbol: price
        for symbol, tick in ticks.items()
        if isinstance(symbol, str) and (price := _mark_price(tick)) is not None
    }


def _target(raw: object) -> dict[str, float] | None:
    if not isinstance(raw, dict):
        return None
    target: dict[str, float] = {}
    for symbol, value in _mapping(raw).items():
        weight = _number(value)
        if not isinstance(symbol, str) or not symbol or weight is None:
            return None
        target[symbol] = weight
    return target


def _executable_weight(sizing: dict[str, object]) -> float | None:
    """从单条权重口径换算证据推导可执行权重；证据不完整时返回 None."""
    if str(sizing.get("sizing_mode", "weight")) != "weight" or sizing.get("status") != "SIZED":
        return None
    quantity, notional = _number(sizing.get("target_quantity")), _number(sizing.get("unit_notional"))
    equity = _number(sizing.get("equity"))
    if equity is None or equity <= 0 or quantity is None:
        return None
    if quantity == 0:
        # ZERO_TARGET、不足一手（BELOW_MIN_QUANTITY）或取整到 0：账户真实敞口为零。
        return 0.0
    if notional is None or notional <= 0:
        return None
    return quantity * notional / equity


def _executable_target(result: dict[str, object]) -> dict[str, float] | None:
    """全部品种均有可推导换算证据时返回可执行权重；任一品种证据不足返回 None."""
    results = _mapping(result.get("symbol_results"))
    if not results:
        return None
    target: dict[str, float] = {}
    for symbol, value in results.items():
        weight = _executable_weight(_mapping(_mapping(value).get("sizing")))
        if weight is None or not isinstance(symbol, str) or not symbol:
            return None
        target[symbol] = weight
    return target


def _executable_units(result: dict[str, object]) -> dict[str, float] | None:
    """保存目标数量乘账户金额乘数；缺证据时整条回退，不能从实际权益推导。"""
    results = _mapping(result.get("symbol_results"))
    if not results:
        return None
    units: dict[str, float] = {}
    for symbol, raw in results.items():
        sizing = _mapping(_mapping(raw).get("sizing"))
        if _executable_weight(sizing) is None:
            return None
        quantity = _number(sizing.get("target_quantity"))
        if quantity == 0:
            units[symbol] = 0.0
            continue
        multiplier = _price_multiplier(sizing)
        if quantity is None or multiplier is None:
            return None
        units[symbol] = quantity * multiplier
    return units


def _price_multiplier(sizing: dict[str, object]) -> float | None:
    """从冻结的名义价值/参考价还原账户金额乘数，兼容已换汇的渠道。"""
    notional = _number(sizing.get("unit_notional"))
    price = _number(sizing.get("reference_price"))
    if notional is not None and notional > 0 and price is not None and price > 0:
        return notional / price
    multiplier = _number(sizing.get("unit_multiplier"))
    conversion = sizing.get("value_conversion")
    rate = _number(_mapping(conversion).get("rate")) if conversion is not None else 1.0
    if multiplier is None or multiplier <= 0 or rate is None or rate <= 0:
        return None
    return multiplier * rate


def observation(
    record,
    fallback_weights: dict[str, float] | None = None,
) -> Observation:
    """兼容历史行情结构；完整数量证据进入账本，快照仅补缺目标。"""
    result = _mapping(record.result)
    assets = _mapping(result.get("account_assets"))
    asset = _number(assets.get("total_asset"))
    if not is_asset_observation(assets, result):
        asset = None
    target_weight = _target(_mapping(record.input).get("curr_target"))
    if target_weight is None:
        target_weight = _target(fallback_weights)
    executable = _executable_target(result)
    units = _executable_units(result)
    target = executable if units is not None else target_weight
    if result.get("execution_kind") == "clear_positions":
        target = target_weight = {}
        units = {}
    quantities = (
        {
            symbol: cast("float", _number(_mapping(_mapping(raw).get("sizing")).get("target_quantity")))
            for symbol, raw in _mapping(result.get("symbol_results")).items()
            if symbol in units
        }
        if units is not None
        else None
    )
    return Observation(
        record.id or 0,
        record.execution_id,
        local_time(record.created_at),
        asset,
        target,
        _prices(result),
        target_weight,
        units is None and target_weight is not None and bool(target_weight),
        units,
        quantities,
    )


def _point_fields(point: PerformancePoint | dict[str, object]) -> tuple[str, float | None]:
    if isinstance(point, dict):
        date = str(point.get("date") or "")
        raw = point.get("account_equity")
    else:
        date = point.date
        raw = point.account_equity
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return date, None
    value = float(raw)
    return date, value if math.isfinite(value) and value > 0 else None


def close_before(points: list[PerformancePoint] | list[dict[str, object]], day: str) -> float | None:
    """``day`` 之前最近一日末权益；没有更早日则用基准点。"""
    last_before = None
    baseline = None
    for point in points:
        date, equity = _point_fields(point)
        if equity is None:
            continue
        # 日点 date 是日历日；基准点 date 是完整时间，原生回放保留此契约。
        if "T" in date:
            baseline = equity
            continue
        if date < day:
            last_before = equity
    return last_before if last_before is not None else baseline
