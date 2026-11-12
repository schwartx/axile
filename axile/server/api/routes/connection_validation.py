"""独立于账户创建的显式连接验证接口。"""

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from axile.channels import get_channel
from axile.server.api.routes.account_support import _get_account_or_404
from axile.server.connection_validation import ValidationJob, ValidationRejected, service
from axile.server.error_details import exception_details

router = APIRouter(prefix="/channel-validations", tags=["channel-validations"])
account_router = APIRouter(prefix="/account", tags=["channel-validations"])


def _public_job(job: ValidationJob) -> dict[str, Any]:
    """仅导出任务状态，内部任务可继续承接记录器的状态更新。"""
    return job.model_dump(
        mode="json",
        include={
            "validation_id",
            "run_id",
            "channel",
            "purpose",
            "status",
            "steps",
            "created_at",
            "finished_at",
            "error",
        },
    )


async def _read_config(request: Request) -> dict[str, object]:
    """解析连接请求，格式错误保留原始异常。"""
    body = await request.body()
    if len(body) > 64 * 1024:
        raise HTTPException(413, "连接配置过大")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(422, exception_details(exc, "连接配置格式不正确")) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("account_config"), dict):
        raise HTTPException(422, "请填写连接配置")
    return payload


@router.post("", response_model=None, status_code=202)
async def create_validation(request: Request, response: Response) -> dict[str, Any]:
    """手动解析连接请求，校验错误保留全部诊断。"""
    response.headers["Cache-Control"] = "no-store"
    payload = await _read_config(request)
    if not isinstance(payload.get("channel"), str):
        raise HTTPException(422, "请指定交易渠道")
    try:
        channel, config = payload["channel"], payload["account_config"]
        assert isinstance(channel, str) and isinstance(config, dict)
        return _public_job(service.create(channel, config))
    except ValidationRejected as exc:
        raise HTTPException(exc.status, exception_details(exc)) from exc


@account_router.post("/{account_id}/connection-validation", response_model=None, status_code=202)
async def create_account_validation(account_id: int, request: Request, response: Response) -> dict[str, Any]:
    """只读合并已保存凭据与当前草稿，运行验证但不保存、不改变账户运行状态。"""
    response.headers["Cache-Control"] = "no-store"
    payload = await _read_config(request)
    account = await _get_account_or_404(account_id)
    channel = str(account.trade_channel)
    plugin = get_channel(channel)
    if plugin.descriptor.connection_validation is None:
        raise HTTPException(422, "渠道不支持连接验证")
    draft = payload["account_config"]
    assert isinstance(draft, dict)
    config = {**account.account_config, **draft}
    try:
        return _public_job(service.create(channel, config))
    except ValidationRejected as exc:
        raise HTTPException(exc.status, exception_details(exc)) from exc


@router.get("/{validation_id}", response_model=None)
async def get_validation(validation_id: str, response: Response) -> dict[str, Any]:
    """返回阶段状态与展示摘要，不包含登录凭据或完整表格。"""
    response.headers["Cache-Control"] = "no-store"
    try:
        return _public_job(service.get(validation_id))
    except ValidationRejected as exc:
        raise HTTPException(exc.status, exception_details(exc)) from exc


@router.delete("/{validation_id}", response_model=None)
async def cancel_validation(validation_id: str) -> dict[str, Any]:
    """显式取消并回收临时连接。"""
    try:
        return _public_job(await service.cancel(validation_id))
    except ValidationRejected as exc:
        raise HTTPException(exc.status, exception_details(exc)) from exc


@router.get("/{validation_id}/steps/{step_id}/table", response_model=None)
async def get_validation_table(validation_id: str, step_id: str, response: Response) -> dict[str, Any]:
    """按需读取验证时的表格快照，不发起新的柜台请求。"""
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.get_table(validation_id, step_id).model_dump(
            mode="json", include={"kind", "title", "summary", "description", "columns", "row_count", "rows"}
        )
    except ValidationRejected as exc:
        raise HTTPException(exc.status, exception_details(exc)) from exc
