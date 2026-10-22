"""已退役完整历史读取的测试 oracle，不进入生产调用链。"""

import sqlalchemy as sa

from axile.server.analysis_cost_reader import READ_BATCH_SIZE, read_execution_payloads, scalar_columns
from axile.server.db.models.analysis import cost_execution, cost_trade
from axile.server.performance_costs import timestamp


def _empty_summary() -> dict:
    return {
        "value": None,
        "cost": None,
        "lossBp": None,
        "coverage": None,
        "covered": 0,
        "count": 0,
        "amountComplete": True,
        "fees": {},
        "feeCovered": 0,
    }


async def _execution_summaries(session, snapshot_id: str, record_ids: list[int]) -> dict[int, dict]:
    """Aggregate numeric facts in SQLite so a large fill list stays out of Python."""
    valid = sa.and_(cost_trade.c.value.is_not(None), cost_trade.c.cost.is_not(None))
    covered_value = sa.func.sum(sa.case((valid, cost_trade.c.value), else_=0))
    rows = await session.execute(
        sa.select(
            cost_trade.c.record_id,
            sa.func.count().label("count"),
            sa.func.count(cost_trade.c.value).label("valued"),
            sa.func.sum(cost_trade.c.value).label("value"),
            sa.func.sum(sa.case((valid, cost_trade.c.cost))).label("cost"),
            sa.func.count(sa.case((valid, 1))).label("covered"),
            covered_value.label("covered_value"),
            sa.func.sum(sa.case((valid, cost_trade.c.loss_bp * cost_trade.c.value), else_=0)).label("weighted"),
        )
        .where(cost_trade.c.snapshot_id == snapshot_id, cost_trade.c.record_id.in_(record_ids))
        .group_by(cost_trade.c.record_id)
    )
    summaries = {record_id: _empty_summary() for record_id in record_ids}
    for row in rows.mappings():
        value, covered = row["value"], row["covered_value"]
        complete = row["count"] == row["valued"]
        summaries[row["record_id"]].update(
            value=value,
            cost=row["cost"],
            lossBp=row["weighted"] / covered if covered > 0 else None,
            coverage=covered / value if complete and value and value > 0 else None,
            covered=row["covered"],
            count=row["count"],
            amountComplete=complete,
        )
    fees = await session.execute(
        sa.select(cost_trade.c.record_id, cost_trade.c.fee_currency, sa.func.sum(cost_trade.c.fee), sa.func.count())
        .where(
            cost_trade.c.snapshot_id == snapshot_id,
            cost_trade.c.record_id.in_(record_ids),
            cost_trade.c.fee.is_not(None),
            cost_trade.c.fee_currency.is_not(None),
            cost_trade.c.fee_currency != "",
        )
        .group_by(cost_trade.c.record_id, cost_trade.c.fee_currency)
    )
    for record_id, currency, value, count in fees:
        summaries[record_id]["fees"][currency] = value
        summaries[record_id]["feeCovered"] += count
    return summaries


async def read_execution_results(session, snapshot_id: str, baseline: str | None, end: str | None) -> list[dict]:
    """Restore legacy full-history output using bounded typed reads and SQL summaries."""
    lower = timestamp(baseline) if baseline else 0
    upper = timestamp(end) if end else -1
    output = []
    after = None
    while True:
        filters = [
            cost_execution.c.snapshot_id == snapshot_id,
            cost_execution.c.time >= lower,
            cost_execution.c.time <= upper,
        ]
        if after is not None:
            time, record_id = after
            filters.append(
                sa.or_(
                    cost_execution.c.time > time,
                    sa.and_(cost_execution.c.time == time, cost_execution.c.record_id > record_id),
                )
            )
        rows = (
            (
                await session.execute(
                    sa.select(*scalar_columns(cost_execution))
                    .where(*filters)
                    .order_by(cost_execution.c.time, cost_execution.c.record_id)
                    .limit(READ_BATCH_SIZE)
                )
            )
            .mappings()
            .all()
        )
        if not rows:
            break
        summaries = await _execution_summaries(session, snapshot_id, [row["record_id"] for row in rows])
        for payload in await read_execution_payloads(session, snapshot_id, rows):
            output.append({**payload, "summary": summaries[payload["record"]["id"]]})
        after = rows[-1]["time"], rows[-1]["record_id"]
        if len(rows) < READ_BATCH_SIZE:
            break
    return output
