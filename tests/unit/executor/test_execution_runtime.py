"""ExecutionRuntime 生命周期与 facade 兼容测试。"""

from __future__ import annotations

from typing import Any

from axile.common.trade_channel import TradeChannel
from axile.executor.abstract_executor.base import AbstractExecutor
from axile.executor.models.execution_result import ExecutionStatus
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_input import CTPAccountConfig, UnifiedStandardInput
from axile.executor.models.unified_order import OrderDirection, OrderType, TradeRecord, UnifiedOrder
from axile.executor.models.unified_output import UnifiedStandardOutput
from axile.executor.models.unified_price import UnifiedPriceData


class _Logger:
    def info(self, _message: object, *args: object, **kwargs: object) -> None:
        _ = (args, kwargs)

    def warning(self, _message: object, *args: object, **kwargs: object) -> None:
        _ = (args, kwargs)

    def error(self, _message: object, *args: object, **kwargs: object) -> None:
        _ = (args, kwargs)


class _RuntimeExecutor(AbstractExecutor):
    def __init__(self) -> None:
        self.logger = _Logger()
        super().__init__(
            TradeChannel.CTP,
            CTPAccountConfig.model_validate(
                {
                    "broker_id": "b",
                    "investor_id": "i",
                    "password": "p",
                    "td_front": "tcp://td:1",
                    "md_front": "tcp://md:2",
                    "app_id": "app",
                    "auth_code": "auth",
                }
            ),
        )

    def _initialize_connection(self, account_config: CTPAccountConfig) -> None:
        self.account_config = account_config

    def _verify_connection(self) -> bool:
        return True

    def _check_trading_time(self) -> bool:
        return True

    def get_account_assets(self) -> UnifiedAccountAssets:
        return UnifiedAccountAssets(
            available_cash=1000.0,
            total_asset=1000.0,
            market_value=0.0,
            positions=[],
        )

    def get_market_data(self, symbols: list[str]) -> dict[str, UnifiedPriceData]:
        return {
            symbol: UnifiedPriceData(
                symbol=symbol,
                last_price=100.0,
                bid_price=99.0,
                ask_price=101.0,
                bid_volume=1.0,
                ask_volume=1.0,
                volume=1.0,
                turnover=1.0,
                timestamp=1,
                update_time="2026-03-26T09:00:00",
            )
            for symbol in symbols
        }

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
        symbol = "rb2610" if symbol is None else symbol
        return [
            UnifiedOrder(
                order_id=f"pending-{symbol}",
                symbol=symbol,
                direction=OrderDirection.BUY,
                order_type=OrderType.LIMIT,
                volume=1.0,
                price=100.0,
                status="PENDING",
            )
        ]

    def _query_trades_impl(self, symbol: str, order_id: str) -> list[TradeRecord]:
        return [
            TradeRecord.create(
                trade_id=f"trade-{order_id}",
                trade_time="2026-03-26T09:00:00",
                trade_volume=1.0,
                trade_price=100.0,
                extra={"symbol": symbol, "order_id": order_id},
            )
        ]

    def _cancel_order_impl(self, symbol: str, order_id: str) -> bool:
        _ = (symbol, order_id)
        return True

    def _cleanup(self) -> None:
        return None

    def _get_account_mark(self) -> str:
        return "runtime"

    def _get_default_trade_rules_for_empty(self, symbols: list[str]) -> dict[str, Any]:
        return {symbol: {} for symbol in symbols}

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


def test_prepare_execution_runtime_reuses_active_runtime_and_preserves_audit_seq() -> None:
    executor = _RuntimeExecutor()
    executor.set_audit_context({"execution_id": "exec-1", "account_id": 7, "algorithm": "TEST"})

    runtime = executor.prepare_execution_runtime()

    assert executor.prepare_execution_runtime() is runtime
    assert executor.prepare_execution_runtime() is runtime
    assert runtime.audit_context == {"execution_id": "exec-1", "account_id": 7, "algorithm": "TEST"}
    assert executor.next_audit_seq() == 1
    assert executor.next_audit_seq() == 2
    assert runtime.next_audit_seq() == 3


def test_execute_releases_runtime_between_runs(monkeypatch) -> None:
    executor = _RuntimeExecutor()
    standard_input = UnifiedStandardInput.from_dict(
        {
            "channel_type": TradeChannel.CTP.value,
            "account_config": {
                "broker_id": "b",
                "investor_id": "i",
                "password": "p",
                "td_front": "tcp://td:1",
                "md_front": "tcp://md:2",
                "app_id": "app",
                "auth_code": "auth",
            },
            "curr_target": {"rb2610": 0.1},
            "algorithm": {"method": "TEST"},
        }
    )
    observed_runtimes: list[object] = []
    observed_seqs: list[int] = []

    class _FakeEngine:
        def run(self, incoming_input: UnifiedStandardInput) -> UnifiedStandardOutput:
            assert incoming_input is standard_input
            runtime = executor.require_execution_runtime()
            observed_runtimes.append(runtime)
            observed_seqs.append(executor.next_audit_seq())
            return UnifiedStandardOutput(
                account_assets=executor.get_account_assets(),
                inputs=incoming_input,
                status=ExecutionStatus.SUCCEEDED,
                channel_type=executor.channel_type,
                success=True,
            )

    monkeypatch.setattr(executor, "_execution_engine", lambda: _FakeEngine())

    executor.set_audit_context({"execution_id": "exec-1", "account_id": 1, "algorithm": "TEST"})
    executor.execute(standard_input)
    assert executor._active_execution_runtime is None

    executor.set_audit_context({"execution_id": "exec-2", "account_id": 2, "algorithm": "TEST"})
    executor.execute(standard_input)

    assert executor._active_execution_runtime is None
    assert observed_seqs == [1, 1]
    assert observed_runtimes[0] is not observed_runtimes[1]
