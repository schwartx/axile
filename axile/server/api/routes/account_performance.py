"""账户绩效设置和 WBT 收益对比接口，不操作执行与调度."""

import asyncio
import json
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from axile.executor.algorithms.utils.clock import clock_async_sleep, clock_monotonic
from axile.server._native import _core
from axile.server.api.deps import SessionDep
from axile.server.api.routes.account_support import _get_account_or_404
from axile.server.core.db import analysis_store
from axile.server.db.models.analysis import analysis_snapshot as snapshots
from axile.server.db.models.performance import (
    AccountPerformance,
    PerformanceSettings,
    PerformanceSnapshot,
    RangeKey,
)
from axile.server.execution.legacy_compat import normalize_legacy_result
from axile.server.performance_analysis import (
    ENGINE_VERSION,
    LOGIC_VERSION,
    enqueue,
    read_snapshot_status,
)
from axile.server.performance_details import CostQuery, _cursor, read_costs

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
async def save_performance_settings(
    session: SessionDep, account_id: int, settings: PerformanceSettings
) -> PerformanceSettings:
    """原子更新回测配置，不触发账户调度协调."""
    account = await _get_account_or_404(session, account_id)
    account.backtest_weight_type = settings.backtest_weight_type
    account.backtest_fee_rate = settings.backtest_fee_rate
    session.add(account)
    try:
        await session.commit()
        await session.refresh(account)
    except Exception:
        await session.rollback()
        raise
    return PerformanceSettings.model_validate(account, from_attributes=True)


@router.get("/performance/{account_id}", response_model=AccountPerformance)
async def get_performance(
    session: SessionDep,
    request: Request,
    account_id: int,
    range: RangeKey = "all",
    include_backtest: bool = True,
) -> AccountPerformance:
    """完整绩效等待当前快照；纯账户收益不依赖回测成功."""
    await _get_account_or_404(session, account_id)
    path = session.get_bind().url.database
    await session.rollback()
    store = analysis_store()
    deadline = clock_monotonic() + 120
    enqueued = False
    while True:
        await session.rollback()
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
async def get_snapshot(session: SessionDep, account_id: int, range: RangeKey = "all") -> dict:
    """只读取已发布结果及更新状态，不触达历史大 JSON."""
    await _get_account_or_404(session, account_id)
    return await _op(_core.snapshot, analysis_store(), account_id, range, LOGIC_VERSION, ENGINE_VERSION)


@router.post("/performance/{account_id}/refresh", status_code=202)
async def refresh_performance(session: SessionDep, account_id: int) -> dict:
    """幂等排队；已有成功结果在新批次发布前保持可读."""
    await _get_account_or_404(session, account_id)
    path = session.get_bind().url.database
    await session.rollback()
    await enqueue(path, account_id, force=True)
    return {"status": "pending", "account_id": account_id}


@router.get("/performance/{account_id}/costs", response_model=None)
async def get_costs(session: SessionDep, account_id: int, query: Annotated[CostQuery, Query()]) -> dict | Response:
    """按不可变快照版本聚合、排序并分页读取成本投影."""
    await _get_account_or_404(session, account_id)
    fact_version = await session.scalar(
        sa.select(snapshots.c.cost_schema_version).where(
            snapshots.c.id == query.snapshot_id, snapshots.c.account_id == account_id
        )
    )
    if fact_version == 0:
        # 旧编码快照仍走 Python 读取;backfill 迁移决策落地后删除。
        return await read_costs(session, account_id, query)
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
    """仅使用冻结页内证据归一展示，不回读当前执行事实。"""
    payload = json.loads(content)
    for item in payload.get("data", []):
        record = item.get("record")
        if not isinstance(record, dict):
            continue
        raw = record.get("raw_result")
        if not isinstance(raw, dict):
            continue
        normalized = normalize_legacy_result(raw)
        symbols = normalized.get("symbol_results", {})
        for symbol in symbols.values():
            if isinstance(symbol, dict):
                symbol.pop("memory", None)
                symbol.pop("technical_detail", None)
        normalized.pop("technical_detail", None)
        record["raw_result"] = normalized
        for attempt in item.get("attempts", []):
            symbol = symbols.get(attempt.get("symbol"), {})
            attempt["reason"] = str(symbol.get("error") or "")[:200]
    return Response(
        content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")), media_type="application/json"
    )
