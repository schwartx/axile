"""账户执行控制相关路由."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
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
    TargetSizingPublic,
    TargetSizingRowPublic,
    TargetWeightSnapshot,
    TargetWeightSnapshotPublic,
)
from axile.server.error_details import exception_details
from axile.server.execution.ctp_channels import reconcile_china_channel_account
from axile.server.execution.legacy_compat import normalize_execution_event_details, normalize_legacy_result
from axile.server.execution.lifecycle import enqueue_empty_positions, enqueue_execute_trade
from axile.server.execution.live import live_hub
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
from axile.server.target_weight_snapshots import (
    append_target_weight_snapshot,
    get_latest_account_target_snapshot,
    get_previous_account_target_snapshots,
    target_snapshot_public,
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
    plugin = get_channel(account.trade_channel)
    plan = plan_executable_target(
        positions,
        snapshot.normalized_weights,
        float(assets.get("total_asset") or 0.0),
        canonicalize_symbol=plugin.canonicalize_symbol,
        quantize_target_quantity=plugin.quantize_target_quantity,
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


type _Canonicalize = Callable[[str], str]


@dataclass
class _SizingEvidence:
    status: str
    rows: dict[str, TargetSizingRowPublic] = field(default_factory=dict)
    quantities: dict[str, float] | None = None
    sizing_execution_id: str | None = None
    sizing_calculated_at: str | None = None
    null_target_quantity: bool = False
    inflight: bool = False


def _canonical_weight_map(
    weights: dict[str, float] | None,
    canonicalize: _Canonicalize,
) -> dict[str, float]:
    if not weights:
        return {}
    return {canonicalize(symbol): float(weight) for symbol, weight in weights.items() if isinstance(symbol, str)}


def _canonical_weights_equal(
    left: dict[str, float] | None,
    right: dict[str, float] | None,
    canonicalize: _Canonicalize,
) -> bool:
    mapped_left = _canonical_weight_map(left, canonicalize)
    mapped_right = _canonical_weight_map(right, canonicalize)
    if not mapped_left or set(mapped_left) != set(mapped_right):
        return False
    return all(
        math.isclose(mapped_left[symbol], mapped_right[symbol], rel_tol=1e-9, abs_tol=1e-12) for symbol in mapped_left
    )


def _complete_quantities(
    rows: dict[str, TargetSizingRowPublic],
    account_weights: dict[str, float],
) -> dict[str, float] | None:
    if not rows or not set(account_weights).issubset(rows):
        return None
    quantities = {symbol: float(row.target_quantity) for symbol, row in rows.items() if row.target_quantity is not None}
    if not set(account_weights).issubset(quantities):
        return None
    return quantities


def _sizing_rows_from_summary(
    summary_artifact: ExecutionArtifact,
    *,
    canonicalize: _Canonicalize,
    strategy_weights: dict[str, float],
    account_weights: dict[str, float],
    target_context: dict[str, object],
) -> dict[str, TargetSizingRowPublic]:
    reconciliation = summary_artifact.content.get("reconciliation")
    symbols = reconciliation.get("symbols") if isinstance(reconciliation, dict) else None
    if not isinstance(symbols, list):
        return {}
    rows: dict[str, TargetSizingRowPublic] = {}
    for item in symbols:
        if not isinstance(item, dict) or not isinstance(item.get("symbol"), str):
            continue
        raw_sizing = item.get("sizing")
        if not isinstance(raw_sizing, dict):
            continue
        symbol = canonicalize(item["symbol"])
        row = cast("dict[str, object]", dict(raw_sizing))
        row["symbol"] = symbol
        row["strategy_weight"] = strategy_weights.get(symbol)
        row["account_weight"] = account_weights.get(symbol, row.get("account_weight"))
        raw_weight = strategy_weights.get(symbol)
        account_weight = account_weights.get(symbol)
        row["account_multiplier"] = (
            account_weight / raw_weight if raw_weight not in (None, 0.0) and account_weight is not None else None
        )
        row["weight_precision"] = target_context.get("weight_precision")
        rows[symbol] = TargetSizingRowPublic.model_validate(row)
    return rows


async def _load_execution_artifacts(session: SessionDep, execution_id: str) -> dict[str, ExecutionArtifact]:
    artifacts = (
        (
            await session.execute(
                select(ExecutionArtifact).where(
                    ExecutionArtifact.execution_id == execution_id,
                    col(ExecutionArtifact.artifact_type).in_(
                        [ExecutionArtifactType.TARGET_SNAPSHOT, ExecutionArtifactType.EXECUTION_SUMMARY]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    return {artifact.artifact_type: artifact for artifact in artifacts if artifact.execution_id == execution_id}


async def _sizing_evidence_for_snapshot(
    session: SessionDep,
    snapshot: TargetWeightSnapshot,
    *,
    canonicalize: _Canonicalize,
    strategy_weights: dict[str, float],
    account_weights: dict[str, float],
) -> _SizingEvidence:
    status = "pending_execution" if snapshot.source == "manual" or not snapshot.execution_id else "unavailable"
    rows: dict[str, TargetSizingRowPublic] = {}
    null_target_quantity = False
    inflight = False
    if snapshot.execution_id:
        by_type = await _load_execution_artifacts(session, snapshot.execution_id)
        target_artifact = by_type.get(ExecutionArtifactType.TARGET_SNAPSHOT)
        summary_artifact = by_type.get(ExecutionArtifactType.EXECUTION_SUMMARY)
        target_context: dict[str, object] = {}
        if target_artifact is not None:
            raw_context = target_artifact.content.get("sizing_context")
            if isinstance(raw_context, dict):
                target_context = cast("dict[str, object]", raw_context)
        if summary_artifact is not None and summary_artifact.schema_version >= 2:
            rows = _sizing_rows_from_summary(
                summary_artifact,
                canonicalize=canonicalize,
                strategy_weights=strategy_weights,
                account_weights=account_weights,
                target_context=target_context,
            )
            status = "available" if rows and set(account_weights).issubset(rows) else "unavailable"
        elif summary_artifact is not None:
            status = "legacy"
        elif target_artifact is not None and target_artifact.schema_version >= 2:
            status = "pending_execution"
            inflight = True
        else:
            status = "legacy"

    quantities: dict[str, float] | None = None
    if status == "available":
        quantities = _complete_quantities(rows, account_weights)
        if quantities is None:
            status = "unavailable"
            null_target_quantity = True
    elif rows:
        null_target_quantity = any(
            symbol in rows and rows[symbol].target_quantity is None for symbol in account_weights
        )

    return _SizingEvidence(
        status=status,
        rows=rows,
        quantities=quantities,
        sizing_execution_id=snapshot.execution_id,
        sizing_calculated_at=snapshot.calculated_at,
        null_target_quantity=null_target_quantity,
        inflight=inflight,
    )


async def _account_target_public(
    session: SessionDep,
    account: object,
    snapshot: TargetWeightSnapshot | None,
) -> TargetWeightSnapshotPublic:
    """返回账户权重与同权重最近一次完整数量换算证据."""
    if snapshot is None:
        return TargetWeightSnapshotPublic()
    plugin = get_channel(getattr(account, "trade_channel"))
    strategy_weights = _canonical_weight_map(snapshot.raw_weights, plugin.canonicalize_symbol)
    account_weights = _canonical_weight_map(snapshot.normalized_weights, plugin.canonicalize_symbol)
    evidence = await _sizing_evidence_for_snapshot(
        session,
        snapshot,
        canonicalize=plugin.canonicalize_symbol,
        strategy_weights=strategy_weights,
        account_weights=account_weights,
    )
    if evidence.status != "available" and not evidence.inflight and snapshot.id is not None and snapshot.account_id:
        previous_snapshots = await get_previous_account_target_snapshots(
            session,
            snapshot.account_id,
            snapshot.portfolio_id,
            before_id=snapshot.id,
        )
        for previous in previous_snapshots:
            if not _canonical_weights_equal(
                previous.normalized_weights,
                snapshot.normalized_weights,
                plugin.canonicalize_symbol,
            ):
                continue
            previous_evidence = await _sizing_evidence_for_snapshot(
                session,
                previous,
                canonicalize=plugin.canonicalize_symbol,
                strategy_weights=strategy_weights,
                account_weights=account_weights,
            )
            if previous_evidence.status == "available" and previous_evidence.quantities is not None:
                evidence = previous_evidence
                break
        else:
            if evidence.status == "unavailable" and not evidence.null_target_quantity:
                evidence.status = "pending_execution"

    return target_snapshot_public(
        snapshot,
        weight_kind="normalized",
        weights=account_weights,
        quantities=evidence.quantities,
        strategy_weights=strategy_weights,
        account_weights=account_weights,
        sizing=TargetSizingPublic(
            status=cast("Any", evidence.status),
            execution_id=evidence.sizing_execution_id,
            calculated_at=evidence.sizing_calculated_at,
            rows=evidence.rows,
        ),
    )


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


# 心跳间隔（秒）：无事件时也定期发注释帧，避免中间层因空闲断连。
_SSE_HEARTBEAT_SEC = 15.0


def _sse_frame(event: str, data: object) -> str:
    """把一帧序列化为 SSE 线格式（``event:`` + ``data:`` + 空行）。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/executions/stream")
async def executions_stream(request: Request) -> StreamingResponse:
    """
    执行实时态 SSE 流（全账户）.

    连上先推一帧 ``snapshot``（当前所有在途执行 + 阶段），随后逐帧转发实时
    ``event``；空闲时按 :data:`_SSE_HEARTBEAT_SEC` 发心跳注释保活。数据方向纯为
    server→client，故用 SSE 而非 WebSocket。前端按 ``account_id`` 自行过滤。

    Notes
    -----
    必须声明在 ``/executions/{execution_id}`` 之前，否则 ``stream`` 会被当作
    ``execution_id`` 路径参数捕获。
    """

    async def _event_stream() -> AsyncIterator[str]:
        async with live_hub.subscribe() as queue:
            # 首帧：当前在途执行快照，保证「刚连上」即正确（冷渲染与 REST 同源）。
            yield _sse_frame("snapshot", live_hub.snapshot())
            while True:
                if await request.is_disconnected():
                    break
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=_SSE_HEARTBEAT_SEC)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                event_name = cast("str", frame.get("type", "event"))
                yield _sse_frame(event_name, frame)

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 关闭反向代理缓冲，确保逐帧实时下发。
            "X-Accel-Buffering": "no",
        },
    )


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
