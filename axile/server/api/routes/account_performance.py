"""账户绩效设置和 WBT 收益对比接口，不操作执行与调度."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import text
from sqlmodel import col, select
from starlette.concurrency import run_in_threadpool

from axile.executor.algorithms.utils.clock import clock_async_sleep, clock_monotonic
from axile.server.api.deps import SessionDep
from axile.server.api.routes.account_support import _get_account_or_404
from axile.server.db.models import ExecuteRecord, PortfolioAccount, TargetWeightSnapshot
from axile.server.db.models.performance import (
    AccountPerformance,
    PerformanceBinding,
    PerformanceSettings,
    PerformanceSnapshot,
    RangeKey,
)
from axile.server.performance import calculate_performance, local_time, observation
from axile.server.performance_analysis import enqueue, read_snapshot
from axile.server.performance_details import CostQuery, read_costs

router = APIRouter()


async def _account_performance(session, account_id: int, range_key: RangeKey) -> AccountPerformance:
    """从同一数据库快照独立计算账户收益，不依赖 WBT 或后台队列."""
    await session.rollback()
    await session.execute(text("BEGIN"))
    account = await _get_account_or_404(session, account_id)
    trade_channel = str(account.trade_channel)
    settings = PerformanceSettings.model_validate(account, from_attributes=True)
    targets = (
        await session.execute(
            select(TargetWeightSnapshot.execution_id, TargetWeightSnapshot.normalized_weights).where(
                col(TargetWeightSnapshot.account_id) == account_id
            )
        )
    ).all()
    fallback = {execution_id: weights for execution_id, weights in targets if execution_id}
    bindings = (
        (await session.execute(select(PortfolioAccount).where(col(PortfolioAccount.account_id) == account_id)))
        .scalars()
        .all()
    )
    items = []
    stream = await session.stream_scalars(
        select(ExecuteRecord)
        .where(col(ExecuteRecord.account_id) == account_id)
        .order_by(col(ExecuteRecord.id))
        .execution_options(yield_per=32)
    )
    try:
        async for record in stream:
            items.append(observation(record, fallback.get(record.execution_id or "")))
    finally:
        await stream.close()
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
) -> AccountPerformance:
    """完整绩效等待当前快照；纯账户收益不依赖回测成功."""
    await _get_account_or_404(session, account_id)
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
        result.skips = result.target_skips = None
        result.used_record_count = result.target_used_record_count = 0
        result.sizing_fallback_count = None
        result.sizing_fallback_ranges = []
        for point in result.points:
            point.portfolio_return = point.portfolio_daily_return = point.difference = None
            point.target_portfolio_return = point.target_portfolio_daily_return = None
            point.sizing_fallback = False
    return result


@router.get("/performance/{account_id}/snapshot", response_model=PerformanceSnapshot)
async def get_snapshot(session: SessionDep, account_id: int, range: RangeKey = "all") -> dict:
    """只读取已发布结果及更新状态，不触达历史大 JSON."""
    await _get_account_or_404(session, account_id)
    return await read_snapshot(session, account_id, range)


@router.post("/performance/{account_id}/refresh", status_code=202)
async def refresh_performance(session: SessionDep, request: Request, account_id: int) -> dict:
    """幂等排队；已有成功结果在新批次发布前保持可读."""
    await _get_account_or_404(session, account_id)
    await enqueue(session, account_id, force=True)
    request.app.state.analysis_manager.wake.set()
    return {"status": "pending", "account_id": account_id}


@router.get("/performance/{account_id}/costs")
async def get_costs(session: SessionDep, account_id: int, query: Annotated[CostQuery, Query()]) -> dict:
    """按不可变快照版本聚合、排序并分页读取成本投影."""
    await _get_account_or_404(session, account_id)
    return await read_costs(session, account_id, query)
