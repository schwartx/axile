"""账户级当前委托服务，独立于 execution 生命周期。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from axile.common.trade_channel import TradeChannel
from axile.executor.algorithms.utils.clock import clock_now_iso
from axile.executor.ctp_input_validation import validate_cancel_id
from axile.server.connection_state import connection_config_key
from axile.server.db.models.account import AccountContext
from axile.server.db.models.account_order import (
    AccountActivityPublic,
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
    refresh_task: asyncio.Task | None = None


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
    complete = result.get("complete", True) and all(not order.uncertain for order in orders.values())
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
        version=result.get("version", 0),
        instance_id=result.get("instance_id"),
        error=result.get("error"),
    )


async def _query(account: AccountContext, state: _OrdersState, refresh: bool) -> AccountOrdersPublic:
    from axile.server.orders_live import orders_hub, read_orders_snapshot

    if not refresh:
        return await _snapshot_result(account, state, read_orders_snapshot(account))
    async with state.lock:
        tracked = [(r.symbol, r.order_id) for r in state.receipts.values() if r.state not in {"terminal", "failed"}]
        result = await _command(account, {"command": "query", "tracked": tracked})
        snapshot = await _snapshot_result(account, state, _latest_result(account, result))
        orders_hub.changed(account.id)
        return snapshot


def _latest_result(account: AccountContext, result: dict[str, Any]) -> dict[str, Any]:
    """命令返回前若已收到同一 worker 的更新或重建基线，优先使用新证据。"""
    from axile.server.orders_live import read_orders_snapshot

    mirrored = read_orders_snapshot(account)
    if (
        mirrored is not None
        and result.get("worker_instance_id")
        and mirrored.get("worker_instance_id") == result["worker_instance_id"]
        and (mirrored.get("generation", 0), mirrored.get("version", 0))
        >= (result.get("generation", 0), result.get("version", 0))
    ):
        return mirrored
    return result


async def _snapshot_result(account: AccountContext, state: _OrdersState, result: dict | None) -> AccountOrdersPublic:
    """镜像读取不等待人工操作的柜台响应，复制回执后再补数据库关联。"""
    if result is None:
        result = {"data": [], "complete": False, "sampled_at": clock_now_iso(), "error": "账户委托尚未同步"}
    snapshot = _merge_snapshot(state, result).model_copy(deep=True)
    state.cached = snapshot
    await _link_executions(account, snapshot)
    return snapshot


async def _link_executions(account: AccountContext, result: AccountOrdersPublic) -> None:
    """REST 与 SSE 复用同一订单执行关联，外部终端委托保持未关联。"""
    from sqlalchemy import tuple_
    from sqlmodel import col, select

    from axile.server.core.db import SessionLocal
    from axile.server.db.models.execution import ExecutionEvent

    identities = [(order.symbol, order.order_id) for order in result.data if order.execution_id is None]
    if not identities:
        return
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(ExecutionEvent.symbol, ExecutionEvent.order_id, ExecutionEvent.execution_id)
                .where(
                    col(ExecutionEvent.account_id) == account.id,
                    tuple_(col(ExecutionEvent.symbol), col(ExecutionEvent.order_id)).in_(identities),
                )
                .order_by(col(ExecutionEvent.id).desc())
            )
        ).all()
    linked = {}
    for symbol, order_id, execution_id in rows:
        linked.setdefault((symbol, order_id), execution_id)
    for order in result.data:
        if order.execution_id is None:
            order.execution_id = linked.get((order.symbol, order.order_id))


async def query_account_orders(account: AccountContext, *, refresh: bool = False) -> AccountOrdersPublic:
    """普通读取只使用镜像；显式核对复用已有独立委托操作通路。"""
    if not refresh:
        return await _query(account, _state(account), False)
    state = _state(account)
    if state.refresh_task is None or state.refresh_task.done():
        state.refresh_task = _background(_query(account, state, True))
    return await asyncio.wait_for(asyncio.shield(state.refresh_task), timeout=35)


async def query_account_activity(
    account: AccountContext, *, refresh: bool = False, symbol: str | None = None, limit: int = 100
) -> AccountActivityPublic:
    """复用账户委托通路读取近期记录；仅显式核对访问渠道。"""
    state = _state(account)

    async def query() -> AccountActivityPublic:
        async with state.lock:
            result = await _command(
                account, {"command": "activity", "refresh": refresh, "symbol": symbol, "limit": limit}
            )
            return AccountActivityPublic.model_validate(result)

    task = _background(query())
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
            if receipt.state not in {"terminal", "failed"}:
                receipt.state, receipt.message = result["state"], result["message"]
                if result.get("order") is not None:
                    receipt.order = AccountOrderPublic.model_validate(result["order"])
        except Exception:
            logger.exception("人工撤单结果未知 account_id={} order_id={}", account.id, payload.order_id)
            # 发送后的通信故障可能已经到达柜台，保留幂等记录，绝不自动重试。
            if receipt.state not in {"terminal", "failed"}:
                receipt.state, receipt.message = "unknown", "撤单结果待确认，请刷新核对；未自动重试"
        from axile.server.orders_live import orders_hub

        orders_hub.changed(account.id)
        if receipt.state in {"submitted", "unknown"}:
            _background(_verify_cancel(account, state, payload.request_id))
        # 已结束的回执保留最近 50 条；未确认请求不淘汰，避免再次提交同一撤单。
        ended = [key for key, r in state.receipts.items() if r.state in {"terminal", "failed"}]
        for key in ended[:-50]:
            state.receipts.pop(key)
        return receipt.model_copy(deep=True)


async def _verify_cancel(account: AccountContext, state: _OrdersState, request_id: str) -> None:
    """等待回报后只核实一次相关订单；未知结果由人工核对继续处理。"""
    from axile.server.orders_live import orders_hub, read_orders_snapshot

    assert account.id is not None
    await asyncio.sleep(3)
    async with state.lock:
        if _states.get(account.id, (None, None))[1] is not state:
            return
        latest = read_orders_snapshot(account)
        if latest is not None:
            state.cached = _merge_snapshot(state, latest)
        receipt = state.receipts[request_id]
        if receipt.state in {"terminal", "failed"}:
            return
        try:
            result = await _command(account, {"command": "verify", "tracked": [(receipt.symbol, receipt.order_id)]})
            state.cached = _merge_snapshot(state, _latest_result(account, result))
        except Exception:
            logger.exception("撤单终态核实未完成 account_id={}", account.id)
        if receipt.state not in {"terminal", "failed"}:
            receipt.state, receipt.message = "unknown", "撤单终态尚未确认，请核对委托；未自动重发"
        orders_hub.changed(account.id)


async def cancel_account_order(account: AccountContext, payload: OrderCancelRequest) -> OrderCancelReceipt:
    """提交幂等单笔撤单，HTTP 请求重试不会重复报撤。"""
    if account.trade_channel == TradeChannel.CTP:
        validate_cancel_id(payload.order_id)
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
