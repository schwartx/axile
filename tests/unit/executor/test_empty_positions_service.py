"""`AbstractExecutor.empty_positions()` 流程测试。"""

from __future__ import annotations

from axile.common.trade_channel import TradeChannel
from axile.executor.abstract_executor import AbstractExecutor
from axile.executor.models.execution_result import ExecutionStatus
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_input import CTPAccountConfig
from axile.executor.models.unified_order import OrderDirection, OrderType, TradeRecord, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData


class _EmptyPositionsExecutor(AbstractExecutor):
    def __init__(self, account_assets: UnifiedAccountAssets) -> None:
        self._account_assets = account_assets
        super().__init__(TradeChannel.CTP, None)

    def _initialize_connection(self, account_config: object) -> None:
        self.account_config = account_config

    def _verify_connection(self) -> bool:
        return True

    def _check_trading_time(self) -> bool:
        return True

    def get_account_assets(self) -> UnifiedAccountAssets:
        return self._account_assets

    def get_market_data(self, symbols: list[str]) -> dict[str, UnifiedPriceData]:
        _ = symbols
        return {}

    def _place_order_impl(
        self,
        symbol: str,
        direction: OrderDirection,
        order_type: OrderType,
        volume: float,
        price: float = 0,
        **kwargs: object,
    ) -> UnifiedOrder:
        _ = (direction, order_type, volume, price, kwargs)
        return UnifiedOrder(
            order_id=f"order-{symbol}",
            symbol=symbol,
            direction=OrderDirection.BUY,
            order_type=OrderType.LIMIT,
            volume=1.0,
            price=100.0,
            status="SUBMITTED",
        )

    def _get_pending_orders_impl(self, symbol: str | None = None) -> list[UnifiedOrder]:
        _ = symbol
        return []

    def _query_trades_impl(self, symbol: str, order_id: str) -> list[TradeRecord]:
        _ = (symbol, order_id)
        return []

    def _cancel_order_impl(self, symbol: str, order_id: str) -> bool:
        _ = (symbol, order_id)
        return True

    def _cleanup(self) -> None:
        return None

    def _get_account_mark(self) -> str:
        return "acct-empty"

    def _get_default_trade_rules_for_empty(self, symbols: list[str]) -> dict[str, object]:
        return {symbol: {"price": "PASSIVE"} for symbol in symbols}

    def register_order_callback(self, callback: object) -> None:
        _ = callback

    def register_price_callback(self, callback: object) -> None:
        _ = callback

    def unregister_order_callback(self, callback: object) -> None:
        _ = callback

    def unregister_price_callback(self, callback: object) -> None:
        _ = callback

    def initialize_websocket(self, symbols: list[str] | None = None) -> None:
        _ = symbols

    def is_monitoring(self) -> bool:
        return False


def test_empty_positions_returns_noop_result_when_no_positions() -> None:
    executor = _EmptyPositionsExecutor(
        UnifiedAccountAssets(
            available_cash=1000.0,
            total_asset=1000.0,
            market_value=0.0,
            positions=[],
        )
    )
    executor.account_config = CTPAccountConfig.model_validate(
        {
            "broker_id": "b",
            "investor_id": "i",
            "password": "p",
            "td_front": "tcp://td:1",
            "md_front": "tcp://md:2",
            "app_id": "app",
            "auth_code": "auth",
        }
    )

    result = executor.empty_positions()

    assert result.status == ExecutionStatus.NOOP
    assert result.memory == {"message": "当前账户无持仓，无需清仓"}
