"""账户流控实时监控读接口。"""

from fastapi import APIRouter

from axile.server.account_control.monitor import monitor
from axile.server.api.routes.account_support import _get_account_or_404

router = APIRouter()


@router.get("/{account_id}/control/monitor", response_model=None)
async def control_monitor(account_id: int) -> dict:
    """单次读取与 SSE 相同的缓存模型，不创建交易实例。"""
    await _get_account_or_404(account_id)
    await monitor.refresh()
    return monitor.frames[account_id]
