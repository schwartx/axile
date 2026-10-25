"""
执行期查询运行时桥接器.

负责把执行器侧的下单、撤单、订单回调和成交回调，同步成对
``ExecutionQueryRuntime`` 的 patch 或 invalidate 操作。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from axile.executor.abstract_executor.base import AbstractExecutor
    from axile.executor.models.unified_order import TradeRecord, UnifiedOrder


class ExecutionQueryRuntimeBridge:
    """协调 execution 查询 runtime 的更新与失效."""

    def __init__(self, owner: AbstractExecutor) -> None:
        """
        初始化 execution 查询运行时桥接器.

        Parameters
        ----------
        owner : AbstractExecutor
            持有当前桥接器的执行器对象。
        """
        self._owner = owner

    def _get_runtime(self) -> object | None:
        """
        获取当前活跃的 execution 查询运行时.

        Returns
        -------
        object | None
            当前活跃的查询运行时对象；不存在时返回 ``None``。
        """
        runtime_getter = getattr(self._owner, "get_active_execution_query_runtime", None)
        if callable(runtime_getter):
            return runtime_getter()
        return None

    def _normalize_symbol(self, symbol: object) -> str | None:
        """
        规范化品种代码.

        Parameters
        ----------
        symbol : object
            原始品种值。

        Returns
        -------
        str | None
            去空白后的品种代码；为空时返回 ``None``。
        """
        if symbol is None:
            return None
        normalized = str(symbol).strip()
        return normalized or None

    def _normalize_id(self, identifier: object) -> str | None:
        """
        规范化标识符字符串.

        Parameters
        ----------
        identifier : object
            原始标识符值。

        Returns
        -------
        str | None
            去空白后的标识符；为空时返回 ``None``。
        """
        if identifier is None:
            return None
        normalized = str(identifier).strip()
        return normalized or None

    def resolve_trade_invalidation_scope(self, trade: TradeRecord) -> tuple[str | None, str | None]:
        """
        从统一成交记录中解析局部失效范围.

        Parameters
        ----------
        trade : TradeRecord
            需要解析范围的成交记录。

        Returns
        -------
        tuple[str | None, str | None]
            对应的 ``(symbol, order_id)`` 失效范围。
        """
        symbol = self._normalize_symbol(trade.symbol) or self._normalize_symbol(trade.extra.get("symbol"))
        order_id_candidates: list[object] = [
            trade.order_id,
            trade.extra.get("order_id"),
            trade.extra.get("cl_ord_id"),
            trade.extra.get("order_ref"),
            trade.extra.get("order_sysid"),
            trade.extra.get("exchange_order_id"),
        ]
        raw_candidates = trade.extra.get("order_id_candidates")
        if isinstance(raw_candidates, (list, tuple, set)):
            order_id_candidates.extend(raw_candidates)

        for candidate in order_id_candidates:
            normalized = self._normalize_id(candidate)
            if normalized is not None:
                return symbol, normalized
        return symbol, None

    def apply_pending_order_update(self, order: UnifiedOrder) -> bool:
        """
        尝试将订单更新 patch 到 execution 查询运行时.

        Parameters
        ----------
        order : UnifiedOrder
            最新订单对象。

        Returns
        -------
        bool
            patch 成功时返回 ``True``，否则返回 ``False``。
        """
        runtime = self._get_runtime()
        if runtime is None:
            return False

        apply_pending_order_update = getattr(runtime, "apply_pending_order_update", None)
        if callable(apply_pending_order_update):
            apply_pending_order_update(order)
            return True
        return False

    def remove_pending_order(self, symbol: str, order_id: str) -> bool:
        """
        尝试从 execution 查询运行时移除指定 pending order.

        Parameters
        ----------
        symbol : str
            品种代码。
        order_id : str
            订单标识。

        Returns
        -------
        bool
            删除成功时返回 ``True``，否则返回 ``False``。
        """
        runtime = self._get_runtime()
        if runtime is None:
            return False

        remove_pending_order = getattr(runtime, "remove_pending_order", None)
        if callable(remove_pending_order):
            remove_pending_order(symbol, order_id)
            return True
        return False

    def apply_trade_record(self, trade: TradeRecord) -> bool:
        """
        尝试将成交更新 patch 到 execution 查询运行时.

        Parameters
        ----------
        trade : TradeRecord
            最新成交记录。

        Returns
        -------
        bool
            patch 成功时返回 ``True``，否则返回 ``False``。
        """
        runtime = self._get_runtime()
        if runtime is None:
            return False

        apply_trade_record = getattr(runtime, "apply_trade_record", None)
        if callable(apply_trade_record):
            apply_trade_record(trade)
            return True
        return False

    def invalidate_pending_orders_snapshot(self, symbol: str | None = None) -> bool:
        """
        失效当前 execution 的挂单快照.

        Parameters
        ----------
        symbol : str | None, default=None
            需要局部失效的品种代码；为空时失效全部挂单快照。

        Returns
        -------
        bool
            运行时存在且已成功触发失效时返回 ``True``。
        """
        runtime = self._get_runtime()
        if runtime is None:
            return False

        invalidate_orders = getattr(runtime, "invalidate_orders", None)
        if callable(invalidate_orders):
            invalidate_orders(symbol)
            return True
        return False

    def invalidate_trade_snapshot(
        self,
        symbol: str | None = None,
        order_id: str | None = None,
    ) -> bool:
        """
        失效当前 execution 的成交快照.

        Parameters
        ----------
        symbol : str | None, default=None
            需要局部失效的品种代码。
        order_id : str | None, default=None
            需要局部失效的订单标识。

        Returns
        -------
        bool
            运行时存在且已成功触发失效时返回 ``True``。
        """
        runtime = self._get_runtime()
        if runtime is None:
            return False

        invalidate_trades = getattr(runtime, "invalidate_trades", None)
        if callable(invalidate_trades):
            invalidate_trades(symbol, order_id)
            return True
        return False

    def handle_place_order_result(self, order: UnifiedOrder, *, fallback_symbol: str | None = None) -> bool:
        """
        处理公开下单后的 execution 查询快照更新.

        Parameters
        ----------
        order : UnifiedOrder
            下单结果对应的订单对象。
        fallback_symbol : str | None, default=None
            当订单对象缺少品种信息时使用的回退品种代码。

        Returns
        -------
        bool
            若直接 patch 成功则返回 ``True``，否则返回 ``False``。
        """
        normalized_symbol = self._normalize_symbol(order.symbol) or self._normalize_symbol(fallback_symbol)
        if self.apply_pending_order_update(order):
            return True
        self.invalidate_pending_orders_snapshot(normalized_symbol)
        return False

    def handle_cancel_order_result(self, symbol: str, order_id: str) -> bool:
        """
        处理公开撤单后的 execution 查询快照更新.

        Parameters
        ----------
        symbol : str
            品种代码。
        order_id : str
            订单标识。

        Returns
        -------
        bool
            若直接 patch 成功则返回 ``True``，否则返回 ``False``。
        """
        if self.remove_pending_order(symbol, order_id):
            return True
        self.invalidate_pending_orders_snapshot(symbol)
        return False

    def handle_order_update(self, order: UnifiedOrder) -> bool:
        """
        在订单回调到达时优先 patch execution 共享查询快照.

        Parameters
        ----------
        order : UnifiedOrder
            订单更新对象。

        Returns
        -------
        bool
            若直接 patch 成功则返回 ``True``，否则返回 ``False``。
        """
        symbol = self._normalize_symbol(order.symbol)
        if self.apply_pending_order_update(order):
            return True
        self.invalidate_pending_orders_snapshot(symbol)
        return False

    def handle_trade_record(self, trade: TradeRecord) -> bool:
        """
        在成交回调到达时优先 patch execution 共享查询快照.

        Parameters
        ----------
        trade : TradeRecord
            成交记录对象。

        Returns
        -------
        bool
            若直接 patch 成功则返回 ``True``，否则返回 ``False``。
        """
        symbol, order_id = self.resolve_trade_invalidation_scope(trade)
        if self.apply_trade_record(trade):
            return True
        self.invalidate_trade_snapshot(symbol, order_id)
        return False

    def observe_order_update(self, order: UnifiedOrder) -> None:
        """
        适配统一回调协议的订单观察者入口.

        Parameters
        ----------
        order : UnifiedOrder
            订单更新对象。
        """
        if self._owner.account_orders.apply(order):
            self.handle_order_update(order)

    def observe_trade_record(self, trade: TradeRecord) -> None:
        """
        适配统一回调协议的成交观察者入口.

        Parameters
        ----------
        trade : TradeRecord
            成交记录对象。
        """
        self.handle_trade_record(trade)
