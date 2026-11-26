"""账户执行控制相关路由."""

from __future__ import annotations

import math
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Response, status
from loguru import logger

from axile.channels import get_channel
from axile.channels.rebalance_signal import SIGNAL_CHANNEL
from axile.common.serialization import from_payload
from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionTaskStatus
from axile.executor.models.portfolio_target import PortfolioTarget, TargetMode
from axile.server._native import _core
from axile.server.api.deps import HistoryPaginationDep, SchedDep
from axile.server.api.inputs import json_body
from axile.server.api.routes.account_support import (
    _build_empty_positions_algorithm,
    _get_account_or_404,
    ensure_account_current,
    ensure_trading_account,
)
from axile.server.api.routes.portfolio import resolve_portfolio_target, resolve_signal_target_snapshot
from axile.server.business_facts.assets import latest_asset_snapshot
from axile.server.db.models import ExecutionTerminateRequest, Portfolio
from axile.server.error_details import exception_details
from axile.server.execution.ctp_channels import reconcile_china_channel_account
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
from axile.server.target_sizing import read_account_target as _account_target_public
from axile.server.target_weight_snapshots import (
    append_target_weight_snapshot,
    get_latest_account_target_snapshot,
    target_snapshot_public,
)

router = APIRouter()


@router.get("/{account_id}/rebalance_plan", response_model=None)
async def account_rebalance_plan(account_id: int) -> dict[str, Any]:
    """按当前渠道可执行规则返回账户持仓对照，不在前端重算偏离."""
    account = await _get_account_or_404(account_id)
    portfolio_id = await get_latest_portfolio_id_by_account_id(account_id)
    if portfolio_id is None:
        return {
            "rows": [],
            "target_mode": "weight",
            "off_symbol_count": None,
            "observed_at": None,
            "target_calculated_at": None,
        }
    snapshot = await get_latest_account_target_snapshot(account_id, portfolio_id)
    assets_snapshot = await latest_asset_snapshot(account_id)
    target = None if snapshot is None else snapshot.get("normalized_weights")
    if snapshot is None or assets_snapshot is None or not isinstance(target, dict):
        return {
            "rows": [],
            "target_mode": "weight",
            "off_symbol_count": None,
            "observed_at": None if assets_snapshot is None else assets_snapshot["created_at"],
            "target_calculated_at": None if snapshot is None else snapshot["calculated_at"],
        }
    mode: TargetMode = snapshot.get("target_mode", "weight")
    assets = assets_snapshot["assets"]
    total_asset = assets.get("total_asset")
    positions = assets.get("positions")
    if not isinstance(positions, list) or (
        mode == "weight"
        and (
            isinstance(total_asset, bool)
            or not isinstance(total_asset, (int, float))
            or not math.isfinite(total_asset)
            or any(not isinstance(pos, dict) or pos.get("market_value") is None for pos in positions)
        )
    ):
        return {
            "rows": [],
            "target_mode": mode,
            "off_symbol_count": None,
            "observed_at": assets_snapshot["created_at"],
            "target_calculated_at": snapshot["calculated_at"],
        }
    plugin = get_channel(account.trade_channel)
    public_target = await _account_target_public(account, snapshot)
    if public_target["quantities"] is None:
        return {
            "rows": [],
            "target_mode": mode,
            "off_symbol_count": None,
            "observed_at": assets_snapshot["created_at"],
            "target_calculated_at": snapshot["calculated_at"],
        }
    plan = plan_executable_target(
        positions,
        public_target["weights"],
        public_target["quantities"],
        float(total_asset) if isinstance(total_asset, (int, float)) else 0.0,
        target_mode=mode,
        canonicalize_symbol=plugin.canonicalize_symbol,
    )
    return {
        "rows": [
            {
                "symbol": row.symbol,
                "current_weight": row.current_weight,
                "target_weight": row.target_weight,
                "current_quantity": row.current_quantity,
                "target_quantity": row.target_quantity,
                "action": row.action,
                "side": row.side,
                "aligned": row.aligned,
            }
            for row in plan.rows
        ],
        "target_mode": mode,
        "off_symbol_count": plan.off_symbol_count,
        "observed_at": assets_snapshot["created_at"],
        "target_calculated_at": snapshot["calculated_at"],
    }


