"""自定义函数投资组合路由。"""

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Response, status
from loguru import logger

from axile.channels import get_channel
from axile.executor.models.portfolio_target import PortfolioTarget
from axile.server._native import _core
from axile.server.db.models import Portfolio, PortfolioCreate, PortfolioUpdate, ValidateCustomCalcRequest
from axile.server.db.models.account import AccountContext, AccountSnapshot
from axile.server.db.models.base import now_str
from axile.server.db.models.portfolio import RunAndSavePortfolioRequest
from axile.server.error_details import exception_details
from axile.server.execution.rebalance import _normalize_rebalance_target
from axile.server.execution.registry import (
    clear_account_asset_refresh,
    clear_target_refresh,
    try_register_account_asset_refresh,
    try_register_target_refresh,
)
from axile.server.native_store import native_operation
from axile.server.portfolio_function import PortfolioFunctionError, _error_from_exception
from axile.server.portfolio_runner import calculate_sample_portfolio
from axile.server.portfolio_targets import calculate_portfolio_for_account
from axile.server.target_weight_snapshots import append_target_weight_snapshot, target_snapshot_public

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


async def _portfolio_native(name: str, request: object) -> Any:
    try:
        return await native_operation(name, request)
    except _core.NativeError as exc:
        code, detail = exc.args
        http_status = {
            "NOT_FOUND": 404,
            "CONFLICT": 422,
            "VALIDATION": 422,
            "ARCHIVED": 409,
            "ACCOUNT_CONFLICT": 409,
        }.get(code)
        if http_status is None:
            raise
        raise HTTPException(status_code=http_status, detail=detail) from exc


@router.post("/", status_code=status.HTTP_201_CREATED, response_model=None)
async def create_portfolio(portfolio: PortfolioCreate) -> Response:
    """创建一个由自定义函数计算目标权重的组合。"""
    try:
        value = await _portfolio_native(
            "portfolio_operation_json", {"op": "create", "portfolio": Portfolio.model_validate(portfolio).model_dump()}
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("创建组合失败: {}", exc)
        raise HTTPException(status_code=500, detail=f"服务器错误: {exc}") from exc
    return Response(value, media_type="application/json", status_code=201)


@router.get("/", response_model=None)
async def list_portfolios(archived: bool = False, include_archived: bool = False) -> Response:
    """获取组合列表。"""
    return Response(
        await _portfolio_native(
            "portfolio_query_json", {"op": "list", "archived": archived, "include_archived": include_archived}
        ),
        media_type="application/json",
    )


@router.get("/{portfolio_id:int}", response_model=None)
async def portfolio_info(portfolio_id: int) -> Response:
    """获取组合详情和当前绑定账户。"""
    return Response(
        await _portfolio_native("portfolio_query_json", {"op": "get", "portfolio_id": portfolio_id}),
        media_type="application/json",
    )


async def _set_portfolio_archived(portfolio_id: int, *, archived: bool) -> Response:
    """与组合试跑互斥，并在存储事务内校验现有绑定。"""
    if not try_register_target_refresh(portfolio_id, None):
        raise HTTPException(409, detail="组合正在刷新，请结束后再归档或恢复")
    try:
        value = await _portfolio_native(
            "portfolio_operation_json",
            {
                "op": "archive" if archived else "restore",
                "portfolio_id": portfolio_id,
                "updated_at": now_str(),
            },
        )
        return Response(value, media_type="application/json")
    finally:
        clear_target_refresh(portfolio_id, None)


@router.post("/{portfolio_id:int}/archive", response_model=None)
async def archive_portfolio(portfolio_id: int) -> Response:
    """归档没有当前账户绑定的组合，保留源码和目标快照。"""
    return await _set_portfolio_archived(portfolio_id, archived=True)


@router.post("/{portfolio_id:int}/restore", response_model=None)
async def restore_portfolio(portfolio_id: int) -> Response:
    """恢复组合的可选状态，不启动绑定账户。"""
    return await _set_portfolio_archived(portfolio_id, archived=False)


@router.delete("/{portfolio_id:int}", response_model=None)
async def delete_portfolio(portfolio_id: int) -> Response:
    """删除未绑定账户的组合。"""
    return Response(
        await _portfolio_native("portfolio_operation_json", {"op": "delete", "portfolio_id": portfolio_id}),
        media_type="application/json",
    )


@router.patch("/{portfolio_id:int}", response_model=None)
async def update_portfolio(portfolio_id: int, portfolio: PortfolioUpdate) -> Response:
    """局部更新组合；自定义函数不可清空。"""
    try:
        value = await _portfolio_native(
            "portfolio_operation_json",
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
    return Response(value, media_type="application/json", status_code=200)


async def resolve_portfolio_target(portfolio: Portfolio, account: AccountContext | None) -> PortfolioTarget:
    """执行组合函数并返回未经账户杠杆和精度处理的目标权重."""
    try:
        if account is None:
            return await asyncio.to_thread(calculate_sample_portfolio, portfolio.custom_calc_py_code)
        return await calculate_portfolio_for_account(account, portfolio.custom_calc_py_code)
    except HTTPException:
        raise
    except PortfolioFunctionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=exception_details(exc, "自定义组合函数执行失败"),
        ) from exc
    except Exception as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, exception_details(exc, "目标计算失败")) from exc


