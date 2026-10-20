"""账户资产快照数据库模型."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Optional

from sqlalchemy import Column, ForeignKey, Index, Integer, Text
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import relationship
from sqlmodel import Field, Relationship, SQLModel

from axile.server.db.models.base import now_str

if TYPE_CHECKING:
    from axile.server.db.models.account import Account

AccountAssetSnapshotSource = Literal["execution", "manual"]


class AccountAssetSnapshot(AsyncAttrs, SQLModel, table=True):
    """持久化一次来自交易渠道的账户资产观测."""

    __tablename__ = "account_asset_snapshot"
    __table_args__ = (
        Index("ix_account_asset_snapshot_account_id_id", "account_id", "id"),
        Index("ix_account_asset_snapshot_account_created", "account_id", "created_at"),
        Index("ix_account_asset_snapshot_execution_id", "execution_id", unique=True),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(sa_column=Column(Integer, ForeignKey("account.id", ondelete="CASCADE"), nullable=False))
    asset_fact_id: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, ForeignKey("business_asset_snapshot.asset_id", ondelete="SET NULL"), nullable=True),
    )
    source: str = Field(sa_column=Column(Text, nullable=False))
    execution_id: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    created_at: str = Field(default_factory=now_str, sa_column=Column(Text, nullable=False))

    account: "Account" = Relationship(sa_relationship=relationship("Account", back_populates="asset_snapshots"))


class AccountAssetSnapshotPublic(SQLModel):
    """账户资产快照的公开表示."""

    id: Optional[int]
    account_id: int
    assets: dict[str, Any]
    source: AccountAssetSnapshotSource
    execution_id: Optional[str]
    created_at: str


class AccountAssetSnapshotListPublic(SQLModel):
    """账户资产快照分页响应."""

    data: list[AccountAssetSnapshotPublic]
    count: int
