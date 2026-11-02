"""AbstractExecutor 公开订单 API 的账户控制包装测试。"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

import pytest

import axile.executor.execution_runtime as execution_runtime_module
from axile.common.trade_channel import TradeChannel
from axile.executor.abstract_executor.base import AbstractExecutor
from axile.executor.account_control.guard import AccountControlGuard
from axile.executor.account_control.models import (
    AccountControlDecision,
    AccountControlOverride,
)
from axile.executor.account_control.presets import resolve_account_control_policy
from axile.executor.account_control.snapshot import AccountControlCounterSnapshot
from axile.executor.ctp.ctp_execute import CtpSessionRecoveryRequired
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_order import OrderDirection, OrderType, TradeRecord, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData
from axile.executor.order_cancellation import CancelNotAllowedError
from tests.unit.executor._account_control_test_support import normalize_account_control_override


class _LoggerStub:
    def debug(self, _message: object) -> None:
        pass

    def info(self, _message: object) -> None:
        pass

    def warning(self, _message: object) -> None:
        pass

    def error(self, _message: object) -> None:
        pass

    def exception(self, _message: object) -> None:
        pass


def _clock() -> datetime:
    return datetime(2026, 3, 24, 9, 31, 15)


def _clock_from(*moments: datetime):
    sequence = iter(moments)

    def _inner() -> datetime:
        return next(sequence)

    return _inner


def _build_guard(
    override: dict[str, object],
    *,
    clock: Callable[[], datetime] = _clock,
) -> AccountControlGuard:
    normalized_override = normalize_account_control_override(override)
    return AccountControlGuard(
        account_id=99,
        execution_id="exec-base-wrapper",
        channel=TradeChannel.GM,
        policy=resolve_account_control_policy(
            "default",
            AccountControlOverride.model_validate(normalized_override),
        ),
        baseline=AccountControlCounterSnapshot(),
        clock=clock,
    )


def _get_guard(executor: AbstractExecutor) -> AccountControlGuard:
    guard = executor.get_account_control_guard()
    assert guard is not None
    return guard


class _WrapperExecutor(AbstractExecutor):
    def __init__(self, guard: AccountControlGuard) -> None:
        super().__init__(TradeChannel.GM, None)
        self.logger = _LoggerStub()
        self.set_account_control_guard(guard)
        self.raw_calls: list[tuple[str, object]] = []
        self.pending_orders: list[UnifiedOrder] = []
        self.trade_records: list[TradeRecord] = []
        self.cancel_results: dict[str, bool | Exception] = {}

    def _initialize_connection(self, account_config: object) -> None:
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
        _ = symbols
        return {}

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
        self.raw_calls.append(("place", symbol))
        self._list_pending_orders(symbol)
        return UnifiedOrder(
            order_id=f"oid-{symbol}",
            symbol=symbol,
            direction=OrderDirection.BUY,
            order_type=OrderType.LIMIT,
            volume=1.0,
            price=1.0,
            status="待成交",
        )

    def _cancel_order_impl(self, symbol: str, order_id: str) -> bool:
        self.raw_calls.append(("cancel", f"{symbol}:{order_id}"))
        result = self.cancel_results.get(order_id, True)
        if isinstance(result, Exception):
            raise result
        return result

    def _get_pending_orders_impl(self, symbol: str | None) -> list[UnifiedOrder]:
        return self._list_pending_orders(symbol)

    def _query_trades_impl(self, symbol: str, order_id: str) -> list[TradeRecord]:
        self.raw_calls.append(("trades", f"{symbol}:{order_id}"))
        return [
            trade
            for trade in self.trade_records
            if trade.extra.get("symbol") == symbol and trade.extra.get("order_id") == order_id
        ]

    def _list_pending_orders(self, symbol: str | None) -> list[UnifiedOrder]:
        self.raw_calls.append(("pending", symbol))
        if symbol is None:
            return list(self.pending_orders)
        return [order for order in self.pending_orders if order.symbol == symbol]

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

    def _cleanup(self) -> None:
        return None

    def _get_account_mark(self) -> str:
        return "wrapper"

    def _get_default_trade_rules_for_empty(self, symbols: list[str]) -> dict[str, object]:
        _ = symbols
        return {}


def test_place_order_is_account_controlled_only_once_at_public_boundary() -> None:
    """公开 place_order 只记一次 PLACE_ORDER，不把内部私有查询算成 QUERY_ORDER。"""
    executor = _WrapperExecutor(_build_guard({"place_order": {"per_day": 1}, "query_order": {"per_day": 10}}))

    order = executor.place_order("SHSE.600000", OrderDirection.BUY, OrderType.LIMIT, 100, price=12.3)

    assert order.order_id == "oid-SHSE.600000"
    assert executor.raw_calls == [("place", "SHSE.600000"), ("pending", "SHSE.600000")]

    _, events = _get_guard(executor).flush_records()
    assert len(events) == 1
    assert events[0].operation == "place_order"
    assert events[0].decision == AccountControlDecision.ALLOWED


def test_get_pending_orders_consumes_single_query_order_attempt() -> None:
    """公开 get_pending_orders 统一只记一次 QUERY_ORDER。"""
    executor = _WrapperExecutor(_build_guard({"query_order": {"per_day": 1}}))

    assert executor.get_pending_orders("SHSE.600000") == []
    assert executor.raw_calls == [("pending", "SHSE.600000")]

    _, events = _get_guard(executor).flush_records()
    assert len(events) == 1
    assert events[0].operation == "query_order"
    assert events[0].decision == AccountControlDecision.ALLOWED


def test_get_pending_orders_with_none_queries_once() -> None:
    """公开 get_pending_orders(None) 应统一记一次 QUERY_ORDER。"""
    executor = _WrapperExecutor(_build_guard({"query_order": {"per_day": 1}}))

    assert executor.get_pending_orders(None) == []
    assert executor.raw_calls == [("pending", None)]

    _, events = _get_guard(executor).flush_records()
    assert len(events) == 1
    assert events[0].operation == "query_order"
    assert events[0].decision == AccountControlDecision.ALLOWED
    assert events[0].symbol is None


def test_query_trades_consumes_single_query_trades_attempt() -> None:
    """公开 query_trades 统一只记一次 QUERY_TRADES。"""
    executor = _WrapperExecutor(_build_guard({"query_trades": {"per_day": 1}}))
    executor.trade_records = [_make_trade("SHSE.600000", "order-1", "trade-1")]

    trades = executor.query_trades("SHSE.600000", "order-1")

    assert [trade.trade_id for trade in trades] == ["trade-1"]
    assert executor.raw_calls == [("trades", "SHSE.600000:order-1")]

    _, events = _get_guard(executor).flush_records()
    assert len(events) == 1
    assert events[0].operation == "query_trades"
    assert events[0].decision == AccountControlDecision.ALLOWED
    assert events[0].metadata == {"order_id": "order-1"}


def test_prepare_execution_runtime_keeps_pre_execute_audit_seq_and_public_wrapper_records() -> None:
    """active runtime 应承接 pre-execute 审计序列，公开 wrapper 也应写入同一 runtime。"""
    executor = _WrapperExecutor(_build_guard({"query_order": {"per_day": 1}}))
    executor.set_audit_context({"execution_id": "exec-preflight", "account_id": 99, "algorithm": "TEST"})

    runtime = executor.prepare_execution_runtime()

    assert executor.prepare_execution_runtime() is runtime
    assert executor.next_audit_seq() == 1
    assert executor.next_audit_seq() == 2
    assert runtime.next_audit_seq() == 3

    _ = executor.get_pending_orders("SHSE.600000")

    _, events = executor.export_account_control_records()
    assert len(events) == 1
    assert events[0].operation == "query_order"
    assert events[0].decision == AccountControlDecision.ALLOWED


def test_execution_internal_query_helpers_delegate_to_runtime_without_public_events() -> None:
    """execution-internal 查询 helper 应委托 runtime，而不是走公开账户控制包装。"""
    executor = _WrapperExecutor(
        _build_guard(
            {
                "query_order": {"per_day": 1},
                "query_trades": {"per_day": 1},
            }
        )
    )
    captured: dict[str, object] = {}

    class _Runtime:
        def get_pending_orders_for_symbol(self, symbol: str) -> list[UnifiedOrder]:
            captured["pending_symbol"] = symbol
            return [_make_pending_order(symbol, "runtime-order")]

        def get_trades_for_order(self, symbol: str, order_id: str) -> list[TradeRecord]:
            captured["trade_query"] = (symbol, order_id)
            return [_make_trade(symbol, order_id, "runtime-trade")]

    executor.set_active_execution_query_runtime(_Runtime())

    orders = executor._get_pending_orders_for_execution("SHSE.600000")
    trades = executor._query_trades_for_execution("SHSE.600000", "order-1")

    assert [order.order_id for order in orders] == ["runtime-order"]
    assert [trade.trade_id for trade in trades] == ["runtime-trade"]
    assert captured == {
        "pending_symbol": "SHSE.600000",
        "trade_query": ("SHSE.600000", "order-1"),
    }

    _, events = _get_guard(executor).flush_records()
    assert events == []


def test_execution_shared_fetch_routes_through_shared_control_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """execution shared fetch 应复用与 decorator 相同的底层账户控制 helper。"""
    executor = _WrapperExecutor(_build_guard({"query_trades": {"per_day": 1}}))
    captured: dict[str, object] = {}

    def fake_run_controlled_call(
        *,
        guard: object,
        operation: str,
        call: object,
        symbol: str | None = None,
        metadata: object = None,
        success_outcome: object = "succeeded",
        result_metadata_resolver: object = None,
    ) -> object:
        captured.update(
            {
                "guard": guard,
                "operation": operation,
                "symbol": symbol,
                "metadata": metadata,
                "success_outcome": success_outcome,
                "has_result_metadata_resolver": callable(result_metadata_resolver),
            }
        )
        return call()

    monkeypatch.setattr(execution_runtime_module, "run_controlled_call", fake_run_controlled_call)

    result = executor._run_execution_shared_fetch(
        operation="query_trades",
        shared_query_key=("trades_snapshot",),
        query_scope="snapshot",
        symbol=None,
        metadata={"order_id": "order-1"},
        fetcher=lambda: ["trade-1"],
    )

    assert result == ["trade-1"]
    assert captured == {
        "guard": executor.get_account_control_guard(),
        "operation": "query_trades",
        "symbol": None,
        "metadata": {
            "operation": "query_trades",
            "shared_query_key": "trades_snapshot",
            "query_scope": "snapshot",
            "execution_id": "exec-base-wrapper",
            "order_id": "order-1",
        },
        "success_outcome": "fetched",
        "has_result_metadata_resolver": False,
    }


def test_place_order_success_patches_runtime_pending_snapshot() -> None:
    """公开下单成功后应直接 patch runtime pending snapshot。"""
    executor = _WrapperExecutor(_build_guard({"place_order": {"per_day": 1}}))
    applied_orders: list[UnifiedOrder] = []
    invalidations: list[tuple[str, str | None]] = []

    class _Runtime:
        def apply_pending_order_update(self, order: UnifiedOrder) -> None:
            applied_orders.append(order)

        def invalidate_trades(self, symbol: str | None = None, order_id: str | None = None) -> None:
            invalidations.append((symbol, order_id))

    executor.set_active_execution_query_runtime(_Runtime())

    order = executor.place_order("SHSE.600000", OrderDirection.BUY, OrderType.LIMIT, 100, price=12.3)

    assert applied_orders == [order]
    assert invalidations == []


def test_place_order_never_invalidates_trade_snapshot_even_if_order_payload_shows_fill() -> None:
    """公开下单只属于订单域，即便返回订单体现成交推进也不应触碰 trades snapshot。"""

    class _FilledPlaceOrderExecutor(_WrapperExecutor):
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
            self.raw_calls.append(("place", symbol))
            return UnifiedOrder(
                order_id=f"oid-{symbol}",
                symbol=symbol,
                direction=OrderDirection.BUY,
                order_type=OrderType.LIMIT,
                volume=100.0,
                price=12.3,
                filled_volume=50.0,
                avg_price=12.31,
                status="部分成交",
            )

    executor = _FilledPlaceOrderExecutor(_build_guard({"place_order": {"per_day": 1}}))
    applied_orders: list[UnifiedOrder] = []
    invalidations: list[tuple[str | None, str | None]] = []

    class _Runtime:
        def apply_pending_order_update(self, order: UnifiedOrder) -> None:
            applied_orders.append(order)

        def invalidate_trades(self, symbol: str | None = None, order_id: str | None = None) -> None:
            invalidations.append((symbol, order_id))

    executor.set_active_execution_query_runtime(_Runtime())

    order = executor.place_order("SHSE.600000", OrderDirection.BUY, OrderType.LIMIT, 100, price=12.3)

    assert applied_orders == [order]
    assert invalidations == []


def test_cancel_order_submission_retains_runtime_pending_order() -> None:
    """公开撤单提交仅使快照失效，未收到终态不能移除原单。"""
    executor = _WrapperExecutor(_build_guard({"cancel_order": {"per_day": 1}}))
    removed_orders: list[tuple[str, str]] = []
    invalidated: list[str] = []

    class _Runtime:
        def remove_pending_order(self, symbol: str, order_id: str) -> None:
            removed_orders.append((symbol, order_id))

        def invalidate_orders(self, symbol: str) -> None:
            invalidated.append(symbol)

    executor.set_active_execution_query_runtime(_Runtime())
    assert executor.cancel_order("SHSE.600000", "order-1") is True
    assert removed_orders == []
    assert invalidated == ["SHSE.600000"]


def test_passive_order_update_patches_runtime_without_trade_invalidation() -> None:
    """被动 order update 未体现成交推进时，应只 patch pending snapshot。"""
    executor = _WrapperExecutor(_build_guard({"query_order": {"per_day": 1}}))
    applied_orders: list[UnifiedOrder] = []
    invalidated_trades: list[tuple[str | None, str | None]] = []

    class _Runtime:
        def apply_pending_order_update(self, order: UnifiedOrder) -> None:
            applied_orders.append(order)

        def invalidate_trades(self, symbol: str | None = None, order_id: str | None = None) -> None:
            invalidated_trades.append((symbol, order_id))

    executor.set_active_execution_query_runtime(_Runtime())
    order = _make_pending_order("SHSE.600000", "order-1")

    executor.get_execution_query_runtime_bridge().handle_order_update(order)

    assert applied_orders == [order]
    assert invalidated_trades == []


def test_filled_order_update_patches_runtime_without_trade_invalidation() -> None:
    """order update 即便体现成交推进，也只属于订单域，不应触碰 trades snapshot。"""
    executor = _WrapperExecutor(_build_guard({"query_order": {"per_day": 1}}))
    applied_orders: list[UnifiedOrder] = []
    invalidated_trades: list[tuple[str | None, str | None]] = []

    class _Runtime:
        def apply_pending_order_update(self, order: UnifiedOrder) -> None:
            applied_orders.append(order)

        def invalidate_trades(self, symbol: str | None = None, order_id: str | None = None) -> None:
            invalidated_trades.append((symbol, order_id))

    executor.set_active_execution_query_runtime(_Runtime())
    order = _make_pending_order("SHSE.600000", "order-1")
    order.status = "部分成交"
    order.filled_volume = 50

    executor.get_execution_query_runtime_bridge().handle_order_update(order)

    assert applied_orders == [order]
    assert invalidated_trades == []


def test_trade_record_update_patches_runtime_snapshot() -> None:
    """trade update 到达时应直接 patch runtime trade snapshot。"""
    executor = _WrapperExecutor(_build_guard({"query_trades": {"per_day": 1}}))
    applied_trades: list[TradeRecord] = []

    class _Runtime:
        def apply_trade_record(self, trade: TradeRecord) -> None:
            applied_trades.append(trade)

    executor.set_active_execution_query_runtime(_Runtime())
    trade = _make_trade("SHSE.600000", "order-1", "trade-1")

    executor.get_execution_query_runtime_bridge().handle_trade_record(trade)

    assert applied_trades == [trade]


def test_trade_record_update_without_patch_capability_only_invalidates_trade_snapshot() -> None:
    """当 runtime 不能直接 patch trade 时，trade 回调也不应反向触碰 orders snapshot。"""
    executor = _WrapperExecutor(_build_guard({"query_trades": {"per_day": 1}}))
    invalidated_orders: list[str | None] = []
    invalidated_trades: list[tuple[str | None, str | None]] = []

    class _Runtime:
        def invalidate_orders(self, symbol: str | None = None) -> None:
            invalidated_orders.append(symbol)

        def invalidate_trades(self, symbol: str | None = None, order_id: str | None = None) -> None:
            invalidated_trades.append((symbol, order_id))

    executor.set_active_execution_query_runtime(_Runtime())
    trade = _make_trade("SHSE.600000", "order-1", "trade-1")

    executor.get_execution_query_runtime_bridge().handle_trade_record(trade)

    assert invalidated_orders == []
    assert invalidated_trades == [("SHSE.600000", "order-1")]


def test_cancel_order_consumes_single_cancel_order_attempt() -> None:
    """公开 cancel_order 统一只记一次 CANCEL_ORDER。"""
    executor = _WrapperExecutor(_build_guard({"cancel_order": {"per_day": 1}}))

    assert executor.cancel_order("SHSE.600000", "order-1") is True
    assert executor.raw_calls == [("cancel", "SHSE.600000:order-1")]

    _, events = _get_guard(executor).flush_records()
    assert len(events) == 1
    assert events[0].operation == "cancel_order"
    assert events[0].decision == AccountControlDecision.ALLOWED


def test_cancel_all_orders_queries_once_and_cancels_every_pending_order(monkeypatch) -> None:
    """公开 cancel_all_orders 应查询一次账户挂单并逐笔撤单。"""
    executor = _WrapperExecutor(
        _build_guard(
            {
                "query_order": {"per_day": 1},
                "cancel_order": {"per_day": 2},
            },
            clock=_clock_from(
                datetime(2026, 3, 24, 9, 31, 15, 0),
                datetime(2026, 3, 24, 9, 31, 15, 100000),
                datetime(2026, 3, 24, 9, 31, 15, 350000),
                datetime(2026, 3, 24, 9, 31, 15, 600000),
            ),
        )
    )
    executor.pending_orders = [
        _make_pending_order("SHSE.600000", "order-1"),
        _make_pending_order("SZSE.000001", "order-2"),
    ]

    monkeypatch.setattr(
        executor,
        "reconcile_terminal_order",
        lambda symbol, order_id: next(
            order.model_copy(update={"status": "已撤销"})
            for order in executor.pending_orders
            if order.symbol == symbol and order.order_id == order_id
        ),
    )
    executor.cancel_all_orders()

    assert executor.raw_calls == [
        ("pending", None),
        ("cancel", "SHSE.600000:order-1"),
        ("cancel", "SZSE.000001:order-2"),
    ]

    _, events = _get_guard(executor).flush_records()
    assert len(events) == 3
    assert [event.operation for event in events] == [
        "query_order",
        "cancel_order",
        "cancel_order",
    ]
    assert [event.metadata for event in events[1:]] == [
        {"order_id": "order-1"},
        {"order_id": "order-2"},
    ]


def test_cancel_all_orders_aggregates_false_and_exception_failures() -> None:
    """公开 cancel_all_orders 应聚合所有失败的 order_id 并在末尾抛错。"""
    executor = _WrapperExecutor(
        _build_guard(
            {
                "query_order": {"per_day": 1},
                "cancel_order": {"per_day": 3},
            },
            clock=_clock_from(
                datetime(2026, 3, 24, 9, 31, 15, 0),
                datetime(2026, 3, 24, 9, 31, 15, 100000),
                datetime(2026, 3, 24, 9, 31, 15, 350000),
                datetime(2026, 3, 24, 9, 31, 15, 600000),
                datetime(2026, 3, 24, 9, 31, 15, 850000),
            ),
        )
    )
    executor.pending_orders = [
        _make_pending_order("SHSE.600000", "order-1"),
        _make_pending_order("SZSE.000001", "order-2"),
        _make_pending_order("SHSE.600519", "order-3"),
    ]
    executor.cancel_results = {
        "order-2": False,
        "order-3": RuntimeError("downstream exploded"),
    }

    with pytest.raises(RuntimeError, match="order-1.*order-2.*order-3"):
        executor.cancel_all_orders()

    assert executor.raw_calls == [
        ("pending", None),
        ("cancel", "SHSE.600000:order-1"),
        ("cancel", "SZSE.000001:order-2"),
        ("cancel", "SHSE.600519:order-3"),
    ]

    _, events = _get_guard(executor).flush_records()
    assert len(events) == 4
    assert [event.operation for event in events] == [
        "query_order",
        "cancel_order",
        "cancel_order",
        "cancel_order",
    ]
    assert [event.metadata for event in events[1:]] == [
        {"order_id": "order-1"},
        {"order_id": "order-2"},
        {"order_id": "order-3"},
    ]


def test_cancel_all_orders_propagates_ctp_session_recovery_without_continuing() -> None:
    executor = _WrapperExecutor(_build_guard({"query_order": {"per_day": 1}, "cancel_order": {"per_day": 2}}))
    executor.pending_orders = [
        _make_pending_order("SHSE.600000", "order-recovery"),
        _make_pending_order("SZSE.000001", "order-not-attempted"),
    ]
    executor.cancel_results = {
        "order-recovery": CtpSessionRecoveryRequired("ReqQryOrder同步拒绝: return_code=-2", return_code=-2)
    }

    with pytest.raises(CtpSessionRecoveryRequired, match="return_code=-2"):
        executor.cancel_all_orders()

    assert executor.raw_calls == [
        ("pending", None),
        ("cancel", "SHSE.600000:order-recovery"),
    ]


def _make_pending_order(symbol: str, order_id: str) -> UnifiedOrder:
    return UnifiedOrder(
        order_id=order_id,
        symbol=symbol,
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        volume=1.0,
        price=1.0,
        status="待成交",
    )


def _make_trade(symbol: str, order_id: str, trade_id: str) -> TradeRecord:
    return TradeRecord.create(
        trade_id=trade_id,
        trade_time="2026-03-24T09:31:15",
        trade_volume=1.0,
        trade_price=12.3,
        extra={"symbol": symbol, "order_id": order_id},
    )


def test_startup_cleanup_keeps_missing_old_order_until_actual_terminal():
    executor = _WrapperExecutor(_build_guard({}))
    old = _make_pending_order("SHSE.600000", "old-from-previous-execution")
    executor.account_orders.apply(old)
    executor.cancel_results[old.order_id] = CancelNotAllowedError("当前不可撤单")
    # 下一轮查询不再返回旧单；缺失不能被解释为已撤销。
    executor.pending_orders = []
    with pytest.raises(RuntimeError, match="当前不可撤单"):
        executor.cancel_all_orders()
    assert executor.account_orders.snapshot()["data"][0].order_id == old.order_id
    assert ("cancel", f"{old.symbol}:{old.order_id}") in executor.raw_calls
    executor.account_orders.apply(old.model_copy(update={"status": "已成交", "filled_volume": old.volume}))
    executor.set_account_control_guard(None)
    executor.cancel_all_orders()
    assert executor.account_orders.snapshot()["data"] == []


def test_startup_cleanup_does_not_treat_submitted_cancel_as_terminal():
    executor = _WrapperExecutor(_build_guard({}))
    executor.pending_orders = [_make_pending_order("SHSE.600000", "submitted-only")]
    with pytest.raises(RuntimeError, match="请求已提交"):
        executor.cancel_all_orders()
    assert executor.account_orders.snapshot()["data"][0].order_id == "submitted-only"
