"""ExecutionQueryRuntime 共享查询语义测试。"""

from __future__ import annotations

import threading

import pytest

from axile.executor.constants.order_status import OrderStatus
from axile.executor.execution_query_runtime import ExecutionQueryRuntime
from axile.executor.models.unified_order import OrderDirection, OrderType, TradeRecord, UnifiedOrder


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        """返回仿真计时值。"""
        return self.time()

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def event_wait(self, event: threading.Event, timeout: float) -> bool:
        return event.wait(timeout)


def test_pending_snapshot_expires_without_callbacks_and_remains_shared() -> None:
    """REST 无回报时仍能读到已成交订单消失，同时同轮不同品种共享快照。"""
    clock = _Clock()
    open_orders = [_build_pending_order("rb2610", "rb-order")]
    calls: list[int] = []

    def fetch() -> list[UnifiedOrder]:
        calls.append(1)
        return list(open_orders)

    runtime = ExecutionQueryRuntime(fetch_pending_orders_snapshot=fetch, clock=clock)
    assert len(runtime.get_pending_orders_for_symbol("rb2610")) == 1
    open_orders.clear()
    clock.sleep(0.9)
    assert len(runtime.get_pending_orders_for_symbol("rb2610")) == 1
    clock.sleep(0.1)
    assert runtime.get_pending_orders_for_symbol("rb2610") == []
    assert runtime.get_pending_orders_for_symbol("ag2612") == []
    assert calls == [1, 1]


def test_expired_pending_snapshot_singleflights_concurrent_symbols() -> None:
    clock = _Clock()
    started = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def fetch() -> list[UnifiedOrder]:
        calls.append(1)
        if len(calls) == 2:
            started.set()
            assert release.wait(1)
        return [_build_pending_order("rb2610", "rb-order")]

    runtime = ExecutionQueryRuntime(fetch_pending_orders_snapshot=fetch, clock=clock)
    runtime.get_pending_orders_for_symbol("rb2610")
    clock.sleep(1)
    results: dict[str, list[UnifiedOrder]] = {}
    first = threading.Thread(target=lambda: results.update(rb=runtime.get_pending_orders_for_symbol("rb2610")))
    second = threading.Thread(target=lambda: results.update(ag=runtime.get_pending_orders_for_symbol("ag2612")))
    first.start()
    assert started.wait(1)
    second.start()
    release.set()
    first.join(1)
    second.join(1)
    assert not first.is_alive() and not second.is_alive()
    assert len(results["rb"]) == 1 and results["ag"] == []
    assert calls == [1, 1]


@pytest.mark.parametrize("update", ["patch", "invalidate"])
def test_late_pending_response_cannot_overwrite_callback_or_invalidation(update: str) -> None:
    clock = _Clock()
    pending = _build_pending_order("rb2610", "rb-order")
    calls: list[int] = []
    runtime: ExecutionQueryRuntime

    def fetch() -> list[UnifiedOrder]:
        calls.append(1)
        if len(calls) == 2:
            if update == "patch":
                runtime.apply_pending_order_update(pending.model_copy(update={"status": OrderStatus.FILLED}))
            else:
                runtime.invalidate_orders()
            return [pending]
        return [pending] if len(calls) == 1 else []

    runtime = ExecutionQueryRuntime(fetch_pending_orders_snapshot=fetch, clock=clock)
    runtime.get_pending_orders_for_symbol("rb2610")
    clock.sleep(1)
    runtime.get_pending_orders_for_symbol("rb2610")
    assert runtime.get_pending_orders_for_symbol("rb2610") == []
    assert calls == [1, 1, 1]


def test_narrow_pending_query_is_fresh_without_clock_advance() -> None:
    calls: list[str] = []

    def fetch(symbol: str) -> list[UnifiedOrder]:
        calls.append(symbol)
        return [_build_pending_order(symbol, "rb-order")] if len(calls) == 1 else []

    runtime = ExecutionQueryRuntime(fetch_pending_orders_by_symbol=fetch, clock=_Clock())
    assert len(runtime.get_pending_orders_for_symbol("rb2610")) == 1
    assert runtime.get_pending_orders_for_symbol("rb2610") == []
    assert calls == ["rb2610", "rb2610"]


