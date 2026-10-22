"""Single-worker, durable performance queue and atomic snapshot publication."""

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert

from axile.server.db.models.analysis import analysis_snapshot as snapshots
from axile.server.db.models.analysis import analysis_state as states
from axile.server.performance_replay import ENGINE_VERSION

# 独立权益账本替代日内收益求和；重建旧回测曲线与同版卡片。
LOGIC_VERSION = "18"
RETRY_DELAYS = (5, 30, 120)


async def enqueue(session, account_id: int, force: bool = False) -> None:
    """Coalesce refresh requests; a ready or exhausted batch can be forced again."""
    await session.execute(insert(states).values(account_id=account_id).on_conflict_do_nothing())
    if force:
        await session.execute(
            states.update()
            .where(
                states.c.account_id == account_id,
                states.c.running_version.is_(None),
                sa.or_(
                    states.c.error.is_not(None),
                    sa.and_(states.c.published_version == states.c.source_version, ~states.c.requested),
                ),
            )
            .values(source_version=states.c.source_version + 1, failures=0, retry_at=0, error=None)
        )
    await session.execute(states.update().where(states.c.account_id == account_id).values(requested=True))
    await session.commit()


async def read_snapshot_status(session, account_id: int) -> str:
    """仅读取发布状态，等待计算时不反复展开完整绩效快照。"""
    row = (
        (
            await session.execute(
                sa.select(
                    states,
                    snapshots.c.id.label("snapshot_id"),
                    snapshots.c.logic_version.label("snapshot_logic_version"),
                    snapshots.c.engine_version.label("snapshot_engine_version"),
                )
                .select_from(states.outerjoin(snapshots, states.c.current_snapshot == snapshots.c.id))
                .where(states.c.account_id == account_id)
            )
        )
        .mappings()
        .first()
    )
    if (
        row
        and row["snapshot_id"] is not None
        and (row["snapshot_logic_version"] != LOGIC_VERSION or row["snapshot_engine_version"] != ENGINE_VERSION)
    ):
        return "pending"
    return _snapshot_status(row, row is not None and row["snapshot_id"] is not None)


def _snapshot_status(row, has_snapshot: bool):
    """共享卡片和绩效页的发布状态。"""
    status = "empty"
    if row:
        if row["error"]:
            status = "failed"
        elif (
            row["requested"] or row["running_version"] is not None or row["published_version"] != row["source_version"]
        ):
            status = (
                "stale"
                if has_snapshot
                else "pending"
                if row["requested"] or row["running_version"] is not None
                else "empty"
            )
        elif has_snapshot:
            status = "ready"
    return status
