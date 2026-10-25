"""`AbstractExecutor.empty_positions()` 流程测试。"""

from __future__ import annotations

import pytest

from axile.common.trade_channel import TradeChannel
from axile.executor.abstract_executor import AbstractExecutor
from axile.executor.abstract_executor.facade import DEFAULT_CLEAR_TIMEOUT
from axile.executor.models.execution_result import ExecutionStatus
from axile.executor.models.unified_account_assets import Position, PositionDirection, UnifiedAccountAssets
from axile.executor.models.unified_input import CTPAccountConfig
from axile.executor.models.unified_order import OrderDirection, OrderType, TradeRecord, UnifiedOrder
from axile.executor.models.unified_output import UnifiedStandardOutput
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


@pytest.mark.parametrize("available", [0, 1])
def test_empty_positions_checks_configuration_only_for_closeable_positions(available):
    executor = _EmptyPositionsExecutor(
        UnifiedAccountAssets(
            available_cash=0,
            total_asset=100,
            market_value=100,
            positions=[
                Position(
                    symbol="A", volume=1, available_volume=available, market_value=100, direction=PositionDirection.LONG
                )
            ],
        )
    )
    if available:
        with pytest.raises(ValueError, match="账户配置不能为空"):
            executor.empty_positions(algorithm=object())
    else:
        assert executor.empty_positions(algorithm=object()).status == ExecutionStatus.NOOP


@pytest.mark.parametrize("timeout", [None, 0, -1, 25])
def test_empty_positions_builds_final_input_without_losing_rules_or_notifications(monkeypatch, timeout):
    assets = UnifiedAccountAssets(
        available_cash=0,
        total_asset=400,
        market_value=400,
        positions=[
            Position(symbol="A", volume=2, available_volume=1, market_value=100, direction=PositionDirection.LONG),
            Position(symbol="B", volume=3, available_volume=0, market_value=300, direction=PositionDirection.SHORT),
        ],
    )
    executor = _EmptyPositionsExecutor(assets)
    executor.account_config = CTPAccountConfig(
        broker_id="b",
        investor_id="i",
        password="p",
        td_front="tcp://td:1",
        md_front="tcp://md:2",
        app_id="app",
        auth_code="auth",
    )
    captured = []
    expected = UnifiedStandardOutput(
        account_assets=assets,
        status=ExecutionStatus.SUCCEEDED,
        channel_type=TradeChannel.CTP,
        success=True,
    )

    def execute(standard_input, **kwargs):
        captured.append((standard_input, kwargs))
        return expected

    monkeypatch.setattr(executor, "execute", execute)
    params = (
        {}
        if timeout is None
        else {"execution_timeout": timeout, "algorithm": {"method": "SINGLE-MAKER", "params": {"max_wait_seconds": 9}}}
    )
    notification = {"before": 400}
    assert (
        executor.empty_positions(
            cleanup=False,
            retain_runtime=True,
            trade_rules={"A": {"price": "ACTIVE"}},
            futures_close_policy={"fallback": "today_first"},
            forbidden_symbols=["A", 7],
            feishu_key="key",
            execution_notification_code="def notify(context): pass",
            feishu_account={"id": 1},
            extra={"audit": {"execution_id": "e"}},
            notification_snapshot=notification,
            ignored="not forwarded",
            **params,
        )
        is expected
    )
    actual, flags = captured.pop()
    assert actual.channel_type == TradeChannel.CTP and actual.account_config == executor.account_config
    assert actual.curr_target == {"A": 0.0}
    assert actual.last_target == {"A": 0.25, "B": -0.75}
    assert actual.trade_rules == {"A": {"price": "ACTIVE"}}
    assert actual.futures_close_policy.fallback == "today_first"
    assert actual.forbidden_symbols == ["A"] and actual.risk_symbols == []
    assert actual.algorithm == {"method": "SINGLE-MAKER", "params": {"max_wait_seconds": 60 if timeout is None else 9}}
    assert actual.execution_timeout == (DEFAULT_CLEAR_TIMEOUT if timeout is None else timeout)
    assert actual.feishu_key == "key" and actual.execution_notification_code == "def notify(context): pass"
    assert actual.feishu_account == {"id": 1} and actual.extra == {
        "audit": {"execution_id": "e"},
        "channel_type": "ctp",
    }
    assert "ignored" not in actual.model_dump()
    assert flags == {"cleanup": False, "retain_runtime": True, "notification_snapshot": notification}
