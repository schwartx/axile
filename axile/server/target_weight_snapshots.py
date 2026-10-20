"""目标权重快照的结构化持久化与显式读取。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

import sqlalchemy as sa
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, desc, select

from axile.server.analysis_observations import write_observation_fact
from axile.server.business_facts.columns import read_columns, store_columns
from axile.server.db.models import ExecuteRecord, TargetSizingPublic, TargetWeightSnapshot, TargetWeightSnapshotPublic
from axile.server.db.models.target_weight_snapshot import target_weight_snapshot_weight

_WEIGHT_ROLES = ("raw_weights", "normalized_weights")


@dataclass(frozen=True, slots=True)
class TargetWeightSnapshotData:
    """已经显式读取权重明细的快照视图，不触发隐式数据库查询。"""

    id: int
    portfolio_id: int
    account_id: int | None
    raw_weights: dict[str, float] | None
    normalized_weights: dict[str, float] | None
    source: str
    execution_id: str | None
    calculated_at: str


def encode_snapshot_weights(
    snapshot_id: int,
    payload: Mapping[str, object],
) -> tuple[dict[str, object], dict[sa.Table, list[dict[str, object]]]]:
    """实时写入和历史迁移共用的权重映射，不规范化策略与账户权重。"""
    columns: dict[str, object] = {}
    states: dict[str, object] = {}
    rows = []
    for role in _WEIGHT_ROLES:
        if role not in payload:
            columns[role + "_kind"] = "missing"
            continue
        value = payload[role]
        if value is None:
            columns[role + "_kind"] = "null"
        elif isinstance(value, dict) and all(isinstance(key, str) for key in value):
            columns[role + "_kind"] = "mapping"
            for key, weight in value.items():
                typed, metadata = store_columns({"value": weight}, {"value": "number"})
                rows.append(
                    {
                        "snapshot_id": snapshot_id,
                        "role": role,
                        "map_key": key,
                        **typed,
                        "audit_evidence": metadata,
                    }
                )
        else:
            columns[role + "_kind"] = "invalid"
            invalid_columns, invalid_metadata = store_columns({"value": value}, {"value": "json"})
            states[role] = {"columns": invalid_columns, "audit_evidence": invalid_metadata}
    columns["weights_metadata"] = states
    return columns, {target_weight_snapshot_weight: rows}


def decode_snapshot_weights(
    head: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, object]:
    """保留快照中未提供、空值、空映射、异常值及原始品种键。"""
    result = {}
    for role in _WEIGHT_ROLES:
        kind = head[role + "_kind"]
        if kind == "missing":
            continue
        if kind == "null":
            result[role] = None
        elif kind == "invalid":
            invalid = head["weights_metadata"][role]
            result[role] = read_columns(invalid["columns"], invalid["audit_evidence"], {"value": "json"})["value"]
        elif kind == "mapping":
            ordered = sorted((row for row in rows if row["role"] == role), key=lambda row: row["map_key"])
            result[role] = {
                row["map_key"]: read_columns(dict(row), row["audit_evidence"], {"value": "number"})["value"]
                for row in ordered
            }
        else:
            raise ValueError("Unknown target weight snapshot container state")
    invalid = {}
    for row in rows:
        if row.get("audit_evidence"):
            invalid.setdefault(row["role"], {})[row["map_key"]] = row["audit_evidence"]
    if invalid:
        result["audit_evidence"] = {"invalid_target_values": invalid}
    return result


async def _snapshot_views(
    session: AsyncSession,
    snapshots: Sequence[TargetWeightSnapshot],
) -> list[TargetWeightSnapshotData]:
    if not snapshots:
        return []
    ids = [snapshot.id for snapshot in snapshots]
    grouped: dict[int, list[Mapping[str, Any]]] = {}
    rows = (
        await session.execute(
            sa.select(target_weight_snapshot_weight)
            .where(
                target_weight_snapshot_weight.c.snapshot_id.in_(ids),
            )
            .order_by(target_weight_snapshot_weight.c.map_key)
        )
    ).mappings()
    for row in rows:
        grouped.setdefault(row["snapshot_id"], []).append(dict(row))
    result = []
    for snapshot in snapshots:
        snapshot_id = snapshot.id
        if snapshot_id is None:
            raise ValueError("Target snapshot must be persisted before reading weights")
        weights = decode_snapshot_weights(
            {
                "raw_weights_kind": snapshot.raw_weights_kind,
                "normalized_weights_kind": snapshot.normalized_weights_kind,
                "weights_metadata": snapshot.weights_metadata,
            },
            grouped.get(snapshot_id, ()),
        )
        result.append(
            TargetWeightSnapshotData(
                id=snapshot_id,
                portfolio_id=snapshot.portfolio_id,
                account_id=snapshot.account_id,
                raw_weights=cast(dict[str, float] | None, weights.get("raw_weights")),
                normalized_weights=cast(dict[str, float] | None, weights.get("normalized_weights")),
                source=snapshot.source,
                execution_id=snapshot.execution_id,
                calculated_at=snapshot.calculated_at,
            )
        )
    return result


async def append_target_weight_snapshot(
    session: AsyncSession,
    *,
    portfolio_id: int,
    account_id: int | None,
    raw_weights: dict[str, float] | None,
    normalized_weights: dict[str, float] | None,
    source: Literal["manual", "execution"],
    execution_id: str | None = None,
) -> TargetWeightSnapshotData:
    """追加计算快照和类型化明细后提交，返回已显式读取的快照视图。"""
    columns, batches = encode_snapshot_weights(
        0, {"raw_weights": raw_weights, "normalized_weights": normalized_weights}
    )
    snapshot = TargetWeightSnapshot(
        portfolio_id=portfolio_id,
        account_id=account_id,
        source=source,
        execution_id=execution_id,
        **cast(dict[str, Any], columns),
    )
    session.add(snapshot)
    await session.flush()
    for table, rows in batches.items():
        if rows:
            for row in rows:
                row["snapshot_id"] = snapshot.id
            await session.execute(table.insert(), rows)
    if account_id is not None and execution_id is not None and normalized_weights is not None:
        record = await session.scalar(
            select(ExecuteRecord).where(
                ExecuteRecord.account_id == account_id,
                ExecuteRecord.execution_id == execution_id,
            )
        )
        if record is not None:
            from axile.server.analysis_history import read_analysis_record

            await write_observation_fact(
                session,
                await read_analysis_record(session, record),
                fallback_weights=normalized_weights,
            )
    await session.commit()
    await session.refresh(snapshot)
    return (await _snapshot_views(session, [snapshot]))[0]


async def get_latest_portfolio_target_snapshot(
    session: AsyncSession,
    portfolio_id: int,
) -> TargetWeightSnapshotData | None:
    """读取组合最近一条具有原始权重的计算快照。"""
    snapshot = await session.scalar(
        select(TargetWeightSnapshot)
        .where(
            TargetWeightSnapshot.portfolio_id == portfolio_id,
            TargetWeightSnapshot.raw_weights_kind != "null",
            TargetWeightSnapshot.raw_weights_kind != "missing",
        )
        .order_by(desc(TargetWeightSnapshot.id))
        .limit(1)
    )
    return (await _snapshot_views(session, [snapshot]))[0] if snapshot is not None else None


async def get_latest_account_target_snapshot(
    session: AsyncSession,
    account_id: int,
    portfolio_id: int,
) -> TargetWeightSnapshotData | None:
    """读取账户在当前组合下最近一条归一化权重快照。"""
    snapshot = await session.scalar(
        select(TargetWeightSnapshot)
        .where(
            TargetWeightSnapshot.account_id == account_id,
            TargetWeightSnapshot.portfolio_id == portfolio_id,
            TargetWeightSnapshot.normalized_weights_kind != "null",
            TargetWeightSnapshot.normalized_weights_kind != "missing",
        )
        .order_by(desc(TargetWeightSnapshot.id))
        .limit(1)
    )
    return (await _snapshot_views(session, [snapshot]))[0] if snapshot is not None else None


async def get_previous_account_target_snapshots(
    session: AsyncSession,
    account_id: int,
    portfolio_id: int,
    *,
    before_id: int,
    limit: int = 50,
) -> list[TargetWeightSnapshotData]:
    """读取同一账户组合下早于指定快照的近期目标，批量读取对应明细。"""
    result = await session.execute(
        select(TargetWeightSnapshot)
        .where(
            TargetWeightSnapshot.account_id == account_id,
            TargetWeightSnapshot.portfolio_id == portfolio_id,
            TargetWeightSnapshot.normalized_weights_kind != "null",
            TargetWeightSnapshot.normalized_weights_kind != "missing",
            col(TargetWeightSnapshot.execution_id).is_not(None),
            col(TargetWeightSnapshot.id) < before_id,
        )
        .order_by(desc(TargetWeightSnapshot.id))
        .limit(limit)
    )
    return await _snapshot_views(session, list(result.scalars()))


async def get_latest_account_target_snapshots_for_accounts(
    session: AsyncSession,
    pairs: list[tuple[int, int]],
) -> dict[int, TargetWeightSnapshotData]:
    """以窗口查询和一次明细查询，读取各账户在当前组合下的最新目标。"""
    if not pairs:
        return {}
    wanted = set(pairs)
    row_number = (
        func.row_number()
        .over(
            partition_by=(col(TargetWeightSnapshot.account_id), col(TargetWeightSnapshot.portfolio_id)),
            order_by=desc(col(TargetWeightSnapshot.id)),
        )
        .label("rn")
    )
    ranked = (
        select(TargetWeightSnapshot, row_number)
        .where(
            col(TargetWeightSnapshot.account_id).in_([account for account, _ in pairs]),
            col(TargetWeightSnapshot.portfolio_id).in_([portfolio for _, portfolio in pairs]),
            TargetWeightSnapshot.normalized_weights_kind != "null",
            TargetWeightSnapshot.normalized_weights_kind != "missing",
        )
        .subquery()
    )
    statement = select(TargetWeightSnapshot).from_statement(sa.select(ranked).where(ranked.c.rn <= 1))
    snapshots = [
        snapshot
        for snapshot in (await session.execute(statement)).scalars()
        if (snapshot.account_id, snapshot.portfolio_id) in wanted
    ]
    return {
        snapshot.account_id: snapshot
        for snapshot in await _snapshot_views(session, snapshots)
        if snapshot.account_id is not None
    }


async def read_target_snapshot_weights(
    session: AsyncSession,
    *,
    account_id: int,
    execution_id: str | None,
) -> dict[str, float] | None:
    """按执行定位账户权重，不加载整个目标历史或原始权重。"""
    if execution_id is None:
        return None
    head = (
        (
            await session.execute(
                sa.select(
                    col(TargetWeightSnapshot.id),
                    col(TargetWeightSnapshot.normalized_weights_kind),
                    col(TargetWeightSnapshot.weights_metadata),
                )
                .where(
                    col(TargetWeightSnapshot.account_id) == account_id,
                    col(TargetWeightSnapshot.execution_id) == execution_id,
                )
                .order_by(desc(TargetWeightSnapshot.id))
                .limit(1)
            )
        )
        .mappings()
        .first()
    )
    if head is None or head["normalized_weights_kind"] in {"null", "missing"}:
        return None
    if head["normalized_weights_kind"] == "invalid":
        invalid = head["weights_metadata"]["normalized_weights"]
        return cast(
            dict[str, float], read_columns(invalid["columns"], invalid["audit_evidence"], {"value": "json"})["value"]
        )
    rows = (
        await session.execute(
            sa.select(target_weight_snapshot_weight)
            .where(
                target_weight_snapshot_weight.c.snapshot_id == head["id"],
                target_weight_snapshot_weight.c.role == "normalized_weights",
            )
            .order_by(target_weight_snapshot_weight.c.map_key)
        )
    ).mappings()
    return cast(
        dict[str, float],
        {row["map_key"]: read_columns(dict(row), row["audit_evidence"], {"value": "number"})["value"] for row in rows},
    )


def target_snapshot_public(
    snapshot: TargetWeightSnapshotData | None,
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
    stored = snapshot.raw_weights if weight_kind == "raw" else snapshot.normalized_weights
    return TargetWeightSnapshotPublic(
        weights=weights if weights is not None else (stored or {}),
        quantities=quantities,
        strategy_weights=strategy_weights,
        account_weights=account_weights,
        sizing=sizing,
        calculated_at=snapshot.calculated_at,
        source=cast(Literal["manual", "execution"], snapshot.source),
        execution_id=snapshot.execution_id,
        context_account_id=snapshot.account_id,
    )
