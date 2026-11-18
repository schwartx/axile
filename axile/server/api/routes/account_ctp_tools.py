"""CTP 账户的柜台只读工具。"""

import asyncio
from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response

from axile.server.api.routes.account_support import _get_account_snapshot_or_404, ensure_account_current
from axile.server.error_details import exception_details
from axile.server.execution.registry import clear_account_asset_refresh, try_register_account_asset_refresh
from axile.server.execution.resident_connection import AccountConnectionUnavailable
from axile.server.execution.worker_backend.manager import (
    WorkerBackendExecutionError,
    WorkerBackendTimeoutError,
    get_worker_backend_manager,
)
from axile.server.execution.worker_backend.protocol import CtpAccountQuery

router = APIRouter()
FuturesSymbol = Annotated[str, Query(max_length=32, pattern=r"^[A-Za-z]+[0-9]{3,4}$")]


@router.get("/{account_id}/margin-rates")
async def query_margin_rates(account_id: int, symbol: FuturesSymbol, response: Response) -> dict[str, object]:
    """查询单个期货合约的投机保证金参数。"""
    return await _query_ctp_account(account_id, response, "query_margin_rates", symbol, "保证金率")


@router.get("/{account_id}/commission-rates")
async def query_commission_rates(account_id: int, symbol: FuturesSymbol, response: Response) -> dict[str, object]:
    """查询单个期货合约的开仓、平仓及平今费率。"""
    return await _query_ctp_account(account_id, response, "query_commission_rates", symbol, "手续费率")


@router.get("/{account_id}/settlement-statement")
async def query_settlement_statement(
    account_id: int, response: Response, trading_day: date | None = None
) -> dict[str, object]:
    """读取指定交易日或最近一次柜台结算单。"""
    day = trading_day.isoformat().replace("-", "") if trading_day is not None else ""
    return await _query_ctp_account(account_id, response, "query_settlement_info", day, "结算单")


async def _query_ctp_account(
    account_id: int, response: Response, command: CtpAccountQuery, argument: str, label: str
) -> dict[str, object]:
    account = await _get_account_snapshot_or_404(account_id)
    ensure_account_current(account)
    if str(account.trade_channel) != "ctp":
        raise HTTPException(422, detail=f"{label}查询仅支持 CTP 账户")
    manager = get_worker_backend_manager()
    if not try_register_account_asset_refresh(account_id):
        raise HTTPException(409, detail="账户正在执行或查询，请稍后重试")

    task = asyncio.create_task(manager.query_ctp_account(account, command, argument))

    def release(done: asyncio.Task[dict[str, object]]) -> None:
        clear_account_asset_refresh(account_id)
        if not done.cancelled():
            done.exception()

    task.add_done_callback(release)
    response.headers["Cache-Control"] = "no-store"
    try:
        return await asyncio.shield(task)
    except AccountConnectionUnavailable as exc:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        raise HTTPException(503, detail=exc.execution_error, headers=headers) from exc
    except (TimeoutError, WorkerBackendTimeoutError) as exc:
        raise HTTPException(504, detail=f"{label}查询超时，请稍后重试") from exc
    except WorkerBackendExecutionError as exc:
        raise HTTPException(502, detail=exception_details(exc, f"{label}查询失败")) from exc
