"""以已记录的目标数量对账账户持仓，不重新换算目标。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from axile.channels.contracts import CanonicalizeSymbol

_QTY_EPS = 1e-9


def _is_short(direction: object) -> bool:
    """判断持仓方向是否为空头（兼容中英文写法）。"""
    return isinstance(direction, str) and ("空" in direction or "short" in direction.lower())


def _signed_lots(row: Mapping[str, object]) -> float | None:
    """读取净仓数量；缺失或无效时不推定为空仓。"""
    extra = row.get("extra")
    net = extra.get("net_position") if isinstance(extra, dict) else None
    value = net if net is not None else row.get("volume")
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        quantity = float(value)
    except ValueError:
        return None
    if not math.isfinite(quantity):
        return None
    if net is not None:
        return quantity
    return -abs(quantity) if _is_short(row.get("direction")) else abs(quantity)


@dataclass(frozen=True, slots=True)
class ExecutableTarget:
    """账户口径下、已对齐持仓代码的目标对账结果。"""

    weights: dict[str, float]
    quantities: dict[str, float]
    off_symbol_count: int | None
    rows: tuple["ExecutableTargetRow", ...] = ()


@dataclass(frozen=True, slots=True)
class ExecutableTargetRow:
    """一个规范化品种的当前仓位、可执行目标及对账结论。"""

    symbol: str
    current_weight: float
    target_weight: float
    current_quantity: float
    target_quantity: float
    action: str
    side: str
    aligned: bool


def _action(current: float, target: float, aligned: bool) -> tuple[str, str]:
    """按带符号数量给出调仓动作与方向。"""
    if aligned:
        return "aligned", "none"
    if abs(current) <= _QTY_EPS:
        action = "open"
    elif abs(target) <= _QTY_EPS:
        action = "close"
    elif current * target < 0:
        action = "flip"
    elif abs(current) < abs(target):
        action = "increase"
    else:
        action = "reduce"
    return action, "buy" if current < target else "sell"


def plan_executable_target(
    positions: list[object],
    target_weights: Mapping[str, float],
    target_quantities: Mapping[str, float],
    equity: float,
    *,
    canonicalize_symbol: CanonicalizeSymbol,
) -> ExecutableTarget:
    """比较已记录的目标数量与实际净仓；权重仅用于展示。

    Parameters
    ----------
    positions : list[object]
        有效资产快照中的持仓；数量不完整时整个对账结果未就绪。
    target_weights : Mapping[str, float]
        账户口径目标权重，用于展示。
    target_quantities : Mapping[str, float]
        同一份完整换算证据中的目标数量，必须覆盖全部目标品种。
    equity : float
        账户总权益，用于计算展示权重。
    canonicalize_symbol : CanonicalizeSymbol
        渠道合约身份归一函数。

    Returns
    -------
    ExecutableTarget
        按数量对账的逐品种结论及待调整数量。

    Raises
    ------
    ValueError
        目标数量缺失或不是有限数值。
    """
    weights = {canonicalize_symbol(symbol): weight for symbol, weight in target_weights.items()}
    quantities = {canonicalize_symbol(symbol): quantity for symbol, quantity in target_quantities.items()}
    if not weights.keys() <= quantities.keys() or any(
        isinstance(quantity, bool) or not math.isfinite(quantity) for quantity in quantities.values()
    ):
        raise ValueError("目标数量必须完整且为有限数值")

    cur_lots: dict[str, float] = {}
    cur_mv: dict[str, float] = {}
    for position in positions:
        if not isinstance(position, dict) or not isinstance(position.get("symbol"), str):
            return ExecutableTarget(weights, quantities, None)
        symbol = canonicalize_symbol(position["symbol"])
        lots = _signed_lots(position)
        if lots is None:
            return ExecutableTarget(weights, quantities, None)
        cur_lots[symbol] = cur_lots.get(symbol, 0.0) + lots
        market_value = abs(float(position.get("market_value") or 0.0))
        cur_mv[symbol] = cur_mv.get(symbol, 0.0) + (
            -market_value if _is_short(position.get("direction")) else market_value
        )

    rows = []
    for symbol in weights.keys() | quantities.keys() | cur_lots.keys():
        current = cur_lots.get(symbol, 0.0)
        target = quantities.get(symbol, 0.0)
        aligned = abs(current - target) <= _QTY_EPS
        action, side = _action(current, target, aligned)
        rows.append(
            ExecutableTargetRow(
                symbol,
                cur_mv.get(symbol, 0.0) / equity if equity > 0 else 0.0,
                weights.get(symbol, 0.0),
                current,
                target,
                action,
                side,
                aligned,
            )
        )
    rows.sort(key=lambda row: abs(row.current_weight - row.target_weight), reverse=True)
    return ExecutableTarget(weights, quantities, sum(not row.aligned for row in rows), tuple(rows))
