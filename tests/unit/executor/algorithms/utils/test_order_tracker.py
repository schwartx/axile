"""OrderTracker 超时诊断测试。"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from axile.common.trade_channel import TradeChannel
from axile.executor.algorithms.utils.order_tracker import ChaseConfig, OrderTracker
from axile.executor.algorithms.utils.outcome import summarize_outcome
from axile.executor.constants.order_status import OrderStatus
from axile.executor.ctp.ctp_execute import CtpSessionRecoveryRequired
from axile.executor.models.order_channel_health import OrderChannelHealth
from axile.executor.models.unified_account_assets import Position, PositionDirection, UnifiedAccountAssets
from axile.executor.models.unified_order import OrderDirection, OrderType, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData
from axile.executor.termination import ExecutionTerminated
from tests.fixtures.algorithm_fakes import ClockStub


class _Logger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def debug(self, message: object, *args: object, **kwargs: object) -> None:
        _ = args, kwargs
        self.messages.append(str(message))

    def info(self, message: object, *args: object, **kwargs: object) -> None:
        _ = args, kwargs
        self.messages.append(str(message))

    def warning(self, message: object, *args: object, **kwargs: object) -> None:
        _ = args, kwargs
        self.messages.append(str(message))

    def error(self, message: object, *args: object, **kwargs: object) -> None:
        _ = args, kwargs
        self.messages.append(str(message))

    def exception(self, message: object, *args: object, **kwargs: object) -> None:
        _ = args, kwargs
        self.messages.append(str(message))


class _FakeExecutor:
    channel_type = TradeChannel.GM
    symbol = "SHSE.600000"

    def __init__(self) -> None:
        self.logger = _Logger()
        self.audit_context: dict[str, Any] = {}

    def get_current_volume(self, account_assets: UnifiedAccountAssets) -> float:
        _ = account_assets
        return 0.0

    def get_positions(self, account_assets: UnifiedAccountAssets) -> list[tuple[float, PositionDirection]]:
        _ = account_assets
        return []

    def place_order(
        self,
        direction: OrderDirection,
        order_type: OrderType,
        volume: float,
        price: float = 0,
        **kwargs: object,
    ) -> UnifiedOrder:
        _ = direction, order_type, volume, price, kwargs
        raise NotImplementedError

    def get_pending_orders(self) -> list[UnifiedOrder]:
        return []

    def query_trades(self, order_id: str) -> list[object]:
        _ = order_id
        return []

    def is_termination_requested(self) -> bool:
        return False

    def get_termination_mode(self) -> str | None:
        return None

    def handle_termination_checkpoint(self) -> None:
        return None

    def set_audit_context(self, context: dict[str, Any]) -> None:
        self.audit_context = dict(context)

    def next_audit_seq(self) -> int:
        return 1

    def register_order_audit_metadata(self, order_id: str, metadata: dict[str, Any]) -> None:
        _ = order_id, metadata

    def get_order_audit_metadata(self, order_id: str) -> dict[str, Any]:
        _ = order_id
        return {}

    def emit_audit_event(self, **kwargs: object) -> bool:
        _ = kwargs
        return True

    def get_account_assets(self) -> UnifiedAccountAssets:
        return UnifiedAccountAssets(
            available_cash=1000.0,
            total_asset=1000.0,
            market_value=0.0,
            positions=[],
        )

    def get_market_data(self) -> UnifiedPriceData | None:
        return None

    def get_tick_size(self) -> float | None:
        return 0.01

    def register_order_callback(self, callback: object) -> None:
        _ = callback

    def register_price_callback(self, callback: object) -> None:
        _ = callback

    def unregister_order_callback(self, callback: object) -> None:
        _ = callback

    def unregister_price_callback(self, callback: object) -> None:
        _ = callback

    def is_monitoring(self) -> bool:
        return False

    def cancel_order(self, order_id: str) -> bool:
        _ = order_id
        return True


class _ChaseExecutor(_FakeExecutor):
    """记录审计事件、并让 ``place_order`` 返回新订单的执行器替身。

    ``cancel_order`` 会把订单推进终态（经 tracker 的订单回调），模拟撤单**确实生效**
    的渠道。追价换单要求撤单确认后才下新单，因此替身必须提供这一步；只返回 ``True``
    而不推终态的替身对应的是 GM 那种「仅提交成功」的语义，见
    ``test_chase_skipped_when_cancel_unconfirmed``。
    """

    def __init__(self) -> None:
        super().__init__()
        self.emitted: list[dict[str, Any]] = []
        self._placed = 0
        self.tracker: OrderTracker | None = None

    def cancel_order(self, order_id: str) -> bool:
        if self.tracker is not None:
            tracked = self.tracker.pending_orders.get(order_id)
            if tracked is not None:
                cancelled = tracked.model_copy(deep=True)
                cancelled.status = OrderStatus.CANCELED
                self.tracker.on_order_update(cancelled)
        return True

    def place_order(
        self,
        direction: OrderDirection,
        order_type: OrderType,
        volume: float,
        price: float = 0,
        **kwargs: object,
    ) -> UnifiedOrder:
        _ = kwargs
        self._placed += 1
        return UnifiedOrder(
            order_id=f"chase-{self._placed}",
            symbol=self.symbol,
            direction=direction,
            order_type=order_type,
            volume=volume,
            price=price,
            status=OrderStatus.SUBMITTED,
            filled_volume=0.0,
            avg_price=0.0,
        )

    def emit_audit_event(self, **kwargs: object) -> bool:
        self.emitted.append(kwargs)
        return True


def test_check_and_chase_emits_chase_event() -> None:
    """追价重挂应补发 ORDER_SUBMITTED + ``COMMON.ORDER_CHASE`` 审计事件。"""
    executor = _ChaseExecutor()
    tracker = OrderTracker(
        executor=executor,
        chase_config=ChaseConfig(enabled=True, ticks=1, max_count=5, interval=0.0),
    )
    executor.tracker = tracker
    order = _build_pending_order("gm-order-1")
    tracker.add_order(order, direction=OrderDirection.BUY)
    # 盘口显著偏离挂单价（ask 11.0 > 挂单 10.0），触发追价；BUY 追向 bid=10.9。
    tracker.latest_prices[order.symbol] = UnifiedPriceData(
        symbol=order.symbol,
        last_price=10.9,
        bid_price=10.9,
        ask_price=11.0,
        bid_volume=100.0,
        ask_volume=100.0,
        volume=1000.0,
        timestamp=int(time.time() * 1000),
        update_time="2026-07-14T23:00:00",
    )

    tracker._check_and_chase()

    chase = [e for e in executor.emitted if e.get("reason_code") == "COMMON.ORDER_CHASE"]
    assert len(chase) == 1
    details = chase[0]["details"]["chase"]
    assert details["index"] == 1
    assert details["from_price"] == 10.0
    assert details["to_price"] == 10.9
    assert details["prev_order_id"] == "gm-order-1"


def test_passive_order_at_best_bid_does_not_chase_on_wide_spread() -> None:
    """挂在最优买价的被动 BUY：买价未被超越时不应追价，即便价差很宽。

    旧实现量「对手价 ask」，宽价差下会误判需要追价，再在重挂时因 ``new_price == order.price``
    空转（无成交、无事件）。新实现量「本方 bid」，与重挂目标一致，此处正确不触发。
    """
    executor = _ChaseExecutor()
    tracker = OrderTracker(
        executor=executor,
        chase_config=ChaseConfig(enabled=True, ticks=1, max_count=5, interval=0.0),
    )
    order = _build_pending_order("bid-hold-1")
    tracker.add_order(order, direction=OrderDirection.BUY)
    # 我们就是最优买价（bid == 挂单价 10.0），但价差很宽（ask=10.5）。买价未上移 → 不该追。
    tracker.latest_prices[order.symbol] = UnifiedPriceData(
        symbol=order.symbol,
        last_price=10.0,
        bid_price=10.0,
        ask_price=10.5,
        bid_volume=100.0,
        ask_volume=100.0,
        volume=1000.0,
        timestamp=int(time.time() * 1000),
        update_time="2026-07-14T23:00:00",
    )

    assert tracker._get_orders_needing_chase() == []


def test_wait_for_completion_returns_immediately_when_no_orders() -> None:
    """本轮无任何挂单（例如所有品种被跳过）时应立即判定完成，而非空转到 timeout。

    回归 R3：all_done_event 仅由订单回调在 pending 归零时置位，0 单场景该回调永不触发；
    若无此早退守卫，wait_for_completion 会一直空转到 timeout（可达 max_wait_seconds 1 小时）。
    """
    executor = _ChaseExecutor()
    tracker = OrderTracker(
        executor=executor,
        chase_config=ChaseConfig(enabled=True, ticks=1, max_count=5, interval=0.0),
    )
    # 未 add_order：pending 为空、无换单在途。即便给一个很大的 timeout，也应立刻返回 True。
    assert tracker.wait_for_completion(timeout=3600) is True


def test_wait_for_completion_cancel_failure_keeps_execution_error(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = OrderTracker(executor=_FakeExecutor())
    tracker.add_order(_build_pending_order("gm-order-1"))
    monkeypatch.setattr(
        "axile.executor.algorithms.utils.order_tracker.cancel_pending_orders_via_query",
        lambda _executor: ["gm-order-1"],
    )
    with pytest.raises(RuntimeError, match="部分订单撤销失败") as raised:
        tracker.wait_for_completion(timeout=0)
    assert raised.value.execution_error == "撤单失败，订单终态尚未确认"
    assert tracker.explicit_error == "撤单失败，订单终态尚未确认"


def _build_pending_order(order_id: str, symbol: str = "SHSE.600000") -> UnifiedOrder:
    return UnifiedOrder(
        order_id=order_id,
        symbol=symbol,
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        volume=100.0,
        price=10.0,
        status=OrderStatus.SUBMITTED,
        filled_volume=0.0,
        avg_price=0.0,
    )


def test_order_tracker_timeout_emits_reconciliation_reason() -> None:
    """超时摘要应标明资产已收敛，而非只记录 timeout。"""
    tracker = OrderTracker(executor=_FakeExecutor())
    tracker.add_order(_build_pending_order("gm-order-1"))

    summary = tracker._build_timeout_summary(
        UnifiedAccountAssets(
            available_cash=1200.0,
            total_asset=1200.0,
            market_value=0.0,
            positions=[],
        )
    )

    assert summary["reconciliation_reason"] == "asset_state_converged_after_timeout"


def test_order_tracker_timeout_summary_keeps_pending_order_ids() -> None:
    """超时摘要应保留仍在等待收敛的订单标识。"""
    tracker = OrderTracker(executor=_FakeExecutor())
    tracker.add_order(_build_pending_order("gm-order-1"))
    tracker.add_order(_build_pending_order("gm-order-2", symbol="SZSE.000001"))

    summary = tracker._build_timeout_summary(
        UnifiedAccountAssets(
            available_cash=800.0,
            total_asset=1000.0,
            market_value=200.0,
            positions=[
                Position(
                    symbol="SZSE.000001",
                    volume=100.0,
                    available_volume=100.0,
                    market_value=200.0,
                    direction=PositionDirection.LONG,
                    avg_price=2.0,
                )
            ],
        )
    )

    assert summary["pending_order_ids"] == ["gm-order-1", "gm-order-2"]
    assert summary["pending_symbols"] == ["SHSE.600000", "SZSE.000001"]
    assert summary["reconciliation_reason"] == "pending_orders_still_active_after_timeout"


class _ReconcileExecutor(_ChaseExecutor):
    """模拟支持 REST 终态对账的执行器替身。"""

    def __init__(self) -> None:
        super().__init__()
        self.reconcile_calls: list[tuple[str, str]] = []
        self.terminal_to_return: UnifiedOrder | None = None

    def reconcile_terminal_order(self, symbol: str, order_id: str) -> UnifiedOrder | None:
        self.reconcile_calls.append((symbol, order_id))
        return self.terminal_to_return


class _TimeoutCancelExecutor(_ReconcileExecutor):
    """撤单不推送回报，仅允许后续查询确认真实终态。"""

    def __init__(self, order: UnifiedOrder, *, cancel_ok: bool = True, confirm_after: int = 1) -> None:
        super().__init__()
        self.order = order
        self.cancel_ok = cancel_ok
        self.confirm_after = confirm_after
        self.cancel_calls: list[str] = []
        self.query_error = False

    def get_pending_orders(self) -> list[UnifiedOrder]:
        if not self.cancel_calls:
            return [self.order]
        if self.query_error:
            raise OSError("order query unavailable")
        return []

    def cancel_order(self, order_id: str) -> bool:
        self.cancel_calls.append(order_id)
        return self.cancel_ok

    def reconcile_terminal_order(self, symbol: str, order_id: str) -> UnifiedOrder | None:
        self.reconcile_calls.append((symbol, order_id))
        if len(self.reconcile_calls) < self.confirm_after:
            return None
        return self.terminal_to_return


@pytest.mark.parametrize("cancel_ok", [True, False])
@pytest.mark.parametrize("terminal_status", [OrderStatus.CANCELED, OrderStatus.FILLED])
def test_timeout_cancel_reconciles_terminal_before_result(cancel_ok: bool, terminal_status: OrderStatus) -> None:
    """丢失回报时仍确认撤单/成交竞态，结果不得误报订单终态未知。"""
    order = _build_pending_order("cancel-without-ws")
    executor = _TimeoutCancelExecutor(order, cancel_ok=cancel_ok)
    filled = order.volume if terminal_status == OrderStatus.FILLED else 25.0
    executor.terminal_to_return = order.model_copy(
        update={"status": terminal_status, "filled_volume": filled, "avg_price": order.price}
    )
    tracker = OrderTracker(executor=executor, clock=ClockStub())
    tracker.add_order(order)

    tracker.wait_for_completion(timeout=0)

    assert executor.cancel_calls == [order.order_id]
    assert not tracker.pending_orders
    assert tracker.completed_orders[order.order_id].filled_volume == filled
    outcome = summarize_outcome(0, filled, order.volume, tracker.get_all_orders(), tracker.get_all_trades())
    assert outcome["error"] != "订单最终状态尚未确认（含撤单结果未知）"
    assert outcome["status"].value == ("SUCCEEDED" if terminal_status == OrderStatus.FILLED else "PARTIAL")


def test_timeout_cancel_does_not_extend_deadline_for_delayed_terminal() -> None:
    order = _build_pending_order("delayed-cancel")
    executor = _TimeoutCancelExecutor(order, confirm_after=2)
    executor.terminal_to_return = order.model_copy(update={"status": OrderStatus.CANCELED})
    clock = ClockStub()
    tracker = OrderTracker(executor=executor, clock=clock)
    tracker.add_order(order)

    tracker.wait_for_completion(timeout=0)

    assert len(executor.reconcile_calls) == 1
    assert executor.cancel_calls == [order.order_id]
    assert order.order_id in tracker.pending_orders
    assert clock.time() == 0
    assert executor._placed == 0


@pytest.mark.parametrize("query_error", [False, True])
def test_timeout_cancel_keeps_unknown_when_confirmation_unavailable(query_error: bool) -> None:
    order = _build_pending_order("unknown-cancel")
    executor = _TimeoutCancelExecutor(order)
    executor.query_error = query_error
    clock = ClockStub()
    tracker = OrderTracker(executor=executor, clock=clock)
    tracker.add_order(order)

    tracker.wait_for_completion(timeout=0)

    assert order.order_id in tracker.pending_orders
    assert not tracker.completed_orders
    assert len(executor.reconcile_calls) == (0 if query_error else 1)
    assert clock.time() == 0
    outcome = summarize_outcome(0, 0, order.volume, tracker.get_all_orders(), tracker.get_all_trades())
    assert outcome["error"] == "订单最终状态尚未确认（含撤单结果未知）"


def test_query_pending_orders_propagates_ctp_session_recovery() -> None:
    executor = _FakeExecutor()
    tracker = OrderTracker(executor=executor)
    tracker.add_order(_build_pending_order("ctp-query-recovery"))
    recovery = CtpSessionRecoveryRequired("ReqQryOrder同步拒绝: return_code=-2", return_code=-2)
    executor.get_pending_orders = lambda: (_ for _ in ()).throw(recovery)  # type: ignore[method-assign]

    with pytest.raises(CtpSessionRecoveryRequired, match="return_code=-2"):
        tracker._query_pending_orders()


def test_refresh_timeout_account_assets_propagates_ctp_session_recovery() -> None:
    executor = _FakeExecutor()
    tracker = OrderTracker(executor=executor)
    recovery = CtpSessionRecoveryRequired("ReqQryTradingAccount同步拒绝: return_code=-2", return_code=-2)
    executor.get_account_assets = lambda: (_ for _ in ()).throw(recovery)  # type: ignore[method-assign]

    with pytest.raises(CtpSessionRecoveryRequired, match="return_code=-2"):
        tracker._refresh_timeout_account_assets()


def test_reconcile_missing_pending_converges_on_ws_frame_loss() -> None:
    """WS 终态帧丢失：订单卡 pending、已从挂单列表消失时，REST 对账应查终态并收敛。"""
    executor = _ReconcileExecutor()
    tracker = OrderTracker(executor=executor)
    order = _build_pending_order("gm-lost-1")
    tracker.add_order(order, direction=OrderDirection.BUY)
    # 交易所已不再挂（get_pending_orders 返回 []）、WS 未送终态 → 订单卡 pending；对账查到 FILLED。
    executor.terminal_to_return = order.model_copy(
        update={"status": OrderStatus.FILLED, "filled_volume": order.volume, "avg_price": order.price}
    )

    tracker._query_pending_orders()

    assert executor.reconcile_calls == [(order.symbol, "gm-lost-1")]
    assert "gm-lost-1" in tracker.completed_orders
    assert "gm-lost-1" not in tracker.pending_orders
    assert tracker.all_done_event.is_set()


def test_reconcile_skipped_without_capability() -> None:
    """执行器不提供 reconcile_terminal_order（如 CTP/GM）时对账跳过、订单保持不变、不抛。"""
    executor = _ChaseExecutor()  # 无 reconcile_terminal_order
    tracker = OrderTracker(executor=executor)
    tracker.add_order(_build_pending_order("gm-keep-1"), direction=OrderDirection.BUY)

    tracker._query_pending_orders()

    assert "gm-keep-1" in tracker.pending_orders
    assert not tracker.all_done_event.is_set()


def test_reconcile_ignores_non_terminal_order() -> None:
    """对账返回 None（未终态/查询失败）时不收敛、不误置完成。"""
    executor = _ReconcileExecutor()
    executor.terminal_to_return = None
    tracker = OrderTracker(executor=executor)
    tracker.add_order(_build_pending_order("gm-pending-1"), direction=OrderDirection.BUY)

    tracker._query_pending_orders()

    assert executor.reconcile_calls == [("SHSE.600000", "gm-pending-1")]
    assert "gm-pending-1" in tracker.pending_orders
    assert not tracker.all_done_event.is_set()


class _WsChannelExecutor(_ReconcileExecutor):
    """在 reconcile 能力之上，模拟可上报 WS 回报通道健康度并登记下单探针的执行器替身。"""

    def __init__(self) -> None:
        super().__init__()
        self.health = OrderChannelHealth.HEALTHY
        self.ack_probes: list[str] = []

    def get_order_channel_health(self) -> OrderChannelHealth:
        return self.health

    def expect_order_ack(self, order_id: str) -> None:
        self.ack_probes.append(order_id)


def test_add_order_registers_ws_ack_probe() -> None:
    """订单纳入跟踪即登记下单探针（供 WS 回报超时判定）。"""
    executor = _WsChannelExecutor()
    tracker = OrderTracker(executor=executor)
    tracker.add_order(_build_pending_order("ws-1"), direction=OrderDirection.BUY)
    assert executor.ack_probes == ["ws-1"]


def test_query_interval_healthy_uses_base_interval() -> None:
    """WS 健康时沿用基础慢频，WS 作为加速主力。"""
    executor = _WsChannelExecutor()
    executor.health = OrderChannelHealth.HEALTHY
    tracker = OrderTracker(executor=executor, order_refresh_interval=30.0, degraded_query_interval=3.0)
    assert tracker._current_query_interval(timeout=600.0, check_interval=1.0) == 30.0


def test_query_interval_speeds_up_when_channel_degraded() -> None:
    """WS 降级 / 失联时收敛到快频，REST 对账接管保正确性。"""
    executor = _WsChannelExecutor()
    tracker = OrderTracker(executor=executor, order_refresh_interval=30.0, degraded_query_interval=3.0)

    executor.health = OrderChannelHealth.DEGRADED
    assert tracker._current_query_interval(timeout=600.0, check_interval=1.0) == 3.0

    executor.health = OrderChannelHealth.DOWN
    assert tracker._current_query_interval(timeout=600.0, check_interval=1.0) == 3.0


def test_query_interval_unknown_channel_keeps_base() -> None:
    """无 WS 概念渠道（无健康上报）保持原有慢频，不改变既有行为。"""
    executor = _ChaseExecutor()  # 无 get_order_channel_health → UNKNOWN
    tracker = OrderTracker(executor=executor, order_refresh_interval=30.0)
    assert tracker._current_query_interval(timeout=600.0, check_interval=1.0) == 30.0


def test_query_interval_takeover_even_when_base_disabled() -> None:
    """定时查询被配置为禁用时，通道失联仍启用快频接管；健康时保持禁用。"""
    executor = _WsChannelExecutor()
    tracker = OrderTracker(executor=executor, order_refresh_interval=0.0, degraded_query_interval=2.0)

    executor.health = OrderChannelHealth.DOWN
    assert tracker._current_query_interval(timeout=600.0, check_interval=1.0) == 2.0

    executor.health = OrderChannelHealth.HEALTHY
    assert tracker._current_query_interval(timeout=600.0, check_interval=1.0) == float("inf")


def test_completed_order_repeat_update_not_buffered() -> None:
    """已终态订单被 REST 高频对账反复查到时幂等丢弃，不进早到缓冲、不泄漏。"""
    executor = _WsChannelExecutor()
    tracker = OrderTracker(executor=executor)
    order = _build_pending_order("done-1")
    tracker.add_order(order, direction=OrderDirection.BUY)

    terminal = order.model_copy(
        update={"status": OrderStatus.FILLED, "filled_volume": order.volume, "avg_price": order.price}
    )
    tracker.on_order_update(terminal)
    assert "done-1" in tracker.completed_orders

    # 模拟 REST 对账在订单终态后又反复查到该单
    tracker.on_order_update(terminal.model_copy())
    tracker.on_order_update(terminal.model_copy())

    assert tracker._early_order_updates == {}


# --- 盘口陈旧时 REST 兜底刷新 --------------------------------------------------


class _FixedClock:
    """固定时钟：仅提供 ``time()``，供盘口新鲜度判定的确定性测试使用。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


