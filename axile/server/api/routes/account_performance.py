"""账户绩效设置和 WBT 收益对比接口，不操作执行与调度."""

from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import text
from sqlmodel import col, select
from starlette.concurrency import run_in_threadpool

from axile.executor.algorithms.utils.clock import clock_async_sleep, clock_monotonic
from axile.server.api.deps import SessionDep
from axile.server.api.routes.account_support import _get_account_or_404
from axile.server.db.models import ExecuteRecord, PortfolioAccount, TargetWeightSnapshot
from axile.server.db.models.analysis import analysis_snapshot as snapshots
from axile.server.db.models.performance import (
    AccountPerformance,
    PerformanceBinding,
    PerformanceSettings,
    PerformanceSnapshot,
    RangeKey,
)
from axile.server.native_account_performance import native_account_performance
from axile.server.native_analysis import NativeAnalysisError, get_native_analysis
from axile.server.performance import calculate_performance, local_time, observation
from axile.server.performance_analysis import (
    ENGINE_VERSION,
    LOGIC_VERSION,
    enqueue,
    read_snapshot,
    read_snapshot_status,
)
from axile.server.performance_details import CostQuery, _cursor, read_costs

router = APIRouter()


def _native_http_error(error: NativeAnalysisError) -> HTTPException:
    """将原生查询错误映射为现有 API 错误，不在失败后重复计算。"""
    status_code = {
        "SNAPSHOT_EXPIRED": 410,
        "INVALID_COST_CURSOR": 400,
        "INVALID_COST_QUERY": 400,
        "INVALID_REQUEST": 422,
    }.get(error.code, 503)
    return HTTPException(status_code, detail={"code": error.code, "message": str(error)})


async def _native_response(session, command: str, **fields) -> Response | None:
    """原样传递 Rust 编码结果，避免逐行模型构造和再次序列化。"""
    try:
        native = get_native_analysis(session)
        if native is None:
            return None
        body = await native.query(command, logic_version=LOGIC_VERSION, engine_version=ENGINE_VERSION, **fields)
    except NativeAnalysisError as error:
        raise _native_http_error(error) from error
    return Response(body, media_type="application/json")


async def _native_ready_performance(session, request: Request, account_id: int, range_key: str, include_backtest: bool):
    """等待小型发布状态，再由 Rust 直接读取已发布结果。"""
    try:
        native = get_native_analysis(session)
    except NativeAnalysisError as error:
        raise _native_http_error(error) from error
    if native is None:
        return None
    status = await read_snapshot_status(session, account_id)
    if status != "ready" and not include_backtest:
        return None
    if status != "ready":
        await enqueue(session, account_id)
        request.app.state.analysis_manager.wake.set()
        deadline = clock_monotonic() + 120
        while status != "ready":
            await session.rollback()
            status = await read_snapshot_status(session, account_id)
            if status == "failed" or clock_monotonic() > deadline:
                raise HTTPException(503, detail="绩效尚未就绪，请读取快照状态或稍后重试")
            if status != "ready":
                await clock_async_sleep(0.1)
    return await _native_response(
        session, "performance_result", account_id=account_id, range=range_key, include_backtest=include_backtest
    )


