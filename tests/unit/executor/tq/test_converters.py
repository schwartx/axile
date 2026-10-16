from __future__ import annotations

import math

import pytest

from axile.executor.constants.order_status import OrderStatus
from axile.executor.models.unified_account_assets import PositionDirection
from axile.executor.tq.converters import account_to_unified, order_to_unified, quote_to_unified, trade_to_unified
from axile.executor.tq.symbols import TQInstrument, TQSymbolResolver


def _resolver() -> TQSymbolResolver:
    return TQSymbolResolver([TQInstrument("SHFE.rb2610", "rb2610", "SHFE", "FUTURE")])


@pytest.mark.parametrize("message", ["开仓资金不足", "平今仓手数不足", ""])
def test_sdk_error_flag_identifies_rejected_orders_without_matching_message(message):
    order = order_to_unified(
        {
            "order_id": "rejected",
            "exchange_id": "SHFE",
            "instrument_id": "rb2610",
            "direction": "BUY",
            "volume_orign": 1,
            "volume_left": 1,
            "status": "FINISHED",
            "is_error": True,
            "last_msg": message,
        },
        _resolver(),
    )
    assert order.status == OrderStatus.REJECTED
    assert order.extra["last_msg"] == message


def test_quote_and_order_return_to_common_symbol() -> None:
    resolver = _resolver()
    quote = quote_to_unified(
        {
            "exchange_id": "SHFE",
            "instrument_id": "rb2610",
            "last_price": 3200,
            "bid_price1": 3199,
            "ask_price1": 3201,
            "bid_volume1": 2,
            "ask_volume1": 3,
            "datetime": 1_700_000_000_000_000_000,
        },
        resolver,
    )
    order = order_to_unified(
        {
            "order_id": "o1",
            "exchange_id": "SHFE",
            "instrument_id": "rb2610",
            "direction": "BUY",
            "offset": "OPEN",
            "price_type": "LIMIT",
            "volume_orign": 2,
            "volume_left": 0,
            "limit_price": 3200,
            "status": "FINISHED",
        },
        resolver,
    )

    assert quote.symbol == "rb2610"
    assert order.symbol == "rb2610"
    assert order.status == OrderStatus.FILLED


def test_trade_reads_tqsdk_trade_entity_fields() -> None:
    trade = trade_to_unified(
        {
            "trade_id": "t1",
            "exchange_trade_id": "exchange-t1",
            "order_id": "o1",
            "exchange_id": "SHFE",
            "instrument_id": "rb2610",
            "direction": "SELL",
            "offset": "CLOSE",
            "price": 3092.0,
            "volume": 4,
            "trade_date_time": 1_700_000_000_000_000_000,
        },
        _resolver(),
    )

    assert trade.symbol == "rb2610"
    assert trade.order_id == "o1"
    assert trade.trade_volume == 4.0
    assert trade.trade_price == 3092.0
    assert trade.trade_value == 4.0 * 3092.0
    assert trade.extra["offset"] == "CLOSE"
    assert trade.extra["exchange_trade_id"] == "exchange-t1"


def test_trade_price_nan_falls_back_to_zero() -> None:
    trade = trade_to_unified(
        {
            "trade_id": "t2",
            "order_id": "o1",
            "exchange_id": "SHFE",
            "instrument_id": "rb2610",
            "direction": "BUY",
            "offset": "OPEN",
            "price": float("nan"),
            "volume": 1,
            "trade_date_time": 1_700_000_000_000_000_000,
        },
        _resolver(),
    )

    assert math.isfinite(trade.trade_price)
    assert trade.trade_price == 0.0
    assert trade.trade_value == 0.0


def test_positions_expose_complete_today_yesterday_breakdown() -> None:
    assets = account_to_unified(
        {"available": 900_000, "balance": 1_000_000},
        {
            "SHFE.rb2610": {
                "pos_long_today": 2,
                "pos_long_his": 3,
                "pos_short_today": 1,
                "pos_short_his": 0,
                "position_cost_long": 50_000,
                "position_cost_short": 10_000,
            }
        },
        _resolver(),
        {"SHFE.rb2610": {"last_price": 3200, "volume_multiple": 10, "datetime": "2026-09-22T14:59:00"}},
    )

    assert [(position.direction, position.volume) for position in assets.positions] == [
        (PositionDirection.LONG.value, 5),
        (PositionDirection.SHORT.value, 1),
    ]
    assert assets.positions[0].extra["net_position"] == 4
    assert assets.positions[1].extra["long_yd"] == 3
    assert [position.market_value for position in assets.positions] == [160_000, 32_000]
    assert assets.market_value == 192_000
    assert assets.total_asset == 1_000_000
    assert assets.available_cash == 900_000
    assert assets.positions[0].extra["position_cost"] == 50_000
    assert assets.positions[0].extra["market_value_provenance"]["price_status"] == "reference"


def test_position_value_requires_every_contract_to_be_valued() -> None:
    assets = account_to_unified(
        {"available": 10, "balance": 20},
        {"SHFE.rb2610": {"pos_long_today": 1}, "SHFE.ag2610": {"pos_short_today": 2}},
        _resolver(),
        {"SHFE.rb2610": {"last_price": 100, "volume_multiple": 10}},
    )
    assert assets.positions[0].market_value == 1000
    assert assets.positions[1].market_value is None
    assert assets.market_value is None
    assert assets.positions[1].extra["market_value_provenance"]["reason"] == "missing_quote"


def test_invalid_price_multiplier_and_empty_positions() -> None:
    row = {"SHFE.rb2610": {"pos_long_today": 1}}
    for quote, reason in [
        ({"last_price": float("nan"), "volume_multiple": 10}, "invalid_price"),
        ({"last_price": 100, "volume_multiple": 0}, "invalid_multiplier"),
        ({"last_price": 1e308, "volume_multiple": 1e308}, "calculation_overflow"),
    ]:
        assets = account_to_unified({}, row, _resolver(), {"SHFE.rb2610": quote})
        assert assets.market_value is None
        assert assets.positions[0].extra["market_value_provenance"]["reason"] == reason
    assert account_to_unified({}, {}, _resolver()).market_value == 0