class _MarketDataExecutor(_FakeExecutor):
    """可配置 ``get_market_data`` 返回值并计数其调用次数的执行器替身。"""

    def __init__(self, snapshot: UnifiedPriceData | None) -> None:
        super().__init__()
        self._snapshot = snapshot
        self.market_data_calls = 0

    def get_market_data(self) -> UnifiedPriceData | None:
        self.market_data_calls += 1
        return self._snapshot


def _price(symbol: str, bid: float, ask: float, timestamp_ms: int) -> UnifiedPriceData:
    """构造一个带指定时间戳的盘口快照。"""
    return UnifiedPriceData(
        symbol=symbol,
        last_price=(bid + ask) / 2,
        bid_price=bid,
        ask_price=ask,
        bid_volume=100.0,
        ask_volume=100.0,
        volume=1000.0,
        timestamp=timestamp_ms,
        update_time="2026-07-14T23:00:00",
    )


def test_usable_price_fresh_skips_rest() -> None:
    """盘口新鲜时直接用 WS 快照，不打 REST。"""
    clock = _FixedClock(now=1000.0)
    executor = _MarketDataExecutor(_price("SHSE.600000", 11.0, 11.02, timestamp_ms=999_000))
    tracker = OrderTracker(executor=executor, clock=clock, price_stale_after=5.0)
    fresh = _price("SHSE.600000", 10.0, 10.02, timestamp_ms=int(1000.0 * 1000))  # 年龄 0s
    tracker.latest_prices["SHSE.600000"] = fresh

    result = tracker._usable_price("SHSE.600000")

    assert result is fresh
    assert executor.market_data_calls == 0


