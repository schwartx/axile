"""真实算法吞掉行情异常后的会话归因与执行结果边界。"""

from threading import Event

import pytest

from axile.common.order_param_model import OrderParamModel
from axile.domain.execution import ExecutionReasonFamily
from axile.executor import execution_engine as engine_module
from axile.executor.algorithms.core.base import AlgorithmInput, resolve_algorithm
from axile.executor.algorithms.defaults.ctp_target_pos_task.impl import CTPTargetPosTaskParams
from axile.executor.algorithms.defaults.pov.impl import PovParams
from axile.executor.algorithms.defaults.single_maker.impl import SingleMakerParams
from axile.executor.algorithms.defaults.twap.impl import TwapParams
from axile.executor.algorithms.utils import clock as clock_module
from axile.executor.constants.order_status import OrderStatus
from axile.executor.execution_engine import ExecutionEngine
from axile.executor.execution_session import ExecutionSession
from axile.executor.market_data_validation import MarketDataUnavailableError
from axile.executor.models.execution_result import AlgorithmResult, ExecutionOutcome, ExecutionStatus
from axile.executor.models.unified_account_assets import PositionDirection, UnifiedAccountAssets
from axile.executor.models.unified_callback import OrderUpdateCallback, PriceDataCallback, TradeRecordCallback
from axile.executor.models.unified_order import OrderDirection, OrderType, TradeRecord, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData
from tests.unit.executor.test_base_executor_symbol_dispatch import _assets, _price, _TestExecutor

SYMBOL = "rb2612.SHFE"
REASON = "COMMON.MARKET_DATA.UNAVAILABLE"


class _FrozenClock:
    def __init__(self) -> None:
        self.now = 1_793_664_000.0
        self.waits: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds

    def event_wait(self, event: Event, timeout: float | None = None) -> bool:
        if event.is_set():
            return True
        assert timeout is not None, "离线测试禁止无限等待"
        self.sleep(timeout)
        return event.is_set()


class _QuoteExecutor(_TestExecutor):
    order_param_model = OrderParamModel.OFFSET

    def __init__(self, quote: UnifiedPriceData) -> None:
        super().__init__()
        self.quote = quote
        self.position = 0.0
        self.submissions: list[UnifiedOrder] = []
        self.fill_trades: list[TradeRecord] = []
        self.order_callbacks: list[OrderUpdateCallback] = []
        self.trade_callbacks: list[TradeRecordCallback] = []
        self.price_callbacks: list[PriceDataCallback] = []

    def get_account_assets(self) -> UnifiedAccountAssets:
        self.account_assets_calls += 1
        positions = [(SYMBOL, self.position, PositionDirection.LONG)] if self.position else []
        return _assets(positions=positions)

    def get_market_data(self, symbols: list[str]) -> dict[str, UnifiedPriceData]:
        self.market_data_requests.append(list(symbols))
        return {symbol: self.quote.model_copy(deep=True) for symbol in symbols}

    def register_order_callback(self, callback: OrderUpdateCallback) -> None:
        self.order_callbacks.append(callback)

    def unregister_order_callback(self, callback: OrderUpdateCallback) -> None:
        self.order_callbacks.remove(callback)

    def register_trade_callback(self, callback: TradeRecordCallback) -> None:
        self.trade_callbacks.append(callback)

    def unregister_trade_callback(self, callback: TradeRecordCallback) -> None:
        self.trade_callbacks.remove(callback)

    def register_price_callback(self, callback: PriceDataCallback) -> None:
        self.price_callbacks.append(callback)
        # POV 必须真实收到成交量回调才会走参与下单，而非只覆盖到期补单。
        callback(self.quote.model_copy(deep=True))

    def unregister_price_callback(self, callback: PriceDataCallback) -> None:
        self.price_callbacks.remove(callback)

    def _place_order_impl(
        self,
        symbol: str,
        direction: OrderDirection,
        order_type: OrderType,
        volume: float,
        price: float = 0,
        **kwargs: object,
    ) -> UnifiedOrder:
        order = super()._place_order_impl(symbol, direction, order_type, volume, price, **kwargs)
        order.order_id = f"order-{len(self.submissions) + 1}"
        order.status = OrderStatus.FILLED
        order.filled_volume = volume
        order.avg_price = self.quote.last_price
        self.submissions.append(order)
        self.position += volume if direction == OrderDirection.BUY else -volume
        trade = TradeRecord.create(
            trade_id=f"trade-{order.order_id}",
            symbol=symbol,
            order_id=order.order_id,
            trade_volume=volume,
            trade_price=order.avg_price,
        )
        self.fill_trades.append(trade)
        for callback in self.trade_callbacks:
            callback(trade)
        for callback in self.order_callbacks:
            callback(order)
        return order

    def _query_trades_impl(self, symbol: str, order_id: str) -> list[TradeRecord]:
        return [trade for trade in self.fill_trades if trade.symbol == symbol and trade.order_id == order_id]


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FrozenClock:
    clock = _FrozenClock()
    monkeypatch.setattr(clock_module, "_default_clock", clock)
    return clock


