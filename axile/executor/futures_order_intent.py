"""期货渠道共享的开平意图翻译与今昨仓拆分。"""

import math
from dataclasses import dataclass
from typing import Any, cast

from axile.executor.models.unified_account_assets import Position, PositionDirection, UnifiedAccountAssets
from axile.executor.models.unified_order import OrderDirection


def is_close_intent(direction: OrderDirection, position_side: object) -> bool:
    """仅反向买卖表示平掉指定持仓侧；同向买卖表示开仓。"""
    if position_side not in (None, "LONG", "SHORT"):
        raise ValueError(f"不支持的 position_side: {position_side}")
    return (direction == OrderDirection.SELL and position_side == "LONG") or (
        direction == OrderDirection.BUY and position_side == "SHORT"
    )


def plan_futures_close_orders(
    symbol: str,
    direction: OrderDirection,
    volume: float,
    account_assets: UnifiedAccountAssets,
    exchange: str,
    *,
    priority: str = "yesterday_first",
) -> list[tuple[float, dict[str, object]]]:
    """按账户决策分配今昨数量，交易所不区分时使用普通平仓。"""
    if priority not in {"yesterday_first", "today_first"}:
        raise ValueError("无效的平仓顺序")
    if exchange not in ("SHFE", "INE"):
        return [(volume, {"offset_flag": "close"})]
    side = PositionDirection.LONG if direction == OrderDirection.SELL else PositionDirection.SHORT
    prefix = "long" if side == PositionDirection.LONG else "short"
    yesterday = available = 0.0
    for position in account_assets.positions:
        if position.symbol != symbol or position.direction != side or position.volume <= 0:
            continue
        td = float(position.extra.get(f"{prefix}_td", float("nan")))
        yd = float(position.extra.get(f"{prefix}_yd", float("nan")))
        if not all(math.isfinite(v) and v >= 0 for v in (td, yd)) or not math.isclose(
            td + yd, position.volume, rel_tol=0.0, abs_tol=1e-6
        ):
            raise ValueError(f"{symbol}: 今昨仓明细缺失或不一致，拒绝猜测平仓标志")
        # 统一快照只有总冻结量，无法确定冻结的是今仓还是昨仓。
        if position.available_volume < position.volume:
            raise ValueError(f"{symbol}: 持仓存在冻结量，无法安全分配今昨平仓")
        yesterday += yd
        available += position.available_volume
    if volume > available:
        raise ValueError(f"{symbol}: 平仓量超过可用持仓")
    today = available - yesterday
    candidates = [(yesterday, "close_yesterday"), (today, "close_today")]
    if priority == "today_first":
        candidates.reverse()
    remaining = volume
    result = []
    for available_quantity, offset in candidates:
        quantity = min(remaining, available_quantity)
        if quantity > 0:
            result.append((quantity, {"offset_flag": offset}))
            remaining -= quantity
    return result


def single_close_offset(plan: list[tuple[float, dict[str, object]]]) -> str:
    """单笔接口不能表达混合今昨仓；要求调用方通过分单接口提交。"""
    if len(plan) != 1:
        raise ValueError("平仓涉及今昨两类持仓，请先调用 plan_close_orders 拆单")
    return str(plan[0][1]["offset_flag"])


@dataclass(frozen=True)
class FuturesPosition:
    """可信快照中的实际多空持仓；今仓方向整体保护，防止中金所平今计费。"""

    long: int = 0
    short: int = 0
    long_today: int = 0
    short_today: int = 0

    @property
    def net(self) -> int:
        """实际净持仓。"""
        return self.long - self.short

    def targets(self, target: float, *, clear: bool = False) -> tuple[float, float]:
        """求满足保护条件和净仓目标的最小多空持仓。"""
        if clear:
            return 0, 0
        protected_long = self.long if self.long_today else 0
        protected_short = self.short if self.short_today else 0
        long = max(protected_long, target + protected_short)
        return long, long - target

    def legs(self, targets: tuple[float, float]) -> list[tuple[OrderDirection, float, str]]:
        """先平多余仓位，再补足缺少的方向。"""
        long, short = targets
        return [
            (direction, volume, offset)
            for direction, volume, offset in (
                (OrderDirection.SELL, self.long - long, "close"),
                (OrderDirection.BUY, self.short - short, "close"),
                (OrderDirection.BUY, long - self.long, "open"),
                (OrderDirection.SELL, short - self.short, "open"),
            )
            if volume > 0
        ]


