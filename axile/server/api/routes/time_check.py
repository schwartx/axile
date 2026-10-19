"""系统时间诊断：读取不会触发外部请求。"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from axile.server.time_check.models import TimeCheckSettings
from axile.server.time_check.service import preview, service

router = APIRouter(prefix="/system/time-check", tags=["system"])


def ensure_ready() -> None:
    """初始化向导期间不访问尚未迁移的数据库。"""
    if not service.ready:
        raise HTTPException(503, "时间检查服务尚未就绪")


@router.get("")
async def status() -> dict:
    """获取缓存状态及最近检查依据。"""
    ensure_ready()
    return service.snapshot()


@router.put("/settings")
async def settings(config: TimeCheckSettings) -> dict:
    """保存后热更新调度，不自动发起检查。"""
    ensure_ready()
    try:
        return await service.save(config)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/run", status_code=202)
async def run() -> dict:
    """启动或复用在途检查。"""
    ensure_ready()
    return await service.run("manual")


class PreviewRequest(BaseModel):
    """与通用定时编辑器一致的 Cron 文本。"""

    cron_expr: str = Field(min_length=1, max_length=10000)


@router.post("/schedule-preview")
async def schedule_preview(payload: PreviewRequest) -> dict:
    """仅计算排程，不保存或查询外部时间。"""
    try:
        return {"timezone": "Asia/Shanghai", "times": preview(payload.cron_expr)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