async def _account_performance(session, account_id: int, range_key: RangeKey) -> AccountPerformance | Response:
    """从同一数据库快照独立计算账户收益，不依赖 WBT 或后台队列."""
    try:
        body = await native_account_performance(session, account_id, range_key)
    except NativeAnalysisError as error:
        raise _native_http_error(error) from error
    if body is not None:
        return Response(body, media_type="application/json")
    await session.rollback()
    await session.execute(text("BEGIN"))
    account = await _get_account_or_404(session, account_id)
    trade_channel = str(account.trade_channel)
    settings = PerformanceSettings.model_validate(account, from_attributes=True)
    records = (
        (await session.execute(select(ExecuteRecord).where(col(ExecuteRecord.account_id) == account_id)))
        .scalars()
        .all()
    )
    targets = (
        (await session.execute(select(TargetWeightSnapshot).where(col(TargetWeightSnapshot.account_id) == account_id)))
        .scalars()
        .all()
    )
    bindings = (
        (await session.execute(select(PortfolioAccount).where(col(PortfolioAccount.account_id) == account_id)))
        .scalars()
        .all()
    )
    fallback = {item.execution_id: item.normalized_weights for item in targets if item.execution_id}
    items = [observation(record, fallback.get(record.execution_id or "")) for record in records]
    binding_values = [(local_time(binding.created_at), binding.portfolio_id) for binding in bindings]
    await session.rollback()
    result = await run_in_threadpool(calculate_performance, items, settings, range_key, False, trade_channel)
    if result.baseline and result.end:
        start, end = local_time(result.baseline), local_time(result.end)
        result.bindings = [
            PerformanceBinding(time=when.isoformat(), portfolio_id=portfolio_id)
            for when, portfolio_id in sorted(binding_values, key=lambda item: item[0])
            if start <= when <= end
        ]
    return result


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
    session: SessionDep, request: Request, account_id: int, range: RangeKey = "all", include_backtest: bool = True
) -> AccountPerformance | Response:
    """完整绩效等待当前快照；纯账户收益不依赖回测成功."""
    await _get_account_or_404(session, account_id)
    native_response = await _native_ready_performance(session, request, account_id, range, include_backtest)
    if native_response is not None:
        return native_response
    snapshot = await read_snapshot(session, account_id, range)
    if snapshot["status"] != "ready" and not include_backtest:
        return await _account_performance(session, account_id, range)
    if snapshot["status"] != "ready":
        await enqueue(session, account_id)
        request.app.state.analysis_manager.wake.set()
        deadline = clock_monotonic() + 120
        while snapshot["status"] != "ready":
            await session.rollback()
            snapshot = await read_snapshot(session, account_id, range)
            if snapshot["status"] == "failed" or clock_monotonic() > deadline:
                raise HTTPException(503, detail="绩效尚未就绪，请读取快照状态或稍后重试")
            if snapshot["status"] != "ready":
                await clock_async_sleep(0.1)
    result = AccountPerformance.model_validate(snapshot["result"])
    if not include_backtest:
        result.backtest_included = False
        result.skips = None
        result.used_record_count = 0
        for point in result.points:
            point.portfolio_return = point.portfolio_daily_return = point.difference = None
    return result


@router.get("/performance/{account_id}/snapshot", response_model=PerformanceSnapshot)
async def get_snapshot(session: SessionDep, account_id: int, range: RangeKey = "all") -> dict | Response:
    """只读取已发布结果及更新状态，不触达历史大 JSON."""
    await _get_account_or_404(session, account_id)
    native_response = await _native_response(session, "snapshot", account_id=account_id, range=range)
    if native_response is not None:
        return native_response
    return await read_snapshot(session, account_id, range)


@router.post("/performance/{account_id}/refresh", status_code=202)
async def refresh_performance(session: SessionDep, request: Request, account_id: int) -> dict:
    """幂等排队；已有成功结果在新批次发布前保持可读."""
    await _get_account_or_404(session, account_id)
    await enqueue(session, account_id, force=True)
    request.app.state.analysis_manager.wake.set()
    return {"status": "pending", "account_id": account_id}


@router.get("/performance/{account_id}/costs", response_model=None)
async def get_costs(session: SessionDep, account_id: int, query: Annotated[CostQuery, Query()]) -> dict | Response:
    """按不可变快照版本聚合、排序并分页读取成本投影."""
    await _get_account_or_404(session, account_id)
    scope, _ = _cursor(query, account_id)
    fact_version = await session.scalar(
        sa.select(snapshots.c.cost_schema_version).where(
            snapshots.c.id == query.snapshot_id, snapshots.c.account_id == account_id
        )
    )
    if fact_version == 0:
        return await read_costs(session, account_id, query)
    native_response = await _native_response(
        session, "costs", account_id=account_id, cursor_scope=scope, **query.model_dump()
    )
    if native_response is not None:
        return native_response
    return await read_costs(session, account_id, query)
