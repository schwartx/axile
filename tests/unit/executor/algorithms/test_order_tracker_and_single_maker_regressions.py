from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from axile.domain.execution import ExecutionEventStatus
from axile.executor.algorithms.core.base import AlgorithmInput, ExecutorProtocol
from axile.executor.algorithms.defaults.single_maker import impl as single_maker_impl
from axile.executor.algorithms.utils.order_tracker import OrderTracker
from axile.executor.constants.order_status import OrderStatus
from axile.executor.models.execution_result import ExecutionStatus
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_order import OrderDirection, OrderType, TradeRecord, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData
from tests.fixtures.algorithm_fakes import ClockStub as _ClockStub
from tests.fixtures.algorithm_fakes import build_fallback_tracker as _build_tracker


class _SingleMakerExecutor:
    def __init__(self) -> None:
        self.symbol = "rb2610"
        self.logger = MagicMock()
        self._account_assets = UnifiedAccountAssets(
            available_cash=10_000.0,
            total_asset=10_000.0,
            market_value=0.0,
            positions=[],
        )
        self.pending_orders: list[UnifiedOrder] = [
            UnifiedOrder(
                order_id="pending-rb-1",
                symbol=self.symbol,
                direction=OrderDirection.BUY,
                order_type=OrderType.LIMIT,
                volume=1.0,
                price=100.0,
                status=OrderStatus.PENDING,
            )
        ]
        self.pending_query_calls = 0
        self.cancel_order_calls: list[str] = []

    def get_account_assets(self) -> UnifiedAccountAssets:
        return self._account_assets

    def get_pending_orders(self) -> list[UnifiedOrder]:
        self.pending_query_calls += 1
        return [order.model_copy(deep=True) for order in self.pending_orders]

    def cancel_order(self, order_id: str) -> bool:
        self.cancel_order_calls.append(order_id)
        return True

    def get_current_volume(self, _account_assets: object) -> float:
        return 0.0

    def get_market_data(self) -> UnifiedPriceData:
        return UnifiedPriceData(
            symbol=self.symbol,
            last_price=100.0,
            bid_price=99.0,
            ask_price=101.0,
            bid_volume=1.0,
            ask_volume=1.0,
            volume=10.0,
            turnover=1000.0,
            timestamp=1,
            update_time="2026-03-21T00:00:00",
        )

    def calculate_target_volume(
        self,
        curr_target: dict[str, float],
        account_assets: UnifiedAccountAssets,
        market_data: dict[str, UnifiedPriceData],
        trade_rules: dict[str, dict[str, object]],
        last_target: dict[str, float],
        forbidden_symbols: list[str] | None = None,
    ) -> dict[str, float]:
        _ = (account_assets, market_data, trade_rules, last_target, forbidden_symbols)
        return dict(curr_target)

    def is_termination_requested(self) -> bool:
        return False

    def get_termination_mode(self) -> str | None:
        return None

    def handle_termination_checkpoint(self) -> None:
        return None


class _SharedTickSingleMakerExecutor(_SingleMakerExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.shared_tick = UnifiedPriceData(
            symbol=self.symbol,
            last_price=100.0,
            bid_price=99.0,
            ask_price=101.0,
            bid_volume=1.0,
            ask_volume=1.0,
            volume=10.0,
            turnover=1000.0,
            timestamp=1,
            update_time="2026-03-21T00:00:00",
            extra={"source": {"shared": True}},
        )

    def get_market_data(self) -> UnifiedPriceData:
        return self.shared_tick


class _RefreshingExecutor:
    def __init__(self, pending_orders: list[UnifiedOrder]) -> None:
        self.symbol = "rb2610"
        self.logger = MagicMock()
        self.pending_orders = pending_orders
        self.query_calls = 0
        self.cancel_order_calls: list[str] = []
        self.checkpoint_calls = 0

    def get_pending_orders(self) -> list[UnifiedOrder]:
        self.query_calls += 1
        return self.pending_orders

    def cancel_order(self, order_id: str) -> bool:
        self.cancel_order_calls.append(order_id)
        return True

    def is_termination_requested(self) -> bool:
        return False

    def get_termination_mode(self) -> str | None:
        return None

    def handle_termination_checkpoint(self) -> None:
        self.checkpoint_calls += 1


def _patch_single_maker_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    submit_side_effect: BaseException,
) -> None:
    tracker = MagicMock()
    tracker.wait_for_completion.return_value = True
    tracker.get_all_orders.return_value = []

    monkeypatch.setattr(single_maker_impl, "setup_order_tracker", lambda *_args, **_kwargs: tracker)
    monkeypatch.setattr(
        single_maker_impl,
        "determine_order_price",
        lambda *_args, **_kwargs: (OrderType.LIMIT, 100.0),
    )

    def _raise_submit(*_args: object, **_kwargs: object) -> list[UnifiedOrder]:
        raise submit_side_effect

    monkeypatch.setattr(single_maker_impl, "submit_and_track_split_orders", _raise_submit)
    monkeypatch.setattr(single_maker_impl, "teardown_order_tracker", lambda *_args, **_kwargs: None)


