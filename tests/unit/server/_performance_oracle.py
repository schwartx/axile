"""Retired Python performance oracle; test/benchmark only, not a supported API."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, cast

from axile.server.asset_observations import is_asset_observation
from axile.server.db.models.performance import (
    AccountPerformance,
    PerformanceCalendar,
    PerformanceCalendarRange,
    PerformanceFallbackRange,
    PerformancePoint,
    PerformanceSettings,
    PerformanceSkips,
    RangeKey,
)
from axile.server.performance import local_time
from axile.server.performance_analysis import ENGINE_VERSION
from axile.server.trading_calendar import CalendarDecisionStatus, evaluate_channel_calendar_day

if TYPE_CHECKING:
    import pandas as pd


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


def _calendar_ranges(days: list[date]) -> list[PerformanceCalendarRange]:
    """把已排序自然日合并为连续闭区间。"""
    if not days:
        return []
    ranges: list[PerformanceCalendarRange] = []
    start = previous = days[0]
    for day in days[1:]:
        if day != previous + timedelta(days=1):
            ranges.append(PerformanceCalendarRange(start=start.isoformat(), end=previous.isoformat()))
            start = day
        previous = day
    ranges.append(PerformanceCalendarRange(start=start.isoformat(), end=previous.isoformat()))
    return ranges


def performance_calendar(channel: str | None, baseline: str | None, end: str | None) -> PerformanceCalendar:
    """构造严格裁剪到绩效结果区间的渠道日历事实。"""
    if channel is None or baseline is None or end is None:
        return PerformanceCalendar(status="unavailable")
    first, last = local_time(baseline).date(), local_time(end).date()
    closed: list[date] = []
    unavailable: list[date] = []
    calendar_id = label = None
    current = first
    try:
        while current <= last:
            # get_channel 对未注册渠道抛 KeyError；这里按缺日历降级而不是上抛。
            decision = evaluate_channel_calendar_day(channel, current)
            calendar_id = decision.calendar_id or calendar_id
            label = decision.label or label
            if decision.status is CalendarDecisionStatus.NOT_REQUIRED:
                return PerformanceCalendar(status="not_required")
            if decision.status is CalendarDecisionStatus.AVAILABLE_CLOSED:
                closed.append(current)
            elif decision.status is CalendarDecisionStatus.UNAVAILABLE:
                unavailable.append(current)
            current += timedelta(days=1)
    except KeyError:
        return PerformanceCalendar(status="unavailable")
    status = (
        "partial"
        if unavailable and len(unavailable) <= (last - first).days
        else "unavailable"
        if unavailable
        else "available"
    )
    return PerformanceCalendar(
        status=status,
        calendar_id=calendar_id,
        label=label,
        closed_ranges=_calendar_ranges(closed),
        unavailable_ranges=_calendar_ranges(unavailable),
    )


@dataclass(frozen=True)
class ReplayClose:
    """相同初始资金下的模拟日末累计收益和日收益。"""

    cumulative_return: float
    daily_return: float | None


@dataclass(frozen=True)
class Holding:
    """价格到账户金额的敏感度与原始数量分离，换汇变化不产生虚假成交。"""

    units: float
    quantity: float | None = None


def _positions(item: Observation, equity: float, target_weight: bool) -> dict[str, Holding]:
    """优先保留历史目标数量的价格敏感度；权重回放仅使用模拟权益。"""
    if not target_weight and item.target_units is not None:
        return {
            symbol: Holding(units, (item.target_quantities or {}).get(symbol))
            for symbol, units in item.target_units.items()
            if units != 0
        }
    weights = (item.target_weight if target_weight else item.target) or {}
    if equity <= 0 and any(weights.values()):
        raise ValueError("模拟权益非正，无法继续按权重调仓")
    return {symbol: Holding(weight * equity / item.prices[symbol]) for symbol, weight in weights.items() if weight != 0}


def _turnover(old: Holding, new: Holding, price: float) -> float:
    """有原始数量时按数量变化收费；权重回放按模拟名义敞口变化收费。"""
    if old.quantity is not None and new.quantity is not None:
        multiplier = new.units / new.quantity if new.quantity else old.units / old.quantity if old.quantity else 0.0
        return abs(new.quantity - old.quantity) * multiplier * price
    return abs(new.units - old.units) * price


def replay_daily(
    items: list[Observation], initial_equity: float, fee_rate: float, target_weight: bool = False
) -> dict[str, ReplayClose]:
    """先结算旧仓、再应用新目标，费用按实际换手名义金额计入独立账本。

    历史目标数量是理论调仓，不代表真实成交。首个观测作为已建仓的共同基准，
    不扣基准前的建仓费；其后增减仓、反手和换月均按双边数量变化扣费。
    缺失观测由参与筛选跳过，恢复时按上次有效持仓一次结算跨缺口盈亏。
    """
    if not items:
        return {}
    equity = initial_equity
    previous = items[0]
    positions = _positions(previous, equity, target_weight)
    closes = {previous.time.date().isoformat(): equity}
    for item in items[1:]:
        equity += sum(
            holding.units * (item.prices[symbol] - previous.prices[symbol]) for symbol, holding in positions.items()
        )
        target = _positions(item, equity, target_weight)
        turnover = sum(
            _turnover(positions.get(symbol, Holding(0, 0)), target.get(symbol, Holding(0, 0)), item.prices[symbol])
            for symbol in positions.keys() | target.keys()
        )
        equity -= fee_rate * turnover
        positions = target
        previous = item
        closes[item.time.date().isoformat()] = equity
    result = {}
    previous_close = initial_equity
    for day, close in closes.items():
        result[day] = ReplayClose(
            cumulative_return=close / initial_equity - 1,
            daily_return=close / previous_close - 1 if previous_close > 0 else None,
        )
        previous_close = close
    return result


def _is_baseline(item: Observation) -> bool:
    return (
        item.asset is not None
        and item.asset > 0
        and item.target is not None
        and all(symbol in item.prices for symbol, weight in item.target.items() if weight != 0)
    )


def select_range(items: list[Observation], range_key: RangeKey) -> list[Observation]:
    """相同时刻取最后记录，并选择区间前的共同基准."""
    unique = {item.time: item for item in sorted(items, key=lambda item: (item.time, item.id))}
    ordered = list(unique.values())
    if not ordered:
        return []
    cutoff = ordered[0].time if range_key == "all" else ordered[-1].time - timedelta(days=int(range_key))
    before = [i for i, item in enumerate(ordered) if item.time <= cutoff and _is_baseline(item)]
    after = [i for i, item in enumerate(ordered) if item.time >= cutoff and _is_baseline(item)]
    if before:
        return ordered[before[-1] :]
    if after:
        return ordered[after[0] :]
    # 无共同基准时仍允许显示独立账户曲线。
    return [item for item in ordered if item.time >= cutoff]


def _required_symbols(item: Observation, previous: dict[str, float], target_weight: bool = False) -> set[str] | None:
    """返回参与回测所需的品种集合；目标缺失时返回 None."""
    target = item.target_weight if target_weight else item.target
    if target is None:
        return None
    required = {symbol for symbol, weight in previous.items() if weight != 0}
    required.update(symbol for symbol, weight in target.items() if weight != 0)
    return required


def participating_items(items: list[Observation]) -> list[Observation]:
    """返回可构建回测 bar 的观察序列；参与条件依赖延续中的上一目标."""
    participants: list[Observation] = []
    previous: dict[str, float] = {}
    for item in items:
        required = _required_symbols(item, previous)
        if required is not None and not (required - item.prices.keys()):
            participants.append(item)
            previous = item.target or {}
    return participants


@dataclass
class BacktestInput:
    """回放输入与参与统计；保留权重表供诊断，收益由独立账本计算."""

    frame: pd.DataFrame | None
    used: int
    skips: PerformanceSkips
    backtest_start: datetime | None
    participants: list[Observation]


def build_wbt_input(
    items: list[Observation], target_weight: bool = False, *, materialize_frame: bool = True
) -> BacktestInput:
    """跳过缺目标或缺盘口的观测；跳过记录的持仓延续，目标回放不等于真实成交."""
    rows: list[dict[str, object]] = []
    previous: dict[str, float] = {}
    used = 0
    backtest_start: datetime | None = None
    participants: list[Observation] = []
    missing_target = 0
    missing_ticks = 0
    first_time: str | None = None
    last_time: str | None = None
    for item in items:
        target = item.target_weight if target_weight else item.target
        required = _required_symbols(item, previous, target_weight)
        missing = required is not None and bool(required - item.prices.keys())
        if required is None or missing:
            # 跳过：不产生行、不更新延续目标，账本按上一已知持仓延续。
            missing_target += target is None
            missing_ticks += target is not None
            first_time = first_time or item.time.isoformat()
            last_time = item.time.isoformat()
            continue
        assert target is not None and required is not None
        # 旧仓必须写入显式零权重退出行；其余有报价的零仓也保留供 WBT 建立历史。
        if materialize_frame:
            symbols = required | (target.keys() & item.prices.keys())
            rows.extend(
                {"dt": item.time, "symbol": symbol, "weight": target.get(symbol, 0.0), "price": item.prices[symbol]}
                for symbol in sorted(symbols)
            )
        backtest_start = backtest_start or item.time
        previous = target
        used += 1
        participants.append(item)
    skipped = missing_target + missing_ticks
    frame = None
    if materialize_frame:
        # 写入回放事实和原生回放不需要 DataFrame，避免账户 worker 为此加载 pandas。
        import pandas as pd

        frame = pd.DataFrame(rows, columns=["dt", "symbol", "weight", "price"])
    return BacktestInput(
        frame=frame.astype({"weight": "float64", "price": "float64"}) if frame is not None else None,
        used=used,
        skips=PerformanceSkips(
            count=skipped,
            missing_target=missing_target,
            missing_ticks=missing_ticks,
            first_time=first_time if skipped else None,
            last_time=last_time if skipped else None,
        ),
        backtest_start=backtest_start,
        participants=participants,
    )


def _portfolio_daily(
    items: list[Observation], settings: PerformanceSettings, initial_equity: float, target_weight: bool = False
) -> dict[str, ReplayClose]:
    """使用独立权益账本生成日收益，保留统一的回放入口。"""
    return replay_daily(items, initial_equity, settings.backtest_fee_rate, target_weight)


def _replay_close(
    daily: dict[str, ReplayClose] | None, day: str, start: str | None, previous: float
) -> ReplayClose | None:
    """缺参与观测的日子延续上次估值；缺口已通过 skips 披露，恢复日结算。"""
    if daily is None or start is None or day < start:
        return None
    return daily.get(day, ReplayClose(previous, 0.0))


def _merge_daily(
    items: list[Observation],
    daily: dict[str, ReplayClose] | None,
    backtest_start: datetime | None,
    participants: list[Observation] | None = None,
    target_daily: dict[str, ReplayClose] | None = None,
    target_start: datetime | None = None,
    fallback_days: set[str] | None = None,
) -> list[PerformancePoint]:
    base = next((item for item in items if item.asset is not None and item.asset > 0), None)
    if base is None or base.asset is None:
        return []
    # 账户与组合优先在同一批参与记录上配对采样；无参与记录的日子保留账户观察，
    # 组合按持仓延续，闭市心跳不会单独移动账户日点。
    by_day: dict[str, Observation] = {}
    for item in items:
        if item.time >= base.time:
            by_day[item.time.date().isoformat()] = item
    for item in participants or ():
        if item.time >= base.time:
            by_day[item.time.date().isoformat()] = item
    portfolio = target_portfolio = 0.0
    previous_asset = base.asset
    points: list[PerformancePoint] = []
    # 回测首个参与观测之前组合收益未知；其后无参与观测的日子按持仓延续计 0.
    backtest_day = backtest_start.date().isoformat() if backtest_start is not None else None
    target_day = target_start.date().isoformat() if target_start is not None else None
    for day, last in by_day.items():
        # 日末资产缺失时不拿更早资产伪装同日末值；次日累计仍可由共同基准恢复。
        account = last.asset / base.asset - 1 if last.asset is not None else None
        account_daily = (
            last.asset / previous_asset - 1
            if last.asset is not None and previous_asset is not None and previous_asset > 0
            else None
        )
        previous_asset = last.asset
        portfolio_close = _replay_close(daily, day, backtest_day, portfolio)
        target_close = _replay_close(target_daily, day, target_day, target_portfolio)
        if portfolio_close is not None:
            portfolio = portfolio_close.cumulative_return
        if target_close is not None:
            target_portfolio = target_close.cumulative_return
        points.append(
            PerformancePoint(
                date=day,
                observed_at=last.time.isoformat(),
                record_id=last.id,
                execution_id=last.execution_id,
                account_return=account,
                account_equity=last.asset,
                portfolio_return=portfolio if portfolio_close is not None else None,
                account_daily_return=account_daily,
                portfolio_daily_return=portfolio_close.daily_return if portfolio_close is not None else None,
                target_portfolio_return=target_portfolio if target_close is not None else None,
                target_portfolio_daily_return=target_close.daily_return if target_close is not None else None,
                sizing_fallback=day in (fallback_days or set()),
                difference=account - portfolio if account is not None and portfolio_close is not None else None,
            )
        )
    # 独立基准点保留执行时间，以便基准当天仍有后续执行时不覆盖日末收益。
    from_backtest = daily is not None and backtest_start is not None and backtest_start <= base.time
    points.insert(
        0,
        PerformancePoint(
            date=base.time.isoformat(),
            observed_at=base.time.isoformat(),
            record_id=base.id,
            execution_id=base.execution_id,
            account_return=0.0,
            account_equity=base.asset,
            portfolio_return=0.0 if from_backtest else None,
            target_portfolio_return=0.0
            if target_daily is not None and target_start is not None and target_start <= base.time
            else None,
            difference=0.0 if from_backtest else None,
        ),
    )
    return points


def calculate_performance(
    observations: list[Observation],
    settings: PerformanceSettings,
    range_key: RangeKey,
    include_backtest: bool = True,
    trade_channel: str | None = None,
) -> AccountPerformance:
    """使用独立模拟权益与实际资产比值生成同口径收益对比。"""
    items = select_range(observations, range_key)
    result = AccountPerformance(
        settings=settings,
        backtest_included=include_backtest,
        engine_version=ENGINE_VERSION,
        range=range_key,
        record_count=len(observations),
        calendar=PerformanceCalendar(status="unavailable"),
    )
    if not items:
        return result
    result.observation_count = len(items)
    daily = None
    backtest_start: datetime | None = None
    participants: list[Observation] | None = None
    target_daily: dict[str, ReplayClose] | None = None
    target_start: datetime | None = None
    fallback_days: set[str] = set()
    if include_backtest:
        # 权益账本只消费参与记录和统计，不需要旧 WBT 权重表。
        built = build_wbt_input(items, materialize_frame=False)
        target_built = build_wbt_input(items, target_weight=True, materialize_frame=False)
        result.used_record_count = built.used
        result.target_used_record_count = target_built.used
        result.skips = built.skips
        result.target_skips = target_built.skips
        backtest_start = built.backtest_start
        target_start = target_built.backtest_start
        initial_equity = next((item.asset for item in items if item.asset is not None and item.asset > 0), None)
        daily = (
            _portfolio_daily(built.participants, settings, initial_equity)
            if built.used and initial_equity is not None
            else None
        )
        target_daily = (
            _portfolio_daily(target_built.participants, settings, initial_equity, target_weight=True)
            if target_built.used and initial_equity is not None
            else None
        )
        participants = built.participants or None
        fallback_days = {item.time.date().isoformat() for item in built.participants if item.sizing_fallback}
        result.sizing_fallback_count = sum(item.sizing_fallback for item in built.participants)
        current: PerformanceFallbackRange | None = None
        for item in built.participants:
            if item.sizing_fallback:
                when = item.time.isoformat()
                if current is None:
                    current = PerformanceFallbackRange(start=when, end=when, count=0)
                    result.sizing_fallback_ranges.append(current)
                current.end = when
                current.count += 1
            else:
                current = None
    else:
        # 快速路径用同一参与谓词配对采样，不构建也不运行回测。
        participants = participating_items(items) or None
    result.points = _merge_daily(items, daily, backtest_start, participants, target_daily, target_start, fallback_days)
    result.baseline = result.points[0].date if result.points else None
    result.end = items[-1].time.isoformat()
    result.invalid_asset_count = sum(item.asset is None for item in items)
    result.calendar = performance_calendar(trade_channel, result.baseline, result.end)
    return result
