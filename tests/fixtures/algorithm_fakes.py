"""跨算法测试复用的轻量执行器与时钟替身。"""

from __future__ import annotations

import threading
from typing import cast
from unittest.mock import MagicMock

from axile.executor.algorithms.core.base import ExecutorProtocol
from axile.executor.algorithms.utils.order_tracker import ChaseConfig, OrderTracker
from axile.executor.models.unified_order import OrderDirection, OrderType, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData


class FallbackExecutor:
    """记录撤单、市价兜底与审计调用的最小执行器。"""

    def __init__(self) -> None:
        self.symbol = "rb2610"
        self.logger = MagicMock()
        self.audit_context = {"execution_id": "exec-1", "account_id": 1, "algorithm": "SINGLE-MAKER"}
        self.cancel_outcome: bool | Exception = True
        self.place_outcome: UnifiedOrder | Exception | None = None
        self.place_calls: list[dict[str, object]] = []
        self.audit_events: list[dict[str, object]] = []
        self._audit_seq = 0

    def next_audit_seq(self) -> int:
        self._audit_seq += 1
        return self._audit_seq

    def emit_audit_event(self, **kwargs: object) -> bool:
        self.audit_events.append(kwargs)
        return True

    def cancel_order(self, order_id: str) -> bool:
        _ = order_id
        if isinstance(self.cancel_outcome, Exception):
            raise self.cancel_outcome
        return self.cancel_outcome

    def place_order(
        self,
        direction: OrderDirection,
        order_type: OrderType,
        volume: float,
        price: float = 0.0,
        **kwargs: object,
    ) -> UnifiedOrder:
        self.place_calls.append(
            {
                "symbol": self.symbol,
                "direction": direction,
                "order_type": order_type,
                "volume": volume,
                "price": price,
                "kwargs": kwargs,
            }
        )
        if isinstance(self.place_outcome, Exception):
            raise self.place_outcome
        if self.place_outcome is not None:
            return self.place_outcome
        return UnifiedOrder(
            order_id=f"market-{len(self.place_calls)}",
            symbol=self.symbol,
            direction=direction,
            order_type=order_type,
            volume=volume,
            price=price,
            status="待成交",
            filled_volume=0.0,
            avg_price=0.0,
        )

    def get_tick_size(self) -> float | None:
        return 0.01

    def is_termination_requested(self) -> bool:
        return False

    def get_termination_mode(self) -> str | None:
        return None

    def handle_termination_checkpoint(self) -> None:
        return None


class ClockStub:
    """以推进逻辑时间代替真实等待。"""

    def __init__(self) -> None:
        self.current = 0.0

    def time(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.current += seconds

    def event_wait(self, event: threading.Event, timeout: float) -> bool:
        if event.is_set():
            return True
        self.current += timeout
        return event.is_set()


def build_fallback_tracker() -> tuple[FallbackExecutor, OrderTracker, UnifiedOrder]:
    """创建已经达到追价上限、可进入市价兜底的订单跟踪器。"""
    executor = FallbackExecutor()
    tracker = OrderTracker(
        executor=cast("ExecutorProtocol", executor),
        chase_config=ChaseConfig(enabled=True, ticks=1, max_count=1, interval=1.0),
    )
    order = UnifiedOrder(
        order_id="limit-1",
        symbol="rb2610",
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        volume=1.0,
        price=100.0,
        status="待成交",
        filled_volume=0.0,
        avg_price=0.0,
    )
    tracker.add_order(order, direction=OrderDirection.BUY, target_volume=1.0, current_volume=0.0)
    tracker._chase_info[order.order_id]["chase_count"] = 1
    tracker.latest_prices["rb2610"] = UnifiedPriceData(
        symbol="rb2610",
        last_price=100.0,
        asks=[(100.1, 1.0)],
        bids=[(99.9, 1.0)],
        volume=10.0,
        turnover=1000.0,
        timestamp=1,
        update_time="2026-03-18T00:00:00",
    )
    return executor, tracker, order
