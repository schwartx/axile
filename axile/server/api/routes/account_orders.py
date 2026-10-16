"""账户当前委托查询及逐笔撤单接口。"""

from fastapi import APIRouter, HTTPException, Query
from loguru import logger

from axile.server.account_orders import cancel_account_order, query_account_activity, query_account_orders
from axile.server.api.deps import SessionDep
from axile.server.api.routes.account_support import _get_account_or_404
from axile.server.db.models.account_order import (
    AccountActivityPublic,
    AccountOrdersPublic,
    OrderCancelReceipt,
    OrderCancelRequest,
)
from axile.server.execution.resident_connection import AccountConnectionUnavailable

router = APIRouter()


@router.get("/{account_id}/orders/activity", response_model=AccountActivityPublic)
async def recent_activity(
    session: SessionDep,
    account_id: int,
    refresh: bool = Query(False),
    symbol: str | None = Query(None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:/-]+$"),
    limit: int = Query(100, ge=1, le=500),
) -> AccountActivityPublic:
    """读取账户近期委托与成交，查询范围及失败独立返回。"""
    account = (await _get_account_or_404(session, account_id)).snapshot()
    await session.close()
    try:
        return await query_account_activity(account, refresh=refresh, symbol=symbol, limit=limit)
    except AccountConnectionUnavailable as exc:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        raise HTTPException(503, detail=exc.execution_error, headers=headers) from exc
    except TimeoutError as exc:
        raise HTTPException(504, detail="近期交易记录查询超时，请稍后核对") from exc
    except Exception as exc:
        logger.exception("近期交易记录查询失败 account_id={}", account_id)
        raise HTTPException(502, detail="近期交易记录查询失败，请稍后核对") from exc


@router.get("/{account_id}/orders", response_model=AccountOrdersPublic)
async def current_orders(session: SessionDep, account_id: int, refresh: bool = Query(False)) -> AccountOrdersPublic:
    """读取整个账户的未完成委托；读取失败不能作为零挂单证明。"""
    account = (await _get_account_or_404(session, account_id)).snapshot()
    await session.close()
    try:
        result = await query_account_orders(account, refresh=refresh)
    except AccountConnectionUnavailable as exc:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        raise HTTPException(503, detail=exc.execution_error, headers=headers) from exc
    except TimeoutError as exc:
        raise HTTPException(504, detail="当前委托查询超时，状态待确认") from exc
    except Exception as exc:
        logger.exception("当前委托查询失败 account_id={}", account_id)
        raise HTTPException(502, detail="当前委托查询失败，请稍后刷新") from exc
    return result


@router.post("/{account_id}/orders/cancel", response_model=OrderCancelReceipt, status_code=202)
async def cancel_order(session: SessionDep, account_id: int, payload: OrderCancelRequest) -> OrderCancelReceipt:
    """受理单笔撤单；202 及 submitted 均不表示柜台已完成撤单。"""
    account = (await _get_account_or_404(session, account_id)).snapshot()
    await session.close()
    try:
        return await cancel_account_order(account, payload)
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
