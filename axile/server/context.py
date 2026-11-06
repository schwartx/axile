"""自定义组合函数使用的统一账户上下文."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from axile.executor.models.unified_account_assets import Position, PositionDirection, UnifiedAccountAssets
from axile.executor.models.unified_order import TradeRecord, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData


class PortfolioExecutor(Protocol):
    """组合上下文依赖的最小执行器查询协议."""

    def get_account_assets(self) -> UnifiedAccountAssets:
        """获取统一账户资产."""
        ...

    def get_market_data(self, symbols: list[str]) -> dict[str, UnifiedPriceData]:
        """获取统一行情."""
        ...

    def get_pending_orders(self, symbol: str | None = None) -> list[UnifiedOrder]:
        """获取未完成订单."""
        ...

    def query_trades(self, symbol: str, order_id: str) -> list[TradeRecord]:
        """查询成交记录."""
        ...


@dataclass(frozen=True, slots=True)
class ContextSettings:
    """本次计算使用的只读执行配置；杠杆已解析渠道默认值。"""

    long_leverage: float = 1.0
    short_leverage: float = 1.0
    weight_precision: float = 0.01


class Context:
    """向组合函数提供执行设置，并按需查询所选账户.

    Notes
    -----
    ``executor`` 是账户 worker 内复用的真实对象。用户代码对它、回调或模块
    全局状态的修改可能延续到后续成功请求；这是可信高级代码的既定能力。
    """

    def __init__(self, executor: PortfolioExecutor | None = None, *, settings: ContextSettings | None = None) -> None:
        """初始化组合上下文."""
        self._executor = executor
        self._settings = settings if settings is not None else ContextSettings()
        self._account: UnifiedAccountAssets | None = None
        self._quotes: dict[str, UnifiedPriceData] = {}

    @property
    def executor(self) -> PortfolioExecutor:
        """返回所选账户的执行器；无账户试跑不提供虚构数据。"""
        if self._executor is None:
            raise ValueError("读取账户、持仓、行情或订单数据需要先选择账户")
        return self._executor

    @property
    def settings(self) -> ContextSettings:
        """返回本次计算的多空杠杆及权重精度，不包含渠道凭据。"""
        return self._settings

    @property
    def account(self) -> UnifiedAccountAssets:
        """返回本次计算内缓存的统一账户快照."""
        if self._account is None:
            self._account = self.executor.get_account_assets()
        return self._account

    @property
    def positions(self) -> list[Position]:
        """返回统一账户快照中的持仓列表."""
        return self.account.positions

    def get_positions(
        self,
        symbol: str | None = None,
        direction: PositionDirection | str | None = None,
    ) -> list[Position]:
        """按标的和方向筛选统一持仓."""
        direction_value = direction.value if isinstance(direction, PositionDirection) else direction
        return [
            position
            for position in self.positions
            if (symbol is None or position.symbol == symbol)
            and (direction_value is None or position.direction == direction_value)
        ]

    def get_quote(self, symbol: str) -> UnifiedPriceData:
        """获取并缓存指定标的的统一行情快照."""
        cached = self._quotes.get(symbol)
        if cached is not None:
            return cached

        quotes = self.executor.get_market_data([symbol])
        quote = quotes.get(symbol)
        if quote is None and len(quotes) == 1:
            quote = next(iter(quotes.values()))
        if quote is None:
            raise ValueError(f"无法获取行情: {symbol}")
        self._quotes[symbol] = quote
        self._quotes[quote.symbol] = quote
        return quote

    def get_price(self, symbol: str) -> float:
        """返回指定标的的最新价."""
        return self.get_quote(symbol).last_price

    def get_pending_orders(self, symbol: str | None = None) -> list[UnifiedOrder]:
        """返回当前账户的未完成订单."""
        return self.executor.get_pending_orders(symbol)

    def query_trades(self, symbol: str, order_id: str) -> list[TradeRecord]:
        """返回指定订单的成交明细."""
        return self.executor.query_trades(symbol, order_id)


__all__ = ["Context", "ContextSettings", "PortfolioExecutor"]
