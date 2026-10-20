"""目标权重计算快照数据库模型."""

from __future__ import annotations

from typing import Literal, Optional

from sqlalchemy import JSON as SA_JSON
from sqlalchemy import Column, Float, ForeignKey, Index, Integer, Table, Text
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlmodel import Field, SQLModel

from axile.executor.models.value_conversion import PriceValueConversion
from axile.server.db.models.base import now_str

TargetWeightSnapshotSource = Literal["manual", "execution"]
TargetSizingAvailability = Literal["available", "pending_execution", "legacy", "unavailable"]


class TargetSizingRowPublic(SQLModel):
    """单品种从策略权重到可执行数量的公开换算证据."""

    symbol: str
    sizing_mode: str = "weight"
    status: str = "SIZED"
    reason_code: str
    strategy_weight: Optional[float] = None
    account_weight: Optional[float] = None
    account_multiplier: Optional[float] = None
    weight_precision: Optional[float] = None
    equity: Optional[float] = None
    reference_price: Optional[float] = None
    unit_multiplier: Optional[float] = None
    unit_notional: Optional[float] = None
    target_notional: Optional[float] = None
    raw_quantity: Optional[float] = None
    target_quantity: Optional[float] = None
    current_quantity: Optional[float] = None
    quantity_step: Optional[float] = None
    min_quantity: Optional[float] = None
    min_notional: Optional[float] = None
    value_conversion: Optional[PriceValueConversion] = None


class TargetSizingPublic(SQLModel):
    """目标快照的数量换算可用性与逐品种证据."""

    status: TargetSizingAvailability
    execution_id: Optional[str] = None
    calculated_at: Optional[str] = None
    rows: dict[str, TargetSizingRowPublic] = Field(default_factory=dict)


class TargetWeightSnapshot(AsyncAttrs, SQLModel, table=True):
    """持久化一次成功的组合目标权重计算."""

    __tablename__ = "target_weight_snapshot"
    __table_args__ = (
        Index("ix_target_weight_snapshot_portfolio_id_id", "portfolio_id", "id"),
        Index("ix_target_weight_snapshot_account_id_id", "account_id", "id"),
        Index("ix_target_weight_snapshot_execution_id", "execution_id", unique=True),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    portfolio_id: int = Field(sa_column=Column(Integer, ForeignKey("portfolio.id", ondelete="CASCADE"), nullable=False))
    account_id: Optional[int] = Field(
        default=None,
        sa_column=Column(Integer, ForeignKey("account.id", ondelete="SET NULL"), nullable=True),
    )
    raw_weights_kind: str = Field(default="null", sa_column=Column(Text, nullable=False))
    normalized_weights_kind: str = Field(default="null", sa_column=Column(Text, nullable=False))
    weights_metadata: dict[str, object] = Field(default_factory=dict, sa_column=Column(SA_JSON, nullable=False))
    source: str = Field(sa_column=Column(Text, nullable=False))
    execution_id: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    calculated_at: str = Field(default_factory=now_str, sa_column=Column(Text, nullable=False))


target_weight_snapshot_weight = Table(
    "target_weight_snapshot_weight",
    SQLModel.metadata,
    Column("snapshot_id", Integer, ForeignKey("target_weight_snapshot.id", ondelete="CASCADE"), primary_key=True),
    Column("role", Text, primary_key=True),
    Column("map_key", Text, primary_key=True),
    Column("value", Float),
    Column("audit_evidence", SA_JSON, nullable=False),
    sqlite_with_rowid=False,
)


class TargetWeightSnapshotPublic(SQLModel):
    """页面读取的最近一次目标权重计算结果."""

    weights: dict[str, float] = Field(default_factory=dict)
    quantities: Optional[dict[str, float]] = None
    strategy_weights: Optional[dict[str, float]] = None
    account_weights: Optional[dict[str, float]] = None
    sizing: Optional[TargetSizingPublic] = None
    calculated_at: Optional[str] = None
    source: Optional[TargetWeightSnapshotSource] = None
    execution_id: Optional[str] = None
    context_account_id: Optional[int] = None
