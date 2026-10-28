"""分析设置仍保存在服务端；旧的服务端计算入口明确停止服务。"""

from fastapi import APIRouter, HTTPException, Response

from axile.server._native import _core
from axile.server.api.routes.account_support import _get_account_or_404
from axile.server.db.models.base import now_str
from axile.server.db.models.performance import PerformanceSettings
from axile.server.native_store import native_operation

router = APIRouter()


def _native_http_error(error: _core.NativeError) -> HTTPException:
    return HTTPException(503, detail={"code": error.args[0], "message": error.args[1]})


def _json_response(content: bytes | str) -> Response:
    return Response(content=content, media_type="application/json")


@router.patch("/performance-settings/{account_id}", response_model=PerformanceSettings)
async def save_performance_settings(account_id: int, settings: PerformanceSettings) -> Response:
    """原子更新回测配置，不触发账户调度协调."""
    try:
        content = await native_operation(
            "account_update_backtest",
            {"account_id": account_id, "patch": settings.model_dump(mode="json"), "now": now_str()},
        )
    except _core.NativeError as error:
        if error.args[0] == "ACCOUNT_NOT_FOUND":
            raise HTTPException(404, detail="账户不存在") from error
        raise _native_http_error(error) from error
    return _json_response(content)


@router.get("/performance/{account_id}")
@router.get("/performance/{account_id}/snapshot")
@router.get("/performance/{account_id}/status")
@router.get("/performance/{account_id}/chart")
@router.get("/performance/{account_id}/execution-summary")
@router.get("/performance/{account_id}/execution-detail")
@router.get("/performance/{account_id}/costs")
@router.post("/performance/{account_id}/refresh")
async def browser_analysis_required(account_id: int) -> None:
    """旧客户端得到明确迁移提示，避免隐式恢复服务端全历史计算。"""
    await _get_account_or_404(account_id)
    raise HTTPException(
        410, detail={"code": "BROWSER_ANALYSIS_REQUIRED", "message": "绩效已改为浏览器本地计算，请更新或刷新前端"}
    )
