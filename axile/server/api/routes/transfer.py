"""同一操作者实例间的数据传送入口。"""

from __future__ import annotations

import asyncio
import json
import secrets
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse

from axile.server import data_transfer as service

router = APIRouter(prefix="/transfer", tags=["transfer"])


def _plan(value: dict) -> service.TransferPlan:
    from pydantic import ValidationError

    try:
        return service.TransferPlan.model_validate(value)
    except ValidationError as exc:
        # 根校验失败不能把完整请求中的接收密钥带入日志和响应。
        raise HTTPException(
            422,
            {
                "message": "请检查时间范围、接收端与授权、定时间隔",
                "errors": exc.errors(include_input=False, include_context=False),
            },
        ) from exc


@router.get("/settings")
def get_settings():
    """返回规则、接收授权与最近调度结果。"""
    value = service.settings()
    last = service.root() / "last-run.json"
    return {**value, "last_run": json.loads(last.read_text()) if last.exists() else None}


@router.put("/settings")
def save_settings(plan: dict):
    """保存规则并更新定时任务。"""
    service.save_plan(_plan(plan))
    service.configure_schedule()
    return {"saved": True}


@router.post("/export")
async def export(plan: dict):
    """导出离线传送包。"""
    try:
        async with service._lock:
            package, _ = await asyncio.to_thread(service.export_package, _plan(plan))
        return FileResponse(package, filename=package.name, media_type="application/octet-stream")
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, "无法导出，请检查时间范围与数据库版本") from exc


async def receive_file(file: UploadFile) -> dict:
    """流式接收有大小预算的数据包。"""
    async with service._lock:
        with tempfile.TemporaryDirectory(dir=service.root(), prefix="upload-") as temporary:
            path = Path(temporary) / "data.axile"
            total = 0
            try:
                with path.open("wb") as destination:
                    while data := await file.read(1024 * 1024):
                        total += len(data)
                        if total > service.MAX_BYTES:
                            raise HTTPException(413, "数据包过大，请缩短范围")
                        destination.write(data)
                return await asyncio.to_thread(service.import_package, path)
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(
                    409, "接收失败：请检查数据包完整性、版本和本机是否为空或属于同一来源；请暂停本机调度"
                ) from exc
            finally:
                await file.close()


@router.post("/import")
async def import_file(file: UploadFile = File(...)):
    """通过本机页面导入离线包。"""
    return await receive_file(file)


@router.get("/identity")
def identity(x_transfer_token: str = Header(default="")):
    """验证接收授权并返回实例身份，拒绝自传送死锁。"""
    value = service.settings()
    if not secrets.compare_digest(x_transfer_token, value["receive_token"]):
        raise HTTPException(403, "接收授权无效")
    return {"source_id": value["source_id"]}


@router.post("/receive")
async def receive(file: UploadFile = File(...), x_transfer_token: str = Header(default="")):
    """验证专用授权后接收联网传送。"""
    if not secrets.compare_digest(x_transfer_token, service.settings()["receive_token"]):
        raise HTTPException(403, "接收授权无效")
    return await receive_file(file)


@router.post("/send")
async def send(plan: dict):
    """发送数据并在接收确认后执行清理。"""
    try:
        return await service.send(_plan(plan))
    except Exception as exc:
        raise HTTPException(409, "传送未完成，源数据保留，请检查接收端与授权") from exc


@router.post("/acknowledge")
async def acknowledge(receipt: dict):
    """验证确认并按保留规则清理已传送数据。"""
    try:
        async with service._lock:
            return await asyncio.to_thread(
                service.acknowledge, receipt, service.TransferPlan.model_validate(service.settings()["plan"])
            )
    except Exception as exc:
        raise HTTPException(409, "接收确认无效或清理未完成") from exc
