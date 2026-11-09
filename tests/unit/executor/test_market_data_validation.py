"""行情使用契约：来源不同也不能绕过首次定量、首次下单及追价检查。"""

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from loguru import logger

from axile.common.trade_channel import TradeChannel
from axile.executor.abstract_executor.capability import AbstractExecutorCapabilityMixin
from axile.executor.algorithms.utils.clock import get_default_clock
from axile.executor.algorithms.utils.order_tracker import OrderTracker
from axile.executor.execution_session import ExecutionSession
from axile.executor.market_data_validation import MarketDataUnavailableError, market_data_error
from axile.executor.models.unified_order import OrderDirection, OrderType
from axile.executor.models.unified_price import UnifiedPriceData


def quote(now=1000.0, **changes):
    values = dict(
        symbol="600584.SH",
        last_price=10.0,
        bid_price=9.9,
        ask_price=10.1,
        bid_volume=100,
        ask_volume=100,
        volume=0,
        timestamp=int(now * 1000),
        update_time="",
        extra={"source": "test", "time_basis": "exchange"},
    )
    values.update(changes)
    return UnifiedPriceData(**values)


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"timestamp": 0}, "时间无效"),
        ({"timestamp": 1001001}, "时间超前"),
        ({"timestamp": 994000}, "已过期"),
        ({"last_price": float("nan")}, "参考价格无效"),
        ({"last_price": float("inf")}, "参考价格无效"),
    ],
)
def test_invalid_sizing_quote(changes, reason):
    assert reason in market_data_error(quote(**changes), now=1000, max_age=5, purpose="sizing")


@pytest.mark.parametrize("purpose", ["sizing", "order"])
@pytest.mark.parametrize("connected", [True, False])
def test_future_tolerance_requires_a_live_account(purpose, connected):
    owner = Owner(quote(1001))
    owner.account_config = object() if connected else None
    expected = None if connected else "行情时间超前"
    assert owner.market_data_error(owner.quote, purpose=purpose, now=1000) == expected


def test_sizing_does_not_require_order_book():
    q = quote(bid_price=0, ask_price=0, book_valid=False)
    assert market_data_error(q, now=1000, max_age=5, purpose="sizing") is None
    assert market_data_error(q, now=1000, max_age=5, purpose="order") == "缺少真实盘口"


@pytest.mark.parametrize(
    "changes", [{"bid_price": float("nan")}, {"ask_price": float("inf")}, {"bid_price": 11}, {"book_valid": False}]
)
def test_invalid_order_book(changes):
    assert market_data_error(quote(**changes), now=1000, max_age=5, purpose="order")


class Owner(AbstractExecutorCapabilityMixin):
    channel_type = TradeChannel.GM
    logger = logger

    def __init__(self, q):
        self.quote = q
        self.orders = []
        self.cancels = []
        self.events = []
        self.callbacks = []

    def is_termination_requested(self):
        return False

    def get_market_data(self, symbols):
        return {self.quote.symbol: self.quote}

    def place_order(self, *args, **kwargs):
        self.orders.append(args)
        return "order"

    def cancel_order(self, *args):
        self.cancels.append(args)
        return True

    def emit_audit_event(self, **kwargs):
        self.events.append(kwargs)
        return True

    def register_price_callback_for_symbol(self, cb, symbol):
        self.callbacks.append(cb)

    def unregister_price_callback_for_symbol(self, cb, symbol):
        self.callbacks.remove(cb)


@pytest.fixture
def now(monkeypatch):
    value = [1000.0]
    monkeypatch.setattr(get_default_clock(), "time", lambda: value[0])
    return value


def test_quote_expires_between_read_and_place_order(now):
    owner = Owner(quote())
    session = ExecutionSession(owner, "600584.SH")
    session.get_market_data()
    now[0] += 11
    with pytest.raises(MarketDataUnavailableError, match="已过期"):
        session.place_order(OrderDirection.BUY, OrderType.LIMIT, 100, 9.9)
    assert owner.orders == []
    assert session.cancel_order("existing")
    assert owner.events[-1]["details"]["source"] == "test"
    assert owner.events[-1]["details"]["quote_timestamp"] == 1000000


