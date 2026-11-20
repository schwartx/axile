"""为飞书通知读取与账户页面一致的最近可信资产快照。"""

from __future__ import annotations

import loguru

from axile.server.business_facts.assets import latest_asset_snapshot
from axile.server.db.models.account import AccountContext


async def load_notification_snapshot(account: AccountContext) -> dict[str, object] | None:
    """仅为配置了执行通知的账户读取备用快照，查询失败不影响执行。"""
    if not account.execution_notification_code or account.id is None:
        return None
    try:
        snapshot = await latest_asset_snapshot(account.id)
        if snapshot is None:
            return None
        return {key: snapshot[key] for key in ("id", "created_at", "assets")}
    except Exception as exc:
        loguru.logger.opt(exception=exc).warning("飞书通知备用资产快照读取失败: account_id={}", account.id)
        return None
