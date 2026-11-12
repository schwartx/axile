"""账户级当前委托服务，独立于 execution 生命周期。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from loguru import logger

from axile.common.trade_channel import TradeChannel
from axile.executor.algorithms.utils.clock import clock_now_iso
from axile.executor.ctp_input_validation import validate_cancel_id
from axile.server.account_assets import reconcile_account_assets
from axile.server.connection_state import connection_config_key
from axile.server.db.models.account import AccountContext
from axile.server.db.models.account_order import OrderCancelRequest
from axile.server.execution.registry import get_running_execution_id
from axile.server.native_store import native_operation


@dataclass
class _OrdersState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    receipts: dict[str, dict[str, Any]] = field(default_factory=dict)
    cached: dict[str, Any] | None = None
    refresh_task: asyncio.Task | None = None
    reconciliations: dict[str, dict[str, object]] = field(default_factory=dict)


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


def _merge_snapshot(state: _OrdersState, result: dict[str, Any]) -> dict[str, Any]:
    orders = {(row["symbol"], row["order_id"]): row for row in result["data"]}
    resolved = {(row["symbol"], row["order_id"]): row for row in result.get("resolved", [])}
    failures = {(row["symbol"], row["order_id"]): row["message"] for row in result.get("cancel_failures", [])}
    complete = result.get("complete", True) and all(not order["uncertain"] for order in orders.values())
    for receipt in state.receipts.values():
        if receipt["state"] in {"terminal", "failed"}:
            continue
        key = receipt["symbol"], receipt["order_id"]
        terminal = resolved.get(key)
        current = orders.get(key)
        if terminal is not None:
            receipt["state"], receipt["order"] = "terminal", terminal
            receipt["message"] = f"订单实际状态：{terminal['status']}"
        elif current is not None:
            receipt["order"] = current
            if (receipt["symbol"], receipt["order_id"]) in failures:
                receipt["state"] = "failed"
                receipt["message"] = failures[receipt["symbol"], receipt["order_id"]]
        else:
            # 挂单列表缺失只证明本次未查询到；保留最后已知数量，等待真实终态。
            complete = False
            receipt["state"], receipt["message"] = "unknown", "未查询到订单终态，撤单结果待确认"
            if receipt["order"] is not None:
                orders[key] = {**receipt["order"], "uncertain": True}
    return {
        "data": sorted(
            orders.values(), key=lambda order: (order["create_time"] or "", order["order_id"]), reverse=True
        ),
        "count": len(orders) if complete else None,
        "sampled_at": result["sampled_at"],
        "complete": complete,
        "instance_id": result.get("instance_id"),
        "version": result.get("version", 0),
        "error": result.get("error"),
        "cancellations": list(state.receipts.values())[-50:],
        "asset_refresh": None,
    }


async def _query(account: AccountContext, state: _OrdersState, refresh: bool) -> dict[str, Any]:
    from axile.server.orders_live import orders_hub, read_orders_snapshot

    if not refresh:
        return await _snapshot_result(account, state, read_orders_snapshot(account))
    async with state.lock:
        tracked = [
            (r["symbol"], r["order_id"]) for r in state.receipts.values() if r["state"] not in {"terminal", "failed"}
        ]
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
        if mirrored.get("generation", 0) == result.get("generation", 0):
            # 最近终态窗口不包含所有显式核实结果；同一代数的确认不能被采样丢弃。
            resolved = {
                (row["symbol"], row["order_id"]): row
                for snapshot in (result, mirrored)
                for row in snapshot.get("resolved", [])
            }
            return {**mirrored, "resolved": list(resolved.values())}
        return mirrored
    return result


async def _snapshot_result(account: AccountContext, state: _OrdersState, result: dict | None) -> dict[str, Any]:
    """镜像读取不等待人工操作的柜台响应，复制回执后再补数据库关联。"""
    if result is None:
        result = {"data": [], "complete": False, "sampled_at": clock_now_iso(), "error": "账户委托尚未同步"}
    snapshot = deepcopy(_merge_snapshot(state, result))
    state.cached = snapshot
    await _link_executions(account, snapshot)
    return snapshot


async def _link_executions(account: AccountContext, result: dict[str, Any]) -> None:
    """REST 与 SSE 复用同一订单执行关联，外部终端委托保持未关联。"""
    identities = [(order["symbol"], order["order_id"]) for order in result["data"] if order["execution_id"] is None]
    if not identities:
        return
    rows = await native_operation("order_execution_links", {"account_id": account.id, "identities": identities})
    linked = {(symbol, order_id): execution_id for symbol, order_id, execution_id in rows}
    for order in result["data"]:
        if order["execution_id"] is None:
            order["execution_id"] = linked.get((order["symbol"], order["order_id"]))


async def query_account_orders(
    account: AccountContext, *, refresh: bool = False, reconciliation_id: str | None = None
) -> dict[str, Any]:
    """普通读取只使用镜像；显式核对复用已有独立委托操作通路。"""
    if not refresh:
        state = _state(account)
        snapshot = await _query(account, state, False)
        if reconciliation_id is not None:
            snapshot["asset_refresh"] = state.reconciliations.get(reconciliation_id) or {
                "status": "failed",
                "reconciliation_id": reconciliation_id,
                "message": "核对任务已失效，请重新核对",
            }
        return snapshot
    state = _state(account)
    if state.refresh_task is None or state.refresh_task.done():
        state.refresh_task = _background(_refresh_orders(account, state))
    return await asyncio.shield(state.refresh_task)


async def _refresh_orders(account: AccountContext, state: _OrdersState) -> dict[str, Any]:
    """HTTP 断开后仍完成记录核对和持仓落库，共用原有等待预算。"""
    deadline = asyncio.get_running_loop().time() + 35

    async def records() -> tuple[dict[str, Any], asyncio.Task]:
        snapshot = deepcopy(await _query(account, state, True))
        # 补查随记录任务启动，HTTP 等待超时也不会断开后续持仓核对。
        return snapshot, _background(_reconcile_assets(account, deadline))

    snapshot, reconciliation = await asyncio.wait_for(asyncio.shield(_background(records())), timeout=35)
    snapshot["asset_refresh"] = await asyncio.shield(reconciliation)
    return snapshot


async def _reconcile_assets(account: AccountContext, deadline: float) -> dict[str, object]:
    """先核对记录再查询持仓；耗尽原有预算后继续落库，不占用委托锁。"""
    state = _state(account)
    identity = uuid4().hex
    pending: dict[str, object] = {
        "status": "pending",
        "reconciliation_id": identity,
        "message": "持仓仍在核对，完成后自动保存，请稍后查看",
    }
    state.reconciliations[identity] = pending

    async def reconcile() -> dict[str, object]:
        result = {**await reconcile_account_assets(account), "reconciliation_id": identity}
        state.reconciliations[identity] = result
        # 保留最近 50 个终态，正在等待的核对不能被淘汰。
        ended = [key for key, value in state.reconciliations.items() if value["status"] != "pending"]
        for key in ended[:-50]:
            state.reconciliations.pop(key)
        return result

    task = _background(reconcile())
    assert account.id is not None
    if get_running_execution_id(account.id) is not None:
        pending["message"] = "账户正在执行，结束后继续核对持仓"
        return pending
    try:
        remaining = max(0, deadline - asyncio.get_running_loop().time())
        return await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
    except TimeoutError:
        return pending


async def query_account_activity(
    account: AccountContext, *, refresh: bool = False, symbol: str | None = None, limit: int = 100
) -> dict[str, Any]:
    """复用账户委托通路读取近期记录；仅显式核对访问渠道。"""
    state = _state(account)
    deadline = asyncio.get_running_loop().time() + 35

    async def records() -> tuple[dict[str, Any], asyncio.Task | None]:
        async with state.lock:
            result = await _command(
                account, {"command": "activity", "refresh": refresh, "symbol": symbol, "limit": limit}
            )
            snapshot = deepcopy(result)
        return snapshot, _background(_reconcile_assets(account, deadline)) if refresh else None

    async def query() -> dict[str, Any]:
        task = _background(records())
        snapshot, reconciliation = await asyncio.wait_for(asyncio.shield(task), timeout=35)
        if reconciliation is not None:
            snapshot["asset_refresh"] = await asyncio.shield(reconciliation)
        return snapshot

    return await asyncio.shield(_background(query()))


async def _cancel(account: AccountContext, state: _OrdersState, payload: OrderCancelRequest) -> dict[str, Any]:
    async with state.lock:
        existing = state.receipts.get(payload.request_id)
        if existing is not None:
            if (existing["order_id"], existing["symbol"]) != (payload.order_id, payload.symbol):
                raise ValueError("同一请求标识不能用于不同订单")
            return deepcopy(existing)
        pending = next(
            (
                r
                for r in state.receipts.values()
                if (r["order_id"], r["symbol"]) == (payload.order_id, payload.symbol)
                and r["state"] in {"submitting", "submitted", "unknown"}
            ),
            None,
        )
        if pending is not None:
            return deepcopy(pending)
        last = (
            next(
                (
                    o
                    for o in state.cached["data"]
                    if o["order_id"] == payload.order_id and o["symbol"] == payload.symbol
                ),
                None,
            )
            if state.cached
            else None
        )
        receipt = {
            "state": "submitting",
            "message": "正在提交撤单请求",
            "requested_at": clock_now_iso(),
            "order": last,
            **payload.model_dump(),
        }
        state.receipts[payload.request_id] = receipt
        try:
            result = await _command(
                account, {"command": "cancel", "symbol": payload.symbol, "order_id": payload.order_id}
            )
            if receipt["state"] not in {"terminal", "failed"}:
                receipt["state"], receipt["message"] = result["state"], result["message"]
                if result.get("order") is not None:
                    receipt["order"] = result["order"]
        except Exception:
            logger.exception("人工撤单结果未知 account_id={} order_id={}", account.id, payload.order_id)
            # 发送后的通信故障可能已经到达柜台，保留幂等记录，绝不自动重试。
            if receipt["state"] not in {"terminal", "failed"}:
                receipt["state"], receipt["message"] = "unknown", "撤单结果待确认，请刷新核对；未自动重试"
        from axile.server.orders_live import orders_hub

        orders_hub.changed(account.id)
        if receipt["state"] in {"submitted", "unknown"}:
            _background(_verify_cancel(account, state, payload.request_id))
        # 已结束的回执保留最近 50 条；未确认请求不淘汰，避免再次提交同一撤单。
        ended = [key for key, r in state.receipts.items() if r["state"] in {"terminal", "failed"}]
        for key in ended[:-50]:
            state.receipts.pop(key)
        return deepcopy(receipt)


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
        if receipt["state"] in {"terminal", "failed"}:
            return
        try:
            result = await _command(
                account, {"command": "verify", "tracked": [(receipt["symbol"], receipt["order_id"])]}
            )
            state.cached = _merge_snapshot(state, _latest_result(account, result))
        except Exception:
            logger.exception("撤单终态核实未完成 account_id={}", account.id)
        if receipt["state"] not in {"terminal", "failed"}:
            receipt["state"], receipt["message"] = "unknown", "撤单终态尚未确认，请核对委托；未自动重发"
        orders_hub.changed(account.id)


async def cancel_account_order(account: AccountContext, payload: OrderCancelRequest) -> dict[str, Any]:
    """提交幂等单笔撤单，HTTP 请求重试不会重复报撤。"""
    if account.trade_channel == TradeChannel.CTP:
        validate_cancel_id(payload.order_id)
    state = _state(account)
    task = _background(_cancel(account, state, payload))
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=35)
    except TimeoutError:
        previous = state.receipts.get(payload.request_id)
        return {
            "state": "unknown",
            "message": "撤单结果待确认；未自动重试",
            "requested_at": clock_now_iso(),
            "order": previous["order"] if previous is not None else None,
            **payload.model_dump(),
        }
