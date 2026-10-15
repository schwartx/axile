"""账户资产主动查询服务."""

from __future__ import annotations

from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.server.db.models.account import AccountContext
from axile.server.execution.worker_backend.manager import (
    WorkerBackendTimeoutError,
    get_worker_backend_manager,
)


async def query_account_assets(account: AccountContext) -> UnifiedAccountAssets:
    """通过账户常驻 worker 查询最新资产。"""
    try:
        return await get_worker_backend_manager().get_account_assets(account)
    except WorkerBackendTimeoutError as exc:
        raise TimeoutError("账户资产查询超时") from exc
