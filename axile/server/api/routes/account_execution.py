"""账户执行控制相关路由."""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, HTTPException, Response, status
from loguru import logger
from sqlmodel import col, func, select

from axile.channels import get_channel
from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionArtifactType, ExecutionTaskStatus
from axile.server.api.deps import HistoryPaginationDep, SchedDep, SessionDep
from axile.server.api.routes.account_support import _get_account_or_404
from axile.server.api.routes.portfolio import resolve_portfolio_target
from axile.server.asset_observations import valid_asset_snapshot_condition
from axile.server.db.models import (
    AccountAssetSnapshot,
    AccountRebalancePlanPublic,
    AccountRebalancePlanRowPublic,
    ExecutionArtifact,
    ExecutionArtifactListPublic,
    ExecutionArtifactPublic,
    ExecutionEvent,
    ExecutionEventListPublic,
    ExecutionEventPublic,
    ExecutionStatusPublic,
    ExecutionTerminateRequest,
    ExecutionTerminateResponse,
    ExecutionTriggerResponse,
    Portfolio,
    TargetWeightSnapshot,
    TargetWeightSnapshotPublic,
)
from axile.server.error_details import exception_details
from axile.server.execution.ctp_channels import reconcile_china_channel_account
from axile.server.execution.legacy_compat import normalize_execution_event_details, normalize_legacy_result
from axile.server.execution.lifecycle import enqueue_empty_positions, enqueue_execute_trade
from axile.server.execution.rebalance import _normalize_rebalance_target
from axile.server.execution.registry import (
    AccountExecutionAlreadyRunningError,
    clear_target_refresh,
    get_execution_status,
    get_queued_execution_id,
    get_running_execution_id,
    terminate_running_account_execution,
    try_register_target_refresh,
)
from axile.server.execution.scheduler import delete_job
from axile.server.integrity import plan_executable_target
from axile.server.repositories import get_latest_portfolio_id_by_account_id
from axile.server.target_sizing import read_account_target_snapshots
from axile.server.target_weight_snapshots import (
    append_target_weight_snapshot,
    get_latest_account_target_snapshot,
)

router = APIRouter()