def futures_position(symbol: str, assets: UnifiedAccountAssets, *, allow_frozen: bool = False) -> FuturesPosition:
    """聚合可信今昨仓；清仓可读取冻结持仓，发单仍按可平量校验。"""
    from axile.executor.models.unified_account_assets import is_degraded_snapshot_source

    if is_degraded_snapshot_source(assets.source):
        raise ValueError(f"{symbol}: 持仓快照尚未确认")
    totals = dict(long=0, short=0, long_today=0, short_today=0)
    for position in assets.positions:
        if position.symbol != symbol:
            continue
        side, total, today, _ = position_quantities(position)
        available = position.available_volume
        if not math.isfinite(available) or not 0 <= available <= total or int(available) != available:
            raise ValueError(f"{symbol}: 可用持仓数量无效")
        if not allow_frozen and available < total:
            raise ValueError(f"{symbol}: 持仓存在冻结量，拒绝规划锁仓或解锁")
        totals[side] += total
        totals[f"{side}_today"] += today
    return FuturesPosition(**totals)


def position_quantities(position: Position) -> tuple[str, int, int, int]:
    """校验单方向拆分；总量只采用统一持仓数量，昨仓表示当前剩余量。"""
    sides = {PositionDirection.LONG: "long", PositionDirection.SHORT: "short"}
    if position.direction not in sides:
        raise ValueError(f"{position.symbol}: CTP 持仓必须指定多空方向")
    side = sides[position.direction]

    def quantity(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{position.symbol}: 持仓数量必须是非负整数")
        if not math.isfinite(value) or value < 0 or int(value) != value:
            raise ValueError(f"{position.symbol}: 持仓数量必须是非负整数")
        return int(value)

    def extra_quantity(*keys: str) -> int | None:
        values = [quantity(position.extra[key]) for key in keys if key in position.extra]
        if len(set(values)) > 1:
            raise ValueError(f"{position.symbol}: 持仓字段不一致 {keys}")
        return values[0] if values else None

    total = quantity(position.volume)
    reported_total = extra_quantity(f"{side}_total", f"{side}_position")
    today = extra_quantity(f"{side}_td", f"{side}_today_position")
    yesterday = extra_quantity(f"{side}_yd", f"{side}_yd_position")
    if reported_total is not None and reported_total != total:
        raise ValueError(f"{position.symbol}: 持仓总量与 volume 不一致")
    if today is None and yesterday is None:
        if total:
            raise ValueError(f"{position.symbol}: 缺少今昨持仓拆分")
        return side, 0, 0, 0
    if today is None:
        today = total - cast(int, yesterday)
    if yesterday is None:
        yesterday = total - today
    if today < 0 or yesterday < 0 or today + yesterday != total:
        raise ValueError(f"{position.symbol}: 今昨持仓与总量不一致")
    return side, total, today, yesterday


def validate_futures_order(
    symbol: str,
    direction: OrderDirection,
    volume: float,
    assets: UnifiedAccountAssets,
    *,
    offset: str,
    protect_today: bool,
) -> None:
    """发送前复核实际库存；计划解锁时不能因新出现的今仓改成锁仓。"""
    position = futures_position(symbol, assets, allow_frozen=offset == "close")
    if offset == "open":
        return
    today = position.long_today if direction == OrderDirection.SELL else position.short_today
    side = PositionDirection.LONG if direction == OrderDirection.SELL else PositionDirection.SHORT
    available = sum(p.available_volume for p in assets.positions if p.symbol == symbol and p.direction == side)
    if protect_today and today:
        raise ValueError(f"{symbol}: 待平方向出现今仓，停止解锁并等待重新规划")
    if volume > available:
        raise ValueError(f"{symbol}: 平仓量超过可用持仓")