@router.get("/{portfolio_id:int}/target_snapshot", response_model=None)
async def portfolio_target_snapshot(portfolio_id: int) -> Response:
    """只读组合最近成功计算的原始目标快照。"""
    return Response(
        await _portfolio_native("portfolio_query_json", {"op": "target_snapshot", "portfolio_id": portfolio_id}),
        media_type="application/json",
    )


@router.post("/{portfolio_id:int}/target_snapshot/refresh", response_model=None)
async def refresh_portfolio_target_snapshot(portfolio_id: int) -> dict[str, Any]:
    """执行一次组合函数并持久化当前目标。"""
    context = await _portfolio_native("portfolio_operation", {"op": "context", "portfolio_id": portfolio_id})
    portfolio = Portfolio.model_validate(context["portfolio"])
    account_id = context["account_id"]
    account = AccountSnapshot.model_validate(context["account"]) if context["account"] is not None else None
    if not try_register_target_refresh(portfolio_id, account_id):
        raise HTTPException(status_code=409, detail="组合或上下文账户正在执行或刷新，请稍后再试")
    try:
        # 取得刷新槽后重读，避免等待期间组合已经归档。
        context = await _portfolio_native("portfolio_operation", {"op": "context", "portfolio_id": portfolio_id})
        portfolio = Portfolio.model_validate(context["portfolio"])
        account = AccountSnapshot.model_validate(context["account"]) if context["account"] is not None else None
        raw_target = await resolve_portfolio_target(portfolio, account)
        raw_target = PortfolioTarget.from_result(raw_target)
        normalized_target = (
            _normalize_rebalance_target(account, raw_target.targets, target_mode=raw_target.mode)
            if account is not None
            else None
        )
        snapshot = await append_target_weight_snapshot(
            portfolio_id=portfolio_id,
            account_id=account_id,
            raw_weights=raw_target.targets,
            target_mode=raw_target.mode,
            normalized_weights=normalized_target,
            source="manual",
        )
        return target_snapshot_public(snapshot, weight_kind="raw")
    finally:
        clear_target_refresh(portfolio_id, account_id)