@router.get("/{account_id}/rebalance_plan", response_model=AccountRebalancePlanPublic)
async def account_rebalance_plan(session: SessionDep, account_id: int) -> AccountRebalancePlanPublic:
    """按当前渠道可执行规则返回账户持仓对照，不在前端重算偏离."""
    account = await _get_account_or_404(session, account_id)
    portfolio_id = await get_latest_portfolio_id_by_account_id(session, account_id)
    if portfolio_id is None:
        return AccountRebalancePlanPublic()
    snapshot = await get_latest_account_target_snapshot(session, account_id, portfolio_id)
    assets_snapshot = (
        await session.execute(
            select(AccountAssetSnapshot)
            .where(AccountAssetSnapshot.account_id == account_id, valid_asset_snapshot_condition())
            .order_by(col(AccountAssetSnapshot.id).desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if snapshot is None or assets_snapshot is None or not isinstance(snapshot.normalized_weights, dict):
        return AccountRebalancePlanPublic(
            observed_at=None if assets_snapshot is None else assets_snapshot.created_at,
            target_calculated_at=None if snapshot is None else snapshot.calculated_at,
        )
    assets = assets_snapshot.assets
    raw_positions = assets.get("positions")
    positions = raw_positions if isinstance(raw_positions, list) else []
    if any(not isinstance(pos, dict) or pos.get("market_value") is None for pos in positions):
        return AccountRebalancePlanPublic(
            observed_at=assets_snapshot.created_at,
            target_calculated_at=snapshot.calculated_at,
        )
    target = await _account_target_public(session, account, snapshot)
    if target.quantities is None:
        return AccountRebalancePlanPublic(
            observed_at=assets_snapshot.created_at,
            target_calculated_at=snapshot.calculated_at,
        )
    plugin = get_channel(account.trade_channel)
    plan = plan_executable_target(
        positions,
        target.weights,
        target.quantities,
        float(assets.get("total_asset") or 0.0),
        canonicalize_symbol=plugin.canonicalize_symbol,
    )
    return AccountRebalancePlanPublic(
        rows=[
            AccountRebalancePlanRowPublic(
                symbol=row.symbol,
                current_weight=row.current_weight,
                target_weight=row.target_weight,
                current_quantity=row.current_quantity,
                target_quantity=row.target_quantity,
                action=row.action,
                side=row.side,
                aligned=row.aligned,
            )
            for row in plan.rows
        ],
        off_symbol_count=plan.off_symbol_count,
        observed_at=assets_snapshot.created_at,
        target_calculated_at=snapshot.calculated_at,
    )


async def _account_target_public(
    session: SessionDep,
    account: object,
    snapshot: TargetWeightSnapshot | None,
) -> TargetWeightSnapshotPublic:
    """读取账户目标及其完整换算证据。"""
    if snapshot is None:
        return TargetWeightSnapshotPublic()
    account_id = getattr(account, "id")
    plugin = get_channel(getattr(account, "trade_channel"))
    targets = await read_account_target_snapshots(
        session, {account_id: snapshot}, {account_id: plugin.canonicalize_symbol}
    )
    return targets[account_id]


def _account_route_module() -> Any:
    from axile.server.api.routes import account as account_routes

    return account_routes


@router.post("/execute/{account_id}", response_model=ExecutionTriggerResponse)
async def execute(session: SessionDep, account_id: int) -> ExecutionTriggerResponse:
    """异步触发立即执行，并返回可用于轮询的执行标识."""
    await _get_account_or_404(session, account_id)

    try:
        submitted = await enqueue_execute_trade(account_id)
    except AccountExecutionAlreadyRunningError as exc:
        logger.warning(f"执行交易冲突: account_id={account_id}, error={exc}")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception(f"执行交易失败: account_id={account_id}, error={exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=exception_details(exc, "执行请求失败"),
        ) from exc

    execution_id = submitted.execution_id
    if execution_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"账户 {account_id} 已有调仓任务在执行中",
        )
    return ExecutionTriggerResponse(
        message="已与等待中的执行合并" if submitted.outcome == "coalesced" else "已触发",
        execution_id=execution_id,
        account_id=account_id,
        accepted=submitted.outcome if submitted.outcome in {"created", "coalesced"} else "created",
    )


@router.get("/{account_id}/target_snapshot", response_model=TargetWeightSnapshotPublic)
async def account_target_snapshot(session: SessionDep, account_id: int) -> TargetWeightSnapshotPublic:
    """只读账户当前绑定组合下最近成功计算的目标快照."""
    account = await _get_account_or_404(session, account_id)
    portfolio_id = await get_latest_portfolio_id_by_account_id(session, account_id)
    if portfolio_id is None:
        return TargetWeightSnapshotPublic()
    snapshot = await get_latest_account_target_snapshot(session, account_id, portfolio_id)
    return await _account_target_public(session, account, snapshot)


@router.post("/{account_id}/target_snapshot/refresh", response_model=TargetWeightSnapshotPublic)
async def refresh_account_target_snapshot(session: SessionDep, account_id: int) -> TargetWeightSnapshotPublic:
    """按用户请求使用真实账户上下文重新计算并保存目标权重."""
    account = await _get_account_or_404(session, account_id)
    portfolio_id = await get_latest_portfolio_id_by_account_id(session, account_id)
    if portfolio_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="账户未绑定组合")
    portfolio = await session.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="账户绑定的组合不存在")
    if not try_register_target_refresh(portfolio_id, account_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="账户正在执行或刷新，请稍后再试")

    try:
        await session.close()
        raw_target = await resolve_portfolio_target(portfolio, account)
        normalized_target = _normalize_rebalance_target(account, raw_target)
        snapshot = await append_target_weight_snapshot(
            session,
            portfolio_id=portfolio_id,
            account_id=account_id,
            raw_weights=raw_target,
            normalized_weights=normalized_target,
            source="manual",
        )
        return await _account_target_public(session, account, snapshot)
    finally:
        clear_target_refresh(portfolio_id, account_id)


@router.get("/executions/{execution_id}", response_model=ExecutionStatusPublic)
async def execution_status(execution_id: str) -> ExecutionStatusPublic:
    """查询指定执行标识对应的任务状态."""
    status_payload = await get_execution_status(execution_id)
    if status_payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="执行不存在",
        )

    return ExecutionStatusPublic.model_validate(status_payload)


@router.post("/{account_id}/terminate", response_model=ExecutionTerminateResponse)
async def terminate_account_execution(
    session: SessionDep,
    account_id: int,
    payload: ExecutionTerminateRequest,
    response: Response,
) -> ExecutionTerminateResponse:
    """终止账户当前正在运行的执行任务."""
    from axile.server.execution.supplements import account_lock

    async with account_lock(account_id):
        return await _terminate_account_execution(session, account_id, payload, response)


async def _terminate_account_execution(
    session: SessionDep,
    account_id: int,
    payload: ExecutionTerminateRequest,
    response: Response,
) -> ExecutionTerminateResponse:
    """终止账户当前正在运行的执行任务."""
    await _get_account_or_404(session, account_id)

    running_execution_id = get_running_execution_id(account_id) or get_queued_execution_id(account_id)
    current_status = None if running_execution_id is None else await get_execution_status(running_execution_id)
    current_task_status = None if current_status is None else cast("ExecutionTaskStatus", current_status["status"])

    from axile.server.execution.supplements import cancel_groups_locked

    cancelled_supplement = await cancel_groups_locked(account_id, payload.reason or "terminated")

    state = await terminate_running_account_execution(
        account_id,
        reason=payload.reason,
        mode=payload.mode,
    )
    if state is None:
        if cancelled_supplement:
            response.status_code = status.HTTP_200_OK
            return ExecutionTerminateResponse(message="已取消剩余补发", account_id=account_id)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="账户当前没有活跃执行")

    response_status = status.HTTP_200_OK
    if current_task_status in {ExecutionTaskStatus.QUEUED, ExecutionTaskStatus.RUNNING}:
        response_status = status.HTTP_202_ACCEPTED
    response.status_code = response_status

    return ExecutionTerminateResponse(
        message="已受理终止请求",
        account_id=account_id,
        execution_id=state.execution_id,
        status=state.status,
    )


