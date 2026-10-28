"""账户回测设置；分析查询由 account_analysis 提供。"""

from fastapi import APIRouter, HTTPException

from axile.server._native import _core
from axile.server.db.models.base import now_str
from axile.server.db.models.performance import PerformanceSettings
from axile.server.native_store import native_operation

router = APIRouter()


@router.patch("/performance-settings/{account_id}", response_model=PerformanceSettings)
async def save_performance_settings(account_id: int, settings: PerformanceSettings):
    """原子更新设置，由事实版本失效触发重算。"""
    try:
        return await native_operation(
            "account_update_backtest",
            {"account_id": account_id, "patch": settings.model_dump(mode="json"), "now": now_str()},
        )
    except _core.NativeError as error:
        status = 404 if error.args[0] == "ACCOUNT_NOT_FOUND" else 503
        raise HTTPException(status, detail={"code": error.args[0], "message": error.args[1]}) from error
