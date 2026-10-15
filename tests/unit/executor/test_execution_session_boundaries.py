"""ExecutionSession symbol-scoped 边界测试。"""

from __future__ import annotations

from typing import Any, cast

import pytest

from axile.common.trade_channel import TradeChannel
from axile.executor.abstract_executor.base import AbstractExecutor
from axile.executor.audit import ExecutionAuditSink
from axile.executor.execution_session import ExecutionSession
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_input import CTPAccountConfig
from axile.executor.models.unified_order import OrderDirection, OrderType, TradeRecord, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData


class _Logger:
    def info(self, _message: object, *args: object, **kwargs: object) -> None:
        _ = (args, kwargs)

    def warning(self, _message: object, *args: object, **kwargs: object) -> None:
        _ = (args, kwargs)

    def error(self, _message: object, *args: object, **kwargs: object) -> None:
        _ = (args, kwargs)


class _AuditSink(ExecutionAuditSink):
    def __init__(self, captured: dict[str, object]) -> None:
        self._captured = captured

    def append_event(self, **kwargs: object) -> bool:
        self._captured["event"] = kwargs
        return True

    def append_artifact(self, **kwargs: object) -> bool:
        self._captured["artifact"] = kwargs
        return True


class _SessionBoundaryExecutor(AbstractExecutor):
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
                update_time="2026-03-22T10:00:00",
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

    def _get_pending_orders_impl(self, _symbol: str | None = None) -> list[UnifiedOrder]:
        return []

    def _query_trades_impl(self, symbol: str, order_id: str) -> list[TradeRecord]:
        return [
            TradeRecord.create(
                trade_id=f"trade-{order_id}",
                trade_time="2026-03-22T10:00:00",
                trade_volume=1.0,
                trade_price=100.0,
                extra={"symbol": symbol, "order_id": order_id},
            )
        ]

    def _cleanup(self) -> None:
        return None

    def _get_account_mark(self) -> str:
        return "session-boundary"

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

    def _cancel_order_impl(self, symbol: str, order_id: str) -> bool:
        _ = (symbol, order_id)
        return True


def test_execution_session_keeps_symbol_local_memory() -> None:
    """symbol 级 memory 不应污染 owner executor 的 execution 级 memory。"""
    executor = _SessionBoundaryExecutor()
    executor.require_execution_runtime().memory["shared"] = "owner"
    session = ExecutionSession(owner=executor, symbol="rb2610", audit_context={"algorithm": "TEST-ALGO"})

    session.memory["local"] = "session"

    assert executor.require_execution_runtime().memory == {"shared": "owner"}
    assert session.memory == {"local": "session"}