@router.get("/executions/{execution_id}/events", response_model=ExecutionEventListPublic)
async def execution_events(
    session: SessionDep,
    execution_id: str,
    pagination: HistoryPaginationDep,
) -> ExecutionEventListPublic:
    """查询指定执行标识对应的事件流."""
    count_stmt = select(func.count()).select_from(ExecutionEvent).where(ExecutionEvent.execution_id == execution_id)
    total = (await session.execute(count_stmt)).scalar_one()
    stmt = (
        select(ExecutionEvent)
        .where(ExecutionEvent.execution_id == execution_id)
        # 按 id（自增=落库/发生顺序）排序，而非 seq：终止类事件 seq=0 会排到 started(seq=1) 之前，
        # 令接口原始序列读作「先终止后开始」。id 单调递增即真实时序，正常事件的 seq 顺序与之一致。
        .order_by(cast("Any", ExecutionEvent.id))
        .offset(pagination.skip)
        .limit(pagination.limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    data = [
        # 旧事件的 debug.error 读时提升到公共层；技术原文不提升。
        ExecutionEventPublic.model_validate(row).model_copy(
            update={"details": normalize_execution_event_details(row.details)}
        )
        for row in rows
    ]
    return ExecutionEventListPublic(
        data=data,
        count=total,
    )


@router.get("/executions/{execution_id}/artifacts", response_model=ExecutionArtifactListPublic)
async def execution_artifacts(
    session: SessionDep,
    execution_id: str,
    pagination: HistoryPaginationDep,
) -> ExecutionArtifactListPublic:
    """查询指定执行标识对应的执行附件."""
    count_stmt = (
        select(func.count()).select_from(ExecutionArtifact).where(ExecutionArtifact.execution_id == execution_id)
    )
    total = (await session.execute(count_stmt)).scalar_one()
    stmt = (
        select(ExecutionArtifact)
        .where(ExecutionArtifact.execution_id == execution_id)
        .order_by(cast("Any", ExecutionArtifact.id))
        .offset(pagination.skip)
        .limit(pagination.limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    artifact_data: list[ExecutionArtifactPublic] = []
    for row in rows:
        item = ExecutionArtifactPublic.model_validate(row)
        content = item.content
        # 旧执行摘要附件读时归一；其他附件形状不同，不做推断。
        if row.artifact_type == ExecutionArtifactType.EXECUTION_SUMMARY and isinstance(content, dict):
            item = item.model_copy(update={"content": normalize_legacy_result(content)})
        artifact_data.append(item)
    return ExecutionArtifactListPublic(
        data=artifact_data,
        count=total,
    )


@router.post("/empty_positions/{account_id}", response_model=ExecutionTriggerResponse)
async def empty_all_positions(
    session: SessionDep,
    sched: SchedDep,
    account_id: int,
    algorithm_method: str | None = None,
    algorithm_params: str | None = None,
) -> ExecutionTriggerResponse:
    """异步触发一键平仓，并返回可用于轮询的执行标识."""
    db_account = await _get_account_or_404(session, account_id)
    account_routes = _account_route_module()

    try:
        algorithm = account_routes._build_empty_positions_algorithm(
            algorithm_method=algorithm_method,
            algorithm_params=algorithm_params,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    # 先停调度，再入队一次性清仓，避免 CRON 在人工清仓期间再次拉起执行。
    from axile.server.execution.supplements import cancel_groups_in_session

    await cancel_groups_in_session(session, account_id, "account_stopped")
    db_account.sqlmodel_update({"is_started": False})
    session.add(db_account)
    await session.commit()
    delete_job(sched, account_id)
    if db_account.trade_channel in {TradeChannel.CTP, TradeChannel.TQ}:
        await reconcile_china_channel_account(db_account.snapshot())

    try:
        submitted = await enqueue_empty_positions(account_id, algorithm=algorithm)
    except AccountExecutionAlreadyRunningError as exc:
        logger.warning(f"清仓冲突: account_id={account_id}, error={exc}")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception(f"清仓失败: account_id={account_id}, error={exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=exception_details(exc, "执行请求失败"),
        ) from exc

    execution_id = submitted.execution_id
    if execution_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"账户 {account_id} 已有调仓任务在执行中",
        )
    return ExecutionTriggerResponse(
        message="已与等待中的清仓合并" if submitted.outcome == "coalesced" else "已触发清仓",
        execution_id=execution_id,
        account_id=account_id,
        accepted=submitted.outcome if submitted.outcome in {"created", "coalesced"} else "created",
    )
