"""账户连接内的委托、成交及操作失败记录，不依赖 execution 生命周期。"""

from __future__ import annotations

from collections import OrderedDict
from threading import RLock
from uuid import uuid4

from axile.executor.algorithms.utils.clock import clock_now_iso
from axile.executor.models.unified_order import TradeRecord, UnifiedOrder
from axile.executor.order_observation import merge_order, with_trade_average


class AccountActivityBook:
    """保留有界观测记录；相同成交去重，迟到委托不能复活或降低累计成交量。"""

    def __init__(self, capacity: int = 2000) -> None:
        self._lock = RLock()
        self._capacity = capacity
        self._orders: OrderedDict[tuple[str, str], UnifiedOrder] = OrderedDict()
        self._trades: OrderedDict[tuple[str, ...], TradeRecord] = OrderedDict()
        self._failures: OrderedDict[str, dict] = OrderedDict()
        self.version = 0
        self.started_at = clock_now_iso()
        self.dropped = False
        self.queries: dict[str, dict] = {}

    def observe_order(self, order: UnifiedOrder) -> None:
        """复制最新委托状态，不持有渠道提供的可变对象。"""
        with self._lock:
            key = order.symbol, order.order_id
            previous = self._orders.get(key)
            if previous is not None:
                if previous.is_completed() and not order.is_completed():
                    return
                if previous.filled_volume > order.filled_volume:
                    return
            self._orders[key] = merge_order(previous, order)
            self._orders.move_to_end(key)
            self._trim(self._orders)

    def observe_trade(self, trade: TradeRecord) -> None:
        """以品种及成交 ID 去重查询与回报，保留逐笔成交。"""
        if not trade.trade_id or trade.trade_volume <= 0:
            return
        with self._lock:
            key = (
                trade.symbol,
                str(trade.extra.get("trading_day", "")),
                str(trade.extra.get("exchange_id", "")),
                trade.trade_id,
            )
            if self._trades.get(key) == trade:
                return
            self.version += 1
            self._trades[key] = trade.model_copy(deep=True)
            self._trim(self._trades)

    def enrich_orders(self, orders: list[UnifiedOrder]) -> tuple[list[UnifiedOrder], int]:
        """同一成交快照投影所有委托，并返回对应版本。"""
        with self._lock:
            trades = list(self._trades.values())
            return [with_trade_average(order, trades) for order in orders], self.version

    def record_failure(
        self,
        operation: str,
        symbol: str,
        order_id: str | None,
        message: str,
        *,
        event_id: str | None = None,
        outcome: str = "unknown",
    ) -> None:
        """记录操作失败；消息由适配层提供，不直接公开异常或原始载荷。"""
        with self._lock:
            identity = event_id or uuid4().hex
            if identity in self._failures:
                return
            self._failures[identity] = {
                "event_id": identity,
                "operation": operation,
                "outcome": outcome,
                "symbol": symbol,
                "order_id": order_id,
                "message": message,
                "time": clock_now_iso(),
            }
            self._trim(self._failures)

    def symbols(self) -> list[str]:
        """返回连接内已观测到的品种，不能视为账户全部历史品种。"""
        with self._lock:
            return sorted({key[0] for key in self._orders} | {key[0] for key in self._trades})

    def mark_query(self, symbol: str | None, *, scope: str, errors: list[str]) -> None:
        """保存每个查询范围的独立核对结果，局部核对不能提升全账户完整性。"""
        with self._lock:
            self.queries[symbol or ""] = {"scope": scope, "errors": errors, "queried_at": clock_now_iso()}

    def snapshot(self, symbol: str | None = None) -> dict:
        """复制当前查询范围的观测，不触发网络请求。"""
        with self._lock:
            return {
                "orders": [
                    with_trade_average(o, list(self._trades.values()))
                    for o in self._orders.values()
                    if symbol is None or o.symbol == symbol
                ],
                "trades": [
                    t.model_copy(deep=True) for t in self._trades.values() if symbol is None or t.symbol == symbol
                ],
                "failures": [dict(f) for f in self._failures.values() if symbol is None or f["symbol"] == symbol],
                "started_at": self.started_at,
                "capacity": self._capacity,
                "dropped": self.dropped,
                **self.queries.get(
                    symbol or "", {"scope": "本次连接观测记录，尚未核对渠道记录", "errors": [], "queried_at": None}
                ),
            }

    def _trim(self, rows: OrderedDict) -> None:
        while len(rows) > self._capacity:
            self.dropped = True
            rows.popitem(last=False)