def test_usable_price_stale_triggers_rest_refresh() -> None:
    """盘口陈旧超阈值时 REST 兜底刷新，并写回 latest_prices。"""
    clock = _FixedClock(now=1000.0)
    rest_snapshot = _price("SHSE.600000", 11.0, 11.02, timestamp_ms=int(1000.0 * 1000))
    executor = _MarketDataExecutor(rest_snapshot)
    tracker = OrderTracker(executor=executor, clock=clock, price_stale_after=5.0)
    # 年龄 10s > 5s 阈值 → 陈旧
    tracker.latest_prices["SHSE.600000"] = _price("SHSE.600000", 10.0, 10.02, timestamp_ms=int(990.0 * 1000))

    result = tracker._usable_price("SHSE.600000")

    assert result is rest_snapshot
    assert tracker.latest_prices["SHSE.600000"] is rest_snapshot
    assert executor.market_data_calls == 1


def test_rest_refresh_is_rate_limited() -> None:
    """同一时钟窗口内多次读价只打一次 REST（限频）。"""
    clock = _FixedClock(now=1000.0)
    rest_snapshot = _price("SHSE.600000", 11.0, 11.02, timestamp_ms=int(1000.0 * 1000))
    executor = _MarketDataExecutor(rest_snapshot)
    tracker = OrderTracker(executor=executor, clock=clock, price_stale_after=5.0, rest_price_refresh_interval=1.0)
    tracker.latest_prices["SHSE.600000"] = _price("SHSE.600000", 10.0, 10.02, timestamp_ms=int(990.0 * 1000))

    tracker._usable_price("SHSE.600000")
    tracker._usable_price("SHSE.600000")

    assert executor.market_data_calls == 1


