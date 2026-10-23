"""自定义函数投资组合路由。"""

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Response, status
from loguru import logger

from axile.server._native import _core
from axile.server.db.models import (
    Message,
    Portfolio,
    PortfolioCreate,
    PortfolioListPublic,
    PortfolioPublic,
    PortfolioUpdate,
    TargetWeightSnapshotPublic,
    ValidateCustomCalcRequest,
    ValidateCustomCalcResponse,
)
from axile.server.db.models.account import AccountContext, AccountSnapshot
from axile.server.db.models.base import now_str
from axile.server.db.models.portfolio import RunAndSavePortfolioRequest
from axile.server.error_details import exception_details
from axile.server.execution.rebalance import _normalize_rebalance_target
from axile.server.execution.registry import clear_target_refresh, try_register_target_refresh
from axile.server.native_store import native_operation
from axile.server.portfolio_runner import calculate_sample_portfolio
from axile.server.portfolio_targets import (
    calculate_portfolio_for_account,
    portfolio_result_from_exception,
)
from axile.server.target_weight_snapshots import append_target_weight_snapshot, target_snapshot_public

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


async def _portfolio_native(name: str, request: object) -> Any:
    try:
        return await native_operation(name, request)
    except _core.NativeError as exc:
        code, detail = exc.args
        http_status = {"NOT_FOUND": 404, "CONFLICT": 422, "VALIDATION": 422}.get(code)
        if http_status is None:
            raise
        raise HTTPException(status_code=http_status, detail=detail) from exc


@router.post("/", status_code=status.HTTP_201_CREATED, response_model=PortfolioPublic)
async def create_portfolio(portfolio: PortfolioCreate) -> PortfolioPublic:
    """创建一个由自定义函数计算目标权重的组合。"""
    try:
        value = await _portfolio_native(
            "portfolio_operation", {"op": "create", "portfolio": Portfolio.model_validate(portfolio).model_dump()}
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("创建组合失败: {}", exc)
        raise HTTPException(status_code=500, detail=f"服务器错误: {exc}") from exc
    return PortfolioPublic.model_validate(value)


@router.get("/", response_model=PortfolioListPublic)
async def list_portfolios() -> Response:
    """获取组合列表。"""
    return Response(await _portfolio_native("portfolio_query_json", {"op": "list"}), media_type="application/json")


@router.get("/{portfolio_id:int}", response_model=PortfolioPublic)
async def portfolio_info(portfolio_id: int) -> Response:
    """获取组合详情和当前绑定账户。"""
    return Response(
        await _portfolio_native("portfolio_query_json", {"op": "get", "portfolio_id": portfolio_id}),
        media_type="application/json",
    )


@router.delete("/{portfolio_id:int}")
async def delete_portfolio(portfolio_id: int) -> Message:
    """删除未绑定账户的组合。"""
    return Message.model_validate(
        await _portfolio_native("portfolio_operation", {"op": "delete", "portfolio_id": portfolio_id})
    )


@router.patch("/{portfolio_id:int}", response_model=PortfolioPublic)
async def update_portfolio(portfolio_id: int, portfolio: PortfolioUpdate) -> PortfolioPublic:
    """局部更新组合；自定义函数不可清空。"""
    try:
        value = await _portfolio_native(
            "portfolio_operation",
            {
                "op": "update",
                "portfolio_id": portfolio_id,
                "changes": portfolio.model_dump(exclude_unset=True),
                "updated_at": now_str(),
            },
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("更新组合失败: {}", exc)
        raise HTTPException(status_code=500, detail=f"服务器错误: {exc}") from exc
    return PortfolioPublic.model_validate(value)


async def resolve_portfolio_target(portfolio: Portfolio, account: AccountContext | None) -> dict[str, float]:
    """执行组合函数并返回未经账户杠杆和精度处理的目标权重."""
    try:
        if account is None:
            result = await asyncio.to_thread(calculate_sample_portfolio, portfolio.custom_calc_py_code)
        else:
            result = await calculate_portfolio_for_account(account, portfolio.custom_calc_py_code)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, exception_details(exc, "目标计算失败")) from exc
    if not result.ok or result.target is None:
        error = result.error or ValueError("自定义组合函数执行失败")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=exception_details(error, "自定义组合函数执行失败"),
        )
    return result.target


@router.get("/{portfolio_id:int}/target_snapshot", response_model=TargetWeightSnapshotPublic)
async def portfolio_target_snapshot(portfolio_id: int) -> Response:
    """只读组合最近成功计算的原始目标快照。"""
    return Response(
        await _portfolio_native("portfolio_query_json", {"op": "target_snapshot", "portfolio_id": portfolio_id}),
        media_type="application/json",
    )


