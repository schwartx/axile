"""跨 execution 保存账户委托；读取快照只访问内存。"""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from threading import RLock
from typing import Any

from axile.executor.account_activity import AccountActivityBook
from axile.executor.algorithms.utils.clock import clock_now_iso
from axile.executor.models.unified_order import UnifiedOrder


class AccountOrderBook:
    """合并完整查询与并发回报，保留有限终态以拒绝迟到的活跃状态。"""

    def __init__(self) -> None:
        self.activity = AccountActivityBook()
        self._lock = RLock()
        self._orders: OrderedDict[tuple[str, str], UnifiedOrder] = OrderedDict()
        self._changed: dict[tuple[str, str], int] = {}
        self._metadata: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        self.version = 0
        self._invalidated_at = 0
        self._baseline_version = 0
        self.complete = False
        self.error: str | None = "账户委托尚未同步"
        self.sampled_at = clock_now_iso()

    def invalidate(self, message: str) -> None:
        """断线或恢复期间保留最后已知委托，但撤销完整性。"""
        with self._lock:
            if not self.complete and self.error == message:
                return
            self.complete, self.error = False, message
            self.version += 1
            self._invalidated_at = self.version

    def begin_sync(self) -> int:
        """记录查询开始水位；后到的回报在查询完成时仍优先保留。"""
        with self._lock:
            return self.version

    def apply(self, order: UnifiedOrder) -> None:
        """应用原生回报；同一订单的累计成交和终态不能倒退。"""
        with self._lock:
            key = order.symbol, order.order_id
            previous = self._orders.get(key)
            value = order.model_copy(deep=True)
            if previous is not None:
                if previous.is_completed() and not value.is_completed():
                    return
                if value.filled_volume < previous.filled_volume:
                    return
                if value == previous:
                    return
            self.version += 1
            self.sampled_at = clock_now_iso()
            self._orders[key] = value
            self.activity.observe_order(value)
            self._orders.move_to_end(key)
            self._changed[key] = self.version
            self._trim()

    def replace(self, orders: list[UnifiedOrder], watermark: int, *, drop_missing: bool = False) -> None:
        """安装完整基线；重放查询期间的回报，缺失旧挂单保持未知。"""
        with self._lock:
            if watermark < self._baseline_version:
                return
            incoming = {(order.symbol, order.order_id): order for order in orders}
            for key, previous in list(self._orders.items()):
                if (
                    self._changed.get(key, 0) > watermark
                    or previous.is_completed()
                    or key in incoming
                    and previous.filled_volume > incoming[key].filled_volume
                ):
                    incoming[key] = previous
                elif key not in incoming and not drop_missing:
                    incoming[key] = previous.model_copy(update={"status": "状态未知"}, deep=True)
            self._orders = OrderedDict((key, value.model_copy(deep=True)) for key, value in incoming.items())
            for order in self._orders.values():
                self.activity.observe_order(order)
            if self._invalidated_at <= watermark:
                self.complete, self.error = True, None
            self.version += 1
            self._baseline_version = self.version
            self._changed = {key: self._changed.get(key, self.version) for key in self._orders}
            self.sampled_at = clock_now_iso()
            self._trim()

    def snapshot(self) -> dict[str, Any]:
        """复制订单与可信状态，不调用渠道、guard 或执行期 runtime。"""
        with self._lock:
            active = [order.model_copy(deep=True) for order in self._orders.values() if not order.is_completed()]
            terminal = [order for order in self._orders.values() if order.is_completed()]
            complete = self.complete and all(order.is_active() for order in active)
            return {
                "data": active,
                "resolved": [order.model_copy(deep=True) for order in terminal[-512:]],
                "complete": complete,
                "error": self.error,
                "version": self.version,
                "sampled_at": self.sampled_at,
            }

    def remember_metadata(self, order_id: str, metadata: dict[str, Any]) -> None:
        """保留订单的执行归属，执行结束后的回报仍可关联原 execution。"""
        with self._lock:
            key = str(metadata.get("symbol") or ""), order_id
            if self._metadata.get(key) != metadata:
                self.version += 1
            self._metadata[key] = deepcopy(metadata)
            self._metadata.move_to_end(key)
            while len(self._metadata) > 4096:
                self._metadata.popitem(last=False)

    def metadata(self, order_id: str, symbol: str | None = None) -> dict[str, Any]:
        """读取订单归属，不创建新的 execution runtime。"""
        with self._lock:
            if symbol is not None:
                value = self._metadata.get((symbol, order_id))
                if value is not None:
                    return deepcopy(value)
            values = [value for key, value in self._metadata.items() if key[1] == order_id]
            if len(values) != 1 or symbol is not None and values[0].get("symbol") not in {None, "", symbol}:
                return {}
            return deepcopy(values[0])

    def _trim(self) -> None:
        ended = [key for key, order in self._orders.items() if order.is_completed()]
        for key in ended[:-4096]:
            self._orders.pop(key, None)
            self._changed.pop(key, None)
