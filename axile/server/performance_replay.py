"""以独立权益账本回放目标持仓，不把日内不同分母的收益率相加。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from axile.server.performance import Observation


ENGINE_VERSION = "equity-ledger-1"


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