def test_market_fallback_waits_for_cancel_confirmation_before_submitting_market_order() -> None:
    executor, tracker, order = _build_tracker()
    fallback = getattr(tracker, "_fallback_to_market_order")

    fallback()

    assert executor.place_calls == []
    chase_info = getattr(tracker, "_chase_info")
    assert chase_info[order.order_id]["market_order_fallback_pending_cancel"] is True

    canceled_order = order.model_copy(update={"status": "已撤销", "filled_volume": 0.4})
    tracker.on_order_update(canceled_order)

    assert len(executor.place_calls) == 1
    assert executor.place_calls[0]["order_type"] == OrderType.MARKET
    assert executor.place_calls[0]["volume"] == pytest.approx(0.6)
    assert "market-1" in tracker.pending_orders
    assert any(event["reason_code"] == "COMMON.MARKET_FALLBACK_ORDER_SUBMITTED" for event in executor.audit_events)


def test_order_tracker_keeps_trade_records_separate_when_trade_callback_precedes_terminal_order_update() -> None:
    executor = _RefreshingExecutor([])
    tracker = OrderTracker(executor=cast("ExecutorProtocol", executor))
    order = UnifiedOrder(
        order_id="gm-cl-1",
        symbol="SHSE.600000",
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        volume=100.0,
        price=12.3,
        status=OrderStatus.PARTIALLY_FILLED,
        filled_volume=0.0,
        avg_price=0.0,
    )
    tracker.add_order(order)

    tracker.on_trade_record(
        TradeRecord.create(
            trade_id="exec-1",
            trade_time="2026-03-23T10:00:00",
            trade_volume=40.0,
            trade_price=12.3,
            extra={
                "symbol": "SHSE.600000",
                "cl_ord_id": "gm-cl-1",
                "exchange_order_id": "1001",
            },
        )
    )

    tracker.on_order_update(
        UnifiedOrder.create(
            order_id="gm-cl-1",
            symbol="SHSE.600000",
            direction=OrderDirection.BUY.value,
            order_type=OrderType.LIMIT.value,
            volume=100.0,
            price=12.3,
            status=OrderStatus.CANCELED,
            extra={
                "cl_ord_id": "gm-cl-1",
                "exchange_order_id": "1001",
            },
        )
    )

    completed_order = tracker.completed_orders["gm-cl-1"]
    assert completed_order.status == OrderStatus.CANCELED
    assert completed_order.filled_volume == pytest.approx(0.0)
    assert completed_order.remaining_volume == pytest.approx(100.0)
    assert [trade.trade_id for trade in tracker.get_order_trades("gm-cl-1")] == ["exec-1"]


def test_order_tracker_trade_callback_does_not_mutate_order_fill_snapshot() -> None:
    executor = _RefreshingExecutor([])
    tracker = OrderTracker(executor=cast("ExecutorProtocol", executor))
    order = UnifiedOrder(
        order_id="gm-cl-1",
        symbol="SHSE.600000",
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        volume=100.0,
        price=12.3,
        status=OrderStatus.PARTIALLY_FILLED,
        filled_volume=0.0,
        avg_price=0.0,
    )
    tracker.add_order(order)

    tracker.on_trade_record(
        TradeRecord.create(
            trade_id="exec-1",
            trade_time="2026-03-23T10:00:00",
            trade_volume=40.0,
            trade_price=12.3,
            extra={
                "symbol": "SHSE.600000",
                "cl_ord_id": "gm-cl-1",
                "exchange_order_id": "1001",
            },
        )
    )

    tracked_order = tracker.pending_orders["gm-cl-1"]
    assert tracked_order.filled_volume == pytest.approx(0.0)
    assert tracked_order.avg_price == pytest.approx(0.0)
    assert [trade.trade_id for trade in tracker.get_order_trades("gm-cl-1")] == ["exec-1"]