@pytest.fixture
def execution(clock: _FrozenClock) -> tuple[_QuoteExecutor, ExecutionEngine, ExecutionSession]:
    owner = _QuoteExecutor(_price(SYMBOL))
    engine = ExecutionEngine(owner)
    session = engine._create_symbol_session(SYMBOL, None)
    assert type(session) is ExecutionSession
    return owner, engine, session


def _expire_before_submit(
    monkeypatch: pytest.MonkeyPatch, session: ExecutionSession, clock: _FrozenClock
) -> list[OrderType]:
    real_place_order = session.place_order
    attempted_types: list[OrderType] = []

    def place_order(direction, order_type, volume, price=0, **kwargs):
        attempted_types.append(order_type)
        # 读行情与算法 deadline 检查都已完成；只推进时钟，不替换校验或伪造异常。
        clock.now += 6
        return real_place_order(direction, order_type, volume, price, **kwargs)

    monkeypatch.setattr(session, "place_order", place_order)
    return attempted_types


def _assert_blocked(result: AlgorithmResult, session: ExecutionSession, owner: _QuoteExecutor) -> None:
    failure = session.market_data_failure
    assert isinstance(failure, MarketDataUnavailableError)
    assert result.error == failure.execution_error
    assert result.reason_code == REASON
    assert result.memory["symbol_decision_reason_family"] == ExecutionReasonFamily.MARKET_RULE.value
    assert result.status == ExecutionStatus.BLOCKED
    assert result.outcome == ExecutionOutcome.BLOCKED
    assert result.outcome_reason == result.error
    assert not result.success
    assert result.orders == result.trades == owner.submissions == []
    assert owner.order_callbacks == owner.trade_callbacks == owner.price_callbacks == []


def test_uncaught_market_data_error_keeps_blocked_reason_and_market_family(
    monkeypatch: pytest.MonkeyPatch, clock: _FrozenClock, execution
) -> None:
    owner, engine, session = execution

    def uncaught_failure(real_session, algorithm_input):
        quote = real_session.get_market_data()
        clock.now += 6
        real_session.place_order(OrderDirection.BUY, OrderType.LIMIT, 1, quote.bid_price)
        pytest.fail("过期行情不得提交")

    monkeypatch.setattr(engine_module, "resolve_algorithm", lambda name, executor: uncaught_failure)
    task = engine_module._PreparedSymbolAlgorithm(
        symbol=SYMBOL, algorithm_name="TEST-UNCAUGHT", algorithm_input=AlgorithmInput(SYMBOL, 1, {})
    )
    result = engine._run_symbol_algorithm_with_error_capture(
        task, lambda: engine._run_algorithm_with_session(session, task.algorithm_name, task.algorithm_input)
    )

    _assert_blocked(result, session, owner)
    assert result.symbol == SYMBOL
    assert result.algorithm == task.algorithm_name
    assert "已过期" in result.error
    assert clock.waits == []


