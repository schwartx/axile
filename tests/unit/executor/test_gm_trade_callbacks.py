"""GM 原生成交回调与查询共享统一 symbol，早到成交仍可回放。"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from axile.executor.algorithms.utils.order_tracker import OrderTracker
from axile.executor.gm.common import convert_gm_trade_to_trade_record
from axile.executor.gm.core import gm_strategy
from axile.executor.gm.core.callback_dispatcher import GMCallbackDispatcher
from axile.executor.models.unified_order import OrderDirection, OrderType, UnifiedOrder


class _DictLikeReport(dict):
    __getattr__ = dict.__getitem__


@pytest.fixture
def native_trade():
    return {
        "exec_id": None,
        "cl_ord_id": "gm-client-1",
        "order_id": "exchange-1",
        "symbol": "SHSE.600000",
        "created_at": datetime.fromisoformat("2026-10-25T10:00:00+08:00"),
        "volume": 40,
        "price": 12.3,
        "account_id": "account-1",
        "native_evidence": {"missing_fee": None, "is_maker": False},
    }


@pytest.mark.parametrize("report_type", [_DictLikeReport, SimpleNamespace])
def test_early_native_trade_callback_replays_after_canonical_order_is_added(monkeypatch, native_trade, report_type):
    tracker = OrderTracker(executor=SimpleNamespace(symbol="600000.SH", logger=MagicMock()))
    dispatcher = GMCallbackDispatcher()
    dispatcher.register_trade_callback(tracker.on_trade_record)
    context = SimpleNamespace(dispatcher=dispatcher, stats={"execution_report_received": 0, "errors": 0})
    monkeypatch.setattr(gm_strategy, "_get_bridge_context", lambda: context)
    monkeypatch.setattr(gm_strategy, "_is_stopped", lambda: False)

    gm_strategy.on_execution_report(None, report_type(**native_trade))
    tracker.add_order(
        UnifiedOrder(
            order_id="gm-client-1",
            symbol="600000.SH",
            direction=OrderDirection.BUY,
            order_type=OrderType.LIMIT,
            volume=100,
            price=12.3,
            status="待成交",
        )
    )

    query_trade = convert_gm_trade_to_trade_record(native_trade)
    assert query_trade.trade_id == "gm-fallback:61d42224d6081314"
    trades = tracker.get_order_trades("gm-client-1")
    assert [trade.trade_id for trade in trades] == [query_trade.trade_id]
    assert trades[0].symbol == query_trade.symbol == "600000.SH"
    assert trades[0].trade_time == query_trade.trade_time
    assert context.stats == {"execution_report_received": 1, "errors": 0}


@pytest.mark.parametrize("report_type", [_DictLikeReport, SimpleNamespace])
@pytest.mark.parametrize("volume,price", [(None, None), (0, 0), (40, 12.3)])
def test_callback_preserves_native_hash_raw_evidence_and_null_numeric_fallbacks(
    native_trade, report_type, volume, price
):
    native_trade.update(volume=volume, price=price)
    original = dict(native_trade)

    trade = gm_strategy._convert_execrpt_to_trade(report_type(**native_trade))
    query_trade = convert_gm_trade_to_trade_record({**native_trade, "volume": volume or 0, "price": price or 0})

    assert trade.trade_id == query_trade.trade_id
    assert trade.trade_volume == (volume or 0)
    assert trade.trade_price == (price or 0)
    assert trade.symbol == "600000.SH"
    assert trade.extra["symbol"] == "SHSE.600000"
    assert trade.extra["raw_trade_data"] == (original if report_type is _DictLikeReport else {})
    assert native_trade == original
