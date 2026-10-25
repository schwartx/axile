"""账户资产快照数据库模型."""

from __future__ import annotations

from typing import Any, Literal, Optional

from axile.server.db.models.base import Model

AccountAssetSnapshotSource = Literal["execution", "manual"]


class AccountAssetSnapshotPublic(Model):
    """账户资产快照的公开表示."""

    id: Optional[int]
    account_id: int
    assets: dict[str, Any]
    source: AccountAssetSnapshotSource
    execution_id: Optional[str]
    created_at: str