def _build_pending_order(symbol: str, order_id: str) -> UnifiedOrder:
    return UnifiedOrder(
        order_id=order_id,
        symbol=symbol,
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        volume=1.0,
        price=100.0,
        status=OrderStatus.PENDING,
    )


def _build_trade(symbol: str, order_id: str, trade_id: str) -> TradeRecord:
    return TradeRecord.create(
        trade_id=trade_id,
        trade_time="2026-03-25T09:00:00",
        trade_volume=1.0,
        trade_price=100.0,
        extra={"symbol": symbol, "order_id": order_id},
    )


def test_pending_order_snapshot_is_shared_across_symbols_until_invalidated() -> None:
    """snapshot-backed 挂单查询应只抓一次账户快照，再按 symbol 过滤。"""
    fetch_calls: list[str] = []

    def fetch_pending_orders_snapshot() -> list[UnifiedOrder]:
        fetch_calls.append("pending")
        return [
            _build_pending_order("rb2610", "rb-order"),
            _build_pending_order("ag2612", "ag-order"),
        ]

    runtime = ExecutionQueryRuntime(fetch_pending_orders_snapshot=fetch_pending_orders_snapshot)

    rb_orders = runtime.get_pending_orders_for_symbol("rb2610")
    ag_orders = runtime.get_pending_orders_for_symbol("ag2612")
    runtime.invalidate_all()
    rb_orders_after_invalidate = runtime.get_pending_orders_for_symbol("rb2610")

    assert [order.order_id for order in rb_orders] == ["rb-order"]
    assert [order.order_id for order in ag_orders] == ["ag-order"]
    assert [order.order_id for order in rb_orders_after_invalidate] == ["rb-order"]
    assert fetch_calls == ["pending", "pending"]


def test_selective_invalidation_only_clears_target_snapshot() -> None:
    """orders/trades 级失效应只清理对应 snapshot。"""
    fetch_calls: list[str] = []

    def fetch_pending_orders_snapshot() -> list[UnifiedOrder]:
        fetch_calls.append("pending")
        return [_build_pending_order("rb2610", "rb-order")]

    def fetch_trades_snapshot() -> list[TradeRecord]:
        fetch_calls.append("trades")
        return [_build_trade("rb2610", "order-1", "trade-1")]

    runtime = ExecutionQueryRuntime(
        fetch_pending_orders_snapshot=fetch_pending_orders_snapshot,
        fetch_trades_snapshot=fetch_trades_snapshot,
    )

    assert [order.order_id for order in runtime.get_pending_orders_for_symbol("rb2610")] == ["rb-order"]
    assert [trade.trade_id for trade in runtime.get_trades_for_order("rb2610", "order-1")] == ["trade-1"]

    runtime.invalidate_orders()
    assert [order.order_id for order in runtime.get_pending_orders_for_symbol("rb2610")] == ["rb-order"]
    assert [trade.trade_id for trade in runtime.get_trades_for_order("rb2610", "order-1")] == ["trade-1"]

    runtime.invalidate_trades()
    assert [trade.trade_id for trade in runtime.get_trades_for_order("rb2610", "order-1")] == ["trade-1"]

    assert fetch_calls == ["pending", "trades", "pending", "trades"]


def test_symbol_scoped_order_invalidation_keeps_other_symbols_cached_until_refresh() -> None:
    """只失效单个 symbol 的挂单时，不应让其他 symbol 立刻丢缓存。"""
    fetch_calls: list[str] = []

    def fetch_pending_orders_snapshot() -> list[UnifiedOrder]:
        fetch_calls.append("pending")
        return [
            _build_pending_order("rb2610", "rb-order"),
            _build_pending_order("ag2612", "ag-order"),
        ]

    runtime = ExecutionQueryRuntime(fetch_pending_orders_snapshot=fetch_pending_orders_snapshot)

    assert [order.order_id for order in runtime.get_pending_orders_for_symbol("rb2610")] == ["rb-order"]
    runtime.invalidate_orders("rb2610")

    assert [order.order_id for order in runtime.get_pending_orders_for_symbol("ag2612")] == ["ag-order"]
    assert [order.order_id for order in runtime.get_pending_orders_for_symbol("rb2610")] == ["rb-order"]
    assert fetch_calls == ["pending", "pending"]