def test_rest_refresh_skipped_for_other_symbol() -> None:
    """非当前品种不走 REST 兜底（executor 只代理自身 symbol），返回不可用。"""
    clock = _FixedClock(now=1000.0)
    executor = _MarketDataExecutor(_price("OTHER", 11.0, 11.02, timestamp_ms=int(1000.0 * 1000)))
    tracker = OrderTracker(executor=executor, clock=clock, price_stale_after=5.0)
    stale = _price("OTHER", 10.0, 10.02, timestamp_ms=int(990.0 * 1000))
    tracker.latest_prices["OTHER"] = stale

    result = tracker._usable_price("OTHER")

    assert result is None
    assert executor.market_data_calls == 0


def test_rest_refresh_invalid_snapshot_rejects_stale() -> None:
    """REST 返回无效快照时不返回陈旧缓存，不写回、不崩溃。"""
    clock = _FixedClock(now=1000.0)
    invalid = _price("SHSE.600000", 0.0, 10.02, timestamp_ms=int(1000.0 * 1000))  # bid=0 → is_valid False
    executor = _MarketDataExecutor(invalid)
    tracker = OrderTracker(executor=executor, clock=clock, price_stale_after=5.0)
    stale = _price("SHSE.600000", 10.0, 10.02, timestamp_ms=int(990.0 * 1000))
    tracker.latest_prices["SHSE.600000"] = stale

    result = tracker._usable_price("SHSE.600000")

    assert result is None
    assert tracker.latest_prices["SHSE.600000"] is stale
    assert executor.market_data_calls == 1


