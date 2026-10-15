"""账户级当前委托服务，独立于 execution 生命周期。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from axile.executor.algorithms.utils.clock import clock_monotonic, clock_now_iso
from axile.server.connection_state import connection_config_key
from axile.server.db.models.account import AccountContext
from axile.server.db.models.account_order import (
    AccountOrderPublic,
    AccountOrdersPublic,
    OrderCancelReceipt,
    OrderCancelRequest,
)


@dataclass
class _OrdersState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    receipts: dict[str, OrderCancelReceipt] = field(default_factory=dict)
    cached: AccountOrdersPublic | None = None
    sampled: float = 0


_states: dict[int, tuple[str, _OrdersState]] = {}
_tasks: set[asyncio.Task] = set()


def _background(coroutine) -> asyncio.Task:
    """保留 HTTP 断开后仍在运行的任务，并读取其最终异常。"""
    task = asyncio.create_task(coroutine)
    _tasks.add(task)

    def completed(done):
        _tasks.discard(done)
        if not done.cancelled():
            done.exception()

    task.add_done_callback(completed)
    return task


def _state(account: AccountContext) -> _OrdersState:
    key = connection_config_key(account.trade_channel, account.account_config)
    assert account.id is not None
    previous = _states.get(account.id)
    if previous is None or previous[0] != key:
        previous = key, _OrdersState()
        _states[account.id] = previous
    return previous[1]


async def _command(account: AccountContext, payload: dict[str, Any]) -> dict[str, Any]:
    from axile.server.execution.worker_backend.manager import get_worker_backend_manager

    return await get_worker_backend_manager().order_command(account, payload)


def _merge_snapshot(state: _OrdersState, result: dict[str, Any]) -> AccountOrdersPublic:
    orders = {(row.symbol, row.order_id): row for row in map(AccountOrderPublic.model_validate, result["data"])}
    resolved = {
        (row.symbol, row.order_id): row for row in map(AccountOrderPublic.model_validate, result.get("resolved", []))
    }
    failures = {(row["symbol"], row["order_id"]): row["message"] for row in result.get("cancel_failures", [])}
    complete = all(not order.uncertain for order in orders.values())
    for receipt in state.receipts.values():
        if receipt.state in {"terminal", "failed"}:
            continue
        key = receipt.symbol, receipt.order_id
        terminal = resolved.get(key)
        current = orders.get(key)
        if terminal is not None:
            receipt.state, receipt.order = "terminal", terminal
            receipt.message = f"订单实际状态：{terminal.status}"
        elif current is not None:
            receipt.order = current
            if (receipt.symbol, receipt.order_id) in failures:
                receipt.state = "failed"
                receipt.message = failures[receipt.symbol, receipt.order_id]
        else:
            # 挂单列表缺失只证明本次未查询到；保留最后已知数量，等待真实终态。
            complete = False
            receipt.state, receipt.message = "unknown", "未查询到订单终态，撤单结果待确认"
            if receipt.order is not None:
                orders[key] = receipt.order.model_copy(update={"uncertain": True})
    return AccountOrdersPublic(
        data=sorted(orders.values(), key=lambda order: (order.create_time or "", order.order_id), reverse=True),
        count=len(orders) if complete else None,
        complete=complete,
        sampled_at=result["sampled_at"],
        cancellations=list(state.receipts.values())[-50:],
    )


async def _query(account: AccountContext, state: _OrdersState, refresh: bool) -> AccountOrdersPublic:
    async with state.lock:
        if not refresh and state.cached is not None and clock_monotonic() - state.sampled < 4:
            return state.cached.model_copy(deep=True)
        tracked = [(r.symbol, r.order_id) for r in state.receipts.values() if r.state not in {"terminal", "failed"}]
        result = await _command(account, {"command": "query", "tracked": tracked})
        state.cached = _merge_snapshot(state, result)
        state.sampled = clock_monotonic()
        return state.cached.model_copy(deep=True)


async def query_account_orders(account: AccountContext, *, refresh: bool = False) -> AccountOrdersPublic:
    """读取渠道全部未完成委托，短缓存合并同一账户页面的重复读取。"""
    task = _background(_query(account, _state(account), refresh))
    # HTTP 断开不取消已经进入渠道的请求，防止后台操作失去所有者。
    return await asyncio.wait_for(asyncio.shield(task), timeout=35)


async def _cancel(account: AccountContext, state: _OrdersState, payload: OrderCancelRequest) -> OrderCancelReceipt:
    async with state.lock:
        existing = state.receipts.get(payload.request_id)
        if existing is not None:
            if (existing.order_id, existing.symbol) != (payload.order_id, payload.symbol):
                raise ValueError("同一请求标识不能用于不同订单")
            return existing.model_copy(deep=True)
        pending = next(
            (
                r
                for r in state.receipts.values()
                if (r.order_id, r.symbol) == (payload.order_id, payload.symbol)
                and r.state in {"submitting", "submitted", "unknown"}
            ),
            None,
        )
        if pending is not None:
            return pending.model_copy(deep=True)
        last = (
            next((o for o in state.cached.data if o.order_id == payload.order_id and o.symbol == payload.symbol), None)
            if state.cached
            else None
        )
        receipt = OrderCancelReceipt(
            **payload.model_dump(),
            state="submitting",
            message="正在提交撤单请求",
            requested_at=clock_now_iso(),
            order=last,
        )
        state.receipts[payload.request_id] = receipt
        try:
            result = await _command(
                account, {"command": "cancel", "symbol": payload.symbol, "order_id": payload.order_id}
            )
            receipt.state, receipt.message = result["state"], result["message"]
            if result.get("order") is not None:
                receipt.order = AccountOrderPublic.model_validate(result["order"])
        except Exception:
            logger.exception("人工撤单结果未知 account_id={} order_id={}", account.id, payload.order_id)
            # 发送后的通信故障可能已经到达柜台，保留幂等记录，绝不自动重试。
            receipt.state, receipt.message = "unknown", "撤单结果待确认，请刷新核对；未自动重试"
        state.cached = None
        # 已结束的回执保留最近 50 条；未确认请求不淘汰，避免再次提交同一撤单。
        ended = [key for key, r in state.receipts.items() if r.state in {"terminal", "failed"}]
        for key in ended[:-50]:
            state.receipts.pop(key)
        return receipt.model_copy(deep=True)


async def cancel_account_order(account: AccountContext, payload: OrderCancelRequest) -> OrderCancelReceipt:
    """提交幂等单笔撤单，HTTP 请求重试不会重复报撤。"""
    state = _state(account)
    task = _background(_cancel(account, state, payload))
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=35)
    except TimeoutError:
        previous = state.receipts.get(payload.request_id)
        return OrderCancelReceipt(
            **payload.model_dump(),
            state="unknown",
            message="撤单结果待确认；未自动重试",
            requested_at=clock_now_iso(),
            order=previous.order if previous is not None else None,
        )
