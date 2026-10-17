"""精确单合约场景算法：通过 Axile session 下单，不直接调用 CTP SDK。"""

import math
import threading
from typing import Literal

from axile import AlgorithmInput, AlgorithmResult, ExecutorProtocol, TradeChannel, register_algorithm
from axile.executor.algorithms.utils.clock import clock_now_iso, get_default_clock
from axile.executor.constants.order_status import OrderStatus
from axile.executor.models.execution_result import ExecutionStatus
from axile.executor.models.unified_order import OrderDirection, OrderType
from axile.executor.termination import ExecutionTerminated
from pydantic import BaseModel, Field


class ScenarioParams(BaseModel):
    """只有脚本明确指定的合约允许产生委托。"""

    symbol: str
    action: Literal["open", "close", "cancel", "invalid", "reject", "hold"]
    batch: str
    step: str
    lots: int = Field(default=1, ge=1)
    max_order_lots: int = Field(default=1, ge=1)
    max_exposure_lots: int = Field(default=1, ge=1)
    side: Literal["long", "short"] = "long"
    close_offset: Literal["auto", "today", "yesterday"] = "auto"
    timeout_seconds: float = Field(default=45, gt=0, le=3600)
    passive_ticks: int = Field(default=10, ge=1)
    invalid_case: Literal["price", "volume", "unknown"] = "price"
    reject_case: Literal["insufficient_funds", "no_position", "market_state"] = "insufficient_funds"
    reject_volume: int = Field(default=1, ge=1)
    broker_condition: str = ""
    hold_seconds: float = Field(default=0, ge=0, le=3600)
    before_send_hold_seconds: float = Field(default=0, ge=0, le=3600)
    after_send_hold_seconds: float = Field(default=0, ge=0, le=3600)


def exception_details(error):
    """未知错误码保留原值，不推测柜台阶段。"""
    return {
        "type": type(error).__name__,
        "raw": str(error),
        **{
            name: getattr(error, name, None)
            for name in ("return_code", "error_id", "broker_message", "execution_error", "reason_code")
        },
    }


class Capture:
    """回调可早于 place_order 返回；先缓冲，按本次订单身份过滤。"""

    def __init__(self, symbol):
        self.symbol = symbol
        self.lock = threading.RLock()
        self.orders = {}
        self.trades = {}
        self.owned = set()

    def on_order(self, order):
        if order.symbol == self.symbol:
            with self.lock:
                previous = self.orders.get(order.order_id)
                # 不让迟到的活动快照覆盖终态，也不回退已知成交量。
                if previous and (
                    previous.is_completed() and not order.is_completed() or previous.filled_volume > order.filled_volume
                ):
                    return
                self.orders[order.order_id] = order.model_copy(deep=True)

    def on_trade(self, trade):
        if trade.symbol == self.symbol:
            with self.lock:
                self.trades[(trade.order_id, trade.trade_id)] = trade.model_copy(deep=True)

    def add(self, order):
        with self.lock:
            self.owned.add(order.order_id)
            if order.order_id not in self.orders:
                self.on_order(order)

    def snapshots(self):
        with self.lock:
            return [o.model_copy(deep=True) for key, o in self.orders.items() if key in self.owned]

    def fills(self):
        with self.lock:
            return [t.model_copy(deep=True) for (order_id, _), t in self.trades.items() if order_id in self.owned]


def checkpoint(executor, name, params, order_id=None):
    """通过现有审计流暴露可等待的时序锚点。"""
    executor.emit_audit_event(
        event_type="symbol_decision_made",
        status="INFO",
        reason_family="EXECUTION_STRATEGY",
        reason_code=f"FT.CHECKPOINT.{name}",
        symbol=params.symbol,
        details={"batch": params.batch, "step": params.step, "checkpoint": name, "order_id": order_id},
    )


def guard_assets(executor, params):
    """拒绝降级快照、双向持仓与超出测试范围的存量。"""
    assets = executor.get_account_assets()
    if assets.source in {"error", "assumed", "unavailable"}:
        raise ValueError("持仓快照不可用于测试判定")
    positions = [p for p in assets.positions if p.symbol == params.symbol and p.volume]
    if len({p.direction for p in positions}) > 1:
        raise ValueError("测试合约有双向持仓，需人工核对")
    if sum(abs(p.volume) for p in positions) > params.max_exposure_lots:
        raise ValueError("已有持仓超过测试总敞口约束")
    return assets, float(executor.get_current_volume(assets))


