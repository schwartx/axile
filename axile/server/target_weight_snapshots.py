"""目标权重快照的原生持久化与显式读取。"""

from __future__ import annotations

from typing import Any, Literal, cast

from axile.server.db.models import TargetSizingPublic, TargetWeightSnapshotPublic
from axile.server.db.models.base import now_str
from axile.server.native_store import native_operation


async def append_target_weight_snapshot(
    *,
    portfolio_id: int,
    account_id: int | None,
    raw_weights: dict[str, float] | None,
    normalized_weights: dict[str, float] | None,
    source: Literal["manual", "execution"],
    execution_id: str | None = None,
) -> dict[str, Any]:
    """由 Rust 一次提交快照头和权重明细。"""
    return await native_operation(
        "target_operation",
        {
            "op": "append",
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "raw_weights": raw_weights,
            "normalized_weights": normalized_weights,
            "source": source,
            "execution_id": execution_id,
            "calculated_at": now_str(),
        },
    )


async def get_latest_account_target_snapshot(account_id: int, portfolio_id: int) -> dict[str, Any] | None:
    """读取账户在当前组合下最近一条归一化权重快照。"""
    return await native_operation(
        "target_operation", {"op": "latest_account", "account_id": account_id, "portfolio_id": portfolio_id}
    )


async def get_previous_account_target_snapshots(
    account_id: int,
    portfolio_id: int,
    *,
    before_id: int,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """读取同一账户组合下早于指定快照的近期执行目标。"""
    return await native_operation(
        "target_operation",
        {
            "op": "previous_account",
            "account_id": account_id,
            "portfolio_id": portfolio_id,
            "before_id": before_id,
            "limit": limit,
        },
    )


def target_snapshot_public(
    snapshot: dict[str, Any] | None,
    *,
    weight_kind: Literal["raw", "normalized"],
    weights: dict[str, float] | None = None,
    quantities: dict[str, float] | None = None,
    strategy_weights: dict[str, float] | None = None,
    account_weights: dict[str, float] | None = None,
    sizing: TargetSizingPublic | None = None,
) -> TargetWeightSnapshotPublic:
    """将显式加载的快照转换为稳定的页面响应。"""
    if snapshot is None:
        return TargetWeightSnapshotPublic()
    stored = snapshot.get("raw_weights") if weight_kind == "raw" else snapshot.get("normalized_weights")
    return TargetWeightSnapshotPublic(
        weights=weights if weights is not None else (stored or {}),
        quantities=quantities,
        strategy_weights=strategy_weights,
        account_weights=account_weights,
        sizing=sizing,
        calculated_at=snapshot["calculated_at"],
        source=cast(Literal["manual", "execution"], snapshot["source"]),
        execution_id=snapshot["execution_id"],
        context_account_id=snapshot["account_id"],
    )
