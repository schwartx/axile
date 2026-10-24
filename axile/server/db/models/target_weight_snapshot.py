"""目标权重计算快照数据库模型."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field

from axile.executor.models.value_conversion import PriceValueConversion
from axile.server.db.models.base import Model

TargetWeightSnapshotSource = Literal["manual", "execution"]
TargetSizingAvailability = Literal["available", "pending_execution", "legacy", "unavailable"]


class TargetSizingRowPublic(Model):
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


class TargetSizingPublic(Model):
    """目标快照的数量换算可用性与逐品种证据."""

    status: TargetSizingAvailability
    execution_id: Optional[str] = None
    calculated_at: Optional[str] = None
    rows: dict[str, TargetSizingRowPublic] = Field(default_factory=dict)


class TargetWeightSnapshotPublic(Model):
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