def test_market_fallback_uses_trade_records_to_compute_remaining_volume_without_mutating_order() -> None:
    executor, tracker, order = _build_tracker()
    chase_info = getattr(tracker, "_chase_info")
    chase_info[order.order_id]["market_order_fallback_pending_cancel"] = True

    tracker.on_trade_record(
        TradeRecord.create(
            trade_id="trade-1",
            symbol="rb2610",
            order_id="limit-1",
            trade_time="2026-03-24T09:00:00",
            trade_volume=0.4,
            trade_price=100.0,
        )
    )

    canceled_order = order.model_copy(update={"status": "已撤销", "filled_volume": 0.0, "avg_price": 0.0})
    tracker.on_order_update(canceled_order)

    assert len(executor.place_calls) == 1
    assert executor.place_calls[0]["order_type"] == OrderType.MARKET
    assert executor.place_calls[0]["volume"] == pytest.approx(0.6)


def test_order_tracker_does_not_regress_cumulative_fill_when_order_update_is_newer_than_trades() -> None:
    executor = _RefreshingExecutor([])
    tracker = OrderTracker(executor=cast("ExecutorProtocol", executor))
    order = UnifiedOrder(
        order_id="external-order-1",
        symbol="rb2610",
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        volume=1.0,
        price=100.0,
        status=OrderStatus.PARTIALLY_FILLED,
        filled_volume=0.0,
        avg_price=0.0,
    )
    tracker.add_order(order)
    tracker.on_trade_record(
        TradeRecord.create(
            trade_id="t1",
            symbol="rb2610",
            order_id="external-order-1",
            trade_time="2026-03-24T09:00:00",
            trade_volume=0.4,
            trade_price=100.0,
        )
    )

    tracker.on_order_update(
        UnifiedOrder(
            order_id="external-order-1",
            symbol="rb2610",
            direction=OrderDirection.BUY,
            order_type=OrderType.LIMIT,
            volume=1.0,
            price=100.0,
            status=OrderStatus.CANCELED,
            filled_volume=0.6,
            avg_price=100.0,
        )
    )

    completed_order = tracker.completed_orders["external-order-1"]
    assert completed_order.status == OrderStatus.CANCELED
    assert completed_order.filled_volume == pytest.approx(0.6)
    assert completed_order.remaining_volume == pytest.approx(0.4)
    assert [trade.trade_id for trade in tracker.get_order_trades("external-order-1")] == ["t1"]


def test_order_tracker_matches_gm_order_update_by_alias_and_preserves_actionable_id() -> None:
    executor = _RefreshingExecutor([])
    tracker = OrderTracker(executor=cast("ExecutorProtocol", executor))
    provisional_order = UnifiedOrder(
        order_id="gm-cl-1",
        symbol="SHSE.600000",
        direction=OrderDirection.SELL,
        order_type=OrderType.LIMIT,
        volume=100.0,
        price=12.3,
        status=OrderStatus.SUBMITTED,
        extra={
            "cl_ord_id": "gm-cl-1",
            "exchange_order_id": None,
            "sync_pending": True,
        },
    )
    tracker.add_order(provisional_order)

    tracker.on_order_update(
        UnifiedOrder(
            order_id="1001",
            symbol="SHSE.600000",
            direction=OrderDirection.SELL,
            order_type=OrderType.LIMIT,
            volume=100.0,
            price=12.3,
            status=OrderStatus.CANCELED,
            extra={
                "cl_ord_id": "gm-cl-1",
                "exchange_order_id": "1001",
            },
        )
    )

    assert "gm-cl-1" in tracker.completed_orders
    completed_order = tracker.completed_orders["gm-cl-1"]
    assert completed_order.order_id == "gm-cl-1"
    assert completed_order.status == OrderStatus.CANCELED
    assert completed_order.extra["exchange_order_id"] == "1001"


def test_order_tracker_periodically_queries_order_status_during_wait() -> None:
    executor_order = UnifiedOrder(
        order_id="limit-1",
        symbol="rb2610",
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        volume=1.0,
        price=100.0,
        status=OrderStatus.FILLED,
        filled_volume=1.0,
        avg_price=100.0,
    )
    executor = _RefreshingExecutor([executor_order])
    tracker = OrderTracker(
        executor=cast("ExecutorProtocol", executor),
        clock=_ClockStub(),
        order_refresh_interval=2.0,
    )
    pending_order = executor_order.model_copy(update={"status": OrderStatus.PENDING, "filled_volume": 0.0})
    tracker.add_order(pending_order)

    completed = tracker.wait_for_completion(timeout=5.0)

    assert completed is True
    assert executor.query_calls == 1
    assert tracker.pending_orders == {}
    assert tracker.completed_orders["limit-1"].status == OrderStatus.FILLED


