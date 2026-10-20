"""Reconstruct compatibility responses from immutable typed analysis facts."""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa

from axile.server.analysis_cost_facts import RAW_FIELDS, SYMBOL_FIELDS
from axile.server.db.models.analysis import (
    cost_attempt,
    cost_execution,
    cost_execution_symbol,
    cost_position,
    cost_trade,
    cost_transaction,
    cost_transaction_fee,
)
from axile.server.performance_costs import timestamp

READ_BATCH_SIZE = 500
TRADE_FIELDS = {
    "symbol": "symbol",
    "day": "day",
    "time": "time",
    "timeEstimated": "estimated",
    "side": "side",
    "quantity": "quantity",
    "price": "price",
    "reference": "reference",
    "referenceSource": "reference_source",
    "value": "value",
    "cost": "cost",
    "lossBp": "loss_bp",
    "fee": "fee",
    "feeCurrency": "fee_currency",
}
TRANSACTION_FIELDS = {
    **{name: name for name in ("symbol", "side", "quantity", "price", "reference", "time", "before", "target")},
    "referenceSource": "reference_source",
    "endTime": "end_time",
    "timeEstimated": "time_estimated",
}
TRANSACTION_SUMMARY_FIELDS = {
    **{name: name for name in ("value", "cost", "coverage", "covered", "count")},
    "lossBp": "loss_bp",
    "amountComplete": "amount_complete",
    "feeCovered": "fee_covered",
}


def scalar_columns(table: sa.Table) -> list:
    """Exclude legacy JSON even when a typed row retains a historical payload."""
    return [column for column in table.c if column.name != "payload"]


def trade_payload(row: Mapping[str, Any]) -> dict:
    """Restore the public trade field names without opening a legacy payload."""
    return {public: row[column] for public, column in TRADE_FIELDS.items()}


def execution_payload(row: Mapping[str, Any], children: Mapping[str, list]) -> dict:
    """Restore compact evidence, preserving null, absent and empty status values."""
    raw = {name: row["raw_" + name] for index, name in enumerate(RAW_FIELDS) if row["raw_present"] & (1 << index)}
    if row["has_symbol_results"]:
        raw["symbol_results"] = {
            symbol["symbol"]: {
                name: symbol[name] for index, name in enumerate(SYMBOL_FIELDS) if symbol["present"] & (1 << index)
            }
            for symbol in children.get("symbols", [])
        }
    fees: dict[int, dict] = defaultdict(dict)
    for fee in children.get("fees", []):
        fees[fee["transaction_idx"]][fee["currency"]] = fee["value"]
    transactions = [
        {
            **{public: transaction[column] for public, column in TRANSACTION_FIELDS.items()},
            "summary": {
                **{public: transaction[column] for public, column in TRANSACTION_SUMMARY_FIELDS.items()},
                "fees": fees[transaction["idx"]],
            },
        }
        for transaction in children.get("transactions", [])
    ]
    positions = [
        {name: position[name] for name in ("symbol", "quantity", "direction")}
        for position in children.get("positions", [])
    ]
    return {
        "key": row["key"],
        "record": {
            "id": row["record_id"],
            "execution_id": row["execution_id"],
            "created_at": row["created_at"],
            "is_success": row["is_success"],
            "raw_result": raw,
        },
        "noop": row["noop"],
        "tradeCount": row["trade_count"],
        "durationSec": row["duration_sec"],
        "symbolCount": row["symbol_count"],
        "positionCount": row["position_count"],
        "positions": positions if row["positions_present"] else None,
        "transactions": transactions,
        "reason": row["reason"],
        "attempts": [
            {name: attempt[name] for name in ("symbol", "planned", "filled", "reason")}
            for attempt in children.get("attempts", [])
        ],
    }


async def read_execution_payloads(session, snapshot_id: str, headers: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Read child facts in bounded groups rather than performing per-execution queries."""
    output = []
    child_tables = (
        ("symbols", cost_execution_symbol, cost_execution_symbol.c.symbol),
        ("positions", cost_position, cost_position.c.idx),
        ("attempts", cost_attempt, cost_attempt.c.idx),
        ("transactions", cost_transaction, cost_transaction.c.idx),
        ("fees", cost_transaction_fee, cost_transaction_fee.c.transaction_idx),
    )
    for start in range(0, len(headers), READ_BATCH_SIZE):
        batch = headers[start : start + READ_BATCH_SIZE]
        record_ids = [row["record_id"] for row in batch]
        children: dict[int, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        for name, table, ordering in child_tables:
            rows = await session.execute(
                sa.select(table)
                .where(table.c.snapshot_id == snapshot_id, table.c.record_id.in_(record_ids))
                .order_by(table.c.record_id, ordering)
            )
            for row in rows.mappings():
                children[row["record_id"]][name].append(row)
        output.extend(execution_payload(row, children[row["record_id"]]) for row in batch)
    return output


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
