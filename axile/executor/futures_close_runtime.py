"""CTP/TQ 共用的平仓规则运行时；费用数据与静态合约目录分离。"""

from __future__ import annotations

import math
import threading

from axile.domain.execution import ExecutionEventStatus, ExecutionEventType, ExecutionReasonFamily
from axile.executor.algorithms.utils.clock import clock_now_iso
from axile.executor.futures_close_policy import PRESET_VERSION, FuturesClosePolicy, futures_product, rule_priority
from axile.executor.futures_order_intent import plan_futures_close_orders
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_order import OrderDirection


class FuturesCloseRuntimeMixin:
    """每次执行固定配置，账户费率缓存随交易日变化而失效。"""

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
                quotes = getattr(self, "get_market_data")([symbol])
                quote = quotes.get(symbol)
                price = float(quote.last_price) if quote is not None else 0.0
                price = price if math.isfinite(price) and price > 0 else 0.0
                instrument = getattr(self, "_instruments", {}).get(symbol)
                multiplier = float(getattr(instrument, "VolumeMultiple", 0) or 0)
                yesterday, today = rates.costs(price, multiplier)
                decision.update(
                    yesterday_cost=yesterday,
                    today_cost=today,
                    reference_price=price if price > 0 else None,
                    queried_at=rates.queried_at,
                    trading_day=day,
                )
                if math.isclose(yesterday, today, rel_tol=1e-9, abs_tol=1e-9):
                    decision["fallback_reason"] = "平昨、平今预计费用相同"
                else:
                    decision.update(priority="yesterday_first" if yesterday < today else "today_first", source="fee")
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
