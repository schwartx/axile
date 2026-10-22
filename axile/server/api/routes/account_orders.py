"""账户当前委托查询及逐笔撤单接口。"""

from datetime import date
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Response
from loguru import logger
from starlette.concurrency import run_in_threadpool

from axile.executor.algorithms.utils.clock import clock_now_ms
from axile.server._native import _core
from axile.server.account_orders import cancel_account_order, query_account_activity, query_account_orders
from axile.server.api.deps import SessionDep
from axile.server.api.routes.account_support import _get_account_or_404
from axile.server.core.db import analysis_store
from axile.server.db.models.account_order import (
    AccountActivityPublic,
    AccountOrdersPublic,
    OrderCancelReceipt,
    OrderCancelRequest,
)
from axile.server.execution.resident_connection import AccountConnectionUnavailable
from axile.server.order_submissions import submission_window
from axile.server.submission_monitor import (
    SubmissionMonitorUpdate,
    save_submission_monitor,
)

router = APIRouter()


@router.get("/{account_id}/orders/monitor")
async def submission_monitor(session: SessionDep, account_id: int) -> dict:
    """读取今日原生报撤监测；不发送任何柜台请求。"""
    from axile.executor.account_control.presets import resolve_account_control_policy
    from axile.executor.algorithms.utils.clock import clock_now

    account = await _get_account_or_404(session, account_id)
    if str(account.trade_channel) != "ctp":
        raise HTTPException(422, detail="当前仅支持 CTP 原生报撤监测")
    policy = resolve_account_control_policy(account.account_control_preset, account.account_control_override)
    day = clock_now().astimezone(ZoneInfo(policy.timezone)).date().isoformat()
    start, end = submission_window(day, policy.timezone)
    return await run_in_threadpool(
        _core.submission_monitor, analysis_store(), account_id, day, policy.timezone, start, end, clock_now_ms()
    )


@router.put("/{account_id}/orders/monitor")
async def update_submission_monitor(session: SessionDep, account_id: int, payload: SubmissionMonitorUpdate) -> dict:
    """立即保存并重评估监测阈值，不改变风控额度或触发交易。"""
    from axile.executor.account_control.presets import resolve_account_control_policy

    account = (await _get_account_or_404(session, account_id)).snapshot()
    if str(account.trade_channel) != "ctp":
        raise HTTPException(422, detail="当前仅支持 CTP 原生报撤监测")
    policy = resolve_account_control_policy(account.account_control_preset, account.account_control_override)
    await session.close()
    await run_in_threadpool(save_submission_monitor, account_id, payload, policy.timezone)
    return await submission_monitor(session, account_id)


@router.get("/{account_id}/orders/submissions")
async def order_submissions(
    session: SessionDep,
    account_id: int,
    control_date: date,
    limit: int = Query(100, ge=1, le=500),
) -> Response:
    """读取已接入 CTP 入口的原生调用事实，不访问柜台、不以额度用量补算。"""
    from axile.executor.account_control.presets import resolve_account_control_policy

    account = await _get_account_or_404(session, account_id)
    if str(account.trade_channel) != "ctp":
        raise HTTPException(422, detail="当前仅支持已接入的 CTP 原生报单流水")
    policy = resolve_account_control_policy(account.account_control_preset, account.account_control_override)
    day = control_date.isoformat()
    start, end = submission_window(day, policy.timezone)
    content = await run_in_threadpool(
        _core.order_submissions, analysis_store(), account_id, day, policy.timezone, start, end, limit
    )
    return Response(content=content, media_type="application/json")


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
