"""账户在位性：可执行数量相对目标的偏离.

有渠道量化钩子时，待调整 = 现持净仓 ≠ 量化后的目标数量。
没有钩子时仍用权重 0.5 个百分点阈值，避免未装备渠道的仪表盘突然变未知。
合约身份由渠道 ``canonicalize_symbol`` 处理，本模块不写郑商所特例。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from axile.channels.contracts import CanonicalizeSymbol, QuantizeTargetQuantity

# 无数量口径时的权重回退；与前端 ``rebalancePlan`` 在 ``quantities`` 缺失时一致。
REBALANCE_THRESHOLD = 0.5
_ZERO = 1e-6
_QTY_EPS = 1e-9


def _identity_symbol(symbol: str) -> str:
    return symbol


def _to_float(value: object) -> float:
    """把任意标量安全转为浮点数；无法转换时返回 0."""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _is_short(direction: object) -> bool:
    """判断持仓方向是否为空头（兼容中英文写法）."""
    if not isinstance(direction, str):
        return False
    return "空" in direction or "short" in direction.lower()


def _round2(value: float) -> float:
    """与 JS ``Number.prototype.toFixed(2)`` 对齐的两位小数."""
    return float(f"{value:.2f}")


def _signed_lots(row: Mapping[str, object]) -> float | None:
    """净仓手数；``extra.net_position`` 优先，否则带符号 ``volume``."""
    extra = row.get("extra")
    if isinstance(extra, dict) and extra.get("net_position") is not None:
        return _to_float(extra.get("net_position"))
    if row.get("volume") is None:
        return None
    mag = abs(_to_float(row.get("volume")))
    return -mag if _is_short(row.get("direction")) else mag


@dataclass(frozen=True, slots=True)
class ExecutableTarget:
    """账户口径下、已对齐持仓代码的可执行目标."""

    weights: dict[str, float]
    quantities: dict[str, float] | None
    off_symbol_count: int
    rows: tuple["ExecutableTargetRow", ...] = ()


@dataclass(frozen=True, slots=True)
class ExecutableTargetRow:
    """一个规范化品种的当前仓位、可执行目标及对账结论."""

    symbol: str
    current_weight: float
    target_weight: float
    current_quantity: float | None
    target_quantity: float | None
    action: str
    side: str
    aligned: bool


def _action(current: float, target: float, aligned: bool) -> tuple[str, str]:
    """按带符号仓位给出账户展示所需的调仓动作与方向."""
    if aligned:
        return "aligned", "none"
    if abs(current) < _ZERO:
        action = "open"
    elif abs(target) < _ZERO:
        action = "close"
    elif current * target < 0:
        action = "flip"
    elif abs(current) < abs(target):
        action = "increase"
    else:
        action = "reduce"
    return action, "buy" if current < target else "sell"


def _position_totals(
    positions: list[object], canonicalize_symbol: CanonicalizeSymbol
) -> tuple[dict[str, float], dict[str, float], dict[str, float], dict[str, bool]]:
    """汇总同一合约的净仓、市值和单位名义价值。"""
    cur_lots: dict[str, float] = {}
    cur_mv: dict[str, float] = {}
    notional_per_unit: dict[str, float] = {}
    has_book: dict[str, bool] = {}

    for row in positions:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            continue
        native = canonicalize_symbol(symbol)
        mag = abs(_to_float(row.get("market_value")))
        signed_mv = -mag if _is_short(row.get("direction")) else mag
        cur_mv[native] = cur_mv.get(native, 0.0) + signed_mv
        lots = _signed_lots(row)
        if lots is not None:
            cur_lots[native] = cur_lots.get(native, 0.0) + lots
        volume = abs(_to_float(row.get("volume")))
        if volume > 0 and mag > 0:
            notional_per_unit[native] = mag / volume
        if lots is not None or mag > 0:
            has_book[native] = True
    return cur_lots, cur_mv, notional_per_unit, has_book


def plan_executable_target(
    positions: list[object],
    target: Mapping[str, float],
    equity: float,
    *,
    canonicalize_symbol: CanonicalizeSymbol = _identity_symbol,
    quantize_target_quantity: QuantizeTargetQuantity | None = None,
) -> ExecutableTarget:
    """
    把持仓与目标收到渠道原生代码，并按可下单数量判定偏离.

    Parameters
    ----------
    positions : list[object]
        账户快照持仓。
    target : Mapping[str, float]
        账户口径目标权重。
    equity : float
        账户总权益。
    canonicalize_symbol : CanonicalizeSymbol, optional
        合约身份归一；缺省恒等。
    quantize_target_quantity : QuantizeTargetQuantity | None, optional
        权重→数量。``None`` 时不下发 quantities，off 回退权重阈值。

    Returns
    -------
    ExecutableTarget
        ``weights`` 已是持仓代码空间；有量化钩子时带 ``quantities``。
    """
    cur_lots, cur_mv, notional_per_unit, has_book = _position_totals(positions, canonicalize_symbol)

    weights: dict[str, float] = {}
    for key, value in target.items():
        if not isinstance(key, str) or not key:
            continue
        weights[canonicalize_symbol(key)] = float(value or 0.0)

    symbols = set(weights) | set(cur_lots) | set(cur_mv)
    base = equity if equity > 0 else 0.0
    if quantize_target_quantity is None:
        rows = []
        for symbol in symbols:
            current_weight = cur_mv.get(symbol, 0.0) / base if base > 0 else 0.0
            target_weight = weights.get(symbol, 0.0)
            aligned = abs(_round2((current_weight - target_weight) * 100.0)) <= REBALANCE_THRESHOLD
            if abs(current_weight) < _ZERO and abs(target_weight) < _ZERO:
                continue
            action, side = _action(current_weight, target_weight, aligned)
            rows.append(
                ExecutableTargetRow(
                    symbol, current_weight, target_weight, cur_lots.get(symbol), None, action, side, aligned
                )
            )
        return ExecutableTarget(
            weights=weights,
            quantities=None,
            off_symbol_count=sum(not row.aligned for row in rows),
            rows=tuple(sorted(rows, key=lambda row: abs(row.current_weight - row.target_weight), reverse=True)),
        )

    quantities: dict[str, float] = {}
    rows = []
    for symbol in symbols:
        weight = weights.get(symbol, 0.0)
        current = cur_lots.get(symbol)
        booked = has_book.get(symbol, False)
        if abs(weight) < _ZERO and current is None and not booked:
            continue
        target_qty: float | None
        if abs(weight) < _ZERO:
            target_qty = 0.0
            quantities[symbol] = 0.0
        elif symbol in notional_per_unit:
            target_qty = float(quantize_target_quantity(weight, equity, notional_per_unit[symbol]))
            quantities[symbol] = target_qty
        else:
            target_qty = None
        current_weight = cur_mv.get(symbol, 0.0) / base if base > 0 else 0.0
        if target_qty is None:
            aligned = False
        elif current is None:
            aligned = not booked and abs(target_qty) <= _QTY_EPS
        else:
            aligned = abs(current - target_qty) <= _QTY_EPS
        action, side = _action(current_weight, weight, aligned)
        rows.append(ExecutableTargetRow(symbol, current_weight, weight, current, target_qty, action, side, aligned))
    rows.sort(key=lambda row: abs(row.current_weight - row.target_weight), reverse=True)
    return ExecutableTarget(
        weights=weights,
        quantities=quantities,
        off_symbol_count=sum(not row.aligned for row in rows),
        rows=tuple(rows),
    )


__all__ = [
    "REBALANCE_THRESHOLD",
    "ExecutableTarget",
    "ExecutableTargetRow",
    "plan_executable_target",
]