def choose_price(executor, params, direction, passive=False):
    """被动价按 tick 对齐并处于涨跌停以内；行情不足不下单。"""
    quote = executor.get_market_data()
    tick = executor.get_tick_size()
    if quote is None or not quote.book_valid or not tick or tick <= 0:
        raise ValueError("缺少有效盘口或 tick")
    price = quote.ask_price if direction == OrderDirection.BUY else quote.bid_price
    if passive:
        lower = float(quote.extra.get("lower_limit_price", 0))
        upper = float(quote.extra.get("upper_limit_price", 0))
        if not 0 < lower < upper:
            raise ValueError("缺少涨跌停边界，不能构造合法挂单")
        if direction == OrderDirection.BUY:
            price = max(lower, quote.bid_price - params.passive_ticks * tick)
            price = math.ceil(price / tick - 1e-8) * tick
            if price >= quote.ask_price:
                raise ValueError("不能构造不立即成交的买单")
        else:
            price = min(upper, quote.ask_price + params.passive_ticks * tick)
            price = math.floor(price / tick + 1e-8) * tick
            if price <= quote.bid_price:
                raise ValueError("不能构造不立即成交的卖单")
    return float(price), quote


def order_plan(executor, params, assets, current, target):
    """开仓保持目标幂等；平仓只减少约定方向和数量。"""
    if params.lots > min(params.max_order_lots, params.max_exposure_lots):
        raise ValueError("算法手数超限")
    sign = 1 if params.side == "long" else -1
    direction = OrderDirection.BUY if sign > 0 else OrderDirection.SELL
    if params.action in {"open", "hold"}:
        if target != sign * params.lots or current * sign < 0:
            raise ValueError("杠杆、精度或已有反向持仓导致目标不符合约定")
        volume = abs(target) - abs(current)
        if volume < 0:
            raise ValueError("现有仓位超过开仓目标")
        return direction, [(volume, {"offset_flag": "open"})] if volume else []
    if params.action == "close":
        if target != 0 or current != sign * params.lots:
            raise ValueError("平仓前置必须恰为约定方向和数量，目标必须归零")
        direction = OrderDirection.SELL if sign > 0 else OrderDirection.BUY
        plan = executor.plan_close_orders(direction, params.lots, assets)
        if params.close_offset != "auto":
            prefix = "long" if sign > 0 else "short"
            suffix = "td" if params.close_offset == "today" else "yd"
            available = sum(
                float(p.extra.get(f"{prefix}_{suffix}", 0)) for p in assets.positions if p.symbol == params.symbol
            )
            if available < params.lots:
                raise ValueError("指定今昨仓数量不足，不能以其他平仓标志替代")
            offset = "close_today" if params.close_offset == "today" else "close_yesterday"
            return direction, [(params.lots, {"offset_flag": offset})]
        return direction, plan
    volume = params.reject_volume if params.action == "reject" else params.lots
    if params.action == "cancel" and abs(current) + volume > params.max_exposure_lots:
        raise ValueError("即使挂单意外成交也不能突破总敞口，请先平测试仓")
    if params.action == "reject" and volume > params.max_order_lots:
        raise ValueError("柜台拒绝场景手数超过显式单笔上限")
    if params.action == "reject" and abs(current) + volume > params.max_exposure_lots:
        raise ValueError("柜台拒绝场景即使成交也不能突破显式总敞口约束")
    offset = "close" if params.action == "reject" and params.reject_case == "no_position" else "open"
    if offset == "close":
        direction = OrderDirection.SELL if sign > 0 else OrderDirection.BUY
        if current != 0:
            raise ValueError("无仓可平场景要求测试合约空仓")
    return direction, [(volume, {"offset_flag": offset})]


def wait_orders(executor, capture, deadline, *, pending=False):
    """等待实际回报，pending 模式也要求至少一个原生订单回报。"""
    clock = get_default_clock()
    while clock.time() < deadline:
        executor.handle_termination_checkpoint()
        orders = capture.snapshots()
        if orders and all(o.is_completed() for o in orders):
            return True
        if pending and orders and all(o.extra.get("order_sys_id") and o.status != OrderStatus.UNKNOWN for o in orders):
            return True
        executor.sleep_or_terminate(min(0.2, max(0, deadline - clock.time())))
    return False


def cancel_owned(executor, capture, memory):
    """只撤销本次算法创建的订单，逐笔记录失败，不做批量清理。"""
    for order in capture.snapshots():
        if order.is_completed():
            continue
        if any(attempt["order_id"] == order.order_id for attempt in memory["cancellations"]):
            continue
        attempt = {"order_id": order.order_id, "requested_at": clock_now_iso()}
        memory["cancellations"].append(attempt)
        try:
            attempt["accepted"] = executor.cancel_order(order.order_id)
        except ExecutionTerminated:
            raise
        except Exception as exc:
            attempt["error"] = exception_details(exc)


