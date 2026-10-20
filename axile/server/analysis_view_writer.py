"""Freeze read views inside the explicit Python compatibility publication transaction."""

import sqlalchemy as sa

from axile.server.alembic.analysis0036_read_views import FEE_INSERT, MARK_COMPLETE, SUMMARY_INSERT


async def prepare_read_views(session, snapshot_id: str) -> bool:
    """Prepare one typed snapshot without committing or decoding historical JSON.

    The caller owns the surrounding publication transaction and the source-version
    check. Unsupported legacy snapshots keep their existing marker and return False.
    """
    identity = {"snapshot_id": snapshot_id}
    row = (
        await session.execute(
            sa.text("SELECT result_schema_version,cost_schema_version FROM performance_snapshot WHERE id=:snapshot_id"),
            identity,
        )
    ).one_or_none()
    if row is None or row.result_schema_version != 2 or row.cost_schema_version != 1:
        return False
    for name in ("performance_execution_fee", "performance_execution_summary"):
        await session.execute(sa.text(f"DELETE FROM {name} WHERE snapshot_id=:snapshot_id"), identity)
    for sql in (SUMMARY_INSERT, FEE_INSERT, MARK_COMPLETE):
        await session.execute(sa.text(sql), identity)
    return True