@router.post("/execute/{account_id}", response_model=None)
async def execute(account_id: int) -> dict[str, Any]:
    """异步触发立即执行，并返回可用于轮询的执行标识."""
    account = await _get_account_or_404(account_id)
    ensure_account_current(account)
    ensure_trading_account(account)

    try:
        submitted = await enqueue_execute_trade(account_id)
    except HTTPException:
        raise
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
    return {
        "message": "已与等待中的执行合并" if submitted.outcome == "coalesced" else "已触发",
        "execution_id": execution_id,
        "account_id": account_id,
        "accepted": submitted.outcome if submitted.outcome in {"created", "coalesced"} else "created",
    }


@router.get("/{account_id}/target_snapshot", response_model=None)
async def account_target_snapshot(account_id: int) -> dict[str, Any]:
    """只读账户当前绑定组合下最近成功计算的目标快照."""
    account = await _get_account_or_404(account_id)
    portfolio_id = await get_latest_portfolio_id_by_account_id(account_id)
    if portfolio_id is None:
        return target_snapshot_public(None, weight_kind="normalized")
    snapshot = await get_latest_account_target_snapshot(account_id, portfolio_id)
    return await _account_target_public(account, snapshot)


@router.post("/{account_id}/target_snapshot/refresh", response_model=None)
async def refresh_account_target_snapshot(account_id: int) -> dict[str, Any]:
    """按用户请求使用真实账户上下文重新计算并保存目标权重."""
    account = await _get_account_or_404(account_id)
    ensure_account_current(account)
    try:
        context = await native_operation("portfolio_operation", {"op": "bound_context", "account_id": account_id})
    except _core.NativeError as exc:
        if exc.args[0] in {"ARCHIVED", "ACCOUNT_CONFLICT"}:
            raise HTTPException(409, detail=exc.args[1]) from exc
        raise
    portfolio_id = context["portfolio_id"]
    if portfolio_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="账户未绑定组合")
    portfolio = context["portfolio"]
    if portfolio is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="账户绑定的组合不存在")
    if not try_register_target_refresh(portfolio_id, account_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="账户正在执行或刷新，请稍后再试")

    try:
        model = from_payload(Portfolio, portfolio)
        if account.trade_channel == SIGNAL_CHANNEL:
            fields = await resolve_signal_target_snapshot(model, account)
        else:
            raw_target = PortfolioTarget.from_result(await resolve_portfolio_target(model, account))
            fields = {
                "raw_weights": raw_target.targets,
                "target_mode": raw_target.mode,
                "normalized_weights": _normalize_rebalance_target(
                    account, raw_target.targets, target_mode=raw_target.mode
                ),
            }
        snapshot = await append_target_weight_snapshot(
            portfolio_id=portfolio_id, account_id=account_id, source="manual", **fields
        )
        return await _account_target_public(account, snapshot)
    finally:
        clear_target_refresh(portfolio_id, account_id)


@router.get("/executions/{execution_id}", response_model=None)
async def execution_status(execution_id: str) -> dict[str, Any]:
    """查询指定执行标识对应的任务状态."""
    status_payload = await get_execution_status(execution_id)
    if status_payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="执行不存在",
        )

    from axile.server.execution.live import live_hub

    return {
        "progress": live_hub.execution_progress(cast(int, status_payload["account_id"]), execution_id),
        "execution_id": status_payload["execution_id"],
        "account_id": status_payload["account_id"],
        "execution_kind": status_payload.get("execution_kind"),
        "status": status_payload["status"],
        "created_at": status_payload["created_at"],
        "started_at": status_payload.get("started_at"),
        "finished_at": status_payload.get("finished_at"),
        "error": status_payload.get("error"),
        "output_status": status_payload.get("output_status"),
        "outcome": status_payload.get("outcome"),
        "outcome_reason": status_payload.get("outcome_reason"),
        "outcome_symbols": status_payload.get("outcome_symbols", []),
        "record_id": status_payload.get("record_id"),
        "is_success": status_payload.get("is_success"),
        "cancel_requested_at": status_payload.get("cancel_requested_at"),
        "cancel_reason": status_payload.get("cancel_reason"),
        "terminate_mode": status_payload.get("terminate_mode"),
    }