def submit_plan(executor, input_data, params, capture, memory, assets, current):
    direction, plan = order_plan(executor, params, assets, current, float(input_data.target_volume))
    price, quote = choose_price(executor, params, direction, passive=params.action in {"cancel", "hold"})
    memory["quote"] = quote.model_dump(mode="json")
    for volume, extra in plan:
        executor.handle_termination_checkpoint()
        if params.action == "invalid":
            if params.invalid_case == "volume":
                volume = params.max_order_lots + 1
            elif params.invalid_case == "price":
                price += executor.get_tick_size() / 2
            else:
                raise ValueError("未知合约须使用 direct-probe 场景，不能假称规划跳过命中底层校验")
        memory["requests"].append(
            {
                "symbol": params.symbol,
                "direction": direction.value,
                "volume": volume,
                "price": price,
                **extra,
                "at": clock_now_iso(),
            }
        )
        checkpoint(executor, "before_send", params)
        if params.before_send_hold_seconds:
            executor.sleep_or_terminate(params.before_send_hold_seconds)
        try:
            order = executor.place_order(
                direction,
                OrderType.LIMIT,
                volume,
                price,
                trade_rule={**input_data.trade_rule, "max_single_order_size": params.max_order_lots},
                **extra,
            )
        except Exception as exc:
            memory["request_error"] = exception_details(exc)
            raise
        capture.add(order)
        checkpoint(executor, "after_send", params, order.order_id)
        if params.after_send_hold_seconds:
            executor.sleep_or_terminate(params.after_send_hold_seconds)


def collect_final_state(executor, capture, memory, params):
    """普通结束时补收证据；终止异常继续上抛，不在 worker 内拖延退出。"""
    final = None
    cancel_owned(executor, capture, memory)
    settle_deadline = get_default_clock().time() + min(5, params.timeout_seconds)
    while capture.snapshots() and not all(o.is_completed() for o in capture.snapshots()):
        executor.handle_termination_checkpoint()
        if get_default_clock().time() >= settle_deadline:
            break
        get_default_clock().sleep(0.1)
    for order in capture.snapshots():
        try:
            for trade in executor.query_trades(order.order_id):
                capture.on_trade(trade)
        except ExecutionTerminated:
            raise
        except Exception as exc:
            memory.setdefault("query_errors", []).append(exception_details(exc))
    try:
        final, final_volume = guard_assets(executor, params)
        memory["after"] = final.model_dump(mode="json")
        memory["final_volume"] = final_volume
        memory["remaining_orders"] = [o.model_dump(mode="json") for o in executor.get_pending_orders()]
    except ExecutionTerminated:
        raise
    except Exception as exc:
        memory["final_read_error"] = exception_details(exc)
    return final


@register_algorithm("FT-CTP-SCENARIO", channels=[TradeChannel.CTP], params_class=ScenarioParams)
def scenario_algorithm(executor: ExecutorProtocol, algorithm_input: AlgorithmInput) -> AlgorithmResult:
    """有界运行，回报和未完成状态全部进入现有 execution summary。"""
    params = ScenarioParams.model_validate(algorithm_input.params)
    if algorithm_input.symbol != params.symbol:
        return AlgorithmResult(
            symbol=algorithm_input.symbol,
            status=ExecutionStatus.NOOP,
            memory={"feitai": {"ignored_unrelated_symbol": True}},
        )
    capture = Capture(params.symbol)
    memory = {
        "batch": params.batch,
        "step": params.step,
        "action": params.action,
        "requests": [],
        "cancellations": [],
        "started_at": clock_now_iso(),
    }
    status, error, final = ExecutionStatus.FAILED, None, None
    terminated = False
    executor.register_order_callback(capture.on_order)
    executor.register_trade_callback(capture.on_trade)
    try:
        assets, current = guard_assets(executor, params)
        memory["before"] = assets.model_dump(mode="json")
        if executor.get_pending_orders():
            raise ValueError("测试合约有存量挂单，拒绝覆盖或撤销")
        checkpoint(executor, "ready", params)
        if params.hold_seconds:
            executor.sleep_or_terminate(params.hold_seconds)
        submit_plan(executor, algorithm_input, params, capture, memory, assets, current)
        deadline = get_default_clock().time() + params.timeout_seconds
        wait_orders(executor, capture, deadline, pending=params.action == "cancel")
        if params.action == "cancel":
            memory["pending_confirmed"] = any(
                o.extra.get("order_sys_id") and not o.is_completed() for o in capture.snapshots()
            )
            cancel_owned(executor, capture, memory)
            wait_orders(executor, capture, get_default_clock().time() + min(10, params.timeout_seconds))
        orders = capture.snapshots()
        status = ExecutionStatus.SUCCEEDED if all(o.is_completed() for o in orders) else ExecutionStatus.PARTIAL
    except ExecutionTerminated:
        terminated = True
        raise
    except Exception as exc:
        error = str(exc)
        memory["error"] = exception_details(exc)
    finally:
        try:
            if not terminated:
                final = collect_final_state(executor, capture, memory, params)
        finally:
            executor.unregister_order_callback(capture.on_order)
            executor.unregister_trade_callback(capture.on_trade)
    memory["finished_at"] = clock_now_iso()
    if any(not o.is_completed() for o in capture.snapshots()) or memory.get("final_read_error"):
        status = ExecutionStatus.PARTIAL
    return AlgorithmResult(
        symbol=params.symbol,
        algorithm="FT-CTP-SCENARIO",
        orders=capture.snapshots(),
        trades=capture.fills(),
        target_volume=algorithm_input.target_volume,
        account_assets=final,
        status=status,
        error=error,
        memory={"feitai": memory},
    )