@router.post("/{portfolio_id:int}/target_snapshot/refresh", response_model=TargetWeightSnapshotPublic)
async def refresh_portfolio_target_snapshot(portfolio_id: int) -> TargetWeightSnapshotPublic:
    """执行一次组合函数并持久化当前目标。"""
    context = await _portfolio_native("portfolio_operation", {"op": "context", "portfolio_id": portfolio_id})
    portfolio = Portfolio.model_validate(context["portfolio"])
    account_id = context["account_id"]
    account = AccountSnapshot.model_validate(context["account"]) if context["account"] is not None else None
    if not try_register_target_refresh(portfolio_id, account_id):
        raise HTTPException(status_code=409, detail="组合或上下文账户正在执行或刷新，请稍后再试")
    try:
        raw_target = await resolve_portfolio_target(portfolio, account)
        normalized_target = _normalize_rebalance_target(account, raw_target) if account is not None else None
        snapshot = await append_target_weight_snapshot(
            portfolio_id=portfolio_id,
            account_id=account_id,
            raw_weights=raw_target,
            normalized_weights=normalized_target,
            source="manual",
        )
        return target_snapshot_public(snapshot, weight_kind="raw")
    finally:
        clear_target_refresh(portfolio_id, account_id)


async def _run_custom_calc_validation(
    account: AccountContext | None,
    custom_calc_py_code: str,
) -> ValidateCustomCalcResponse:
    """在给定上下文中试跑自定义组合函数并返回结构化结果."""
    try:
        if account is None:
            result = await asyncio.to_thread(calculate_sample_portfolio, custom_calc_py_code)
        else:
            result = await calculate_portfolio_for_account(account, custom_calc_py_code)
    except BaseException as exc:  # noqa: BLE001 - 校验接口统一返回执行器与 IPC 错误
        result = portfolio_result_from_exception(exc)
    if not result.ok or result.target is None:
        error = result.error or portfolio_result_from_exception(ValueError("自定义组合函数执行失败")).error
        assert error is not None
        return ValidateCustomCalcResponse(
            valid=False,
            error=str(error),
            traceback=error.formatted_traceback,
            error_line=error.error_line,
            error_offset=error.error_offset,
            error_type=error.error_type,
            error_message=error.error_message,
        )
    return ValidateCustomCalcResponse(valid=True, target=result.target)


@router.post("/validate_custom_calc", response_model=ValidateCustomCalcResponse)
async def validate_custom_calc(payload: ValidateCustomCalcRequest) -> ValidateCustomCalcResponse:
    """使用样例或真实账户上下文试跑自定义组合函数。"""
    account = None
    if payload.account_id is not None:
        value = await _portfolio_native(
            "portfolio_operation", {"op": "validation_context", "account_id": payload.account_id}
        )
        account = AccountSnapshot.model_validate(value)
    return await _run_custom_calc_validation(account, payload.custom_calc_py_code)


@router.post("/{portfolio_id:int}/run_and_save", response_model=ValidateCustomCalcResponse)
async def run_and_save_portfolio(portfolio_id: int, payload: RunAndSavePortfolioRequest) -> ValidateCustomCalcResponse:
    """只执行一次草稿；通过后由 Rust 原子保存组合与原始快照。"""
    context = await _portfolio_native(
        "portfolio_operation", {"op": "context", "portfolio_id": portfolio_id, "account_id": payload.account_id}
    )
    update = PortfolioUpdate(name=payload.name, custom_calc_py_code=payload.custom_calc_py_code)
    account = AccountSnapshot.model_validate(context["account"]) if context["account"] is not None else None
    if not try_register_target_refresh(portfolio_id, payload.account_id):
        raise HTTPException(status_code=409, detail="组合或上下文账户正在执行或刷新，请稍后再试")
    try:
        result = await _run_custom_calc_validation(account, payload.custom_calc_py_code)
        if result.valid:
            now = now_str()
            await _portfolio_native(
                "portfolio_operation",
                {
                    "op": "save_trial",
                    "portfolio_id": portfolio_id,
                    "changes": update.model_dump(exclude_unset=True),
                    "updated_at": now,
                    "account_id": payload.account_id,
                    "raw_weights": result.target,
                    "normalized_weights": None,
                    "source": "manual",
                    "calculated_at": now,
                },
            )
        return result
    finally:
        clear_target_refresh(portfolio_id, payload.account_id)
