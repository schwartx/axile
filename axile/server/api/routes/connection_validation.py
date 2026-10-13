"""独立于账户创建的显式连接验证接口。"""

import json

from fastapi import APIRouter, HTTPException, Request, Response

from axile.channels import get_channel
from axile.server.api.deps import SessionDep
from axile.server.connection_validation import ValidationJob, ValidationRejected, ValidationTable, service
from axile.server.db.models import Account

router = APIRouter(prefix="/channel-validations", tags=["channel-validations"])
account_router = APIRouter(prefix="/account", tags=["channel-validations"])


async def _read_config(request: Request) -> dict[str, object]:
    """解析含凭据请求，不把请求原文带入格式错误响应。"""
    body = await request.body()
    if len(body) > 64 * 1024:
        raise HTTPException(413, "连接配置过大")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(422, "连接配置格式不正确") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("account_config"), dict):
        raise HTTPException(422, "请填写连接配置")
    return payload


@router.post("", response_model=ValidationJob, status_code=202)
async def create_validation(request: Request, response: Response) -> ValidationJob:
    """手动解析含凭据请求，保证任何 422 都不会携带原始 input。"""
    response.headers["Cache-Control"] = "no-store"
    payload = await _read_config(request)
    if not isinstance(payload.get("channel"), str):
        raise HTTPException(422, "请指定交易渠道")
    try:
        channel, config = payload["channel"], payload["account_config"]
        assert isinstance(channel, str) and isinstance(config, dict)
        return service.create(channel, config)
    except ValidationRejected as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@account_router.post("/{account_id}/connection-validation", response_model=ValidationJob, status_code=202)
async def create_account_validation(
    account_id: int, request: Request, response: Response, session: SessionDep
) -> ValidationJob:
    """只读合并已保存凭据与当前草稿，运行验证但不保存、不改变账户运行状态。"""
    response.headers["Cache-Control"] = "no-store"
    payload = await _read_config(request)
    account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(404, "账户不存在")
    channel = str(account.trade_channel)
    plugin = get_channel(channel)
    if plugin.descriptor.connection_validation is None:
        raise HTTPException(422, "渠道不支持连接验证")
    draft = payload["account_config"]
    assert isinstance(draft, dict)
    config = {**account.account_config, **draft}
    try:
        return service.create(channel, config)
    except ValidationRejected as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.get("/{validation_id}", response_model=ValidationJob)
async def get_validation(validation_id: str, response: Response) -> ValidationJob:
    """返回阶段状态与展示摘要，不包含登录凭据或完整表格。"""
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.get(validation_id)
    except ValidationRejected as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.delete("/{validation_id}", response_model=ValidationJob)
async def cancel_validation(validation_id: str) -> ValidationJob:
    """显式取消并回收临时连接。"""
    try:
        return await service.cancel(validation_id)
    except ValidationRejected as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.get("/{validation_id}/steps/{step_id}/table", response_model=ValidationTable)
async def get_validation_table(validation_id: str, step_id: str, response: Response) -> ValidationTable:
    """按需读取验证时的表格快照，不发起新的柜台请求。"""
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.get_table(validation_id, step_id)
    except ValidationRejected as exc:
        raise HTTPException(exc.status, str(exc)) from exc