def test_order_scoped_trade_invalidation_keeps_other_orders_cached_until_refresh() -> None:
    """只失效单个 order_id 的成交时，不应让其他订单立刻丢缓存。"""
    fetch_calls: list[str] = []

    def fetch_trades_snapshot() -> list[TradeRecord]:
        fetch_calls.append("trades")
        return [
            _build_trade("rb2610", "order-1", "trade-1"),
            _build_trade("rb2610", "order-2", "trade-2"),
        ]

    runtime = ExecutionQueryRuntime(fetch_trades_snapshot=fetch_trades_snapshot)

    assert [trade.trade_id for trade in runtime.get_trades_for_order("rb2610", "order-1")] == ["trade-1"]
    runtime.invalidate_trades("rb2610", "order-1")

    assert [trade.trade_id for trade in runtime.get_trades_for_order("rb2610", "order-2")] == ["trade-2"]
    assert [trade.trade_id for trade in runtime.get_trades_for_order("rb2610", "order-1")] == ["trade-1"]
    assert fetch_calls == ["trades", "trades"]


def test_order_patch_updates_cached_pending_snapshot_without_refetch() -> None:
    """本地 order patch 应直接更新 pending snapshot，而不是触发新抓取。"""
    fetch_calls: list[str] = []

    def fetch_pending_orders_snapshot() -> list[UnifiedOrder]:
        fetch_calls.append("pending")
        return [_build_pending_order("rb2610", "rb-order")]

    runtime = ExecutionQueryRuntime(fetch_pending_orders_snapshot=fetch_pending_orders_snapshot)

    assert [order.order_id for order in runtime.get_pending_orders_for_symbol("rb2610")] == ["rb-order"]

    updated_order = _build_pending_order("rb2610", "rb-order")
    updated_order.price = 101.5
    runtime.apply_pending_order_update(updated_order)

    patched_orders = runtime.get_pending_orders_for_symbol("rb2610")
    assert len(patched_orders) == 1
    assert patched_orders[0].price == 101.5
    assert fetch_calls == ["pending"]


def test_trade_patch_updates_cached_trade_snapshot_without_touching_pending_order() -> None:
    """本地 trade patch 只应更新 trades snapshot，不应反向 patch pending order。"""
    order_fetch_calls: list[str] = []
    trade_fetch_calls: list[str] = []

    pending_order = _build_pending_order("rb2610", "order-1")
    pending_order.volume = 2.0

    def fetch_pending_orders_snapshot() -> list[UnifiedOrder]:
        order_fetch_calls.append("pending")
        return [pending_order.model_copy(deep=True)]

    def fetch_trades_snapshot() -> list[TradeRecord]:
        trade_fetch_calls.append("trades")
        return []

    runtime = ExecutionQueryRuntime(
        fetch_pending_orders_snapshot=fetch_pending_orders_snapshot,
        fetch_trades_snapshot=fetch_trades_snapshot,
    )

    assert [order.order_id for order in runtime.get_pending_orders_for_symbol("rb2610")] == ["order-1"]
    assert runtime.get_trades_for_order("rb2610", "order-1") == []

    runtime.apply_trade_record(_build_trade("rb2610", "order-1", "trade-1"))

    patched_trades = runtime.get_trades_for_order("rb2610", "order-1")
    patched_orders = runtime.get_pending_orders_for_symbol("rb2610")
    assert [trade.trade_id for trade in patched_trades] == ["trade-1"]
    assert len(patched_orders) == 1
    assert patched_orders[0].filled_volume == 0.0
    assert trade_fetch_calls == ["trades"]
    assert order_fetch_calls == ["pending"]


def test_order_patch_does_not_clear_trade_dirty_scope() -> None:
    """order patch 属于订单域，不应顺手清理 trades dirty marker。"""
    trade_fetch_calls: list[str] = []

    def fetch_trades_snapshot() -> list[TradeRecord]:
        trade_fetch_calls.append("trades")
        return [_build_trade("rb2610", "order-1", "trade-1")]

    runtime = ExecutionQueryRuntime(fetch_trades_snapshot=fetch_trades_snapshot)

    assert [trade.trade_id for trade in runtime.get_trades_for_order("rb2610", "order-1")] == ["trade-1"]
    runtime.invalidate_trades("rb2610", "order-1")
    runtime.apply_pending_order_update(_build_pending_order("rb2610", "order-1"))
    assert [trade.trade_id for trade in runtime.get_trades_for_order("rb2610", "order-1")] == ["trade-1"]
    assert trade_fetch_calls == ["trades", "trades"]


