"""Reconstruct compatibility responses from immutable typed analysis facts."""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa

from tests.fixtures.rust_store.oracle_tables import (
    cost_attempt,
    cost_execution_symbol,
    cost_position,
    cost_transaction,
    cost_transaction_fee,
)

READ_BATCH_SIZE = 500
RAW_FIELDS = ("status", "task_status", "error", "outcome", "outcome_reason", "reason_code", "msg", "execution_kind")
SYMBOL_FIELDS = ("status", "error", "outcome", "outcome_reason", "reason_code")
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
