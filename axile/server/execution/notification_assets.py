"""为飞书通知读取与账户页面一致的最近可信资产快照。"""

from __future__ import annotations

import loguru

from axile.server.business_facts.assets import read_snapshots_assets
from axile.server.core.db import SessionLocal
from axile.server.db.models.account import AccountContext
from axile.server.repositories import get_recent_account_asset_snapshots_for_accounts


async def load_notification_snapshot(account: AccountContext) -> dict[str, object] | None:
    """仅为配置了执行通知的账户读取备用快照，查询失败不影响执行。"""
    if not account.execution_notification_code or account.id is None:
        return None
    try:
        async with SessionLocal() as session:
            grouped = await get_recent_account_asset_snapshots_for_accounts(session, [account.id], limit=1)
            snapshots = grouped.get(account.id, [])
            if not snapshots:
                return None
            snapshot = snapshots[0]
            snapshot_id = snapshot.id
            if snapshot_id is None:
                raise ValueError("通知资产快照缺少持久化标识")
            assets_by_snapshot = await read_snapshots_assets(session, [snapshot])
            return {
                "id": snapshot_id,
                "created_at": snapshot.created_at,
                "assets": assets_by_snapshot[snapshot_id],
            }
    except Exception as exc:
        loguru.logger.opt(exception=exc).warning("飞书通知备用资产快照读取失败: account_id={}", account.id)
        return None