@pytest.mark.parametrize("callback_raises", [False, True])
def test_subscription_evidence_is_scoped_to_callback_and_unregisters(now, callback_raises):
    owner = Owner(quote())
    session = ExecutionSession(owner, "600584.SH")
    received = []
    session.get_market_data()

    def callback(fresh):
        received.append(fresh)
        assert session.place_order(OrderDirection.BUY, OrderType.LIMIT, 100, fresh.bid_price) == "order"
        if callback_raises:
            raise RuntimeError("callback failed")

    session.register_price_callback(callback)
    now[0] += 11
    fresh = quote(now[0], bid_price=8.9, ask_price=9.1)
    if callback_raises:
        with pytest.raises(RuntimeError, match="callback failed"):
            owner.callbacks[0](fresh)
    else:
        owner.callbacks[0](fresh)
    # 新回调不能替算法调用链中仍在消费的旧价格续期。
    with pytest.raises(MarketDataUnavailableError, match="已过期"):
        session.place_order(OrderDirection.BUY, OrderType.LIMIT, 100, 9.9)
    assert len(owner.orders) == 1
    assert session.place_order(OrderDirection.BUY, OrderType.LIMIT, 100, 8.9, market_data=fresh) == "order"
    session.unregister_price_callback(callback)
    assert owner.callbacks == []
    assert received == [fresh]


def test_new_session_does_not_reuse_previous_execution_quote(now):
    owner = Owner(quote())
    ExecutionSession(owner, "600584.SH").get_market_data()
    with pytest.raises(MarketDataUnavailableError, match="未就绪"):
        ExecutionSession(owner, "600584.SH").place_order(OrderDirection.BUY, OrderType.LIMIT, 100, 9.9)
    assert not owner.orders


@pytest.mark.parametrize("purpose", ["sizing", "order"])
@pytest.mark.parametrize("age,expected", [(6, None), (10, None), (10.001, "行情已过期")])
def test_default_quote_age_boundary(purpose, age, expected):
    owner = Owner(quote())
    assert owner.market_data_error(owner.quote, purpose=purpose, now=1000 + age) == expected


def test_stale_first_quote_is_rejected(now):
    owner = Owner(quote(989))
    with pytest.raises(MarketDataUnavailableError):
        ExecutionSession(owner, "600584.SH").get_market_data()
    assert not owner.orders


def test_gm_rejects_previous_day_and_auction_phase():
    pytest.importorskip("gm.api")
    from axile.executor.gm.gm_execute import GMExecutor

    executor = object.__new__(GMExecutor)
    tz = ZoneInfo("Asia/Shanghai")
    current = datetime(2026, 11, 3, 9, 20, 1, tzinfo=tz).timestamp()
    yesterday = quote(current - 86400)
    assert executor.market_data_error(yesterday, now=current) == "行情已过期"
    assert executor.market_data_error(quote(current - 2), now=current) == "行情不属于当前竞价阶段"
    assert executor.market_data_error(quote(current), now=current) is None


def test_tracker_uses_channel_validator_instead_of_own_age():
    owner = SimpleNamespace(market_data_error=lambda q, **kwargs: "渠道拒绝", logger=logger)
    tracker = OrderTracker(executor=owner)
    assert not tracker._price_is_fresh(quote(), 1000)


def test_ctp_reuses_trading_day_and_configured_age():
    from axile.executor.ctp.ctp_execute import CTPExecutor

    executor = object.__new__(CTPExecutor)
    executor._trading_day = "20261104"
    executor._config = lambda: SimpleNamespace(quote_max_age_seconds=20)
    q = quote(990, extra={"trading_day": "20261104", "received_at": 990.0})
    assert executor.market_data_error(q, purpose="sizing", now=1000) is None
    q.extra["trading_day"] = "20261103"
    assert executor.market_data_error(q, purpose="sizing", now=1000) == "行情交易日不匹配"


def test_tq_cached_quote_is_not_permanently_fresh():
    from axile.executor.tq.tq_execute import TQExecutor

    executor = object.__new__(TQExecutor)
    assert executor.market_data_error(quote(), purpose="sizing", now=1000) is None
    assert executor.market_data_error(quote(989), purpose="sizing", now=1000) == "行情已过期"
