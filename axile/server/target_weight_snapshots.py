"""目标权重快照的查询与持久化服务."""

from __future__ import annotations

from typing import Literal

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, desc, select

from axile.server.analysis_observations import write_observation_fact
from axile.server.db.models import ExecuteRecord, TargetSizingPublic, TargetWeightSnapshot, TargetWeightSnapshotPublic


async def append_target_weight_snapshot(
    session: AsyncSession,
    *,
    portfolio_id: int,
    account_id: int | None,
    raw_weights: dict[str, float] | None,
    normalized_weights: dict[str, float] | None,
    source: Literal["manual", "execution"],
    execution_id: str | None = None,
) -> TargetWeightSnapshot:
    """追加并提交一条成功计算快照."""
    snapshot = TargetWeightSnapshot(
        portfolio_id=portfolio_id,
        account_id=account_id,
        raw_weights=raw_weights,
        normalized_weights=normalized_weights,
        source=source,
        execution_id=execution_id,
    )
    session.add(snapshot)
    if account_id is not None and execution_id is not None and normalized_weights is not None:
        # 常规路径先写目标再执行；兼容目标晚于执行落库时，只刷新该次执行的回放事实。
        record = await session.scalar(
            select(ExecuteRecord).where(
                ExecuteRecord.account_id == account_id,
                ExecuteRecord.execution_id == execution_id,
            )
        )
        if record is not None:
            await write_observation_fact(session, record, fallback_weights=normalized_weights)
    await session.commit()
    await session.refresh(snapshot)
    return snapshot


async def get_latest_portfolio_target_snapshot(
    session: AsyncSession,
    portfolio_id: int,
) -> TargetWeightSnapshot | None:
    """读取组合最近一条具有原始权重的计算快照."""
    return await session.scalar(
        select(TargetWeightSnapshot)
        .where(
            TargetWeightSnapshot.portfolio_id == portfolio_id,
            col(TargetWeightSnapshot.raw_weights).is_not(None),
        )
        .order_by(desc(TargetWeightSnapshot.id))
        .limit(1)
    )


async def get_latest_account_target_snapshot(
    session: AsyncSession,
    account_id: int,
    portfolio_id: int,
) -> TargetWeightSnapshot | None:
    """读取账户在当前组合下最近一条归一化权重快照."""
    return await session.scalar(
        select(TargetWeightSnapshot)
        .where(
            TargetWeightSnapshot.account_id == account_id,
            TargetWeightSnapshot.portfolio_id == portfolio_id,
            col(TargetWeightSnapshot.normalized_weights).is_not(None),
        )
        .order_by(desc(TargetWeightSnapshot.id))
        .limit(1)
    )


async def get_previous_account_target_snapshots(
    session: AsyncSession,
    account_id: int,
    portfolio_id: int,
    *,
    before_id: int,
    limit: int = 50,
) -> list[TargetWeightSnapshot]:
    """读取同一账户组合下、早于指定快照的近期归一化权重快照."""
    result = await session.execute(
        select(TargetWeightSnapshot)
        .where(
            TargetWeightSnapshot.account_id == account_id,
            TargetWeightSnapshot.portfolio_id == portfolio_id,
            col(TargetWeightSnapshot.normalized_weights).is_not(None),
            col(TargetWeightSnapshot.execution_id).is_not(None),
            col(TargetWeightSnapshot.id) < before_id,
        )
        .order_by(desc(TargetWeightSnapshot.id))
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_latest_account_target_snapshots_for_accounts(
    session: AsyncSession,
    pairs: list[tuple[int, int]],
) -> dict[int, TargetWeightSnapshot]:
    """
    一次查出多个账户在各自当前组合下最新的归一化目标快照.

    Parameters
    ----------
    session : AsyncSession
        当前数据库会话。
    pairs : list[tuple[int, int]]
        ``(account_id, portfolio_id)`` 列表；只返回仍匹配该组合的快照。

    Returns
    -------
    dict[int, TargetWeightSnapshot]
        账户 ID 到最新归一化快照的映射；没有快照的账户不出现。

    Notes
    -----
    仪表盘按账户循环取目标会变成 N+1。这里用窗口函数一次取回，与执行记录
    批量查询同一形状。
    """
    if not pairs:
        return {}

    wanted = set(pairs)
    account_ids = [account_id for account_id, _portfolio_id in pairs]
    portfolio_ids = list({portfolio_id for _account_id, portfolio_id in pairs})
    row_number = (
        func.row_number().over(
            partition_by=(col(TargetWeightSnapshot.account_id), col(TargetWeightSnapshot.portfolio_id)),
            order_by=desc(col(TargetWeightSnapshot.id)),
        )
    ).label("rn")
    ranked = (
        select(TargetWeightSnapshot, row_number)
        .where(
            col(TargetWeightSnapshot.account_id).in_(account_ids),
            col(TargetWeightSnapshot.portfolio_id).in_(portfolio_ids),
            col(TargetWeightSnapshot.normalized_weights).is_not(None),
        )
        .subquery()
    )
    stmt = select(TargetWeightSnapshot).from_statement(select(ranked).where(ranked.c.rn <= 1))

    latest: dict[int, TargetWeightSnapshot] = {}
    for snapshot in (await session.execute(stmt)).scalars().all():
        if snapshot.account_id is None:
            continue
        if (snapshot.account_id, snapshot.portfolio_id) not in wanted:
            continue
        latest[snapshot.account_id] = snapshot
    return latest


def target_snapshot_public(
    snapshot: TargetWeightSnapshot | None,
    *,
    weight_kind: Literal["raw", "normalized"],
    weights: dict[str, float] | None = None,
    quantities: dict[str, float] | None = None,
    strategy_weights: dict[str, float] | None = None,
    account_weights: dict[str, float] | None = None,
    sizing: TargetSizingPublic | None = None,
) -> TargetWeightSnapshotPublic:
    """把数据库快照转换为稳定的页面响应；无记录时返回未计算态."""
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
        source=snapshot.source,
        execution_id=snapshot.execution_id,
        context_account_id=snapshot.account_id,
    )
