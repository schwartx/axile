"""真实算法在捕获处保留行情归因，不依赖会话旁路。"""

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
ALGORITHMS = [
    ("SINGLE-MAKER", SingleMakerParams(max_wait_seconds=30, chase_enabled=False)),
    ("TWAP", TwapParams(total_duration=30, slices=1, max_wait_seconds=30)),
    ("POV", PovParams(participation_rate=1, interval_seconds=1, max_duration=1, complete_on_timeout=False)),
    ("TARGET-POS-TASK", CTPTargetPosTaskParams(max_wait_seconds=30, chase_enabled=False)),
]


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
        clock.now += 11
        return real_place_order(direction, order_type, volume, price, **kwargs)

    monkeypatch.setattr(session, "place_order", place_order)
    return attempted_types


def _assert_blocked(result: AlgorithmResult, session: ExecutionSession, owner: _QuoteExecutor) -> None:
    assert result.error
    assert result.reason_code == REASON
    assert result.reason_family == ExecutionReasonFamily.MARKET_RULE
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
        clock.now += 11
        real_session.place_order(OrderDirection.BUY, OrderType.LIMIT, 1, quote.bids[0][0])
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
    ALGORITHMS,
)
@pytest.mark.parametrize("diagnostics", ["keep", "clear", "forge"])
def test_builtin_caught_pre_submit_stale_quote_is_blocked(
    monkeypatch: pytest.MonkeyPatch, clock: _FrozenClock, execution, algorithm_name, params, diagnostics
) -> None:
    owner, engine, session = execution
    algorithm = resolve_algorithm(algorithm_name, session)
    raw_results: list[AlgorithmResult] = []

    def observe_algorithm(real_session, algorithm_input):
        result = algorithm(real_session, algorithm_input)
        raw_results.append(result.model_copy(deep=True))
        if diagnostics == "clear":
            result.memory.clear()
        elif diagnostics == "forge":
            result.memory = {
                "start_volume": 999,
                "symbol_decision_reason_code": "FORGED",
                "symbol_decision_reason_family": "SYSTEM",
            }
        return result

    monkeypatch.setattr(engine_module, "resolve_algorithm", lambda name, executor: observe_algorithm)
    attempted_types = _expire_before_submit(monkeypatch, session, clock)
    if diagnostics == "forge":
        submit = session.place_order

        def reword(*args, **kwargs):
            try:
                return submit(*args, **kwargs)
            except MarketDataUnavailableError as exc:
                exc.execution_error = "自定义行情说明"
                raise

        monkeypatch.setattr(session, "place_order", reword)
    result = engine._run_algorithm_with_session(session, algorithm_name, AlgorithmInput(SYMBOL, 1, {}, params=params))

    assert attempted_types == [OrderType.LIMIT]
    assert len(raw_results) == 1
    _assert_blocked(raw_results[0], session, owner)
    assert raw_results[0].model_dump(exclude={"memory", "symbol", "algorithm"}) == result.model_dump(
        exclude={"memory", "symbol", "algorithm"}
    )
    assert ("自定义行情说明" if diagnostics == "forge" else "已过期") in result.error
    assert result.symbol == SYMBOL
    assert result.algorithm == algorithm_name
    _assert_blocked(result, session, owner)
    assert clock.waits == []


@pytest.mark.parametrize("algorithm_name,params", ALGORITHMS)
@pytest.mark.parametrize("progress", [False, True])
def test_market_block_then_final_position_query_failure_is_plain_failure(
    monkeypatch, clock, execution, algorithm_name, params, progress
):
    owner, _, session = execution
    place = session.place_order
    monkeypatch.setattr(session, "get_order_volume_bounds", lambda *_: (1, 1))

    def fail_final_query():
        raise RuntimeError("final position unavailable")

    def submit(*args, **kwargs):
        if not progress or owner.submissions:
            clock.now += 11
            monkeypatch.setattr(owner, "get_account_assets", fail_final_query)
        return place(*args, **kwargs)

    monkeypatch.setattr(session, "place_order", submit)
    result = resolve_algorithm(algorithm_name, session)(session, AlgorithmInput(SYMBOL, 2, {}, params=params))

    assert result.status == (ExecutionStatus.PARTIAL if progress else ExecutionStatus.FAILED)
    assert result.error == "最终持仓查询失败，具体原因未确认"
    assert result.reason_code is result.reason_family is result.final_volume is None
    assert len(result.orders) == len(result.trades) == len(owner.submissions) == int(progress)
    assert result.outcome_reason == result.error
    assert owner.order_callbacks == owner.trade_callbacks == owner.price_callbacks == []