def test_execution_session_proxies_symbol_scoped_owner_query_methods() -> None:
    captured: dict[str, object] = {"audit_metadata": {}}

    class _Owner:
        channel_type = TradeChannel.CTP
        logger = _Logger()

        def get_current_volume(self, symbol: str, account_assets: UnifiedAccountAssets) -> float:
            captured["current_volume"] = (symbol, account_assets.total_asset)
            return 2.5

        def get_positions_for_symbol(
            self,
            symbol: str,
            account_assets: UnifiedAccountAssets,
        ) -> list[tuple[float, object]]:
            captured["positions"] = (symbol, account_assets.total_asset)
            return [(1.0, "long")]

        def place_order(
            self,
            symbol: str,
            direction: OrderDirection,
            order_type: OrderType,
            volume: float,
            price: float = 0,
            **kwargs: object,
        ) -> UnifiedOrder:
            captured["place_order"] = (symbol, direction, order_type, volume, price, kwargs)
            return UnifiedOrder(
                order_id=f"order-{symbol}",
                symbol=symbol,
                direction=direction,
                order_type=order_type,
                volume=volume,
                price=price,
                status="SUBMITTED",
            )

        def _get_pending_orders_for_execution(self, symbol: str) -> list[UnifiedOrder]:
            captured["pending_orders_for_execution"] = symbol
            return [
                UnifiedOrder(
                    order_id="pending",
                    symbol=symbol,
                    direction=OrderDirection.BUY,
                    order_type=OrderType.LIMIT,
                    volume=1.0,
                    price=100.0,
                    status="PENDING",
                )
            ]

        def _query_trades_for_execution(self, symbol: str, order_id: str) -> list[TradeRecord]:
            captured["query_trades_for_execution"] = (symbol, order_id)
            return [
                TradeRecord.create(
                    trade_id=f"trade-{order_id}",
                    trade_time="2026-03-24T09:00:00",
                    trade_volume=1.0,
                    trade_price=100.0,
                    extra={"symbol": symbol, "order_id": order_id},
                )
            ]

        def is_termination_requested(self) -> bool:
            return True

        def get_termination_mode(self) -> str | None:
            return "graceful"

        def handle_termination_checkpoint(self, symbol: str | None = None) -> None:
            captured.setdefault("checkpoints", []).append(symbol)

        def get_account_assets(self) -> UnifiedAccountAssets:
            return UnifiedAccountAssets(available_cash=88.0, total_asset=100.0, market_value=12.0, positions=[])

        def get_market_data(self, symbols: list[str]) -> dict[str, UnifiedPriceData]:
            captured["market_data"] = symbols
            return {
                symbols[0]: UnifiedPriceData(
                    symbol=symbols[0],
                    last_price=101.0,
                    bid_price=100.0,
                    ask_price=102.0,
                    bid_volume=1.0,
                    ask_volume=1.0,
                    volume=10.0,
                    turnover=1010.0,
                    timestamp=1,
                    update_time="2024-01-01T00:00:00",
                )
            }

        def get_tick_size(self, symbol: str) -> float | None:
            captured["tick_size"] = symbol
            return 0.01

        def register_order_callback_for_symbol(self, callback: object, symbol: str) -> None:
            captured["order_callback"] = (callback, symbol)

        def register_price_callback_for_symbol(self, callback: object, symbol: str) -> None:
            captured["price_callback"] = (callback, symbol)

        def register_trade_callback_for_symbol(self, callback: object, symbol: str) -> None:
            captured["trade_callback"] = (callback, symbol)

        def register_trade_callback(self, callback: object) -> None:
            captured["global_trade_callback"] = callback

        def unregister_order_callback_for_symbol(self, callback: object, symbol: str) -> None:
            captured["unregister_order_callback"] = (callback, symbol)

        def unregister_price_callback_for_symbol(self, callback: object, symbol: str) -> None:
            captured["unregister_price_callback"] = (callback, symbol)

        def unregister_trade_callback_for_symbol(self, callback: object, symbol: str) -> None:
            captured["unregister_trade_callback"] = (callback, symbol)

        def unregister_trade_callback(self, callback: object) -> None:
            captured["global_unregister_trade_callback"] = callback

        def is_monitoring(self) -> bool:
            return True

        def cancel_order(self, symbol: str, order_id: str) -> bool:
            captured["cancel_order"] = (symbol, order_id)
            return True

        def register_order_audit_metadata(self, order_id: str, metadata: dict[str, object]) -> None:
            cast("dict[str, dict[str, object]]", captured["audit_metadata"])[order_id] = metadata

        def get_order_audit_metadata(self, order_id: str) -> dict[str, object]:
            return cast("dict[str, dict[str, object]]", captured["audit_metadata"])[order_id]

    owner = cast(AbstractExecutor, _Owner())
    session = ExecutionSession(owner=owner, symbol="rb2610", audit_context={"execution_id": "exec-1", "account_id": 7})
    account_assets = UnifiedAccountAssets(available_cash=10.0, total_asset=20.0, market_value=10.0, positions=[])

    assert session.get_current_volume(account_assets) == 2.5
    assert session.get_positions(account_assets) == [(1.0, "long")]
    assert session.place_order(OrderDirection.BUY, OrderType.LIMIT, 3.0, 99.0, tif="GTC").order_id == "order-rb2610"
    assert [order.order_id for order in session.get_pending_orders()] == ["pending"]
    assert [trade.trade_id for trade in session.query_trades("order-1")] == ["trade-order-1"]
    assert session.is_termination_requested() is True
    assert session.get_termination_mode() == "graceful"
    session.handle_termination_checkpoint()
    assert session.get_account_assets().available_cash == 88.0
    assert session.get_market_data() is not None
    assert session.get_tick_size() == 0.01
    session.register_order_callback(object())
    session.register_price_callback(object())
    trade_callback = cast(object, lambda trade: trade)
    session.register_trade_callback(cast("object", trade_callback))
    session.unregister_order_callback(object())
    session.unregister_price_callback(object())
    session.unregister_trade_callback(cast("object", trade_callback))
    assert session.is_monitoring() is True
    assert session.cancel_order("order-1") is True
    session.register_order_audit_metadata("order-1", {"note": "tracked"})
    assert session.get_order_audit_metadata("order-1") == {
        "note": "tracked",
        "audit_context": {"execution_id": "exec-1", "account_id": 7},
    }
    assert captured["place_order"] == ("rb2610", OrderDirection.BUY, OrderType.LIMIT, 3.0, 99.0, {"tif": "GTC"})
    assert captured["checkpoints"] == ["rb2610", "rb2610"]
    assert captured["market_data"] == ["rb2610"]
    assert captured["pending_orders_for_execution"] == "rb2610"
    assert captured["query_trades_for_execution"] == ("rb2610", "order-1")
    assert captured["tick_size"] == "rb2610"
    assert captured["cancel_order"] == ("rb2610", "order-1")
    assert cast("tuple[object, str]", captured["order_callback"])[1] == "rb2610"
    assert cast("tuple[object, str]", captured["price_callback"])[1] == "rb2610"
    assert cast("tuple[object, str]", captured["trade_callback"])[1] == "rb2610"
    assert cast("tuple[object, str]", captured["unregister_order_callback"])[1] == "rb2610"
    assert cast("tuple[object, str]", captured["unregister_price_callback"])[1] == "rb2610"
    assert cast("tuple[object, str]", captured["unregister_trade_callback"])[1] == "rb2610"
    assert set(captured) == {
        "audit_metadata",
        "current_volume",
        "positions",
        "place_order",
        "pending_orders_for_execution",
        "query_trades_for_execution",
        "checkpoints",
        "market_data",
        "tick_size",
        "order_callback",
        "price_callback",
        "trade_callback",
        "unregister_order_callback",
        "unregister_price_callback",
        "unregister_trade_callback",
        "cancel_order",
    }


