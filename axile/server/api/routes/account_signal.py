"""调仓信号向导试算及配置读取。"""

import asyncio
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from axile.channels.rebalance_signal import SIGNAL_CHANNEL, SignalAccountConfig
from axile.common.serialization import to_payload
from axile.server._native import _core
from axile.server.api.inputs import json_body
from axile.server.api.routes.account_support import _get_account_or_404
from axile.server.error_details import exception_details
from axile.server.native_store import native_operation
from axile.server.signal_runner import run_signal
from axile.server.signal_service import signal_quantity_unit

router = APIRouter()


@dataclass(kw_only=True)
class SignalPreviewRequest:
    """以未保存的数据配置试算选中组合。"""

    portfolio_id: int
    account_config: SignalAccountConfig

    __forbid_unknown__ = True


@router.get("/{account_id}/signal-config", response_model=None)
async def signal_config(account_id: int) -> dict[str, Any]:
    """读取调仓信号编辑页需要的函数和手动快照配置。"""
    account = await _get_account_or_404(account_id)
    if account.trade_channel != SIGNAL_CHANNEL:
        raise HTTPException(404, detail="该账户不是调仓信号账户")
    return {"account_config": account.account_config, "quantity_unit": signal_quantity_unit(account.market)}


@router.post("/signal/preview", response_model=None)
async def preview_signal(
    payload: Annotated[SignalPreviewRequest, Depends(json_body(SignalPreviewRequest))],
) -> dict[str, Any]:
    """运行真实的自定义函数和组合代码，返回目标数量。"""
    try:
        context = await native_operation(
            "portfolio_operation", {"op": "context", "portfolio_id": payload.portfolio_id, "account_id": None}
        )
    except _core.NativeError as exc:
        raise HTTPException(422, detail=exc.args[1]) from exc
    portfolio = context["portfolio"]
    try:
        result = await asyncio.to_thread(
            run_signal,
            payload.account_config,
            {"account_id": None, "market": portfolio["market"]},
            portfolio["custom_calc_py_code"],
            30,
        )
    except (ValueError, TimeoutError) as exc:
        raise HTTPException(422, detail=exception_details(exc, "调仓信号试算失败")) from exc
    return {
        "target": to_payload(result.target, mode="json"),
        "quantity_unit": signal_quantity_unit(portfolio["market"]),
    }