def test_trade_patch_does_not_clear_order_dirty_scope() -> None:
    """trade patch 属于成交域，不应顺手清理 orders dirty marker。"""
    order_fetch_calls: list[str] = []

    def fetch_pending_orders_snapshot() -> list[UnifiedOrder]:
        order_fetch_calls.append("pending")
        return [_build_pending_order("rb2610", "order-1")]

    runtime = ExecutionQueryRuntime(fetch_pending_orders_snapshot=fetch_pending_orders_snapshot)

    assert [order.order_id for order in runtime.get_pending_orders_for_symbol("rb2610")] == ["order-1"]
    runtime.invalidate_orders("rb2610")
    runtime.apply_trade_record(_build_trade("rb2610", "order-1", "trade-1"))
    assert [order.order_id for order in runtime.get_pending_orders_for_symbol("rb2610")] == ["order-1"]
    assert order_fetch_calls == ["pending", "pending"]


def test_trade_snapshot_singleflights_concurrent_joiners_and_filters_per_order() -> None:
    """同一份 trades snapshot 应允许多个 joiner 共享一次远端抓取。"""
    fetch_started = threading.Event()
    release_fetch = threading.Event()
    fetch_count = 0
    fetch_count_lock = threading.Lock()

    def fetch_trades_snapshot() -> list[TradeRecord]:
        nonlocal fetch_count
        with fetch_count_lock:
            fetch_count += 1
        fetch_started.set()
        release_fetch.wait(timeout=1)
        return [
            _build_trade("rb2610", "order-1", "trade-1"),
            _build_trade("ag2612", "order-2", "trade-2"),
        ]

    runtime = ExecutionQueryRuntime(fetch_trades_snapshot=fetch_trades_snapshot)
    results: dict[str, list[TradeRecord]] = {}

    def read_rb() -> None:
        results["rb"] = runtime.get_trades_for_order("rb2610", "order-1")

    def read_ag() -> None:
        results["ag"] = runtime.get_trades_for_order("ag2612", "order-2")

    rb_thread = threading.Thread(target=read_rb)
    ag_thread = threading.Thread(target=read_ag)
    rb_thread.start()
    fetch_started.wait(timeout=1)
    ag_thread.start()
    release_fetch.set()
    rb_thread.join()
    ag_thread.join()

    assert fetch_count == 1
    assert [trade.trade_id for trade in results["rb"]] == ["trade-1"]
    assert [trade.trade_id for trade in results["ag"]] == ["trade-2"]


def test_narrow_trade_query_only_singleflights_identical_inflight_requests() -> None:
    """narrow-query 不做全局缓存，只对相同 query key 的并发请求去重。"""
    fetch_started = threading.Event()
    release_fetch = threading.Event()
    fetch_calls: list[tuple[str, str]] = []
    results: list[list[TradeRecord]] = []

    def fetch_trades_by_order(symbol: str, order_id: str) -> list[TradeRecord]:
        fetch_calls.append((symbol, order_id))
        fetch_started.set()
        release_fetch.wait(timeout=1)
        return [_build_trade(symbol, order_id, f"trade-{order_id}")]

    runtime = ExecutionQueryRuntime(fetch_trades_by_order=fetch_trades_by_order)

    def read_once() -> None:
        results.append(runtime.get_trades_for_order("rb2610", "order-1"))

    first = threading.Thread(target=read_once)
    second = threading.Thread(target=read_once)
    first.start()
    fetch_started.wait(timeout=1)
    second.start()
    release_fetch.set()
    first.join()
    second.join()

    third_result = runtime.get_trades_for_order("rb2610", "order-1")

    assert fetch_calls == [("rb2610", "order-1"), ("rb2610", "order-1")]
    assert [[trade.trade_id for trade in batch] for batch in results] == [["trade-order-1"], ["trade-order-1"]]
    assert [trade.trade_id for trade in third_result] == ["trade-order-1"]
