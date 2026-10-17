"""按 symbol 绑定的执行代理.

`ExecutionSession` 自身只保留 symbol 局部的临时 memory / audit_context；
账户控制、审计落库、终止状态和 execution 共享查询都委托给 owner runtime。
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, cast

from axile.common.logging import LogComponent, bind_log_context
from axile.common.order_param_model import OrderParamModel
from axile.executor.constants.order_status import OrderStatus
from axile.executor.models.order_channel_health import OrderChannelHealth
from axile.executor.models.unified_account_assets import PositionDirection, UnifiedAccountAssets
from axile.executor.models.unified_callback import OrderUpdateCallback, PriceDataCallback, TradeRecordCallback
from axile.executor.models.unified_order import OrderDirection, OrderType, TradeRecord, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData
from axile.executor.order_submission_context import order_submission_context
from axile.executor.termination import ExecutionTerminated, wait_or_terminate
from axile.executor.termination_cleanup import (
    TerminationCleanupResult,
    cleanup_query_allowed,
    termination_cleanup_scope,
)

if TYPE_CHECKING:
    from axile.executor.abstract_executor.base import AbstractExecutor
    from axile.executor.execution_runtime import ExecutionRuntime


type ObjectDict = dict[str, object]
type AuditContext = ObjectDict
type OrderAuditMetadata = ObjectDict


class ExecutionSession:
    """
    单品种执行会话.

    Notes
    -----
    这个对象不是新的执行器实现，而是算法层看到的 symbol 级代理：

    - symbol 局部状态放在 session 上，例如 `memory` 与 `audit_context`
    - execution 共享状态仍然放在 runtime 上
    - 真正的渠道能力最终仍委托给 owner executor
    """

    def __init__(
        self,
        owner: AbstractExecutor,
        symbol: str,
        runtime: ExecutionRuntime | None = None,
        audit_context: AuditContext | None = None,
    ) -> None:
        # session 只保存当前 symbol 的局部视图，避免算法直接接触整场 execution 的可变状态。
        self._owner = owner
        self._runtime = runtime
        self.symbol = symbol
        self.channel_type = owner.channel_type
        self.logger = bind_log_context(owner.logger, component=LogComponent.ALGORITHM, symbol=symbol)
        self.memory: ObjectDict = {}
        self.audit_context: AuditContext = {}
        self._audit_seq = 0
        self._audit_seq_lock = threading.Lock()

        if audit_context:
            self.set_audit_context(audit_context)

    def _get_runtime(self) -> ExecutionRuntime | None:
        """返回当前会话绑定的 execution runtime."""
        if self._runtime is not None:
            return self._runtime

        # 某些会话会延迟绑定 runtime，这里回退到 owner 当前 active runtime。
        get_active_runtime = getattr(self._owner, "get_active_execution_runtime", None)
        if callable(get_active_runtime):
            active_runtime = cast("ExecutionRuntime | None", get_active_runtime())
            if active_runtime is not None:
                self._runtime = active_runtime
                return self._runtime
        return None

    def get_current_volume(self, account_assets: UnifiedAccountAssets) -> float:
        """获取当前 symbol 的净持仓数量."""
        return self._owner.get_current_volume(self.symbol, account_assets)

    @property
    def order_param_model(self) -> OrderParamModel:
        """
        当前渠道的订单参数模型.

        Returns
        -------
        OrderParamModel
            owner 声明的模型；owner 未提供（如轻量测试替身）时回退
            ``UNKNOWN``,由算法层按"歧义即失败"处理。
        """
        raw = getattr(self._owner, "order_param_model", None)
        if isinstance(raw, OrderParamModel):
            return raw
        try:
            return OrderParamModel(str(raw)) if raw is not None else OrderParamModel.UNKNOWN
        except ValueError:
            return OrderParamModel.UNKNOWN

    def get_positions(self, account_assets: UnifiedAccountAssets) -> list[tuple[float, PositionDirection]]:
        """获取当前 symbol 的全部方向持仓."""
        return self._owner.get_positions_for_symbol(self.symbol, account_assets)

    def plan_close_orders(
        self, direction: OrderDirection, volume: float, account_assets: UnifiedAccountAssets
    ) -> list[tuple[float, dict[str, object]]]:
        """转发渠道平仓拆单计划；未提供该能力的渠道保持单笔意图。"""
        planner = getattr(self._owner, "plan_close_orders", None)
        if callable(planner):
            return planner(self.symbol, direction, volume, account_assets)
        return [(volume, {})]

    def place_order(
        self,
        direction: OrderDirection,
        order_type: OrderType,
        volume: float,
        price: float = 0,
        **kwargs: object,
    ) -> UnifiedOrder:
        """对当前 symbol 下单."""
        self.handle_termination_checkpoint()
        with order_submission_context(self.audit_context):
            return self._owner.place_order(self.symbol, direction, order_type, volume, price, **kwargs)

    def get_pending_orders(self) -> list[UnifiedOrder]:
        """获取当前 symbol 的未完成订单."""
        return self._owner._get_pending_orders_for_execution(self.symbol)

    def query_trades(self, order_id: str) -> list[TradeRecord]:
        """获取当前 symbol 下指定订单的成交明细."""
        return self._owner._query_trades_for_execution(self.symbol, order_id)

    def is_termination_requested(self) -> bool:
        """当前 execution 是否已收到 terminate 请求."""
        runtime = self._get_runtime()
        if runtime is not None:
            return runtime.is_termination_requested()
        return self._owner.is_termination_requested()

    def get_termination_mode(self) -> str | None:
        """读取当前 terminate 模式."""
        runtime = self._get_runtime()
        if runtime is not None:
            return runtime.get_termination_mode()
        return self._owner.get_termination_mode()

    def handle_termination_checkpoint(self) -> None:
        """在当前 symbol 作用域内处理 terminate 检查点."""
        runtime = self._get_runtime()
        if runtime is not None:
            if not runtime.is_termination_requested():
                return
        elif not self._owner.is_termination_requested():
            return

        try:
            if runtime is not None:
                runtime.handle_termination_checkpoint(self.symbol)
            else:
                self._owner.handle_termination_checkpoint(self.symbol)
        except ExecutionTerminated as exc:
            target = runtime if runtime is not None else self._owner
            # 先记录 ACK，再进入有界清理；重入的终止检查点直接抛出，不能递归清理。
            if exc.mode == "cancel_pending" and not cleanup_query_allowed(target):
                getter = getattr(self._owner, "get_account_control_guard", None)
                guard = getter() if callable(getter) else None
                with termination_cleanup_scope(target, guard, self.symbol):
                    result = self._cancel_pending_orders_before_termination()
                exc.cancel_failed_order_ids = result.failed_order_ids
                exc.cancel_attempted = result.attempted
                exc.cancel_unconfirmed = (
                    not result.query_complete or result.unconfirmed or bool(result.failed_order_ids)
                )
            raise

    def sleep_or_terminate(self, seconds: float) -> None:
        """协作式片间等待：睡满 ``seconds``，或收到 terminate 请求 / 触及总超时时立即中断.

        替代算法片间的纯阻塞 ``clock.sleep``。等待期间若 ``cancel_event`` 被置位，
        立即唤醒并交由本会话的 :meth:`handle_termination_checkpoint` 走 symbol 维度
        的 ``cancel_pending`` 收尾与抛出，从而让终止在片间即时生效。

        启用 deadline 时，等待时长会被钳到剩余额度以内，并在醒来后重新过一次检查点。
        这是算法真正调用的那份实现（切片类算法拿到的是 ``ExecutionSession``），因此
        总超时能不能在片间及时生效，取决于这里而非 runtime 上的同名方法。

        Parameters
        ----------
        seconds : float
            期望等待的秒数；``<= 0`` 时不等待。

        Raises
        ------
        ExecutionTerminated
            调用时已收到 terminate 请求或已超时，或在等待期间发生上述任一情况。
        """
        runtime = self._get_runtime()
        controller = runtime.termination_controller if runtime is not None else None
        wait_or_terminate(
            seconds,
            deadline_remaining=runtime.deadline_remaining_seconds() if runtime is not None else None,
            cancel_event=None if controller is None else controller.cancel_event,
            # 必须传本会话的检查点：它才会先撤当前 symbol 挂单、再把 cancel_failed_order_ids 挂到异常上。
            checkpoint=self.handle_termination_checkpoint,
        )

    def get_account_assets(self) -> UnifiedAccountAssets:
        """获取账户资产快照."""
        return self._owner.get_account_assets()

    def get_market_data(self) -> UnifiedPriceData | None:
        """获取当前 symbol 的行情快照."""
        return self._owner.get_market_data([self.symbol]).get(self.symbol)

    def get_tick_size(self) -> float | None:
        """获取当前 symbol 的最小价格变动单位."""
        return self._owner.get_tick_size(self.symbol)

    def get_order_volume_bounds(
        self, order_type: OrderType = OrderType.LIMIT, trade_rule: dict[str, object] | None = None
    ) -> tuple[int | None, int | None] | None:
        """获取当前品种数量边界；渠道不支持时返回 None，保留小数数量语义。"""
        getter = getattr(self._owner, "get_order_volume_bounds", None)
        return (
            cast("tuple[int | None, int | None] | None", getter(self.symbol, order_type, trade_rule))
            if callable(getter)
            else None
        )

    def get_max_order_volume(
        self, order_type: OrderType = OrderType.LIMIT, trade_rule: dict[str, object] | None = None
    ) -> int | None:
        """兼容仅查询上限的调用方。"""
        bounds = self.get_order_volume_bounds(order_type, trade_rule)
        return bounds[1] if bounds is not None else None

    def get_min_notional(self) -> float | None:
        """获取当前 symbol 下单的最小名义价值."""
        return self._owner.get_min_notional(self.symbol)

    def register_order_callback(self, callback: OrderUpdateCallback) -> None:
        """注册当前 symbol 的订单回调."""
        self._owner.register_order_callback_for_symbol(callback, self.symbol)

    def register_price_callback(self, callback: PriceDataCallback) -> None:
        """注册当前 symbol 的价格回调."""
        self._owner.register_price_callback_for_symbol(callback, self.symbol)

    def register_trade_callback(self, callback: TradeRecordCallback) -> None:
        """注册当前 symbol 的成交回调."""
        self._owner.register_trade_callback_for_symbol(callback, self.symbol)

    def unregister_order_callback(self, callback: OrderUpdateCallback) -> None:
        """注销当前 symbol 的订单回调."""
        self._owner.unregister_order_callback_for_symbol(callback, self.symbol)

    def unregister_price_callback(self, callback: PriceDataCallback) -> None:
        """注销当前 symbol 的价格回调."""
        self._owner.unregister_price_callback_for_symbol(callback, self.symbol)

    def unregister_trade_callback(self, callback: TradeRecordCallback) -> None:
        """注销当前 symbol 的成交回调."""
        self._owner.unregister_trade_callback_for_symbol(callback, self.symbol)

    def is_monitoring(self) -> bool:
        """检查共享 runtime 是否正在监控."""
        return self._owner.is_monitoring()

    def cancel_order(self, order_id: str) -> bool:
        """撤销当前 symbol 的指定订单."""
        return self._owner.cancel_order(self.symbol, order_id)

    def reconcile_terminal_order(self, symbol: str, order_id: str) -> UnifiedOrder | None:
        """
        REST 对账兜底：查询单个订单的真实终态，若已终态则补发终态事件.

        WebSocket 订单回报丢帧时，``OrderTracker`` 会对“本地仍视为 pending、但已从
        交易所挂单列表消失”的订单调用本方法做 REST 单查收敛。能力由底层 owner 提供
        owner 未提供时返回 ``None``，行为等价于跳过对账，
        保持 CTP / GM 等渠道原有语义不变。

        Parameters
        ----------
        symbol : str
            交易对代码。
        order_id : str
            订单 ID。

        Returns
        -------
        UnifiedOrder | None
            已终态订单；owner 不支持、未终态或查询失败时返回 ``None``。
        """
        owner_reconcile = getattr(self._owner, "reconcile_terminal_order", None)
        if not callable(owner_reconcile):
            return None
        return cast("UnifiedOrder | None", owner_reconcile(symbol, order_id))

    def get_order_channel_health(self) -> OrderChannelHealth:
        """
        查询订单回报通道（WebSocket）健康度，供 tracker 调节 REST 对账频率.

        能力由底层 owner 提供；owner 未提供时返回
        ``OrderChannelHealth.UNKNOWN``，令 tracker 保持无 WS 渠道的默认对账节奏。

        Returns
        -------
        OrderChannelHealth
            当前订单回报通道健康度。
        """
        owner_health = getattr(self._owner, "get_order_channel_health", None)
        if not callable(owner_health):
            return OrderChannelHealth.UNKNOWN
        return cast("OrderChannelHealth", owner_health())

    def expect_order_ack(self, order_id: str) -> None:
        """
        为刚下达的订单登记 WebSocket 回报探针（能力由 owner 提供）.

        owner 未实现时静默跳过，保持无 WebSocket 概念渠道的行为不变。

        Parameters
        ----------
        order_id : str
            刚下单返回的订单 ID。
        """
        owner_expect = getattr(self._owner, "expect_order_ack", None)
        if callable(owner_expect):
            owner_expect(order_id)

    def _cancel_pending_orders_before_termination(self) -> TerminationCleanupResult:
        """在 terminate(cancel_pending) 模式下查询并逐笔撤销当前 symbol 挂单."""
        try:
            pending_orders = self.get_pending_orders()
        except Exception as exc:
            if bool(getattr(exc, "requires_session_recovery", False)):
                raise
            self.logger.warning(f"terminate 查询挂单失败: {exc}")
            return TerminationCleanupResult(unconfirmed=True)

        result = TerminationCleanupResult(query_complete=True)
        for order in pending_orders:
            try:
                result.attempted = True
                canceled = self.cancel_order(order.order_id)
            except Exception as exc:
                if bool(getattr(exc, "requires_session_recovery", False)):
                    raise
                self.logger.warning(f"terminate 撤单失败: order_id={order.order_id}, error={exc}")
                result.failed_order_ids.append(order.order_id)
                continue

            if not canceled:
                result.failed_order_ids.append(order.order_id)
            else:
                try:
                    terminal = self.reconcile_terminal_order(self.symbol, order.order_id)
                    if terminal is None or not terminal.is_completed():
                        result.unconfirmed = True
                    elif terminal.status != OrderStatus.CANCELED:
                        result.failed_order_ids.append(order.order_id)
                except Exception as exc:
                    if bool(getattr(exc, "requires_session_recovery", False)):
                        raise
                    result.unconfirmed = True

        return result

    def set_audit_context(self, context: AuditContext) -> None:
        """设置当前 symbol 会话的审计上下文."""
        self.audit_context = dict(context)
        with self._audit_seq_lock:
            self._audit_seq = 0

    def next_audit_seq(self) -> int:
        """获取当前 symbol 会话内递增的审计序号."""
        with self._audit_seq_lock:
            self._audit_seq += 1
            return self._audit_seq

    def register_order_audit_metadata(self, order_id: str, metadata: OrderAuditMetadata) -> None:
        """将订单元数据注册到共享 runtime."""
        stored_metadata = dict(metadata)
        if self.audit_context:
            stored_metadata.setdefault("audit_context", dict(self.audit_context))
        self._owner.register_order_audit_metadata(order_id, stored_metadata)

    def get_order_audit_metadata(self, order_id: str) -> OrderAuditMetadata:
        """读取共享 runtime 中的订单元数据."""
        return self._owner.get_order_audit_metadata(order_id)

    def emit_audit_event(
        self,
        *,
        event_type: object,
        status: object,
        reason_family: object,
        reason_code: str,
        symbol: str | None = None,
        intent_id: str | None = None,
        order_id: str | None = None,
        client_order_id: str | None = None,
        ts_exchange: str | None = None,
        seq: int | None = None,
        details: dict[str, object] | None = None,
        audit_context: AuditContext | None = None,
    ) -> bool:
        """写入当前 symbol 会话的审计事件."""
        runtime = self._get_runtime()
        if runtime is not None:
            return runtime.emit_audit_event(
                event_type=event_type,
                status=status,
                reason_family=reason_family,
                reason_code=reason_code,
                symbol=symbol or self.symbol,
                intent_id=intent_id,
                order_id=order_id,
                client_order_id=client_order_id,
                ts_exchange=ts_exchange,
                seq=seq,
                details=details,
                audit_context=audit_context or self.audit_context,
            )
        return self._owner.emit_audit_event(
            event_type=event_type,
            status=status,
            reason_family=reason_family,
            reason_code=reason_code,
            symbol=symbol or self.symbol,
            intent_id=intent_id,
            order_id=order_id,
            client_order_id=client_order_id,
            ts_exchange=ts_exchange,
            seq=seq if seq is not None else self.next_audit_seq(),
            details=details,
            audit_context=audit_context or self.audit_context,
        )

    def emit_audit_artifact(
        self,
        *,
        artifact_type: object,
        content: dict[str, object],
    ) -> bool:
        """写入当前 symbol 会话的审计附件."""
        runtime = self._get_runtime()
        if runtime is not None:
            return runtime.emit_audit_artifact(
                artifact_type=artifact_type,
                content=content,
                audit_context=self.audit_context,
            )
        return self._owner.emit_audit_artifact(
            artifact_type=artifact_type,
            content=content,
            audit_context=self.audit_context,
        )