@pytest.mark.parametrize(
    "algorithm_name,params",
    [
        ("SINGLE-MAKER", SingleMakerParams(max_wait_seconds=30, chase_enabled=False)),
        ("TWAP", TwapParams(total_duration=30, slices=1, max_wait_seconds=30)),
        (
            "POV",
            PovParams(participation_rate=1, interval_seconds=1, max_duration=1, complete_on_timeout=False),
        ),
        ("TARGET-POS-TASK", CTPTargetPosTaskParams(max_wait_seconds=30, chase_enabled=False)),
    ],
)
def test_builtin_caught_pre_submit_stale_quote_is_blocked(
    monkeypatch: pytest.MonkeyPatch, clock: _FrozenClock, execution, algorithm_name, params
) -> None:
    owner, engine, session = execution
    algorithm = resolve_algorithm(algorithm_name, session)
    raw_results: list[AlgorithmResult] = []

    def observe_algorithm(real_session, algorithm_input):
        result = algorithm(real_session, algorithm_input)
        raw_results.append(result.model_copy(deep=True))
        return result

    monkeypatch.setattr(engine_module, "resolve_algorithm", lambda name, executor: observe_algorithm)
    attempted_types = _expire_before_submit(monkeypatch, session, clock)
    result = engine._run_algorithm_with_session(session, algorithm_name, AlgorithmInput(SYMBOL, 1, {}, params=params))

    assert attempted_types == [OrderType.LIMIT]
    assert len(raw_results) == 1
    assert raw_results[0].status == ExecutionStatus.FAILED
    assert raw_results[0].outcome == ExecutionOutcome.ERROR
    assert raw_results[0].reason_code is None
    assert raw_results[0].error == session.market_data_failure.execution_error
    assert "已过期" in result.error
    assert result.symbol == SYMBOL
    assert result.algorithm == algorithm_name
    _assert_blocked(result, session, owner)
    assert clock.waits == []
    if algorithm_name == "POV":
        assert result.memory["market_volume_seen"] == 10
        assert len(result.memory["slice_details"]) == 1


@pytest.mark.parametrize(
    "on_missing_book,stale,blocked,order_type",
    [
        ("market", False, False, OrderType.MARKET),
        ("market", True, True, OrderType.MARKET),
    ],
)
def test_single_maker_missing_book_market_fallback_keeps_freshness_and_limit_guards(
    monkeypatch: pytest.MonkeyPatch, clock: _FrozenClock, execution, on_missing_book, stale, blocked, order_type
) -> None:
    owner, engine, session = execution
    # 保留有效参考价，明确标识为非真实盘口。
    owner.quote.book_valid = False
    if on_missing_book == "market":
        owner.quote.bid_price = owner.quote.ask_price = 0
    attempted_types = _expire_before_submit(monkeypatch, session, clock) if stale else None
    params = SingleMakerParams(max_wait_seconds=30, chase_enabled=False, on_missing_book=on_missing_book)
    result = engine._run_algorithm_with_session(session, "SINGLE-MAKER", AlgorithmInput(SYMBOL, 1, {}, params=params))

    if blocked:
        _assert_blocked(result, session, owner)
        assert ("已过期" if stale else "缺少真实盘口") in result.error
    else:
        assert result.status == ExecutionStatus.SUCCEEDED
        assert result.outcome == ExecutionOutcome.COMPLETED
        assert result.final_volume == 1
        assert result.error is result.reason_code is session.market_data_failure is None
        assert len(owner.submissions) == len(result.orders) == len(result.trades) == 1
        assert result.orders[0].order_type == order_type
        assert result.orders[0].price == 0
        assert owner.order_callbacks == owner.trade_callbacks == owner.price_callbacks == []
    if attempted_types is not None:
        assert attempted_types == [order_type]
    assert clock.waits == []


@pytest.mark.parametrize("on_missing_book", ["skip", "active", "market"])
@pytest.mark.parametrize("book_valid,bid,ask", [(False, 0, 0), (False, 99, 101), (True, 0, 0)])
@pytest.mark.parametrize("target_volume", [1, -1])
def test_single_maker_missing_book_requires_explicit_market(
    execution, clock, on_missing_book, book_valid, bid, ask, target_volume
):
    owner, engine, session = execution
    owner.quote.book_valid = book_valid
    owner.quote.bid_price, owner.quote.ask_price = bid, ask
    # 卖出从一手多仓减至零，避免测试替身把负持仓错误映射为多头。
    owner.position = 1 if target_volume < 0 else 0
    target_volume = 0 if target_volume < 0 else target_volume
    params = SingleMakerParams(max_wait_seconds=30, chase_enabled=False, on_missing_book=on_missing_book)
    result = engine._run_algorithm_with_session(
        session, "SINGLE-MAKER", AlgorithmInput(SYMBOL, target_volume, {}, params=params)
    )

    if on_missing_book == "market":
        assert result.status == ExecutionStatus.SUCCEEDED
        assert len(owner.submissions) == 1
        assert owner.submissions[0].order_type == OrderType.MARKET
    else:
        assert result.status == ExecutionStatus.BLOCKED
        assert result.reason_code == REASON
        assert result.memory["symbol_decision_reason_family"] == ExecutionReasonFamily.MARKET_RULE.value
        assert result.memory["execution_details"][f"{SYMBOL}_skipped"] == "missing_book"
        assert result.error and "盘口" in result.error
        assert owner.submissions == []
    assert owner.order_callbacks == owner.trade_callbacks == owner.price_callbacks == []
    assert clock.waits == []