def test_order_tracker_replays_early_order_update_after_add_order() -> None:
    executor = _RefreshingExecutor([])
    tracker = OrderTracker(executor=cast("ExecutorProtocol", executor))

    tracker.on_order_update(
        UnifiedOrder(
            order_id="limit-1",
            symbol="rb2610",
            direction=OrderDirection.BUY,
            order_type=OrderType.LIMIT,
            volume=1.0,
            price=100.0,
            status=OrderStatus.FILLED,
            filled_volume=1.0,
            avg_price=100.0,
        )
    )

    tracker.add_order(
        UnifiedOrder(
            order_id="limit-1",
            symbol="rb2610",
            direction=OrderDirection.BUY,
            order_type=OrderType.LIMIT,
            volume=1.0,
            price=100.0,
            status=OrderStatus.PENDING,
            filled_volume=0.0,
            avg_price=0.0,
        )
    )

    assert tracker.pending_orders == {}
    assert tracker.completed_orders["limit-1"].status == OrderStatus.FILLED
    assert tracker.completed_orders["limit-1"].filled_volume == pytest.approx(1.0)


def test_order_tracker_queries_once_before_short_timeout() -> None:
    executor_order = UnifiedOrder(
        order_id="limit-1",
        symbol="rb2610",
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        volume=1.0,
        price=100.0,
        status=OrderStatus.FILLED,
        filled_volume=1.0,
        avg_price=100.0,
    )
    executor = _RefreshingExecutor([executor_order])
    tracker = OrderTracker(
        executor=cast("ExecutorProtocol", executor),
        clock=_ClockStub(),
        order_refresh_interval=30.0,
    )
    pending_order = executor_order.model_copy(update={"status": OrderStatus.PENDING, "filled_volume": 0.0})
    tracker.add_order(pending_order)

    completed = tracker.wait_for_completion(timeout=15.0)

    assert completed is True
    assert executor.query_calls == 1
    assert tracker.completed_orders["limit-1"].status == OrderStatus.FILLED


def test_order_tracker_timeout_cancels_only_tracked_symbols() -> None:
    executor_order = UnifiedOrder(
        order_id="limit-1",
        symbol="rb2610",
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        volume=1.0,
        price=100.0,
        status=OrderStatus.PENDING,
        filled_volume=0.0,
        avg_price=0.0,
    )
    executor = _RefreshingExecutor([executor_order])
    tracker = OrderTracker(
        executor=cast("ExecutorProtocol", executor),
        clock=_ClockStub(),
        order_refresh_interval=30.0,
    )
    tracker.add_order(executor_order)

    completed = tracker.wait_for_completion(timeout=1.0)

    assert completed is False
    assert executor.query_calls == 1
    assert executor.cancel_order_calls == ["limit-1"]


def test_market_fallback_stops_when_cancel_request_fails() -> None:
    executor, tracker, order = _build_tracker()
    executor.cancel_outcome = RuntimeError("cancel failed")
    fallback = getattr(tracker, "_fallback_to_market_order")

    fallback()

    assert executor.place_calls == []
    assert tracker.pending_orders[order.order_id].extra["market_order_fallback_failed"] is True
    assert any(
        event["reason_code"] == "COMMON.MARKET_FALLBACK_CANCEL_FAILED" and event["status"] == ExecutionEventStatus.ERROR
        for event in executor.audit_events
    )


def test_market_fallback_marks_manual_intervention_when_market_order_submission_fails() -> None:
    executor, tracker, order = _build_tracker()
    executor.place_outcome = RuntimeError("market failed")
    fallback = getattr(tracker, "_fallback_to_market_order")

    fallback()
    tracker.on_order_update(order.model_copy(update={"status": "已撤销"}))

    assert order.order_id in tracker.completed_orders
    assert tracker.completed_orders[order.order_id].extra["market_order_fallback_failed"] is True
    assert any(
        event["reason_code"] == "COMMON.MARKET_FALLBACK_ORDER_FAILED" and event["status"] == ExecutionEventStatus.ERROR
        for event in executor.audit_events
    )


