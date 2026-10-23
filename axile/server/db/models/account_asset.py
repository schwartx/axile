"""账户资产快照数据库模型."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import Field

from axile.server.db.models.base import Model, now_str

AccountAssetSnapshotSource = Literal["execution", "manual"]


class AccountAssetSnapshot(Model):
    """持久化一次来自交易渠道的账户资产观测."""

    id: Optional[int] = Field(default=None)
    account_id: int
    asset_fact_id: Optional[str] = Field(default=None)
    source: str
    execution_id: Optional[str] = Field(default=None)
    created_at: str = Field(default_factory=now_str)


class AccountAssetSnapshotPublic(Model):
    """账户资产快照的公开表示."""

    id: Optional[int]
    account_id: int
    assets: dict[str, Any]
    source: AccountAssetSnapshotSource
    execution_id: Optional[str]
    created_at: str