@pytest.mark.parametrize(
    "evidence,status,expected_outcome",
    [
        ("order", ExecutionStatus.FAILED, ExecutionOutcome.ERROR),
        ("trade", ExecutionStatus.FAILED, ExecutionOutcome.ERROR),
        ("position", ExecutionStatus.FAILED, ExecutionOutcome.ERROR),
        ("position", ExecutionStatus.PARTIAL, ExecutionOutcome.NOT_REACHED),
        ("unknown-start", ExecutionStatus.FAILED, ExecutionOutcome.ERROR),
    ],
)
def test_engine_preserves_order_trade_or_position_evidence(
    monkeypatch: pytest.MonkeyPatch, clock: _FrozenClock, execution, evidence, status, expected_outcome
) -> None:
    owner, engine, session = execution
    order = UnifiedOrder(
        order_id="existing-order",
        symbol=SYMBOL,
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        volume=1,
        price=100,
        status=OrderStatus.CANCELED,
    )
    trade = TradeRecord.create(
        trade_id="existing-trade", symbol=SYMBOL, order_id=order.order_id, trade_volume=1, trade_price=100
    )
    orders = [order] if evidence == "order" else []
    trades = [trade] if evidence == "trade" else []
    final_volume = 1 if evidence in {"position", "unknown-start"} else 0
    memory = {} if evidence == "unknown-start" else {"start_volume": 0}

    def caught_failure(real_session, algorithm_input):
        real_session.get_market_data()
        clock.now += 6
        with pytest.raises(MarketDataUnavailableError) as caught:
            real_session.place_order(OrderDirection.BUY, OrderType.LIMIT, 1, 101)
        return AlgorithmResult(
            status=status,
            error=caught.value.execution_error,
            orders=orders,
            trades=trades,
            final_volume=final_volume,
            target_volume=algorithm_input.target_volume,
            memory=memory,
        )

    monkeypatch.setattr(engine_module, "resolve_algorithm", lambda name, executor: caught_failure)
    result = engine._run_algorithm_with_session(session, "TEST-EVIDENCE", AlgorithmInput(SYMBOL, 2, {}))

    assert result.status == status
    assert result.outcome == expected_outcome
    assert result.status != ExecutionStatus.BLOCKED
    assert result.reason_code == REASON
    assert result.memory["symbol_decision_reason_family"] == ExecutionReasonFamily.MARKET_RULE.value
    assert result.error == session.market_data_failure.execution_error
    assert result.orders == orders
    assert result.trades == trades
    assert result.final_volume == final_volume
    assert owner.submissions == []
    assert clock.waits == []


def test_successful_resubmission_clears_failure_before_engine_normalization(
    monkeypatch: pytest.MonkeyPatch, clock: _FrozenClock, execution
) -> None:
    owner, engine, session = execution

    def recover(real_session, algorithm_input):
        real_session.get_market_data()
        clock.now += 6
        with pytest.raises(MarketDataUnavailableError):
            real_session.place_order(OrderDirection.BUY, OrderType.LIMIT, 1, 101)
        assert real_session.market_data_failure is not None
        assert owner.submissions == []
        owner.quote = _price(SYMBOL)
        real_session.get_market_data()
        order = real_session.place_order(OrderDirection.BUY, OrderType.LIMIT, 1, 101)
        assert real_session.market_data_failure is None
        return AlgorithmResult(
            status=ExecutionStatus.SUCCEEDED,
            orders=[order],
            trades=list(owner.fill_trades),
            target_volume=algorithm_input.target_volume,
            final_volume=1,
        )

    monkeypatch.setattr(engine_module, "resolve_algorithm", lambda name, executor: recover)
    result = engine._run_algorithm_with_session(session, "TEST-RECOVERY", AlgorithmInput(SYMBOL, 1, {}))

    assert result.status == ExecutionStatus.SUCCEEDED
    assert result.outcome == ExecutionOutcome.COMPLETED
    assert result.error is result.reason_code is session.market_data_failure is None
    assert len(owner.submissions) == len(result.orders) == len(result.trades) == 1
    assert clock.waits == []