def test_pending_chase_symbols_dedup() -> None:
    """待追单品种快照应去重保序。"""
    executor = _ChaseExecutor()
    tracker = OrderTracker(
        executor=executor,
        chase_config=ChaseConfig(enabled=True, ticks=1, max_count=5, interval=0.0),
    )
    tracker.add_order(_build_pending_order("o1", symbol="SHSE.600000"), direction=OrderDirection.BUY)
    tracker.add_order(_build_pending_order("o2", symbol="SZSE.000001"), direction=OrderDirection.SELL)
    tracker.add_order(_build_pending_order("o3", symbol="SHSE.600000"), direction=OrderDirection.BUY)

    assert tracker._pending_chase_symbols() == ["SHSE.600000", "SZSE.000001"]


class _UnconfirmedCancelExecutor(_ChaseExecutor):
    """模拟 GM：``cancel_order`` 返回 ``True`` 但订单并未进入终态。

    GM 的 ``_cancel_order_impl`` 只表示「撤单请求已投递到 bridge」，不代表交易所
    已经撤掉这笔单。
    """

    def cancel_order(self, order_id: str) -> bool:
        _ = order_id
        return True  # 仅提交成功，不推终态


def _wide_spread_price(symbol: str) -> UnifiedPriceData:
    """构造触发追价的盘口（ask 11.0 显著高于挂单价 10.0）。"""
    return UnifiedPriceData(
        symbol=symbol,
        last_price=10.9,
        bid_price=10.9,
        ask_price=11.0,
        bid_volume=100.0,
        ask_volume=100.0,
        volume=1000.0,
        timestamp=int(time.time() * 1000),
        update_time="2026-07-14T23:00:00",
    )


