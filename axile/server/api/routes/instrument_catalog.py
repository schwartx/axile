"""服务级内存合约目录管理接口，沿用顶层 API 鉴权。"""

from typing import Any

from fastapi import APIRouter, HTTPException

from axile.server.execution.worker_backend.manager import get_worker_backend_manager

router = APIRouter(prefix="/instrument-catalog", tags=["instrument-catalog"])


@router.get("")
def catalog_status() -> dict[str, Any]:
    """读取目录状态，不查询柜台。"""
    return get_worker_backend_manager().instrument_catalog.status()


@router.post("/refresh", status_code=202)
def refresh_catalog() -> dict[str, Any]:
    """异步启动或复用现有全量任务。"""
    return get_worker_backend_manager().instrument_catalog.refresh()


@router.get("/refresh/{task_id}")
def refresh_status(task_id: str) -> dict[str, Any]:
    """查询刷新进度，任务只保存在本进程内存中。"""
    task = get_worker_backend_manager().instrument_catalog.task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="合约目录刷新任务不存在")
    return task
