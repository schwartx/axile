"""CTP/TQ 共用的平仓规则运行时；费用数据与静态合约目录分离。"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import TYPE_CHECKING, Callable, cast

from axile.domain.execution import ExecutionEventStatus, ExecutionEventType, ExecutionReasonFamily
from axile.executor.algorithms.utils.clock import clock_now_iso
from axile.executor.algorithms.utils.final_position import read_final_position
from axile.executor.algorithms.utils.outcome import TARGET_NOT_REACHED, summarize_outcome
from axile.executor.futures_close_policy import PRESET_VERSION, FuturesClosePolicy, futures_product, rule_priority
from axile.executor.futures_order_intent import (
    FuturesPosition,
    futures_position,
    plan_futures_close_orders,
    validate_futures_order,
)
from axile.executor.models.execution_result import AlgorithmResult, ExecutionStatus
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_order import OrderDirection

if TYPE_CHECKING:
    from axile.executor.algorithms.core.base import AlgorithmInput, ExecutorProtocol
    from axile.executor.execution_session import ExecutionSession


class FuturesCloseRuntimeMixin:
    """每次执行固定配置，账户费率缓存随交易日变化而失效。"""

    def futures_position_plan(
        self, symbol: str, target: float, assets: UnifiedAccountAssets, rule: dict[str, object]
    ) -> tuple[FuturesPosition, tuple[float, float]] | None:
        """锁今与清仓共用多空规划，关闭时不接管普通调仓。"""
        clear = rule.get("close_all_positions") is True
        if not (clear or rule.get("avoid_close_today") is True):
            return None
        if clear and target != 0:
            raise ValueError("清仓目标必须为零")
        position = futures_position(symbol, assets, allow_frozen=clear)
        return position, position.targets(target, clear=clear)

    def plan_futures_order(
        self,
        symbol: str,
        direction: OrderDirection,
        volume: float,
        assets: UnifiedAccountAssets,
        *,
        offset: str,
        protect_today: bool,
    ) -> list[tuple[float, dict[str, object]]]:
        """为共享规划的一条腿生成明确开平标志，拒绝把过期解锁计划转成新锁仓。"""
        validate_futures_order(symbol, direction, volume, assets, offset=offset, protect_today=protect_today)
        if offset == "open":
            return [(volume, {"offset_flag": "open"})]
        return getattr(self, "plan_close_orders")(symbol, direction, volume, assets)

    def run_futures_position_algorithm(
        self,
        session: ExecutionSession,
        algorithm_input: AlgorithmInput,
        algorithm: Callable[[ExecutorProtocol, AlgorithmInput], AlgorithmResult],
    ) -> AlgorithmResult | None:
        """共享层顺序执行实际开平腿，算法只控制每腿的报价、切片和成交等待。"""
        if not (
            algorithm_input.trade_rule.get("avoid_close_today") or algorithm_input.trade_rule.get("close_all_positions")
        ):
            return None
        executor = cast("ExecutorProtocol", session)
        symbol, target = session.symbol, algorithm_input.target_volume
        assets = session.get_account_assets()
        plan = self.futures_position_plan(symbol, target, assets, algorithm_input.trade_rule)
        if plan is None:
            return None
        position, targets = plan
        start = position.net
        results: list[AlgorithmResult] = []
        confirmed: FuturesPosition | None = None

        def confirmed_net(snapshot: UnifiedAccountAssets) -> float:
            nonlocal confirmed
            if session.get_pending_orders():
                raise ValueError("挂单尚未收敛，停止后续下单")
            confirmed = futures_position(symbol, snapshot)
            return confirmed.net

        for direction, volume, offset in position.legs(targets):
            confirmed = None
            session.handle_termination_checkpoint()
            leg_target = position.net + (volume if direction == OrderDirection.BUY else -volume)
            session.futures_order = (direction, offset, algorithm_input.trade_rule.get("avoid_close_today") is True)
            try:
                result = algorithm(executor, replace(algorithm_input, target_volume=leg_target))
            finally:
                session.futures_order = None
            results.append(result)
            if not result.success:
                break
            assets, _, query_error = read_final_position(executor, confirmed_net)
            if query_error:
                result.error = query_error
                break
            assert confirmed is not None
            expected_long = (
                position.long + (volume if offset == "open" else -volume)
                if ((direction == OrderDirection.BUY) == (offset == "open"))
                else position.long
            )
            expected_short = (
                position.short + (volume if offset == "open" else -volume)
                if ((direction == OrderDirection.SELL) == (offset == "open"))
                else position.short
            )
            if (confirmed.long, confirmed.short) != (expected_long, expected_short):
                result.error = "开平持仓尚未确认，停止后续下单"
                break
            position = confirmed

        if not results:
            # 直接调用算法的路径也保持原有 NOOP 证据与行情校验。
            results.append(algorithm(executor, algorithm_input))
        last = results[-1]
        if confirmed is None:
            assets, final_net, query_error = read_final_position(executor, lambda a: futures_position(symbol, a).net)
            confirmed = futures_position(symbol, assets) if not query_error else None
        else:
            final_net, query_error = confirmed.net, None
        orders = [order for result in results for order in result.orders]
        trades = [trade for result in results for trade in result.trades]
        reached = confirmed is not None and (confirmed.long, confirmed.short) == targets
        blocked = last.status == ExecutionStatus.BLOCKED and not query_error
        error = last.error or query_error or (None if reached else "多空持仓尚未达到执行目标")
        reason_code = last.reason_code
        if not reached and reason_code == TARGET_NOT_REACHED:
            reason_code = "FUTURES.POSITION.NOT_REACHED"
        summary = summarize_outcome(
            start,
            final_net,
            target,
            orders,
            trades,
            explicit_error=None if blocked else error,
            explicit_blocked_error=last.error if blocked else None,
            target_reached=reached,
            error_reason_code=reason_code,
            error_reason_family=last.reason_family,
            blocked_reason_code=last.reason_code,
            blocked_reason_family=last.reason_family,
        )
        return last.model_copy(
            update={
                **summary,
                "orders": orders,
                "trades": trades,
                "account_assets": assets,
                "target_volume": target,
                "memory": {key: value for result in results for key, value in result.memory.items()},
            }
        )

    def _prepare_futures_close_policy(self, policy: FuturesClosePolicy) -> None:
        policy.validate_channel(getattr(self, "channel_type"))
        self._futures_close_policy = policy.model_copy(deep=True)
        self._futures_fee_failures: dict[str, str] = {}
        if not hasattr(self, "_futures_fee_lock"):
            self._futures_fee_lock = threading.RLock()
            self._futures_fee_cache: dict = {}

    def _close_decision(self, symbol: str, exchange: str, *, trading_day: str | None = None) -> dict[str, object]:
        policy = getattr(self, "_futures_close_policy", FuturesClosePolicy())
        product = futures_product(symbol)
        priority, source = rule_priority(policy, product)
        decision: dict[str, object] = {
            "mode": policy.mode,
            "product": product,
            "priority": priority,
            "source": source,
            "preset_version": PRESET_VERSION,
        }
        if exchange not in {"SHFE", "INE"} or product is None:
            decision["source"] = "exchange"
            decision["reason"] = "交易所决定平仓顺序" if product else "非普通期货合约"
        elif policy.mode == "fee":
            self._apply_fee_decision(symbol, decision, trading_day=trading_day)
        return decision

    def _apply_fee_decision(self, symbol: str, decision: dict[str, object], *, trading_day: str | None = None) -> None:
        if not hasattr(self, "_futures_fee_lock"):
            self._prepare_futures_close_policy(getattr(self, "_futures_close_policy", FuturesClosePolicy()))
        with self._futures_fee_lock:
            try:
                if symbol in self._futures_fee_failures:
                    decision["fallback_reason"] = self._futures_fee_failures[symbol]
                    return
                day = trading_day or str(getattr(self, "_trading_day", ""))
                key = (day, symbol)
                self._futures_fee_cache = {k: v for k, v in self._futures_fee_cache.items() if k[0] == day}
                rates = self._futures_fee_cache.get(key)
                if rates is None:
                    rates = getattr(self, "_query_commission_rates")(symbol)
                    if day:
                        self._futures_fee_cache[key] = rates
                decision.update(
                    commission_rates=rates.model_dump(),
                    reference_price=None,
                    queried_at=rates.queried_at,
                    trading_day=day,
                )
                # 纯按手费用可直接展示金额；按金额费率不伪装为每手费用。
                if not rates.yesterday_money and not rates.today_money:
                    decision.update(yesterday_cost=rates.yesterday_volume, today_cost=rates.today_volume)
                priority = rates.compare_priority()
                if priority == "equal":
                    decision["fallback_reason"] = "平昨、平今预计费用相同"
                elif priority is None:
                    decision["fallback_reason"] = "按金额费率与按手费用高低相反，使用品种规则"
                else:
                    decision.update(priority=priority, source="fee")
            except (AttributeError, ValueError, RuntimeError, TimeoutError) as exc:
                # 终止及会话失效必须继续传播，不能当成费率缺失吞掉。
                from axile.executor.termination import ExecutionTerminated

                if isinstance(exc, ExecutionTerminated) or getattr(exc, "requires_session_recovery", False):
                    raise
                getattr(self, "handle_termination_checkpoint")()
                reason = f"手续费不可用（{type(exc).__name__}），使用品种规则"
                self._futures_fee_failures[symbol] = reason
                decision["fallback_reason"] = reason

    def _plan_futures_close(
        self,
        symbol: str,
        direction: OrderDirection,
        volume: float,
        account_assets: UnifiedAccountAssets,
        exchange: str,
    ) -> list[tuple[float, dict[str, object]]]:
        # 先确认库存；费率兜底绝不能绕过明细/冻结校验。
        plan_futures_close_orders(symbol, direction, volume, account_assets, exchange)
        decision = self._close_decision(
            symbol, exchange, trading_day=str(account_assets.extra.get("trading_day") or "")
        )
        plan = plan_futures_close_orders(
            symbol, direction, volume, account_assets, exchange, priority=str(decision["priority"])
        )
        decision.update(
            symbol=symbol,
            exchange=exchange,
            observed_at=clock_now_iso(),
            legs=[{"volume": quantity, **extra} for quantity, extra in plan],
        )
        runtime_getter = getattr(self, "get_active_execution_runtime", None)
        runtime = runtime_getter() if callable(runtime_getter) else None
        if runtime is not None:
            getattr(runtime, "emit_audit_event")(
                event_type=ExecutionEventType.FUTURES_CLOSE_PLANNED,
                status=ExecutionEventStatus.INFO,
                reason_family=ExecutionReasonFamily.EXECUTION_STRATEGY,
                reason_code="FUTURES.CLOSE_POLICY",
                symbol=symbol,
                details={"futures_close_decision": decision},
            )
        getattr(self, "logger").info("{}: 平仓规则 {}", symbol, decision)
        return plan