def test_chase_skipped_when_cancel_unconfirmed() -> None:
    """核心回归：撤单未确认时**不得**下新单。

    GM 的 ``cancel_order`` 返回 True 只代表请求已提交。若据此立即下新单，
    撤单尚未生效的窗口内新旧单会同时在场，形成双份敞口。
    """
    executor = _UnconfirmedCancelExecutor()
    tracker = OrderTracker(
        executor=executor,
        chase_config=ChaseConfig(enabled=True, ticks=1, max_count=5, interval=0.0, cancel_confirm_timeout=0.05),
    )
    executor.tracker = tracker
    order = _build_pending_order("gm-unconfirmed-1")
    tracker.add_order(order, direction=OrderDirection.BUY)
    tracker.latest_prices[order.symbol] = _wide_spread_price(order.symbol)

    tracker._check_and_chase()

    assert executor._placed == 0, "撤单未确认却发出了新单——存在双份敞口风险"
    chase = [e for e in executor.emitted if e.get("reason_code") == "COMMON.ORDER_CHASE"]
    assert chase == [], "未换单却补发了追价事件"


def test_chase_emits_warning_when_cancel_unconfirmed() -> None:
    """放弃换单必须留下审计痕迹，否则「少挂一单」会变成静默行为。"""
    executor = _UnconfirmedCancelExecutor()
    tracker = OrderTracker(
        executor=executor,
        chase_config=ChaseConfig(enabled=True, ticks=1, max_count=5, interval=0.0, cancel_confirm_timeout=0.05),
    )
    executor.tracker = tracker
    order = _build_pending_order("gm-unconfirmed-2")
    tracker.add_order(order, direction=OrderDirection.BUY)
    tracker.latest_prices[order.symbol] = _wide_spread_price(order.symbol)

    tracker._check_and_chase()

    warnings = [e for e in executor.emitted if e.get("reason_code") == "COMMON.ORDER_CHASE_CANCEL_UNCONFIRMED"]
    assert len(warnings) == 1
    assert warnings[0]["order_id"] == "gm-unconfirmed-2"