def test_single_maker_records_regular_order_submission_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_single_maker_dependencies(monkeypatch, RuntimeError("submit failed"))
    executor = _SingleMakerExecutor()
    algorithm_input = AlgorithmInput(
        symbol="rb2610",
        target_volume=1.0,
        trade_rule={},
        params=single_maker_impl.SingleMakerParams(),
    )

    result = single_maker_impl.single_maker_callback(
        cast("ExecutorProtocol", executor),
        algorithm_input,
    )

    result_memory = cast("dict[str, Any]", result.memory)
    execution_details = cast("dict[str, Any]", result_memory["execution_details"])
    assert execution_details["rb2610_error"] == "RuntimeError: submit failed"


def test_single_maker_first_tick_uses_snapshot_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = MagicMock()
    tracker.get_all_orders.return_value = []
    monkeypatch.setattr(single_maker_impl, "setup_order_tracker", lambda *_args, **_kwargs: tracker)
    monkeypatch.setattr(single_maker_impl, "teardown_order_tracker", lambda *_args, **_kwargs: None)

    executor = _SharedTickSingleMakerExecutor()
    algorithm_input = AlgorithmInput(
        symbol="rb2610",
        target_volume=0.0,
        trade_rule={},
        params=single_maker_impl.SingleMakerParams(),
    )

    result = single_maker_impl.single_maker_callback(
        cast("ExecutorProtocol", executor),
        algorithm_input,
    )

    assert result.first_tick is not None
    assert result.first_tick is not executor.shared_tick

    result.first_tick.extra["source"] = {"shared": False}

    assert executor.shared_tick.extra["source"] == {"shared": True}


def test_single_maker_does_not_cancel_pending_orders_before_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_single_maker_dependencies(monkeypatch, RuntimeError("submit failed"))
    executor = _SingleMakerExecutor()
    algorithm_input = AlgorithmInput(
        symbol="rb2610",
        target_volume=1.0,
        trade_rule={},
        params=single_maker_impl.SingleMakerParams(),
    )

    single_maker_impl.single_maker_callback(
        cast("ExecutorProtocol", executor),
        algorithm_input,
    )

    assert executor.pending_query_calls == 0
    assert executor.cancel_order_calls == []


def test_single_maker_reraises_memory_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_single_maker_dependencies(monkeypatch, MemoryError("out of memory"))
    executor = _SingleMakerExecutor()
    algorithm_input = AlgorithmInput(
        symbol="rb2610",
        target_volume=1.0,
        trade_rule={},
        params=single_maker_impl.SingleMakerParams(),
    )

    with pytest.raises(MemoryError):
        single_maker_impl.single_maker_callback(
            cast("ExecutorProtocol", executor),
            algorithm_input,
        )


def _invalid_book_tick(symbol: str = "rb2610") -> UnifiedPriceData:
    """构造 book_valid=False 的假盘口（买卖一被 last_price 兜底、盘口无效）。"""
    return UnifiedPriceData(
        symbol=symbol,
        last_price=100.0,
        bid_price=100.0,
        ask_price=100.0,
        bid_volume=0.0,
        ask_volume=0.0,
        volume=10.0,
        turnover=0.0,
        timestamp=1,
        update_time="2026-03-21T00:00:00",
        book_valid=False,
    )


def test_single_maker_skips_on_invalid_book_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """盘口无效 + 默认 on_missing_book=skip → 不下任何单，返回 NOOP，记 missing_book。"""
    tracker = MagicMock()
    tracker.get_all_orders.return_value = []
    tracker.get_all_trades.return_value = []
    monkeypatch.setattr(single_maker_impl, "setup_order_tracker", lambda *_a, **_k: tracker)
    monkeypatch.setattr(single_maker_impl, "teardown_order_tracker", lambda *_a, **_k: None)

    submit_calls: list[object] = []
    monkeypatch.setattr(
        single_maker_impl,
        "submit_and_track_split_orders",
        lambda *args, **kwargs: submit_calls.append((args, kwargs)),
    )

    executor = _SingleMakerExecutor()
    monkeypatch.setattr(executor, "get_market_data", _invalid_book_tick)
    algorithm_input = AlgorithmInput(
        symbol="rb2610",
        target_volume=1.0,
        trade_rule={},
        params=single_maker_impl.SingleMakerParams(),
    )

    result = single_maker_impl.single_maker_callback(
        cast("ExecutorProtocol", executor),
        algorithm_input,
    )

    assert submit_calls == []
    assert result.status == ExecutionStatus.BLOCKED
    result_memory = cast("dict[str, Any]", result.memory)
    execution_details = cast("dict[str, Any]", result_memory["execution_details"])
    assert execution_details["rb2610_skipped"] == "missing_book"


