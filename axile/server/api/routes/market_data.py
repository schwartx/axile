"""独立行情快照及 CTP 参考行情入口。"""

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from msgspec import Meta
from starlette.concurrency import run_in_threadpool

from axile.channels.market_data import MarketDataUnavailable
from axile.common.serialization import from_payload, to_payload
from axile.executor.instrument_catalog import CatalogError
from axile.executor.models.unified_price import UnifiedPriceData
from axile.server.api.inputs import json_body
from axile.server.api.routes.account_support import _get_account_snapshot_or_404, ensure_account_current
from axile.server.db.models.account import AccountContext
from axile.server.execution.registry import clear_account_asset_refresh, try_register_account_asset_refresh
from axile.server.execution.resident_connection import AccountConnectionUnavailable
from axile.server.execution.worker_backend.manager import (
    WorkerBackendExecutionError,
    WorkerBackendTimeoutError,
    get_worker_backend_manager,
)
from axile.server.market_data import market_data_status, read_market_data

router = APIRouter(prefix="/market-data", tags=["market-data"])


@dataclass(kw_only=True)
class MarketDataRequest:
    """有限时的单次行情快照查询。"""

    symbols: Annotated[list[Annotated[str, Meta(min_length=1, max_length=80)]], Meta(min_length=1, max_length=50)]
    timeout_seconds: Annotated[float, Meta(gt=0, le=15)] = 5
    allow_stale: bool = False

    __forbid_unknown__ = True

    def validate(self) -> None:
        """拒绝重复和包含首尾空白的代码。"""
        if len(set(self.symbols)) != len(self.symbols) or any(
            not code.strip() or code.strip() != code for code in self.symbols
        ):
            raise ValueError("合约代码须非空、不重复，并且不能带首尾空白")


async def _fill_ctp_contract_specs(
    account: AccountContext, quotes: Iterable[UnifiedPriceData], *, timeout: float
) -> None:
    """优先用缓存，缺项时通过所选账户的交易会话精确查询。"""
    manager = get_worker_backend_manager()
    missing: dict[str, list[UnifiedPriceData]] = {}
    for quote in quotes:
        if exchange := quote.extra.get("exchange_id"):
            symbol = f"{exchange}.{quote.symbol}"
            instrument = manager.instrument_catalog.cached(symbol)
            if instrument is not None and instrument.has_specs:
                quote.extra.update(volume_multiple=instrument.multiplier, price_tick=instrument.tick)
            else:
                missing.setdefault(symbol, []).append(quote)
    if not missing:
        return
    try:
        specs = await manager.query_instruments(account, list(missing), timeout=timeout, require_specs=True)
    except (CatalogError, AccountConnectionUnavailable):
        return
    for symbol, pending_quotes in missing.items():
        for quote in pending_quotes:
            quote.extra.update(volume_multiple=specs[symbol]["multiplier"], price_tick=specs[symbol]["tick"])


async def _read_ctp_reference_quotes(
    account_id: int, account: AccountContext, query: MarketDataRequest
) -> dict[str, UnifiedPriceData]:
    """复用已登录的柜台，账户忙或断开时交给独立行情。"""
    manager = get_worker_backend_manager()
    if not try_register_account_asset_refresh(account_id):
        return {}
    task = asyncio.create_task(
        manager.query_ctp_account(
            account, "query_valuation_quotes", query.symbols, timeout_seconds=query.timeout_seconds
        )
    )

    def release(done: asyncio.Task[dict[str, object]]) -> None:
        clear_account_asset_refresh(account_id)
        if not done.cancelled():
            done.exception()

    task.add_done_callback(release)
    try:
        rows = await asyncio.shield(task)
    except AccountConnectionUnavailable:
        return {}
    return {symbol: from_payload(UnifiedPriceData, row) for symbol, row in rows.items()}


@router.post("/accounts/{account_id}/quotes")
async def market_quotes(
    account_id: int, query: Annotated[MarketDataRequest, Depends(json_body(MarketDataRequest))], response: Response
) -> dict[str, object]:
    """按需读取独立行情；CTP 可显式优先使用参考行情。"""
    account = await _get_account_snapshot_or_404(account_id)
    ensure_account_current(account)
    response.headers["Cache-Control"] = "no-store"
    try:
        rows = {}
        if query.allow_stale and account.trade_channel == "ctp":
            rows = await _read_ctp_reference_quotes(account_id, account, query)
        missing = [symbol for symbol in query.symbols if symbol not in rows]
        if missing:
            rows.update(
                await run_in_threadpool(
                    read_market_data,
                    str(account.trade_channel),
                    account.account_config,
                    missing,
                    timeout=query.timeout_seconds,
                )
            )
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    except (ImportError, KeyError, MarketDataUnavailable) as exc:
        message = str(exc) if isinstance(exc, MarketDataUnavailable) else "行情渠道不可用"
        raise HTTPException(503, detail=message) from exc
    except (TimeoutError, WorkerBackendTimeoutError) as exc:
        raise HTTPException(504, detail=str(exc)) from exc
    except WorkerBackendExecutionError as exc:
        raise HTTPException(502, detail=exc.execution_error or str(exc)) from exc
    if account.trade_channel == "ctp":
        await _fill_ctp_contract_specs(account, rows.values(), timeout=query.timeout_seconds)
    return {"data": to_payload(rows, mode="json")}


@router.get("/accounts/{account_id}/status")
async def market_status(account_id: int) -> dict[str, str]:
    """只读取行情就绪状态。"""
    account = await _get_account_snapshot_or_404(account_id)
    ensure_account_current(account)
    try:
        state = market_data_status(str(account.trade_channel), account.account_config)
    except (ImportError, KeyError, ValueError, MarketDataUnavailable):
        state = "unavailable"
    return {"status": state}