def test_execution_session_emits_audit_event_and_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = _SessionBoundaryExecutor()
    session = ExecutionSession(
        owner=executor,
        symbol="ag2612",
        audit_context={"execution_id": "exec-2", "account_id": "7", "algorithm": "SINGLE-MAKER"},
    )
    captured: dict[str, object] = {}
    executor.set_audit_sink(_AuditSink(captured))

    assert (
        session.emit_audit_event(
            event_type="execution_started",
            status="info",
            reason_family="system",
            reason_code="COMMON.STARTED",
            details={"foo": "bar"},
        )
        is True
    )
    assert session.emit_audit_artifact(artifact_type="standard_input", content={"x": 1}) is True
    assert captured["event"] == {
        "execution_id": "exec-2",
        "account_id": 7,
        "channel": TradeChannel.CTP,
        "algorithm": "SINGLE-MAKER",
        "event_type": "execution_started",
        "status": "info",
        "reason_family": "system",
        "reason_code": "COMMON.STARTED",
        "symbol": "ag2612",
        "intent_id": None,
        "order_id": None,
        "client_order_id": None,
        "ts_exchange": None,
        "seq": 1,
        "details": {"foo": "bar"},
    }
    assert captured["artifact"] == {
        "execution_id": "exec-2",
        "artifact_type": "standard_input",
        "content": {"x": 1},
    }

    missing_context_session = ExecutionSession(owner=executor, symbol="rb2610", audit_context={"execution_id": "only"})
    assert (
        missing_context_session.emit_audit_event(
            event_type="x",
            status="y",
            reason_family="z",
            reason_code="missing",
        )
        is False
    )
    assert (
        ExecutionSession(owner=executor, symbol="rb2610").emit_audit_artifact(
            artifact_type="noop",
            content={},
        )
        is False
    )