def test_single_maker_market_order_on_invalid_book(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """盘口无效 + on_missing_book=market → 下市价单（显式保留成交确定性）。"""
    tracker = MagicMock()
    tracker.wait_for_completion.return_value = True
    tracker.get_all_orders.return_value = []
    tracker.get_all_trades.return_value = []
    monkeypatch.setattr(single_maker_impl, "setup_order_tracker", lambda *_a, **_k: tracker)
    monkeypatch.setattr(single_maker_impl, "teardown_order_tracker", lambda *_a, **_k: None)

    captured: dict[str, object] = {}

    def _spy_submit(*args: object, **_kwargs: object) -> UnifiedOrder:
        captured["order_type"] = args[3]
        captured["price"] = args[5]
        return UnifiedOrder(
            order_id="mkt-1",
            symbol="rb2610",
            direction=OrderDirection.BUY,
            order_type=OrderType.MARKET,
            volume=1.0,
            price=0.0,
            status=OrderStatus.PENDING,
        )

    def _spy_split_submit(*args: object, **_kwargs: object) -> list[UnifiedOrder]:
        return [_spy_submit(*args, **_kwargs)]

    monkeypatch.setattr(single_maker_impl, "submit_and_track_split_orders", _spy_split_submit)

    executor = _SingleMakerExecutor()
    monkeypatch.setattr(executor, "get_market_data", _invalid_book_tick)
    algorithm_input = AlgorithmInput(
        symbol="rb2610",
        target_volume=1.0,
        trade_rule={},
        params=single_maker_impl.SingleMakerParams(on_missing_book="market"),
    )

    single_maker_impl.single_maker_callback(
        cast("ExecutorProtocol", executor),
        algorithm_input,
    )

    assert captured["order_type"] == OrderType.MARKET
    assert captured["price"] == 0.0


def test_single_maker_active_price_on_invalid_book(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """盘口无效 + on_missing_book=active → 强制对手价（即便 price_strategy=PASSIVE）。"""
    tracker = MagicMock()
    tracker.wait_for_completion.return_value = True
    tracker.get_all_orders.return_value = []
    tracker.get_all_trades.return_value = []
    monkeypatch.setattr(single_maker_impl, "setup_order_tracker", lambda *_a, **_k: tracker)
    monkeypatch.setattr(single_maker_impl, "teardown_order_tracker", lambda *_a, **_k: None)

    captured: dict[str, object] = {}

    def _capture(
        direction: OrderDirection,
        market_data: UnifiedPriceData | None,
        price_strategy: str = "PASSIVE",
    ) -> tuple[OrderType, float]:
        _ = (direction, market_data)
        captured["price_strategy"] = price_strategy
        return OrderType.LIMIT, 101.0

    monkeypatch.setattr(single_maker_impl, "determine_order_price", _capture)
    monkeypatch.setattr(
        single_maker_impl,
        "submit_and_track_split_orders",
        lambda *_a, **_k: [
            UnifiedOrder(
                order_id="act-1",
                symbol="rb2610",
                direction=OrderDirection.BUY,
                order_type=OrderType.LIMIT,
                volume=1.0,
                price=101.0,
                status=OrderStatus.PENDING,
            )
        ],
    )

    executor = _SingleMakerExecutor()
    monkeypatch.setattr(executor, "get_market_data", _invalid_book_tick)
    algorithm_input = AlgorithmInput(
        symbol="rb2610",
        target_volume=1.0,
        trade_rule={},
        params=single_maker_impl.SingleMakerParams(price_strategy="PASSIVE", on_missing_book="active"),
    )

    single_maker_impl.single_maker_callback(
        cast("ExecutorProtocol", executor),
        algorithm_input,
    )

    assert captured["price_strategy"] == "ACTIVE"


def test_single_maker_params_on_missing_book_validation() -> None:
    """on_missing_book 默认 skip，接受 skip/active/market，拒绝非法值。"""
    assert single_maker_impl.SingleMakerParams().on_missing_book == "skip"
    assert single_maker_impl.SingleMakerParams(on_missing_book="active").on_missing_book == "active"
    assert single_maker_impl.SingleMakerParams(on_missing_book="market").on_missing_book == "market"
    with pytest.raises(ValidationError):
        single_maker_impl.SingleMakerParams(on_missing_book="bogus")  # type: ignore[arg-type]
