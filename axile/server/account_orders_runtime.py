"""通过既有执行器查询、撤单和核实终态，线程与 worker 共用。"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime
from math import isfinite
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

from axile.executor.account_control.decorators import run_controlled_call
from axile.executor.account_control.guard import AccountControlGuard
from axile.executor.account_control.models import AccountControlContextKind, AccountControlSource
from axile.executor.account_control.scope import account_control_scope
from axile.executor.algorithms.utils.clock import clock_monotonic, clock_now_iso
from axile.executor.models.unified_order import UnifiedOrder
from axile.server.account_control import providers
from axile.server.account_control.store import AccountControlStore
from axile.server.db.models.account import AccountContext
from axile.server.db.models.account_order import AccountOrderPublic
from axile.server.execution.execution_account_control import build_account_control_guard, flush_account_control_records
from axile.server.order_attributes import order_attributes

_IDENTITY_FIELDS = (
    "order_ref",
    "front_id",
    "session_id",
    "exchange_id",
    "order_sys_id",
    "trading_day",
    "client_order_id",
    "cl_ord_id",
    "tq_symbol",
    "exchange_order_id",
    "ex_ord_id",
)


def public_offset(extra: dict[str, Any]) -> str | None:
    """保留渠道原生开平，GM 委托与成交使用同一套 position_effect 映射。"""
    offset = extra.get("offset_flag", extra.get("offset"))
    if offset is not None:
        return str(offset)
    effect = extra.get("position_effect")
    if effect is None:
        raw = extra.get("raw_trade_data", extra.get("raw_order_data", {}))
        if isinstance(raw, dict):
            effect = raw.get("position_effect")
    return {
        "1": "OPEN",
        "2": "CLOSE",
        "3": "CLOSETODAY",
        "4": "CLOSEYESTERDAY",
        "PositionEffect_Open": "OPEN",
        "PositionEffect_Close": "CLOSE",
        "PositionEffect_CloseToday": "CLOSETODAY",
        "PositionEffect_CloseYesterday": "CLOSEYESTERDAY",
    }.get(str(effect))


def _native_time(value: Any) -> str | None:
    """展示渠道实际提供的时间，缺失时不拿本地观测时间补齐。"""
    if isinstance(value, datetime):
        return value.isoformat()
    return value if isinstance(value, str) and value else None


def public_order(order: UnifiedOrder) -> dict[str, Any]:
    """按白名单导出订单，未知开平与执行关联保持为空。"""
    extra = order.extra
    offset = public_offset(extra)
    created = order.create_time
    updated = order.update_time
    if extra.get("channel_type") == "ctp":
        updated = None
    raw = extra.get("raw_order_data")
    if extra.get("channel_type") == "gm" and isinstance(raw, dict):
        created = _native_time(raw.get("created_at"))
        updated = _native_time(raw.get("updated_at"))
    if "insert_date_time" in extra:
        try:
            value = extra["insert_date_time"]
            nano = int(value) if isinstance(value, (int, float, str)) else 0
            created = datetime.fromtimestamp(nano / 1_000_000_000).isoformat() if nano > 0 else None
        except (TypeError, ValueError, OSError, OverflowError):
            created = None
    execution_id = extra.get("execution_id")
    return AccountOrderPublic(
        avg_price=order.avg_price
        if order.filled_volume > 0 and isfinite(order.avg_price) and order.avg_price > 0
        else None,
        avg_price_source=("trades" if extra.get("avg_price_source") == "trades" else "native")
        if order.filled_volume > 0 and isfinite(order.avg_price) and order.avg_price > 0
        else None,
        attributes=order_attributes(extra),
        order_id=order.order_id,
        symbol=order.symbol,
        direction=str(getattr(order.direction, "value", order.direction)),
        offset=str(offset) if offset is not None else None,
        order_type=str(getattr(order.order_type, "value", order.order_type)),
        price=order.price,
        volume=order.volume,
        filled_volume=order.filled_volume,
        remaining_volume=order.remaining_volume,
        status=order.status,
        create_time=created,
        execution_id=execution_id if isinstance(execution_id, str) else None,
        native_identity={key: str(extra[key]) for key in _IDENTITY_FIELDS if extra.get(key) is not None},
        uncertain=not order.is_active() and not order.is_completed(),
        update_time=updated,
        reason=next(
            (
                str(extra[key])
                for key in ("error_msg", "ord_rej_reason_detail", "last_msg", "cancel_reason", "status_msg")
                if extra.get(key)
            ),
            None,
        ),
    ).model_dump(mode="json")


async def _flush_borrowed_guard(guard: Any) -> None:
    _, events = guard.flush_records()
    # delta_uid 对应整个 guard 的累计量，提前插入会丢掉执行收尾前的后续计数。
    await AccountControlStore().flush_execution_records(counter_deltas=[], events=events)


@contextmanager
def order_control_scope(executor: Any, account: AccountContext, active_guard: Any = None, *, command: str = "query"):
    """运行中共用额度；空闲时建立独立审计上下文，始终保持交易 runtime 绑定。"""
    assert account.id is not None
    borrowed_lock = providers.operation_lock(active_guard) if active_guard is not None else None
    if borrowed_lock is not None:
        borrowed_lock.acquire()
        if providers.active_guard(account.id) is not active_guard:
            borrowed_lock.release()
            borrowed_lock, active_guard = None, None
    owned = active_guard is None
    source = AccountControlSource.MANUAL_CANCEL if command == "cancel" else AccountControlSource.ORDER_QUERY
    guard = active_guard or asyncio.run(
        build_account_control_guard(
            account,
            f"orders-{uuid4().hex}",
            register_monitor=False,
            context_kind=AccountControlContextKind.ACCOUNT_OPERATION,
            source=source,
        )
    )
    deadline = clock_monotonic() + 20
    if owned:
        providers.observe_outcomes(guard)

        def checkpoint(_symbol=None):
            if clock_monotonic() >= deadline:
                raise TimeoutError("当前委托操作等待额度超时")

        guard.set_termination_checkpoint(checkpoint)
    try:
        with account_control_scope(executor, guard, source=source):
            yield
    finally:
        if owned:
            adapter = SimpleNamespace(
                export_account_control_records=guard.flush_records, get_account_control_guard=lambda: guard
            )
            asyncio.run(flush_account_control_records(adapter))
        else:
            try:
                asyncio.run(_flush_borrowed_guard(guard))
            finally:
                assert borrowed_lock is not None
                borrowed_lock.release()


def terminal_order(executor: Any, symbol: str, order_id: str) -> UnifiedOrder | None:
    """只有渠道返回明确终态才确认结束，查不到订单不推断撤单成功。"""
    reconcile = getattr(executor, "reconcile_terminal_order", None)
    if not callable(reconcile):
        return None
    order = cast(
        UnifiedOrder | None,
        run_controlled_call(
            guard=executor.get_account_control_guard(),
            operation="query_order",
            symbol=symbol,
            metadata={"order_id": order_id, "source": "current_orders"},
            call=lambda: reconcile(symbol, order_id),
            success_outcome="fetched",
        ),
    )
    return (
        order
        if order is not None and order.is_completed() and order.order_id == order_id and order.symbol == symbol
        else None
    )


def _observe_cancellation(executor: Any, symbol: str, order_id: str) -> None:
    getter = getattr(executor, "get_account_control_guard", None)
    guard = cast(AccountControlGuard | None, getter() if callable(getter) else None)
    if guard is None:
        return
    records = cast(list[dict[str, Any]], guard.monitor_snapshot()["records"])
    event = next(
        (
            event
            for event in records
            if event["operation"] == "cancel_order"
            and event["metadata"].get("order_id") == order_id
            and event["symbol"] == symbol
        ),
        None,
    )
    if event is None:
        return
    observations = getattr(executor, "_manual_cancel_observations", None)
    if observations is None:
        observations = executor._manual_cancel_observations = OrderedDict()
    observations[symbol, order_id] = (guard, event["event_uid"])
    observations.move_to_end((symbol, order_id))
    while len(observations) > 512:
        observations.popitem(last=False)


async def _persist_cancel_outcomes(events: list[dict[str, Any]]) -> None:
    await AccountControlStore().update_event_outcomes(events)


def _cancellation_failures(executor: Any, tracked: list, *, persist: bool = True) -> list[dict[str, Any]]:
    observations = getattr(executor, "_manual_cancel_observations", {})
    events, failures = [], []
    for symbol, order_id in tracked:
        observation = observations.get((symbol, order_id))
        if observation is None:
            continue
        guard, uid = observation
        event = next((event for event in guard.monitor_snapshot()["records"] if event["event_uid"] == uid), None)
        if event is None or event["outcome"] not in {
            "broker_rejected",
            "broker_sync_rejected",
            "downstream_error",
            "policy_blocked",
        }:
            continue
        events.append(event)
        failures.append(
            {
                "symbol": symbol,
                "order_id": order_id,
                "message": event["metadata"].get("error_msg") or "撤单被拒绝或未提交成功",
            }
        )
    if events and persist:
        asyncio.run(_persist_cancel_outcomes(events))
    return failures


def run_order_command(executor: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """查询账户全部未完成委托，或验证身份后提交一次撤单。"""
    if payload["command"] == "activity":
        from axile.server.account_activity_runtime import run_activity_command

        return run_activity_command(executor, payload)
    book = executor.account_orders

    def publish(order: UnifiedOrder) -> dict[str, Any]:
        order = book.activity.enrich_orders([order])[0][0]
        return public_order(order)

    snapshot = book.snapshot()
    if payload["command"] == "query" or payload["command"] == "cancel" and not snapshot["complete"]:
        executor.get_pending_orders(None)
        snapshot = book.snapshot()
    active = {(order.symbol, order.order_id): order for order in snapshot["data"] if not order.is_completed()}
    if payload["command"] == "cancel":
        key = payload["symbol"], payload["order_id"]
        order = active.get(key)
        if order is None:
            terminal = terminal_order(executor, *key)
            return {
                "state": "terminal" if terminal else "unknown",
                "order": publish(terminal) if terminal else None,
                "message": "订单已结束，未提交撤单" if terminal else "未找到当前委托，订单状态待确认；未提交撤单",
            }
        if not order.is_active():
            return {"state": "unknown", "order": publish(order), "message": "订单状态未知，未提交撤单"}
        try:
            submitted = executor.cancel_order(order.symbol, order.order_id)
        finally:
            _observe_cancellation(executor, order.symbol, order.order_id)
        return {
            "state": "submitted" if submitted else "failed",
            "order": publish(order),
            "message": "撤单请求已提交，等待实际回报" if submitted else "撤单请求未提交成功",
        }
    resolved = []
    for key in payload.get("tracked", []):
        symbol, order_id = key
        if payload["command"] != "verify" and (symbol, order_id) in active:
            continue
        try:
            order = terminal_order(executor, symbol, order_id)
        except Exception:
            # 局部终态核实失败不丢弃已经成功读取的账户委托；上层继续保留未知状态。
            order = None
        if order is not None:
            book.apply(order)
            resolved.append(publish(order))
    current = book.snapshot()
    terminals = {(row["symbol"], row["order_id"]): row for row in resolved}
    for order in current["resolved"]:
        terminals.setdefault((order.symbol, order.order_id), publish(order))
    return {
        "data": [publish(order) for order in current["data"]],
        "complete": current["complete"],
        "error": current["error"],
        "version": current["version"],
        "resolved": list(terminals.values()),
        "cancel_failures": _cancellation_failures(executor, payload.get("tracked", [])),
        "sampled_at": clock_now_iso(),
    }
