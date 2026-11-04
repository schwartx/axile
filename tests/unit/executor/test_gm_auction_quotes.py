"""集合竞价快照和订阅共享取价规则，旧行情仍不能参与交易。"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from axile.channels.gm_sessions import SHANGHAI
from axile.executor.execution_session import ExecutionSession
from axile.executor.gm.core import api_bridge, gm_strategy
from axile.executor.gm.core.api_bridge import CurrentRequest, GMApiBridge
from axile.executor.gm.gm_execute import GMExecutor
from axile.executor.market_data_validation import MarketDataUnavailableError
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_input import GMAccountConfig
from axile.executor.models.unified_order import OrderDirection, OrderType


def test_current_requests_auction_ticks(monkeypatch):
    sdk = Mock(return_value=[])
    monkeypatch.setattr(api_bridge, "current", sdk)
    GMApiBridge.execute_request(CurrentRequest(symbols=["SHSE.600584"]))
    sdk.assert_called_once_with(symbols=["SHSE.600584"], include_call_auction=True)


@pytest.mark.parametrize("source", ["snapshot", "subscription"])
@pytest.mark.parametrize("last_trade_price", [0.0, 99.0])
@pytest.mark.parametrize("hour,minute", [(9, 16), (9, 24), (14, 58), (9, 30)])
@pytest.mark.parametrize("bid,ask", [(10.0, 10.0), (0.0, 10.0), (9.9, 10.0), (float("inf"), float("inf"))])
def test_auction_reference_price_and_submission(monkeypatch, source, last_trade_price, hour, minute, bid, ask):
    now = datetime(2026, 11, 4, hour, minute, tzinfo=SHANGHAI)
    instance = GMExecutor(
        GMAccountConfig(connection_mode="service", account_id="test", token="test", serv_addr="127.0.0.1:7001"),
        connect=False,
    )
    tick = dict(
        symbol="SHSE.600584",
        created_at=now,
        price=last_trade_price,
        last_volume=0,
        quotes=[dict(bid_p=bid, ask_p=ask, bid_v=100, ask_v=100)],
    )
    bridge = Mock(return_value=[tick])
    monkeypatch.setattr(instance, "_call_bridge", bridge)
    if source == "snapshot":
        quote = instance.get_market_data(["600584.SH"])["600584.SH"]
    else:
        quote = gm_strategy._convert_tick_to_unified(
            SimpleNamespace(**(tick | {"quotes": [SimpleNamespace(**tick["quotes"][0])]}))
        )
    auction = (hour, minute) != (9, 30)
    matched = bid == ask == 10.0
    expected_price = (10.0 if matched else 0.0) if auction else last_trade_price
    assert quote.last_price == expected_price
    if auction:
        assert quote.extra["gm_last_trade_price"] == last_trade_price
        assert quote.extra["price_basis"] == "auction_indicative"
    monkeypatch.setattr(instance, "market_data_now", lambda: now.timestamp())
    if (auction and not matched) or (not auction and last_trade_price == 0):
        assert instance.market_data_error(quote, purpose="sizing") == "参考价格无效"
        return
    if not matched:
        return
    assets = UnifiedAccountAssets(available_cash=10000, total_asset=10000, market_value=0, positions=[])
    decisions = instance._calculate_target_sizing_base(
        {"600584.SH": 0.1},
        assets,
        {"600584.SH": quote},
        {},
        {},
    )
    assert decisions["600584.SH"].reference_price == quote.last_price
    assert decisions["600584.SH"].target_quantity is not None
    session = ExecutionSession(instance, "600584.SH")
    submit = Mock(return_value="order")
    monkeypatch.setattr(instance, "place_order", submit)
    monkeypatch.setattr(session, "emit_audit_event", lambda **_: True)
    assert session.place_order(OrderDirection.BUY, OrderType.LIMIT, 100, 10, market_data=quote) == "order"
    monkeypatch.setattr(instance, "market_data_now", lambda: now.timestamp() + 6)
    with pytest.raises(MarketDataUnavailableError, match="行情已过期"):
        session.place_order(OrderDirection.BUY, OrderType.LIMIT, 100, 10, market_data=quote)
    assert submit.call_count == 1
