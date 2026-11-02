from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

import pytest

from axile.executor.models.unified_order import OrderDirection, OrderType, TradeRecord, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData


def _load_dispatcher_class(module_name: str, relative_path: str, class_name: str) -> type[Any]:
    module_path = Path(__file__).resolve().parents[3] / relative_path
    spec = spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, class_name)


GMCallbackDispatcher = _load_dispatcher_class(
    "test_gm_callback_dispatcher_module",
    "axile/executor/gm/core/callback_dispatcher.py",
    "GMCallbackDispatcher",
)


def _build_order(symbol: str) -> UnifiedOrder:
    return UnifiedOrder(
        order_id=f"order-{symbol}",
        symbol=symbol,
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        volume=1.0,
        price=100.0,
        status="FILLED",
    )


def _build_trade(symbol: str) -> TradeRecord:
    return TradeRecord.create(
        trade_id=f"trade-{symbol}",
        trade_time="2024-01-01T00:00:00",
        trade_volume=1.0,
        trade_price=100.0,
        symbol=symbol,
    )


def _build_price(symbol: str) -> UnifiedPriceData:
    return UnifiedPriceData(
        symbol=symbol,
        last_price=100.0,
        asks=[(101.0, 1.0)],
        bids=[(99.0, 1.0)],
        volume=10.0,
        turnover=1000.0,
        timestamp=1,
        update_time="2024-01-01T00:00:00",
    )


@pytest.mark.parametrize(
    ("dispatcher_cls", "has_stats"),
    [
        (GMCallbackDispatcher, True),
    ],
)
def test_dispatchers_register_broadcast_isolate_errors_and_clear_callbacks(
    dispatcher_cls: type[Any],
    has_stats: bool,  # noqa: FBT001
) -> None:
    dispatcher = dispatcher_cls()
    events: list[tuple[str, str]] = []

    def order_callback(order: UnifiedOrder) -> None:
        events.append(("order", order.symbol))

    def trade_callback(trade: TradeRecord) -> None:
        events.append(("trade", trade.trade_id))

    def price_callback(price: UnifiedPriceData) -> None:
        events.append(("price", price.symbol))

    def broken_order_callback(_order: UnifiedOrder) -> None:
        raise RuntimeError("broken-order")

    dispatcher.dispatch_order_update(_build_order("rb2610"))
    dispatcher.dispatch_trade_record(_build_trade("rb2610"))
    dispatcher.dispatch_price_data(_build_price("rb2610"))
    assert dispatcher.get_callback_count() == {
        "order_callbacks": 0,
        "trade_callbacks": 0,
        "price_callbacks": 0,
    }

    dispatcher.register_order_callback(order_callback)
    dispatcher.register_order_callback(order_callback)
    dispatcher.register_order_callback(broken_order_callback)
    dispatcher.register_trade_callback(trade_callback)
    dispatcher.register_trade_callback(trade_callback)
    dispatcher.register_price_callback(price_callback)
    dispatcher.register_price_callback(price_callback)
    assert dispatcher.get_callback_count() == {
        "order_callbacks": 2,
        "trade_callbacks": 1,
        "price_callbacks": 1,
    }

    dispatcher.dispatch_order_update(_build_order("rb2610"))
    dispatcher.dispatch_trade_record(_build_trade("ag2612"))
    dispatcher.dispatch_price_data(_build_price("au2612"))
    assert events == [
        ("order", "rb2610"),
        ("trade", "trade-ag2612"),
        ("price", "au2612"),
    ]

    dispatcher.unregister_order_callback(broken_order_callback)
    dispatcher.unregister_order_callback(broken_order_callback)
    dispatcher.unregister_order_callback(order_callback)
    dispatcher.unregister_trade_callback(trade_callback)
    dispatcher.unregister_trade_callback(trade_callback)
    dispatcher.unregister_price_callback(price_callback)
    dispatcher.unregister_price_callback(price_callback)
    assert dispatcher.get_callback_count() == {
        "order_callbacks": 0,
        "trade_callbacks": 0,
        "price_callbacks": 0,
    }

    if has_stats:
        assert dispatcher.get_stats() == {
            "order_updates_received": 2,
            "trade_records_received": 2,
            "price_updates_received": 2,
        }

    dispatcher.register_order_callback(order_callback)
    dispatcher.register_trade_callback(trade_callback)
    dispatcher.register_price_callback(price_callback)
    dispatcher.clear_all_callbacks()
    assert dispatcher.get_callback_count() == {
        "order_callbacks": 0,
        "trade_callbacks": 0,
        "price_callbacks": 0,
    }
