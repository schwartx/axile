"""自定义函数投资组合路由."""

import asyncio
from typing import cast

from fastapi import APIRouter, HTTPException, status
from loguru import logger
from sqlmodel import select

from axile.server.api.deps import SessionDep
from axile.server.db.models import (
    Account,
    Message,
    Portfolio,
    PortfolioCreate,
    PortfolioListPublic,
    PortfolioLitePublic,
    PortfolioPublic,
    PortfolioUpdate,
    TargetWeightSnapshotPublic,
    ValidateCustomCalcRequest,
    ValidateCustomCalcResponse,
)
from axile.server.db.models.account import AccountContext
from axile.server.error_details import exception_details
from axile.server.execution.rebalance import _normalize_rebalance_target
from axile.server.execution.registry import clear_target_refresh, try_register_target_refresh
from axile.server.portfolio_runner import calculate_sample_portfolio
from axile.server.portfolio_targets import (
    calculate_portfolio_for_account,
    portfolio_result_from_exception,
)
from axile.server.repositories import get_latest_account_id_by_portfolio_id, get_portfolios_every_account
from axile.server.target_weight_snapshots import (
    append_target_weight_snapshot,
    get_latest_portfolio_target_snapshot,
    target_snapshot_public,
)

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


async def _portfolio_public(session: SessionDep, portfolio: Portfolio) -> PortfolioPublic:
    """构造带当前绑定账户的组合公开响应."""
    portfolio_id = cast("int", portfolio.id)
    account_id = await get_latest_account_id_by_portfolio_id(session, portfolio_id)
    return PortfolioPublic(**portfolio.model_dump(), account_id=account_id)


@router.post("/", status_code=status.HTTP_201_CREATED, response_model=PortfolioPublic)
async def create_portfolio(session: SessionDep, portfolio: PortfolioCreate) -> PortfolioPublic:
    """创建一个由自定义函数计算目标权重的组合."""
    try:
        db_portfolio = Portfolio.model_validate(portfolio)
        session.add(db_portfolio)
        await session.commit()
        await session.refresh(db_portfolio)
    except Exception as exc:
        await session.rollback()
        logger.exception("创建组合失败: {}", exc)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"服务器错误: {exc}") from exc
    return PortfolioPublic(**db_portfolio.model_dump(), account_id=None)


@router.get("/", response_model=PortfolioListPublic)
async def list_portfolios(session: SessionDep) -> PortfolioListPublic:
    """获取组合列表."""
    portfolios = (await session.execute(select(Portfolio))).scalars().all()
    return PortfolioListPublic(data=[PortfolioLitePublic.model_validate(portfolio) for portfolio in portfolios])


@router.get("/{portfolio_id:int}", response_model=PortfolioPublic)
async def portfolio_info(session: SessionDep, portfolio_id: int) -> PortfolioPublic:
    """获取组合详情和当前绑定账户."""
    db_portfolio = await session.get(Portfolio, portfolio_id)
    if db_portfolio is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="组合不存在")
    return await _portfolio_public(session, db_portfolio)


@router.delete("/{portfolio_id:int}")
async def delete_portfolio(session: SessionDep, portfolio_id: int) -> Message:
    """删除未绑定账户的组合."""
    db_portfolio = await session.get(Portfolio, portfolio_id)
    if db_portfolio is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="组合不存在")

    if portfolio_id in (await get_portfolios_every_account(session)).values():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="该组合与账户关联，必须解绑才能删除组合",
        )

    await session.delete(db_portfolio)
    await session.commit()
    return Message(message="成功删除组合")


@router.patch("/{portfolio_id:int}", response_model=PortfolioPublic)
async def update_portfolio(session: SessionDep, portfolio_id: int, portfolio: PortfolioUpdate) -> PortfolioPublic:
    """局部更新组合；自定义函数不可清空."""
    db_portfolio = await session.get(Portfolio, portfolio_id)
    if db_portfolio is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="组合不存在")

    try:
        db_portfolio.sqlmodel_update(portfolio.model_dump(exclude_unset=True))
        session.add(db_portfolio)
        await session.commit()
        await session.refresh(db_portfolio)
    except Exception as exc:
        await session.rollback()
        logger.exception("更新组合失败: {}", exc)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"服务器错误: {exc}") from exc
    return await _portfolio_public(session, db_portfolio)


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
async def portfolio_target_snapshot(session: SessionDep, portfolio_id: int) -> TargetWeightSnapshotPublic:
    """只读组合最近成功计算的原始目标快照."""
    if await session.get(Portfolio, portfolio_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="组合不存在")
    snapshot = await get_latest_portfolio_target_snapshot(session, portfolio_id)
    return target_snapshot_public(snapshot, weight_kind="raw")


@router.post("/{portfolio_id:int}/target_snapshot/refresh", response_model=TargetWeightSnapshotPublic)
async def refresh_portfolio_target_snapshot(session: SessionDep, portfolio_id: int) -> TargetWeightSnapshotPublic:
    """按用户请求执行一次组合函数并持久化当前目标."""
    portfolio = await session.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="组合不存在")

    account_id = await get_latest_account_id_by_portfolio_id(session, portfolio_id)
    if not try_register_target_refresh(portfolio_id, account_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="组合或上下文账户正在执行或刷新，请稍后再试")
    try:
        if account_id is None:
            await session.close()
            raw_target = await resolve_portfolio_target(portfolio, None)
            normalized_target = None
        else:
            account = await session.get(Account, account_id)
            if account is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="绑定账户不存在")
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
async def validate_custom_calc(session: SessionDep, payload: ValidateCustomCalcRequest) -> ValidateCustomCalcResponse:
    """使用样例或真实账户上下文试跑自定义组合函数."""
    if payload.account_id is None:
        return await _run_custom_calc_validation(None, payload.custom_calc_py_code)

    account = await session.get(Account, payload.account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账户不存在")
    await session.close()
    return await _run_custom_calc_validation(account, payload.custom_calc_py_code)
