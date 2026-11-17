"""组合试跑的显式账户快照；不连接渠道，也不提供交易能力。"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from math import isfinite
from typing import TYPE_CHECKING

from axile.executor.models.unified_account_assets import Position, PositionDirection, UnifiedAccountAssets
from axile.executor.models.unified_price import UnifiedPriceData

if TYPE_CHECKING:
    from axile.channels.contracts import ChannelDescriptor


def _finite_values(value: object) -> None:
    if isinstance(value, float) and not isfinite(value):
        raise ValueError("模拟账户所有数值必须是有限数值")
    if is_dataclass(value) and not isinstance(value, type):
        for item in fields(value):
            _finite_values(getattr(value, item.name))
    elif isinstance(value, dict):
        for item in value.values():
            _finite_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _finite_values(item)


def validate_leverage(descriptor: ChannelDescriptor, values: dict[str, float]) -> None:
    """复用渠道的杠杆范围和步进。"""
    limits = descriptor.leverage
    for name, value in values.items():
        steps = (value - limits.min) / limits.step
        if not isfinite(value) or not limits.min <= value <= limits.max or abs(steps - round(steps)) > 1e-9:
            raise ValueError(f"{name} 需在 {limits.min:g}..{limits.max:g} 之间，步进 {limits.step:g}")


@dataclass(kw_only=True)
class SimulationAccount:
    """用户可编辑的统一账户、行情及多空杠杆快照。"""

    account: UnifiedAccountAssets
    quotes: list[UnifiedPriceData]
    long_leverage: float
    short_leverage: float

    __forbid_unknown__ = True

    def validate(self) -> None:
        """仅约束模拟输入，不改变真实统一模型的默认行为。"""
        _finite_values(self)
        if not self.account.currency.strip():
            raise ValueError("模拟账户币种不能为空")
        if self.account.available_cash < 0 or self.account.total_asset < 0:
            raise ValueError("模拟账户资金不能为负数")
        symbols: set[str] = set()
        for quote in self.quotes:
            if not quote.symbol.strip() or quote.symbol != quote.symbol.strip():
                raise ValueError("模拟行情标的不能为空或包含首尾空白")
            if quote.symbol in symbols:
                raise ValueError(f"重复模拟行情: {quote.symbol}")
            symbols.add(quote.symbol)
            if quote.last_price <= 0 or quote.volume < 0 or quote.turnover < 0 or quote.timestamp < 0:
                raise ValueError(f"模拟行情价格需为正数，成交量、成交额和时间戳不能为负数: {quote.symbol}")
            if any(price <= 0 or volume < 0 for side in (quote.bids, quote.asks) for price, volume in side):
                raise ValueError(f"模拟盘口价格需为正数、数量不能为负数: {quote.symbol}")
        position_keys: set[tuple[str, PositionDirection]] = set()
        for position in self.account.positions:
            self._validate_position(position, symbols)
            key = (position.symbol, position.direction)
            if key in position_keys:
                raise ValueError(f"重复模拟持仓: {position.symbol} {position.direction.value}")
            position_keys.add(key)

    @staticmethod
    def _validate_position(position: Position, symbols: set[str]) -> None:
        if not position.symbol.strip() or position.symbol != position.symbol.strip():
            raise ValueError("模拟持仓标的不能为空或包含首尾空白")
        if position.direction not in tuple(PositionDirection):
            raise ValueError(f"模拟持仓方向无效: {position.symbol}")
        if position.volume < 0 or not 0 <= position.available_volume <= position.volume:
            raise ValueError(f"模拟持仓数量需非负，可用量不能超过持仓量: {position.symbol}")
        if position.avg_price is not None and position.avg_price < 0:
            raise ValueError(f"模拟持仓成本价不能为负数: {position.symbol}")
        if position.symbol not in symbols:
            raise ValueError(f"缺少模拟行情: {position.symbol}")

    def validate_channel(self, descriptor: ChannelDescriptor) -> None:
        """校验账户币种、杠杆及期货估值所需参数。"""
        self.validate()
        if self.account.currency != descriptor.currency:
            raise ValueError(f"模拟账户币种必须为 {descriptor.currency}")
        validate_leverage(descriptor, {"long_leverage": self.long_leverage, "short_leverage": self.short_leverage})
        if descriptor.units.quantity_kind == "contract":
            for quote in self.quotes:
                for name in ("volume_multiple", "price_tick"):
                    value = quote.extra.get(name)
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                        raise ValueError(f"模拟期货行情 {quote.symbol} 需要正数 {name}")


def simulation_account_preset(
    currency: str,
    instruments: tuple[tuple[str, float, float, float, float], ...],
    *,
    long_leverage: float,
    short_leverage: float,
) -> SimulationAccount:
    """根据渠道给定的标的、价格、数量、乘数与 tick 构造完整静态示例。"""
    update_time = "2026-01-01T00:00:00+00:00"
    return SimulationAccount(
        account=UnifiedAccountAssets(
            available_cash=1_000_000,
            total_asset=1_000_000,
            market_value=0,
            positions=[],
            currency=currency,
            update_time=update_time,
            source="simulation",
        ),
        quotes=[
            UnifiedPriceData(
                symbol=symbol,
                last_price=price,
                bids=[(price - tick, 100)],
                asks=[(price + tick, 100)],
                volume=1000,
                turnover=price * 1000 * multiplier,
                timestamp=1767225600000,
                update_time=update_time,
                extra={"volume_multiple": multiplier, "price_tick": tick},
            )
            for symbol, price, _, multiplier, tick in instruments
        ],
        long_leverage=long_leverage,
        short_leverage=short_leverage,
    )