async def _run_custom_calc_validation(
    account: AccountContext | None,
    custom_calc_py_code: str,
    *,
    leverage_overrides: dict[str, float] | None = None,
) -> dict[str, Any]:
    """在给定上下文中试跑自定义组合函数并返回结构化结果."""
    try:
        if account is None:
            result = await asyncio.to_thread(
                calculate_sample_portfolio,
                custom_calc_py_code,
                leverage_overrides=leverage_overrides,
            )
        else:
            result = await calculate_portfolio_for_account(
                account,
                custom_calc_py_code,
                leverage_overrides=leverage_overrides,
            )
    except BaseException as exc:  # noqa: BLE001 - 校验接口统一返回执行器与 IPC 错误
        error = exc if isinstance(exc, PortfolioFunctionError) else _error_from_exception(exc)
        return {
            "valid": False,
            "target": None,
            "target_mode": "weight",
            "error": str(error),
            "traceback": error.formatted_traceback,
            "error_line": error.error_line,
            "error_offset": error.error_offset,
            "error_type": error.error_type,
            "error_message": error.error_message,
        }
    target = PortfolioTarget.from_result(result)
    return {
        "valid": True,
        "target": target.targets,
        "target_mode": target.mode,
        "error": None,
        "traceback": None,
        "error_line": None,
        "error_offset": None,
        "error_type": None,
        "error_message": None,
    }


@router.post("/validate_custom_calc", response_model=None)
async def validate_custom_calc(payload: ValidateCustomCalcRequest) -> dict[str, Any]:
    """使用无账户或真实账户上下文试跑自定义组合函数。"""
    account = None
    overrides = payload.leverage_overrides.model_dump(exclude_none=True) if payload.leverage_overrides else {}
    if payload.account_id is not None:
        value = await _portfolio_native(
            "portfolio_operation", {"op": "validation_context", "account_id": payload.account_id}
        )
        account = AccountSnapshot.model_validate(value)
        limits = get_channel(account.trade_channel).descriptor.leverage
        for name, leverage in overrides.items():
            steps = (leverage - limits.min) / limits.step
            if not limits.min <= leverage <= limits.max or abs(steps - round(steps)) > 1e-9:
                raise HTTPException(
                    status_code=422, detail=f"{name} 需在 {limits.min:g}..{limits.max:g} 之间，步进 {limits.step:g}"
                )
    if payload.account_id is None:
        return await _run_custom_calc_validation(
            account, payload.custom_calc_py_code, leverage_overrides=overrides or None
        )
    if not try_register_account_asset_refresh(payload.account_id):
        raise HTTPException(409, detail="账户正在执行或刷新，请稍后再试")
    try:
        return await _run_custom_calc_validation(
            account, payload.custom_calc_py_code, leverage_overrides=overrides or None
        )
    finally:
        clear_account_asset_refresh(payload.account_id)


@router.post("/{portfolio_id:int}/run_and_save", response_model=None)
async def run_and_save_portfolio(portfolio_id: int, payload: RunAndSavePortfolioRequest) -> dict[str, Any]:
    """只执行一次草稿；通过后由 Rust 原子保存组合与原始快照。"""
    update = PortfolioUpdate(name=payload.name, custom_calc_py_code=payload.custom_calc_py_code)
    if not try_register_target_refresh(portfolio_id, payload.account_id):
        raise HTTPException(status_code=409, detail="组合或上下文账户正在执行或刷新，请稍后再试")
    try:
        context = await _portfolio_native(
            "portfolio_operation", {"op": "context", "portfolio_id": portfolio_id, "account_id": payload.account_id}
        )
        account = AccountSnapshot.model_validate(context["account"]) if context["account"] is not None else None
        result = await _run_custom_calc_validation(account, payload.custom_calc_py_code)
        if result["valid"]:
            now = now_str()
            await _portfolio_native(
                "portfolio_operation",
                {
                    "op": "save_trial",
                    "portfolio_id": portfolio_id,
                    "changes": update.model_dump(exclude_unset=True),
                    "updated_at": now,
                    "account_id": payload.account_id,
                    "raw_weights": result["target"],
                    "target_mode": result["target_mode"],
                    "normalized_weights": None,
                    "source": "manual",
                    "calculated_at": now,
                },
            )
        return result
    finally:
        clear_target_refresh(portfolio_id, payload.account_id)
