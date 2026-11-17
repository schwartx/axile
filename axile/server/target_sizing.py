"""账户目标展示与数量对账共用的换算证据读取。"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from axile.domain.execution import ExecutionArtifactType
from axile.server.db.models import (
    ExecutionArtifact,
    TargetSizingPublic,
    TargetSizingRowPublic,
    TargetWeightSnapshot,
    TargetWeightSnapshotPublic,
)
from axile.server.target_weight_snapshots import (
    get_previous_account_target_snapshots_for_accounts,
    target_snapshot_public,
)

type _Canonicalize = Callable[[str], str]


@dataclass
class _SizingEvidence:
    status: str
    rows: dict[str, TargetSizingRowPublic] = field(default_factory=dict)
    quantities: dict[str, float] | None = None
    sizing_execution_id: str | None = None
    sizing_calculated_at: str | None = None
    null_target_quantity: bool = False
    inflight: bool = False


def _canonical_weight_map(
    weights: dict[str, float] | None,
    canonicalize: _Canonicalize,
) -> dict[str, float]:
    if not weights:
        return {}
    return {canonicalize(symbol): float(weight) for symbol, weight in weights.items() if isinstance(symbol, str)}


def _canonical_weights_equal(
    left: dict[str, float] | None,
    right: dict[str, float] | None,
    canonicalize: _Canonicalize,
) -> bool:
    mapped_left = _canonical_weight_map(left, canonicalize)
    mapped_right = _canonical_weight_map(right, canonicalize)
    if not mapped_left or set(mapped_left) != set(mapped_right):
        return False
    return all(
        math.isclose(mapped_left[symbol], mapped_right[symbol], rel_tol=1e-9, abs_tol=1e-12) for symbol in mapped_left
    )


def _complete_quantities(
    rows: dict[str, TargetSizingRowPublic],
    account_weights: dict[str, float],
) -> dict[str, float] | None:
    if not set(account_weights).issubset(rows):
        return None
    quantities = {
        symbol: float(row.target_quantity)
        for symbol, row in rows.items()
        if row.target_quantity is not None and math.isfinite(row.target_quantity) and row.status != "UNAVAILABLE"
    }
    if not set(account_weights).issubset(quantities):
        return None
    return quantities


def _sizing_rows_from_summary(
    summary_artifact: ExecutionArtifact,
    *,
    canonicalize: _Canonicalize,
    strategy_weights: dict[str, float],
    account_weights: dict[str, float],
    target_context: dict[str, object],
) -> dict[str, TargetSizingRowPublic]:
    reconciliation = summary_artifact.content.get("reconciliation")
    symbols = reconciliation.get("symbols") if isinstance(reconciliation, dict) else None
    if not isinstance(symbols, list):
        return {}
    rows: dict[str, TargetSizingRowPublic] = {}
    for item in symbols:
        if not isinstance(item, dict) or not isinstance(item.get("symbol"), str):
            continue
        raw_sizing = item.get("sizing")
        if not isinstance(raw_sizing, dict):
            continue
        symbol = canonicalize(item["symbol"])
        row = cast("dict[str, object]", dict(raw_sizing))
        row["symbol"] = symbol
        row["strategy_weight"] = strategy_weights.get(symbol)
        row["account_weight"] = account_weights.get(symbol, row.get("account_weight"))
        raw_weight = strategy_weights.get(symbol)
        account_weight = account_weights.get(symbol)
        row["account_multiplier"] = (
            account_weight / raw_weight if raw_weight not in (None, 0.0) and account_weight is not None else None
        )
        row["weight_precision"] = target_context.get("weight_precision")
        rows[symbol] = TargetSizingRowPublic.model_validate(row)
    return rows


async def _load_execution_artifacts(
    session: AsyncSession,
    snapshots: list[TargetWeightSnapshot],
) -> dict[str, dict[str, ExecutionArtifact]]:
    execution_ids = sorted({snapshot.execution_id for snapshot in snapshots if snapshot.execution_id})
    by_execution: dict[str, dict[str, ExecutionArtifact]] = {}
    for start in range(0, len(execution_ids), 500):
        artifacts = (
            (
                await session.execute(
                    select(ExecutionArtifact).where(
                        col(ExecutionArtifact.execution_id).in_(execution_ids[start : start + 500]),
                        col(ExecutionArtifact.artifact_type).in_(
                            [ExecutionArtifactType.TARGET_SNAPSHOT, ExecutionArtifactType.EXECUTION_SUMMARY]
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        for artifact in artifacts:
            by_execution.setdefault(artifact.execution_id, {})[artifact.artifact_type] = artifact
    return by_execution


def _sizing_evidence_for_snapshot(
    snapshot: TargetWeightSnapshot,
    artifacts: dict[str, dict[str, ExecutionArtifact]],
    *,
    canonicalize: _Canonicalize,
    strategy_weights: dict[str, float],
    account_weights: dict[str, float],
) -> _SizingEvidence:
    status = "pending_execution" if snapshot.source == "manual" or not snapshot.execution_id else "unavailable"
    rows: dict[str, TargetSizingRowPublic] = {}
    null_target_quantity = False
    inflight = False
    if snapshot.execution_id:
        by_type = artifacts.get(snapshot.execution_id, {})
        target_artifact = by_type.get(ExecutionArtifactType.TARGET_SNAPSHOT)
        summary_artifact = by_type.get(ExecutionArtifactType.EXECUTION_SUMMARY)
        target_context: dict[str, object] = {}
        if target_artifact is not None:
            raw_context = target_artifact.content.get("sizing_context")
            if isinstance(raw_context, dict):
                target_context = cast("dict[str, object]", raw_context)
        if summary_artifact is not None and summary_artifact.schema_version >= 2:
            rows = _sizing_rows_from_summary(
                summary_artifact,
                canonicalize=canonicalize,
                strategy_weights=strategy_weights,
                account_weights=account_weights,
                target_context=target_context,
            )
            status = "available" if set(account_weights).issubset(rows) else "unavailable"
        elif summary_artifact is not None:
            status = "legacy"
        elif target_artifact is not None and target_artifact.schema_version >= 2:
            status = "pending_execution"
            inflight = True
        else:
            status = "legacy"

    quantities: dict[str, float] | None = None
    if status == "available":
        quantities = _complete_quantities(rows, account_weights)
        if quantities is None:
            status = "unavailable"
            null_target_quantity = True
    elif rows:
        null_target_quantity = any(
            symbol in rows and rows[symbol].target_quantity is None for symbol in account_weights
        )

    return _SizingEvidence(
        status=status,
        rows=rows,
        quantities=quantities,
        sizing_execution_id=snapshot.execution_id,
        sizing_calculated_at=snapshot.calculated_at,
        null_target_quantity=null_target_quantity,
        inflight=inflight,
    )


async def read_account_target_snapshots(
    session: AsyncSession,
    snapshots: dict[int, TargetWeightSnapshot],
    canonicalizers: dict[int, _Canonicalize],
) -> dict[int, TargetWeightSnapshotPublic]:
    """为目标展示和持仓对账批量读取同一份完整数量换算证据。"""
    artifacts = await _load_execution_artifacts(session, list(snapshots.values()))
    strategy_weights = {
        account_id: _canonical_weight_map(snapshot.raw_weights, canonicalizers[account_id])
        for account_id, snapshot in snapshots.items()
    }
    account_weights = {
        account_id: _canonical_weight_map(snapshot.normalized_weights, canonicalizers[account_id])
        for account_id, snapshot in snapshots.items()
    }
    evidence_by_account = {
        account_id: _sizing_evidence_for_snapshot(
            snapshot,
            artifacts,
            canonicalize=canonicalizers[account_id],
            strategy_weights=strategy_weights[account_id],
            account_weights=account_weights[account_id],
        )
        for account_id, snapshot in snapshots.items()
    }
    unresolved = [
        snapshot
        for account_id, snapshot in snapshots.items()
        if evidence_by_account[account_id].status != "available"
        and not evidence_by_account[account_id].inflight
        and snapshot.id is not None
        and snapshot.account_id
    ]
    previous_by_account = await get_previous_account_target_snapshots_for_accounts(session, unresolved)
    matching = {
        account_id: [
            previous
            for previous in previous_by_account.get(account_id, [])
            if _canonical_weights_equal(
                previous.normalized_weights, snapshots[account_id].normalized_weights, canonicalizers[account_id]
            )
        ]
        for account_id in evidence_by_account
    }
    previous_artifacts = await _load_execution_artifacts(
        session, [previous for previous_list in matching.values() for previous in previous_list]
    )
    for account_id, previous_list in matching.items():
        for previous in previous_list:
            evidence = _sizing_evidence_for_snapshot(
                previous,
                previous_artifacts,
                canonicalize=canonicalizers[account_id],
                strategy_weights=strategy_weights[account_id],
                account_weights=account_weights[account_id],
            )
            if evidence.status == "available" and evidence.quantities is not None:
                evidence_by_account[account_id] = evidence
                break
        else:
            evidence = evidence_by_account[account_id]
            if evidence.status == "unavailable" and not evidence.null_target_quantity:
                evidence.status = "pending_execution"

    return {
        account_id: target_snapshot_public(
            snapshot,
            weight_kind="normalized",
            weights=account_weights[account_id],
            quantities=evidence_by_account[account_id].quantities,
            strategy_weights=strategy_weights[account_id],
            account_weights=account_weights[account_id],
            sizing=TargetSizingPublic(
                status=cast("Any", evidence_by_account[account_id].status),
                execution_id=evidence_by_account[account_id].sizing_execution_id,
                calculated_at=evidence_by_account[account_id].sizing_calculated_at,
                rows=evidence_by_account[account_id].rows,
            ),
        )
        for account_id, snapshot in snapshots.items()
    }
