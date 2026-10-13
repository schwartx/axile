"""独立于账户创建的显式连接验证接口。"""

import json

from fastapi import APIRouter, HTTPException, Request, Response

from axile.server.connection_validation import ValidationJob, ValidationRejected, service

router = APIRouter(prefix="/channel-validations", tags=["channel-validations"])


@router.post("", response_model=ValidationJob, status_code=202)
async def create_validation(request: Request, response: Response) -> ValidationJob:
    """手动解析含凭据请求，保证任何 422 都不会携带原始 input。"""
    response.headers["Cache-Control"] = "no-store"
    body = await request.body()
    if len(body) > 64 * 1024:
        raise HTTPException(413, "连接配置过大")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(422, "连接配置格式不正确") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("channel"), str):
        raise HTTPException(422, "请指定交易渠道")
    if not isinstance(payload.get("account_config"), dict):
        raise HTTPException(422, "请填写连接配置")
    try:
        return service.create(payload["channel"], payload["account_config"])
    except ValidationRejected as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.get("/{validation_id}", response_model=ValidationJob)
async def get_validation(validation_id: str, response: Response) -> ValidationJob:
    """返回完整阶段快照，不包含账户或凭据。"""
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
