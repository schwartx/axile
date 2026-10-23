"""账户执行控制相关路由."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Response, status
from loguru import logger
from pydantic import ValidationError

from axile.channels import get_channel
from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionArtifactType, ExecutionTaskStatus
from axile.server.api.deps import HistoryPaginationDep, SchedDep
from axile.server.api.routes.account_support import _build_empty_positions_algorithm, _get_account_or_404
from axile.server.api.routes.portfolio import resolve_portfolio_target
from axile.server.business_facts.assets import latest_asset_snapshot
from axile.server.db.models import (
    AccountRebalancePlanPublic,
    AccountRebalancePlanRowPublic,
    ExecutionArtifactListPublic,
    ExecutionArtifactPublic,
    ExecutionEventListPublic,
    ExecutionEventPublic,
    ExecutionStatusPublic,
    ExecutionTerminateRequest,
    ExecutionTerminateResponse,
    ExecutionTriggerResponse,
    Portfolio,
    TargetSizingPublic,
    TargetSizingRowPublic,
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
from axile.server.native_store import native_operation
from axile.server.repositories import get_latest_portfolio_id_by_account_id, stop_account_for_clear_positions
from axile.server.target_weight_snapshots import (
    TargetWeightSnapshotData,
    append_target_weight_snapshot,
    get_latest_account_target_snapshot,
    get_previous_account_target_snapshots,
    target_snapshot_public,
)

router = APIRouter()


@router.get("/{account_id}/rebalance_plan", response_model=AccountRebalancePlanPublic)
async def account_rebalance_plan(account_id: int) -> AccountRebalancePlanPublic:
    """按当前渠道可执行规则返回账户持仓对照，不在前端重算偏离."""
    account = await _get_account_or_404(account_id)
    portfolio_id = await get_latest_portfolio_id_by_account_id(account_id)
    if portfolio_id is None:
        return AccountRebalancePlanPublic()
    snapshot = await get_latest_account_target_snapshot(account_id, portfolio_id)
    assets_snapshot = await latest_asset_snapshot(account_id)
    if snapshot is None or assets_snapshot is None or not isinstance(snapshot.normalized_weights, dict):
        return AccountRebalancePlanPublic(
            observed_at=None if assets_snapshot is None else assets_snapshot["created_at"],
            target_calculated_at=None if snapshot is None else snapshot.calculated_at,
        )
    assets = assets_snapshot["assets"]
    total_asset = assets.get("total_asset")
    raw_positions = assets.get("positions")
    positions = raw_positions if isinstance(raw_positions, list) else []
    if (
        isinstance(total_asset, bool)
        or not isinstance(total_asset, (int, float))
        or not math.isfinite(total_asset)
        or any(not isinstance(pos, dict) or pos.get("market_value") is None for pos in positions)
    ):
        return AccountRebalancePlanPublic(
            observed_at=assets_snapshot["created_at"],
            target_calculated_at=snapshot.calculated_at,
        )
    plugin = get_channel(account.trade_channel)
    plan = plan_executable_target(
        positions,
        snapshot.normalized_weights,
        float(total_asset),
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
        observed_at=assets_snapshot["created_at"],
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


def _sizing_rows_from_facts(
    sizings: dict[str, dict[str, object]],
    *,
    canonicalize: _Canonicalize,
    strategy_weights: dict[str, float],
    account_weights: dict[str, float],
    target_context: dict[str, object],
) -> dict[str, TargetSizingRowPublic]:
    """从冻结换算列构造展示，不读取审计附件内重复保存的结果。"""
    rows: dict[str, TargetSizingRowPublic] = {}
    for map_key, raw_sizing in sizings.items():
        symbol = canonicalize(map_key)
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
        try:
            rows[symbol] = TargetSizingRowPublic.model_validate(row)
        except ValidationError:
            # 不完整历史证据保持不可用；不得套用当前规则补填历史换算。
            continue
    return rows


async def _sizing_evidence_for_snapshot(
    snapshot: TargetWeightSnapshotData,
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
        evidence = await native_operation(
            "execution_read",
            {"op": "target_sizing", "execution_id": snapshot.execution_id, "account_id": snapshot.account_id},
        )
        versions = evidence["artifact_versions"]
        input_id = evidence["input_id"]
        if evidence["record_id"] is not None:
            rows = _sizing_rows_from_facts(
                evidence["sizings"],
                canonicalize=canonicalize,
                strategy_weights=strategy_weights,
                account_weights=account_weights,
                target_context=evidence["target_context"],
            )
            status = "available" if rows and set(account_weights).issubset(rows) else "unavailable"
            if not rows and versions.get(ExecutionArtifactType.EXECUTION_SUMMARY, 1) < 2:
                status = "legacy"
        elif ExecutionArtifactType.EXECUTION_SUMMARY in versions:
            status = (
                "pending_execution"
                if input_id is not None or versions[ExecutionArtifactType.EXECUTION_SUMMARY] >= 2
                else "legacy"
            )
            inflight = status == "pending_execution"
        elif input_id is not None or versions.get(ExecutionArtifactType.TARGET_SNAPSHOT, 0) >= 2:
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
    account: object,
    snapshot: TargetWeightSnapshotData | None,
) -> TargetWeightSnapshotPublic:
    """返回账户权重与同权重最近一次完整数量换算证据."""
    if snapshot is None:
        return TargetWeightSnapshotPublic()
    plugin = get_channel(getattr(account, "trade_channel"))
    strategy_weights = _canonical_weight_map(snapshot.raw_weights, plugin.canonicalize_symbol)
    account_weights = _canonical_weight_map(snapshot.normalized_weights, plugin.canonicalize_symbol)
    evidence = await _sizing_evidence_for_snapshot(
        snapshot,
        canonicalize=plugin.canonicalize_symbol,
        strategy_weights=strategy_weights,
        account_weights=account_weights,
    )
    if evidence.status != "available" and not evidence.inflight and snapshot.id is not None and snapshot.account_id:
        previous_snapshots = await get_previous_account_target_snapshots(
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


@router.post("/execute/{account_id}", response_model=ExecutionTriggerResponse)
async def execute(account_id: int) -> ExecutionTriggerResponse:
    """异步触发立即执行，并返回可用于轮询的执行标识."""
    await _get_account_or_404(account_id)

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
async def account_target_snapshot(account_id: int) -> TargetWeightSnapshotPublic:
    """只读账户当前绑定组合下最近成功计算的目标快照."""
    account = await _get_account_or_404(account_id)
    portfolio_id = await get_latest_portfolio_id_by_account_id(account_id)
    if portfolio_id is None:
        return TargetWeightSnapshotPublic()
    snapshot = await get_latest_account_target_snapshot(account_id, portfolio_id)
    return await _account_target_public(account, snapshot)


@router.post("/{account_id}/target_snapshot/refresh", response_model=TargetWeightSnapshotPublic)
async def refresh_account_target_snapshot(account_id: int) -> TargetWeightSnapshotPublic:
    """按用户请求使用真实账户上下文重新计算并保存目标权重."""
    account = await _get_account_or_404(account_id)
    context = await native_operation("portfolio_operation", {"op": "bound_context", "account_id": account_id})
    portfolio_id = context["portfolio_id"]
    if portfolio_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="账户未绑定组合")
    portfolio = context["portfolio"]
    if portfolio is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="账户绑定的组合不存在")
    if not try_register_target_refresh(portfolio_id, account_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="账户正在执行或刷新，请稍后再试")

    try:
        raw_target = await resolve_portfolio_target(Portfolio.model_validate(portfolio), account)
        normalized_target = _normalize_rebalance_target(account, raw_target)
        snapshot = await append_target_weight_snapshot(
            portfolio_id=portfolio_id,
            account_id=account_id,
            raw_weights=raw_target,
            normalized_weights=normalized_target,
            source="manual",
        )
        return await _account_target_public(account, snapshot)
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
    account_id: int,
    payload: ExecutionTerminateRequest,
    response: Response,
) -> ExecutionTerminateResponse:
    """终止账户当前正在运行的执行任务."""
    from axile.server.execution.supplements import account_lock

    async with account_lock(account_id):
        return await _terminate_account_execution(account_id, payload, response)


async def _terminate_account_execution(
    account_id: int,
    payload: ExecutionTerminateRequest,
    response: Response,
) -> ExecutionTerminateResponse:
    """终止账户当前正在运行的执行任务."""
    await _get_account_or_404(account_id)

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
    execution_id: str,
    pagination: HistoryPaginationDep,
) -> ExecutionEventListPublic:
    """查询指定执行标识对应的事件流."""
    page = await native_operation(
        "list_execution_events", {"execution_id": execution_id, "skip": pagination.skip, "limit": pagination.limit}
    )
    data = [
        ExecutionEventPublic.model_validate(row).model_copy(
            update={"details": normalize_execution_event_details(row["details"])}
        )
        for row in page["data"]
    ]
    return ExecutionEventListPublic(
        data=data,
        count=page["count"],
    )


@router.get("/executions/{execution_id}/artifacts", response_model=ExecutionArtifactListPublic)
async def execution_artifacts(
    execution_id: str,
    pagination: HistoryPaginationDep,
) -> ExecutionArtifactListPublic:
    """查询指定执行标识对应的执行附件."""
    page = await native_operation(
        "list_execution_artifacts", {"execution_id": execution_id, "skip": pagination.skip, "limit": pagination.limit}
    )
    artifact_data: list[ExecutionArtifactPublic] = []
    for row in page["data"]:
        item = ExecutionArtifactPublic.model_validate(row)
        content = item.content
        if item.artifact_type == ExecutionArtifactType.EXECUTION_SUMMARY and isinstance(content, dict):
            content = normalize_legacy_result(content)
        artifact_data.append(item.model_copy(update={"content": content}))
    return ExecutionArtifactListPublic(
        data=artifact_data,
        count=page["count"],
    )


@router.post("/empty_positions/{account_id}", response_model=ExecutionTriggerResponse)
async def empty_all_positions(
    sched: SchedDep,
    account_id: int,
    algorithm_method: str | None = None,
    algorithm_params: str | None = None,
) -> ExecutionTriggerResponse:
    """异步触发一键平仓，并返回可用于轮询的执行标识."""
    db_account = await _get_account_or_404(account_id)

    try:
        algorithm = _build_empty_positions_algorithm(
            algorithm_method=algorithm_method,
            algorithm_params=algorithm_params,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    # 先停调度，再入队一次性清仓，避免 CRON 在人工清仓期间再次拉起执行。
    db_account = await stop_account_for_clear_positions(account_id)
    delete_job(sched, account_id)
    if db_account.trade_channel in {TradeChannel.CTP, TradeChannel.TQ}:
        await reconcile_china_channel_account(db_account)

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