def test_chase_leaves_old_order_pending_when_cancel_unconfirmed() -> None:
    """放弃换单后旧单应保持在场，且不残留「换单进行中」标记。"""
    executor = _UnconfirmedCancelExecutor()
    tracker = OrderTracker(
        executor=executor,
        chase_config=ChaseConfig(enabled=True, ticks=1, max_count=5, interval=0.0, cancel_confirm_timeout=0.05),
    )
    executor.tracker = tracker
    order = _build_pending_order("gm-unconfirmed-3")
    tracker.add_order(order, direction=OrderDirection.BUY)
    tracker.latest_prices[order.symbol] = _wide_spread_price(order.symbol)

    tracker._check_and_chase()

    assert "gm-unconfirmed-3" in tracker.pending_orders, "旧单不应被摘除"
    assert tracker._chasing_order_id is None, "残留的换单标记会永久压住 all_done 信号"


def test_chase_cancel_propagates_ctp_session_recovery_without_fallback() -> None:
    executor = _ChaseExecutor()
    tracker = OrderTracker(
        executor=executor,
        chase_config=ChaseConfig(enabled=True, ticks=1, max_count=5, interval=0.0),
    )
    executor.tracker = tracker
    order = _build_pending_order("ctp-chase-recovery")
    tracker.add_order(order, direction=OrderDirection.BUY)
    tracker.latest_prices[order.symbol] = _wide_spread_price(order.symbol)
    recovery = CtpSessionRecoveryRequired("ReqQryOrder同步拒绝: return_code=-2", return_code=-2)
    executor.cancel_order = lambda _order_id: (_ for _ in ()).throw(recovery)  # type: ignore[method-assign]

    with pytest.raises(CtpSessionRecoveryRequired, match="return_code=-2"):
        tracker._check_and_chase()

    assert executor._placed == 0
    assert tracker._chasing_order_id is None
    assert not tracker._chase_info[order.order_id].get("market_order_fallback_pending_cancel", False)


@pytest.mark.parametrize(
    "stage", ["chase_cancel", "fallback_cancel", "fallback_submit", "pending_query", "final_assets"]
)
@pytest.mark.parametrize("error_type", [ExecutionTerminated, MemoryError])
def test_tracker_propagates_interruptions_without_continuing_orders(monkeypatch, stage, error_type):
    executor = _ChaseExecutor()
    tracker = OrderTracker(
        executor=executor,
        chase_config=ChaseConfig(enabled=True, max_count=0 if stage == "fallback_cancel" else 5, interval=0),
    )
    executor.tracker = tracker
    order = _build_pending_order("interrupted")
    tracker.add_order(order, direction=OrderDirection.BUY)
    tracker.latest_prices[order.symbol] = _wide_spread_price(order.symbol)
    error = (
        ExecutionTerminated(reason="stop", mode="cancel_pending", cancel_failed_order_ids=[order.order_id])
        if error_type is ExecutionTerminated
        else MemoryError()
    )
    method, run = {
        "chase_cancel": ("cancel_order", tracker._check_and_chase),
        "fallback_cancel": ("cancel_order", tracker._fallback_to_market_order),
        "fallback_submit": (
            "place_order",
            lambda: tracker._submit_market_fallback_order(order, tracker._chase_info[order.order_id]),
        ),
        "pending_query": ("get_pending_orders", tracker._query_pending_orders),
        "final_assets": ("get_account_assets", tracker._refresh_timeout_account_assets),
    }[stage]
    monkeypatch.setattr(executor, method, MagicMock(side_effect=error))
    monkeypatch.setattr(tracker, "_is_market_fallback_price_safe", lambda *args: True)
    with pytest.raises(error_type) as raised:
        run()
    assert raised.value is error
    assert executor._placed == 0
    assert tracker._chasing_order_id is None
    assert not tracker._fallback_submitting


def test_chase_proceeds_when_cancel_confirmed() -> None:
    """同步确认撤单的渠道必须照常换单，不受本次改动影响。"""
    executor = _ChaseExecutor()
    tracker = OrderTracker(
        executor=executor,
        chase_config=ChaseConfig(enabled=True, ticks=1, max_count=5, interval=0.0),
    )
    executor.tracker = tracker
    order = _build_pending_order("external-order-1")
    tracker.add_order(order, direction=OrderDirection.BUY)
    tracker.latest_prices[order.symbol] = _wide_spread_price(order.symbol)

    tracker._check_and_chase()

    assert executor._placed == 1, "撤单已确认却没有换单"


