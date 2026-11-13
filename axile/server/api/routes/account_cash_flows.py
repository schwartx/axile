"""账户出入金流水：手工录入、修订、核对与渠道同步。"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from axile.channels import get_channel
from axile.executor.algorithms.utils.clock import clock_now_iso
from axile.executor.models.cash_flow import CashFlowInput
from axile.server._native import _core
from axile.server.account_cash_flows import sync_cash_flows
from axile.server.api.routes.account_support import _get_account_or_404, ensure_account_current
from axile.server.execution.registry import clear_account_asset_refresh, try_register_account_asset_refresh
from axile.server.execution.resident_connection import AccountConnectionUnavailable
from axile.server.native_store import native_operation

router = APIRouter()
AccountId = Annotated[int, Path(gt=0, le=2**63 - 1)]
FlowId = Annotated[int, Path(gt=0, le=2**63 - 1)]


class CashFlowEdit(CashFlowInput):
    """修订必须绑定用户读取的版本。"""

    revision: int = Field(gt=0, le=2**63 - 1)


class CashFlowResolution(BaseModel):
    """核对或撤销绑定已有流水，关联手工记录需提供双方版本。"""

    model_config = ConfigDict(extra="forbid")
    revision: int = Field(gt=0, le=2**63 - 1)
    manual_id: int | None = Field(default=None, gt=0, le=2**63 - 1)
    manual_revision: int | None = Field(default=None, gt=0, le=2**63 - 1)


async def operation(account_id: int, op: str, **body):
    """保留原生事务的不存在、冲突与输入错误语义。"""
    try:
        return await native_operation(
            "cash_flow_operation", {"account_id": account_id, "op": op, "now": clock_now_iso(), **body}
        )
    except _core.NativeError as error:
        code, message = error.args[:2]
        status = {
            "ACCOUNT_NOT_FOUND": 404,
            "CASH_FLOW_NOT_FOUND": 404,
            "ACCOUNT_CONFLICT": 409,
            "CASH_FLOW_CHANGED": 409,
            "INVALID_REQUEST": 422,
        }.get(code, 503)
        raise HTTPException(status, detail={"code": code, "message": message}) from error


@router.get("/{account_id}/cash-flows", response_model=None)
async def list_cash_flows(
    account_id: AccountId, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0, le=2**63 - 1)
):
    """按账户读取流水，币种与同步能力从该账户渠道继承。"""
    account = await _get_account_or_404(account_id)
    descriptor = get_channel(str(account.trade_channel)).descriptor
    result = await operation(account_id, "list", limit=limit, offset=offset)
    return {**result, "currency": descriptor.currency, "sync_supported": descriptor.cash_flow_sync}


@router.post("/{account_id}/cash-flows", status_code=201, response_model=None)
async def create_cash_flow(account_id: AccountId, body: CashFlowInput):
    """补录已发生的账户资金变动，触发历史分析重算。"""
    return await operation(account_id, "create", **body.model_dump(mode="json"))


@router.patch("/{account_id}/cash-flows/{flow_id}", response_model=None)
async def edit_cash_flow(account_id: AccountId, flow_id: FlowId, body: CashFlowEdit):
    """修订手工流水并保留旧值。"""
    return await operation(account_id, "edit", id=flow_id, **body.model_dump(mode="json"))


@router.post("/{account_id}/cash-flows/{flow_id}/{action}", response_model=None)
async def resolve_cash_flow(account_id: AccountId, flow_id: FlowId, action: str, body: CashFlowResolution):
    """撤销、确认独立流水，或将待核对渠道流水关联至手工记录。"""
    if action not in {"void", "confirm", "link"}:
        raise HTTPException(404, detail="操作不存在")
    return await operation(account_id, action, id=flow_id, **body.model_dump(mode="json"))


@router.post("/{account_id}/cash-flows/sync", response_model=None)
async def sync_account_cash_flows(account_id: AccountId):
    """查询渠道流水；与执行、资产刷新及目标计算互斥。"""
    account = await _get_account_or_404(account_id)
    ensure_account_current(account)
    if not try_register_account_asset_refresh(account_id):
        raise HTTPException(409, detail="账户正在执行或查询，请稍后重试")
    try:
        return await sync_cash_flows(account)
    except NotImplementedError as error:
        raise HTTPException(422, detail=str(error)) from error
    except AccountConnectionUnavailable as error:
        raise HTTPException(503, detail="请先连接账户，再同步流水") from error
    except Exception as error:
        raise HTTPException(502, detail="流水同步失败，已存流水保持不变") from error
    finally:
        clear_account_asset_refresh(account_id)