@pytest.mark.parametrize(
    "algorithm_name,params",
    [
        ("TWAP", TwapParams(total_duration=2, slices=2, max_wait_seconds=30)),
        ("POV", PovParams(participation_rate=1, interval_seconds=1, max_duration=2, complete_on_timeout=False)),
    ],
)
def test_later_slice_success_clears_previous_market_reason(monkeypatch, execution, algorithm_name, params):
    owner, _, session = execution
    place = session.place_order
    attempts = []

    def submit(*args, **kwargs):
        attempts.append(args)
        if len(attempts) == 1:
            raise MarketDataUnavailableError(SYMBOL, "暂时不可用")
        return place(*args, **kwargs)

    monkeypatch.setattr(session, "place_order", submit)
    result = resolve_algorithm(algorithm_name, session)(session, AlgorithmInput(SYMBOL, 2, {}, params=params))

    assert len(attempts) == 2
    assert result.status == ExecutionStatus.SUCCEEDED
    assert result.error is result.reason_code is result.reason_family is None
    assert result.final_volume == 2
    assert len(result.orders) == len(result.trades) == len(owner.submissions) == 1


@pytest.mark.parametrize("algorithm_name,params", ALGORITHMS)
@pytest.mark.parametrize(
    "first_status", [OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.PENDING]
)
def test_split_submission_keeps_evidence_when_later_child_is_blocked(
    monkeypatch, clock, execution, algorithm_name, params, first_status
):
    owner, _, session = execution
    place = session.place_order
    monkeypatch.setattr(session, "get_order_volume_bounds", lambda *_: (1, 1))
    if first_status != OrderStatus.FILLED:

        def native_submit(*args, **kwargs):
            order = _TestExecutor._place_order_impl(owner, *args, **kwargs)
            order.status = first_status
            owner.submissions.append(order)
            for callback in owner.order_callbacks:
                callback(order)
            return order

        monkeypatch.setattr(owner, "_place_order_impl", native_submit)

    def submit(*args, **kwargs):
        if owner.submissions:
            clock.now += 11
        return place(*args, **kwargs)

    monkeypatch.setattr(session, "place_order", submit)
    result = resolve_algorithm(algorithm_name, session)(session, AlgorithmInput(SYMBOL, 2, {}, params=params))

    assert len(result.orders) == len(owner.submissions) == 1
    assert result.orders[0].status == first_status
    assert len(result.trades) == int(first_status == OrderStatus.FILLED)
    assert result.final_volume == int(first_status == OrderStatus.FILLED)
    assert result.status == (
        ExecutionStatus.PARTIAL if first_status in {OrderStatus.FILLED, OrderStatus.PENDING} else ExecutionStatus.FAILED
    )
    if first_status in {OrderStatus.FILLED, OrderStatus.CANCELED}:
        assert result.reason_code == REASON
        assert result.reason_family == ExecutionReasonFamily.MARKET_RULE
    assert owner.order_callbacks == owner.trade_callbacks == owner.price_callbacks == []
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
    owner.quote.bids = []
    owner.quote.asks = []
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
        assert result.error is result.reason_code is result.reason_family is None
        assert len(owner.submissions) == len(result.orders) == len(result.trades) == 1
        assert result.orders[0].order_type == order_type
        assert result.orders[0].price == 0
        assert owner.order_callbacks == owner.trade_callbacks == owner.price_callbacks == []
    if attempted_types is not None:
        assert attempted_types == [order_type]
    assert clock.waits == []


@pytest.mark.parametrize("on_missing_book", ["skip", "active", "market"])
@pytest.mark.parametrize("bids,asks", [([], []), ([(99, 0)], [(101, 0)]), ([(0, 1)], [(0, 1)])])
@pytest.mark.parametrize("target_volume", [1, -1])
def test_single_maker_missing_book_requires_explicit_market(
    execution, clock, on_missing_book, bids, asks, target_volume
):
    owner, engine, session = execution
    owner.quote.bids = bids
    owner.quote.asks = asks
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
        assert result.reason_family == ExecutionReasonFamily.MARKET_RULE
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
        clock.now += 11
        with pytest.raises(MarketDataUnavailableError) as caught:
            real_session.place_order(OrderDirection.BUY, OrderType.LIMIT, 1, 101)
        return AlgorithmResult(
            status=status,
            error=caught.value.execution_error,
            reason_code=caught.value.reason_code,
            reason_family=ExecutionReasonFamily.MARKET_RULE,
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
    assert result.reason_family == ExecutionReasonFamily.MARKET_RULE
    assert result.error and "已过期" in result.error
    assert result.orders == orders
    assert result.trades == trades
    assert result.final_volume == final_volume
    assert owner.submissions == []
    assert clock.waits == []


def test_successful_resubmission_has_no_session_failure_side_channel(
    monkeypatch: pytest.MonkeyPatch, clock: _FrozenClock, execution
) -> None:
    owner, engine, session = execution

    def recover(real_session, algorithm_input):
        real_session.get_market_data()
        clock.now += 11
        with pytest.raises(MarketDataUnavailableError):
            real_session.place_order(OrderDirection.BUY, OrderType.LIMIT, 1, 101)
        assert owner.submissions == []
        owner.quote = _price(SYMBOL)
        real_session.get_market_data()
        order = real_session.place_order(OrderDirection.BUY, OrderType.LIMIT, 1, 101)
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
    assert result.error is result.reason_code is result.reason_family is None
    assert len(owner.submissions) == len(result.orders) == len(result.trades) == 1
    assert clock.waits == []
