"""账户绩效设置和 WBT 收益对比接口，不操作执行与调度."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response
from starlette.concurrency import run_in_threadpool

from axile.executor.algorithms.utils.clock import clock_async_sleep, clock_monotonic
from axile.server._native import _core
from axile.server.api.routes.account_support import _get_account_or_404
from axile.server.core.db import analysis_store, database_path
from axile.server.db.models.base import now_str
from axile.server.db.models.performance import (
    AccountPerformance,
    PerformanceSettings,
    PerformanceSnapshot,
    RangeKey,
)
from axile.server.native_store import native_operation
from axile.server.performance_analysis import (
    ENGINE_VERSION,
    LOGIC_VERSION,
    enqueue,
    read_snapshot_status,
)
from axile.server.performance_details import CostQuery, _cursor

router = APIRouter()

_NOT_READY = HTTPException(503, detail="绩效尚未就绪，请读取快照状态或稍后重试")


def _native_http_error(error: _core.NativeError) -> HTTPException:
    """将原生查询错误映射为现有 API 错误;绑定常在,无静默回退."""
    status_code = {
        "SNAPSHOT_EXPIRED": 410,
        "INVALID_COST_CURSOR": 400,
        "INVALID_COST_QUERY": 400,
        "INVALID_REQUEST": 422,
    }.get(error.args[0], 503)
    return HTTPException(status_code, detail={"code": error.args[0], "message": error.args[1]})


async def _op(call, *args):
    """在线程池里执行原生查询,错误映射保持既有 HTTP 语义."""
    try:
        return await run_in_threadpool(call, *args)
    except _core.NativeError as error:
        raise _native_http_error(error) from error


async def _account_performance(store: "_core.Store", account_id: int, range_key: RangeKey) -> AccountPerformance:
    """同步计算账户收益;观测事实由发布管线物化,此处不重算投影."""
    result = await _op(_core.account_performance, store, account_id, range_key, False)
    return AccountPerformance.model_validate(result)


@router.patch("/performance-settings/{account_id}", response_model=PerformanceSettings)
async def save_performance_settings(account_id: int, settings: PerformanceSettings) -> PerformanceSettings:
    """原子更新回测配置，不触发账户调度协调."""
    try:
        account = await native_operation(
            "account_update_backtest",
            {"account_id": account_id, "patch": settings.model_dump(mode="json"), "now": now_str()},
        )
    except _core.NativeError as error:
        if error.args[0] == "ACCOUNT_NOT_FOUND":
            raise HTTPException(404, detail="账户不存在") from error
        raise _native_http_error(error) from error
    return PerformanceSettings(
        backtest_weight_type=account["backtest_weight_type"], backtest_fee_rate=account["backtest_fee_rate"]
    )


@router.get("/performance/{account_id}", response_model=AccountPerformance)
async def get_performance(
    account_id: int,
    range: RangeKey = "all",
    include_backtest: bool = True,
) -> AccountPerformance:
    """完整绩效等待当前快照；纯账户收益不依赖回测成功."""
    await _get_account_or_404(account_id)
    path = database_path()
    store = analysis_store()
    deadline = clock_monotonic() + 120
    enqueued = False
    while True:
        status = await read_snapshot_status(path, account_id)
        if status == "ready":
            try:
                async with asyncio.timeout(deadline - clock_monotonic()):
                    return await _op(
                        _core.performance_result,
                        store,
                        account_id,
                        range,
                        include_backtest,
                        LOGIC_VERSION,
                        ENGINE_VERSION,
                    )
            except _core.NativeError as error:
                # ready 之后仍可能因并发发布失效;无回测请求直接转同步计算。
                if error.args[0] != "SNAPSHOT_NOT_READY" or include_backtest:
                    if error.args[0] == "SNAPSHOT_NOT_READY":
                        pass
                    else:
                        raise _native_http_error(error) from error
            except TimeoutError as error:
                raise _NOT_READY from error
        if status == "failed":
            raise _NOT_READY
        if not include_backtest:
            return await _account_performance(store, account_id, range)
        if clock_monotonic() >= deadline:
            raise _NOT_READY
        if not enqueued and status != "ready":
            await enqueue(path, account_id)
            enqueued = True
        await clock_async_sleep(min(0.1, max(0.0, deadline - clock_monotonic())))


@router.get("/performance/{account_id}/snapshot", response_model=PerformanceSnapshot)
async def get_snapshot(account_id: int, range: RangeKey = "all") -> dict:
    """只读取已发布结果及更新状态，不触达历史大 JSON."""
    await _get_account_or_404(account_id)
    return await _op(_core.snapshot, analysis_store(), account_id, range, LOGIC_VERSION, ENGINE_VERSION)


@router.post("/performance/{account_id}/refresh", status_code=202)
async def refresh_performance(account_id: int) -> dict:
    """幂等排队；已有成功结果在新批次发布前保持可读."""
    await _get_account_or_404(account_id)
    await enqueue(database_path(), account_id, force=True)
    return {"status": "pending", "account_id": account_id}


@router.get("/performance/{account_id}/costs", response_model=None)
async def get_costs(account_id: int, query: Annotated[CostQuery, Query()]) -> Response:
    """按不可变快照版本聚合、排序并分页读取成本投影."""
    await _get_account_or_404(account_id)
    cursor_scope, _ = _cursor(query, account_id)
    content = await _op(
        _core.query_costs,
        analysis_store(),
        account_id,
        query.snapshot_id,
        query.range,
        query.dimension,
        query.sort,
        query.day,
        query.start,
        query.end,
        query.record_id,
        query.symbol,
        query.symbol_search,
        query.side,
        query.limit,
        cursor_scope,
        query.cursor,
        LOGIC_VERSION,
        ENGINE_VERSION,
    )
    return _cost_response(content)


def _cost_response(content: bytes | str) -> Response:
    """原生查询已使用冻结页内证据归一展示，直接返回编码结果。"""
    return Response(content=content, media_type="application/json")
