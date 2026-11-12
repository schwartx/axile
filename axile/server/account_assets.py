"""账户资产主动查询服务."""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from axile.executor.algorithms.utils.clock import clock_async_sleep
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.server.asset_observations import is_asset_observation
from axile.server.business_facts.assets import save_manual_asset_snapshot
from axile.server.connection_state import connection_config_key
from axile.server.db.models.account import AccountContext
from axile.server.execution.registry import clear_account_asset_refresh, try_register_account_asset_refresh
from axile.server.execution.worker_backend.manager import (
    WorkerBackendTimeoutError,
    get_worker_backend_manager,
)

_refreshes: dict[int, tuple[str, asyncio.Task[dict[str, Any]]]] = {}


class AccountAssetRefreshBusyError(RuntimeError):
    """账户执行或其他操作正在占用资产查询通路。"""


async def query_account_assets(account: AccountContext) -> UnifiedAccountAssets:
    """通过账户常驻 worker 查询最新资产。"""
    try:
        return await get_worker_backend_manager().get_account_assets(account)
    except WorkerBackendTimeoutError as exc:
        raise TimeoutError("账户资产查询超时") from exc


async def _refresh_and_save(account: AccountContext) -> dict[str, Any]:
    """查询和校验成功后一次落库，失败保留旧快照并释放槽位。"""
    assert account.id is not None
    try:
        assets = await query_account_assets(account)
        if not is_asset_observation(assets.model_dump()):
            raise ValueError("渠道未返回可信账户资产")
        payload = assets.model_dump(mode="json")
        snapshot = await save_manual_asset_snapshot(account.id, payload)
        return {
            "id": snapshot["id"],
            "account_id": snapshot["account_id"],
            "assets": payload,
            "source": snapshot["source"],
            "execution_id": snapshot["execution_id"],
            "created_at": snapshot["created_at"],
        }
    finally:
        clear_account_asset_refresh(account.id)


async def refresh_account_assets(account: AccountContext) -> dict[str, Any]:
    """同账户同配置合并刷新；请求取消不取消已开始的查询与落库。"""
    account_id = account.id
    assert account_id is not None
    key = connection_config_key(account.trade_channel, account.account_config)
    previous = _refreshes.get(account_id)
    if previous is not None and previous[0] == key and not previous[1].done():
        return await asyncio.shield(previous[1])
    if not try_register_account_asset_refresh(account_id):
        raise AccountAssetRefreshBusyError("账户正在执行或刷新资产，请稍后再试")
    task = asyncio.create_task(_refresh_and_save(account))
    _refreshes[account_id] = key, task

    def completed(done: asyncio.Task[dict[str, Any]]) -> None:
        if _refreshes.get(account_id) == (key, done):
            _refreshes.pop(account_id)
        if not done.cancelled():
            done.exception()

    task.add_done_callback(completed)
    return await asyncio.shield(task)


async def reconcile_account_assets(account: AccountContext) -> dict[str, object]:
    """记录核对独立报告资产结果，不丢弃已取得的委托或成交。"""
    try:
        assert account.id is not None
        previous = _refreshes.get(account.id)
        key = connection_config_key(account.trade_channel, account.account_config)
        if previous is not None and previous[0] == key and not previous[1].done():
            try:
                # 更早启动的查询不能证明已反映刚核对的成交；等待后再补查。
                await asyncio.shield(previous[1])
            except Exception:
                pass  # 旧查询失败也继续本次补查，由本次结果报告成功或失败。
        while True:
            try:
                snapshot = await refresh_account_assets(account)
                break
            except AccountAssetRefreshBusyError:
                # 显式核对继续等待执行/目标计算释放槽位，不在等待中访问渠道。
                await clock_async_sleep(1)
        return {"status": "updated", "snapshot_id": snapshot["id"]}
    except Exception:
        logger.exception("联动持仓核对失败 account_id={}", account.id)
        return {"status": "failed", "message": "持仓核对失败，当前显示上次资产快照"}
