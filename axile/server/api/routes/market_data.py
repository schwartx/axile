"""行情快照入口独立于交易会话。"""

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from msgspec import Meta
from starlette.concurrency import run_in_threadpool

from axile.channels.market_data import MarketDataUnavailable
from axile.common.serialization import to_payload
from axile.server.api.inputs import json_body
from axile.server.api.routes.account_support import _get_account_snapshot_or_404, ensure_account_current
from axile.server.execution.worker_backend.manager import get_worker_backend_manager
from axile.server.market_data import market_data_status, read_market_data

router = APIRouter(prefix="/market-data", tags=["market-data"])


@dataclass(kw_only=True)
class MarketDataRequest:
    """有限时的单次行情快照查询。"""

    symbols: Annotated[list[Annotated[str, Meta(min_length=1, max_length=80)]], Meta(min_length=1, max_length=50)]
    timeout_seconds: Annotated[float, Meta(gt=0, le=15)] = 5

    __forbid_unknown__ = True

    def validate(self) -> None:
        """拒绝重复和包含首尾空白的代码。"""
        if len(set(self.symbols)) != len(self.symbols) or any(
            not code.strip() or code.strip() != code for code in self.symbols
        ):
            raise ValueError("合约代码须非空、不重复，并且不能带首尾空白")


@router.post("/accounts/{account_id}/quotes")
async def market_quotes(
    account_id: int, query: Annotated[MarketDataRequest, Depends(json_body(MarketDataRequest))], response: Response
) -> dict[str, object]:
    """从已保存账户设置取得行情源，按需独立连接。"""
    account = await _get_account_snapshot_or_404(account_id)
    ensure_account_current(account)
    response.headers["Cache-Control"] = "no-store"
    try:
        rows = await run_in_threadpool(
            read_market_data,
            str(account.trade_channel),
            account.account_config,
            query.symbols,
            timeout=query.timeout_seconds,
        )
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    except (ImportError, KeyError, MarketDataUnavailable) as exc:
        message = str(exc) if isinstance(exc, MarketDataUnavailable) else "行情渠道不可用"
        raise HTTPException(503, detail=message) from exc
    except TimeoutError as exc:
        raise HTTPException(504, detail=str(exc)) from exc
    if account.trade_channel == "ctp":
        catalog = get_worker_backend_manager().instrument_catalog
        for quote in rows.values():
            if exchange := quote.extra.get("exchange_id"):
                instrument = catalog.cached(f"{exchange}.{quote.symbol}")
                if instrument is not None and instrument.has_specs:
                    quote.extra.update(volume_multiple=instrument.multiplier, price_tick=instrument.tick)
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
