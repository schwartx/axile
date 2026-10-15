"""账户委托的纯内存采样，以及与采样分离的有界恢复任务。"""

from __future__ import annotations

from dataclasses import asdict
from threading import Thread
from typing import Any

from loguru import logger

from axile.executor.algorithms.utils.clock import clock_monotonic
from axile.server.account_orders_runtime import _cancellation_failures, public_order


def sample_orders(state: Any, *, force: bool = False) -> dict[str, Any] | None:
    """监控线程只复制内存；恢复另起任务，交易繁忙时不争用连接。"""
    if not state.resource_lock.acquire(blocking=False):
        return None
    try:
        return _sample_orders(state, force=force)
    finally:
        state.resource_lock.release()


def _sample_orders(state: Any, *, force: bool) -> dict[str, Any] | None:
    binding = state.connection_collector.binding
    executor = state.executor
    book = getattr(executor, "account_orders", None)
    if binding is None or binding != state.orders_binding:
        return None
    if book is None or state.orders_executor is not executor:
        _schedule_recovery(state)
        return None
    if state.disconnected or state.connection_collector.needs_reconciliation:
        book.invalidate("账户连接已中断，委托状态待确认")
    health = getattr(executor, "get_order_channel_health", None)
    if callable(health) and str(getattr(health(), "value", "")) in {"down", "degraded"}:
        book.invalidate("订单回报通道异常，委托状态待确认")
    snapshot = book.snapshot()
    snapshot["generation"] = state.orders_generation
    snapshot["worker_instance_id"] = binding.instance_id
    snapshot["instance_id"] = f"{binding.instance_id}:{state.orders_generation}"
    if snapshot["error"]:
        _schedule_recovery(state)
    rows = []
    for order in snapshot["data"]:
        metadata = book.metadata(order.order_id, order.symbol)
        context = metadata.get("audit_context", {})
        if isinstance(context, dict) and context.get("execution_id"):
            order.extra = {**order.extra, "execution_id": context["execution_id"]}
        rows.append(public_order(order))
    tracked = list(getattr(executor, "_manual_cancel_observations", {}))
    snapshot.update(
        data=rows,
        resolved=[public_order(order) for order in snapshot["resolved"]],
        cancel_failures=_cancellation_failures(executor, tracked, persist=False),
    )
    fingerprint = (snapshot["instance_id"], snapshot["version"], repr(snapshot["cancel_failures"]))
    if not force and getattr(state, "orders_last_sample", None) == fingerprint:
        return {"binding": asdict(binding), "heartbeat": True, "generation": state.orders_generation}
    if not force:
        state.orders_last_sample = fingerprint
    return {"binding": asdict(binding), "snapshot": snapshot}


def _schedule_recovery(state: Any) -> None:
    if not getattr(state, "orders_recovery_enabled", False) or state.disconnected or state.request_active:
        return
    if getattr(state.executor, "connection_recovery_in_progress", False):
        return
    if clock_monotonic() < max(state.next_connect_at, getattr(state, "orders_retry_at", 0)):
        return
    thread = getattr(state, "orders_recovery_thread", None)
    if thread is not None and thread.is_alive():
        return
    state.orders_retry_at = clock_monotonic() + 30
    state.orders_recovery_thread = Thread(target=_recover, args=(state,), name="account-orders-recovery", daemon=True)
    state.orders_recovery_thread.start()


def _recover(state: Any) -> None:
    from axile.server.execution.worker_backend.worker_state import _finalize_executor, _resolve_prepared_executor

    with state.resource_lock:
        account = getattr(state, "orders_account", None)
        if (
            not state.orders_recovery_enabled
            or account is None
            or state.orders_binding != state.connection_collector.binding
            or state.disconnected
            or state.request_active
            or state.active_execution_id is not None
        ):
            return
        try:
            state.connected_once = False
            _resolve_prepared_executor(state=state, account=account, execution_id=None, audit_context={})
        except Exception:
            logger.exception("账户委托恢复未完成 account_id={}", state.account_id)
        finally:
            try:
                _finalize_executor(state.executor)
            except Exception:
                state.requires_worker_restart = True
                logger.exception("账户恢复收尾失败 account_id={}", state.account_id)