@router.post("/{account_id}/terminate", response_model=None)
async def terminate_account_execution(
    account_id: int,
    payload: Annotated[ExecutionTerminateRequest, Depends(json_body(ExecutionTerminateRequest))],
    response: Response,
) -> dict[str, Any]:
    """终止账户当前正在运行的执行任务."""
    from axile.server.execution.supplements import account_lock

    async with account_lock(account_id):
        return await _terminate_account_execution(account_id, payload, response)


async def _terminate_account_execution(
    account_id: int,
    payload: ExecutionTerminateRequest,
    response: Response,
) -> dict[str, Any]:
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
            return {"message": "已取消剩余补发", "account_id": account_id, "execution_id": None, "status": None}
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="账户当前没有活跃执行")

    response_status = status.HTTP_200_OK
    if current_task_status in {ExecutionTaskStatus.QUEUED, ExecutionTaskStatus.RUNNING}:
        response_status = status.HTTP_202_ACCEPTED
    response.status_code = response_status

    return {
        "message": "已受理终止请求",
        "account_id": account_id,
        "execution_id": state.execution_id,
        "status": state.status,
    }


@router.get("/executions/{execution_id}/detail", response_model=None)
async def execution_detail(execution_id: str, account_id: int) -> dict[str, object] | None:
    """按账户归属读取冻结事实，并临时计算执行详情。"""
    record = await native_operation(
        "execution_read", {"op": "detail", "execution_id": execution_id, "account_id": account_id}
    )
    if record is None:
        return None
    return record["view"]


@router.get("/executions/{execution_id}/events", response_model=None)
async def execution_events(
    execution_id: str,
    pagination: HistoryPaginationDep,
) -> Response:
    """查询指定执行标识对应的事件流."""
    content = await native_operation(
        "list_execution_events_json", {"execution_id": execution_id, "skip": pagination.skip, "limit": pagination.limit}
    )
    return Response(content=content, media_type="application/json")


@router.get("/executions/{execution_id}/artifacts", response_model=None)
async def execution_artifacts(
    execution_id: str,
    pagination: HistoryPaginationDep,
) -> Response:
    """查询指定执行标识对应的执行附件."""
    content = await native_operation(
        "list_execution_artifacts_json",
        {"execution_id": execution_id, "skip": pagination.skip, "limit": pagination.limit},
    )
    return Response(content=content, media_type="application/json")


@router.post("/empty_positions/{account_id}", response_model=None)
async def empty_all_positions(
    sched: SchedDep,
    account_id: int,
    algorithm_method: str | None = None,
    algorithm_params: str | None = None,
) -> dict[str, Any]:
    """异步触发一键平仓，并返回可用于轮询的执行标识."""
    db_account = await _get_account_or_404(account_id)
    ensure_account_current(db_account)
    ensure_trading_account(db_account)

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
    if db_account.trade_channel in {TradeChannel("ctp"), TradeChannel("tq")}:
        await reconcile_china_channel_account(db_account)

    try:
        submitted = await enqueue_empty_positions(account_id, algorithm=algorithm)
    except HTTPException:
        raise
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
    return {
        "message": "已与等待中的清仓合并" if submitted.outcome == "coalesced" else "已触发清仓",
        "execution_id": execution_id,
        "account_id": account_id,
        "accepted": submitted.outcome if submitted.outcome in {"created", "coalesced"} else "created",
    }