def test_one_order_chase_success_does_not_clear_another_failure(monkeypatch):
    executor = _ChaseExecutor()
    tracker = OrderTracker(executor=executor, chase_config=ChaseConfig(enabled=True, max_count=5, interval=0))
    executor.tracker = tracker
    for order_id in ["A", "B"]:
        order = _build_pending_order(order_id)
        tracker.add_order(order, direction=OrderDirection.BUY)
        tracker.latest_prices[order.symbol] = _wide_spread_price(order.symbol)
    cancel = executor.cancel_order

    def fail_a(order_id):
        if order_id == "A":
            raise RuntimeError("A cancel failed")
        return cancel(order_id)

    monkeypatch.setattr(executor, "cancel_order", fail_a)
    tracker._check_and_chase()
    assert executor._placed == 1
    assert tracker.explicit_error == "追单失败，具体原因未确认"
    tracker.explicit_error = None
    assert tracker.explicit_error == "追单失败，具体原因未确认"
    monkeypatch.setattr(executor, "cancel_order", cancel)
    tracker._check_and_chase()
    assert tracker.explicit_error is None


def test_market_fallback_clears_only_its_own_error(monkeypatch):
    executor = _ChaseExecutor()
    tracker = OrderTracker(executor=executor)
    tracker._set_order_error("A", "A 未恢复")
    tracker._set_order_error("B", "B 未恢复")
    monkeypatch.setattr(tracker, "_is_market_fallback_price_safe", lambda *args: True)
    tracker._submit_market_fallback_batch(
        _build_pending_order("B"), {"symbol": executor.symbol, "direction": OrderDirection.BUY}
    )
    assert tracker.explicit_error == "A 未恢复"
    tracker._submit_market_fallback_batch(
        _build_pending_order("A"), {"symbol": executor.symbol, "direction": OrderDirection.BUY}
    )
    assert tracker.explicit_error is None


def test_market_fallback_cancel_propagates_ctp_session_recovery_without_marking_failure() -> None:
    executor = _ChaseExecutor()
    tracker = OrderTracker(
        executor=executor,
        chase_config=ChaseConfig(enabled=True, ticks=1, max_count=0, interval=0.0),
    )
    order = _build_pending_order("ctp-fallback-recovery")
    tracker.add_order(order, direction=OrderDirection.BUY)
    recovery = CtpSessionRecoveryRequired("ReqQryOrder同步拒绝: return_code=-2", return_code=-2)
    executor.cancel_order = lambda _order_id: (_ for _ in ()).throw(recovery)  # type: ignore[method-assign]

    with pytest.raises(CtpSessionRecoveryRequired, match="return_code=-2"):
        tracker._fallback_to_market_order()

    assert executor._placed == 0
    assert not tracker._chase_info[order.order_id].get("market_order_fallback_failed", False)
    assert not tracker._chase_info[order.order_id].get("market_order_fallback_pending_cancel", False)


def test_market_fallback_cancel_keeps_common_failure_behavior() -> None:
    executor = _ChaseExecutor()
    tracker = OrderTracker(
        executor=executor,
        chase_config=ChaseConfig(enabled=True, ticks=1, max_count=0, interval=0.0),
    )
    order = _build_pending_order("ordinary-fallback-failure")
    tracker.add_order(order, direction=OrderDirection.BUY)
    executor.cancel_order = lambda _order_id: (_ for _ in ()).throw(RuntimeError("cancel failed"))  # type: ignore[method-assign]

    tracker._fallback_to_market_order()

    assert tracker._chase_info[order.order_id]["market_order_fallback_failed"] is True
    assert executor._placed == 0


def test_chase_uses_post_cancel_fill_for_remaining_volume() -> None:
    """剩余量必须按撤单确认后的成交量算。

    撤单生效前旧单仍可能继续成交；若沿用撤单前快照，新单会多挂这部分。
    """
    executor = _ChaseExecutor()
    tracker = OrderTracker(
        executor=executor,
        chase_config=ChaseConfig(enabled=True, ticks=1, max_count=5, interval=0.0),
    )

    def cancel_with_partial_fill(order_id: str) -> bool:
        tracked = tracker.pending_orders.get(order_id)
        if tracked is not None:
            cancelled = tracked.model_copy(deep=True)
            # 撤单确认时，旧单又成交了 40 手
            cancelled.filled_volume = 40.0
            cancelled.status = OrderStatus.CANCELED
            tracker.on_order_update(cancelled)
        return True

    executor.cancel_order = cancel_with_partial_fill  # type: ignore[method-assign]
    executor.tracker = tracker
    order = _build_pending_order("partial-fill-1")
    tracker.add_order(order, direction=OrderDirection.BUY)
    tracker.latest_prices[order.symbol] = _wide_spread_price(order.symbol)

    tracker._check_and_chase()

    chase = [e for e in executor.emitted if e.get("reason_code") == "COMMON.ORDER_CHASE"]
    assert len(chase) == 1
    # 总量 100，撤单确认时已成交 40 → 新单应为 60，而非按旧快照的 100
    assert chase[0]["details"]["chase"]["remaining_volume"] == 60.0


@pytest.mark.parametrize("timestamp", [990_000, 1_001_000])
def test_rest_refresh_rejects_old_or_future_snapshot(timestamp):
    clock = _FixedClock(now=1000.0)
    quote = _price("SHSE.600000", 10, 11, timestamp_ms=timestamp)
    executor = _MarketDataExecutor(quote)
    tracker = OrderTracker(executor=executor, clock=clock, price_stale_after=5)
    tracker.latest_prices["SHSE.600000"] = quote
    assert tracker._usable_price("SHSE.600000") is None
    assert quote.timestamp == timestamp
    assert tracker._usable_price("SHSE.600000") is None
