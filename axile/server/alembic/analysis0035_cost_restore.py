"""Frozen revision 0035 downgrade support for compact cost compatibility JSON.

This module deliberately imports no runtime analysis models: newer application
code must not change the meaning of a historical Alembic downgrade.
"""

import json
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa

_BATCH_SIZE = 100
_RAW_FIELDS = ("status", "task_status", "error", "outcome", "outcome_reason", "reason_code", "msg", "execution_kind")
_SYMBOL_FIELDS = ("status", "error", "outcome", "outcome_reason", "reason_code")
_TRADE_FIELDS = {
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
_TRANSACTION_FIELDS = {
    **{name: name for name in ("symbol", "side", "quantity", "price", "reference", "time", "before", "target")},
    "referenceSource": "reference_source",
    "endTime": "end_time",
    "timeEstimated": "time_estimated",
}
_SUMMARY_FIELDS = {
    **{name: name for name in ("value", "cost", "coverage", "covered", "count")},
    "lossBp": "loss_bp",
    "amountComplete": "amount_complete",
    "feeCovered": "fee_covered",
}


def _children(connection, table: str, snapshot_id: str, record_id: int, ordering: str):
    """Read one execution's compact children, independently of total history size."""
    return connection.execute(
        sa.text(f"SELECT * FROM {table} WHERE snapshot_id=:snapshot_id AND record_id=:record_id ORDER BY {ordering}"),
        {"snapshot_id": snapshot_id, "record_id": record_id},
    ).mappings()


def _summary(connection, snapshot_id: str, record_id: int) -> dict:
    """Freeze the original ordered, amount-weighted trade summary without a list."""
    count = valued = covered = fee_covered = 0
    value = cost = covered_value = weighted = 0.0
    fees: dict[str, float] = defaultdict(float)
    for trade in _children(connection, "performance_cost_trade", snapshot_id, record_id, "id"):
        count += 1
        if trade["value"] is not None:
            valued += 1
            value += trade["value"]
            if trade["cost"] is not None:
                covered += 1
                covered_value += trade["value"]
                cost += trade["cost"]
                weighted += trade["loss_bp"] * trade["value"]
        if trade["fee"] is not None and trade["fee_currency"]:
            fees[trade["fee_currency"]] += trade["fee"]
            fee_covered += 1
    return {
        "value": value if valued else None,
        "cost": cost if covered else None,
        "lossBp": weighted / covered_value if covered_value > 0 else None,
        "coverage": covered_value / value if valued == count and value > 0 else None,
        "covered": covered,
        "count": count,
        "amountComplete": valued == count,
        "fees": dict(fees),
        "feeCovered": fee_covered,
    }


def _execution_payload(connection, row: Mapping[str, Any]) -> dict:
    """Restore scalar headers and presence-aware preview evidence for one row."""
    snapshot_id, record_id = row["snapshot_id"], row["record_id"]
    raw = {name: row["raw_" + name] for index, name in enumerate(_RAW_FIELDS) if row["raw_present"] & (1 << index)}
    if row["has_symbol_results"]:
        raw["symbol_results"] = {
            symbol["symbol"]: {
                name: symbol[name] for index, name in enumerate(_SYMBOL_FIELDS) if symbol["present"] & (1 << index)
            }
            for symbol in _children(connection, "performance_cost_execution_symbol", snapshot_id, record_id, "symbol")
        }
    fees: dict[int, dict] = defaultdict(dict)
    for fee in _children(
        connection, "performance_cost_transaction_fee", snapshot_id, record_id, "transaction_idx,currency"
    ):
        fees[fee["transaction_idx"]][fee["currency"]] = fee["value"]
    transactions = []
    for transaction in _children(connection, "performance_cost_transaction", snapshot_id, record_id, "idx"):
        summary = {public: transaction[column] for public, column in _SUMMARY_FIELDS.items()}
        summary["amountComplete"] = bool(summary["amountComplete"])
        summary["fees"] = fees[transaction["idx"]]
        value = {public: transaction[column] for public, column in _TRANSACTION_FIELDS.items()}
        value["timeEstimated"] = bool(value["timeEstimated"])
        transactions.append({**value, "summary": summary})
    positions = [
        {name: position[name] for name in ("symbol", "quantity", "direction")}
        for position in _children(connection, "performance_cost_position", snapshot_id, record_id, "idx")
    ]
    attempts = [
        {name: attempt[name] for name in ("symbol", "planned", "filled", "reason")}
        for attempt in _children(connection, "performance_cost_attempt", snapshot_id, record_id, "idx")
    ]
    return {
        "key": row["key"],
        "record": {
            "id": record_id,
            "execution_id": row["execution_id"],
            "created_at": row["created_at"],
            "is_success": row["is_success"],
            "raw_result": raw,
        },
        "noop": bool(row["noop"]),
        "tradeCount": row["trade_count"],
        "durationSec": row["duration_sec"],
        "symbolCount": row["symbol_count"],
        "positionCount": row["position_count"],
        "positions": positions if row["positions_present"] else None,
        "transactions": transactions,
        "reason": row["reason"],
        "attempts": attempts,
    }


def _restore_executions(connection, snapshot_id: str) -> None:
    """Use keyset pages so restoring a growing history never collects all rows."""
    after = None
    while True:
        predicate = "" if after is None else " AND record_id>:after"
        rows = (
            connection.execute(
                sa.text(
                    "SELECT * FROM performance_cost_execution WHERE snapshot_id=:snapshot_id"
                    + predicate
                    + " ORDER BY record_id LIMIT :limit"
                ),
                {"snapshot_id": snapshot_id, "after": after, "limit": _BATCH_SIZE},
            )
            .mappings()
            .all()
        )
        if not rows:
            return
        for row in rows:
            payload = json.loads(row["payload"])
            if not payload:
                payload = _execution_payload(connection, row)
            if "summary" not in payload:
                payload["summary"] = _summary(connection, snapshot_id, row["record_id"])
            connection.execute(
                sa.text(
                    "UPDATE performance_cost_execution SET payload=:payload WHERE snapshot_id=:snapshot_id AND record_id=:record_id"
                ),
                {
                    "snapshot_id": snapshot_id,
                    "record_id": row["record_id"],
                    "payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
                },
            )
        after = rows[-1]["record_id"]


def _restore_trades(connection, snapshot_id: str) -> None:
    """Rebuild only missing legacy payloads from their scalar fact columns."""
    after = None
    while True:
        predicate = "" if after is None else " AND id>:after"
        rows = (
            connection.execute(
                sa.text(
                    "SELECT * FROM performance_cost_trade WHERE snapshot_id=:snapshot_id"
                    + predicate
                    + " ORDER BY id LIMIT :limit"
                ),
                {"snapshot_id": snapshot_id, "after": after, "limit": _BATCH_SIZE},
            )
            .mappings()
            .all()
        )
        if not rows:
            return
        for row in rows:
            if json.loads(row["payload"]):
                continue
            payload = {public: row[column] for public, column in _TRADE_FIELDS.items()}
            payload["timeEstimated"] = bool(payload["timeEstimated"])
            connection.execute(
                sa.text("UPDATE performance_cost_trade SET payload=:payload WHERE snapshot_id=:snapshot_id AND id=:id"),
                {
                    "snapshot_id": snapshot_id,
                    "id": row["id"],
                    "payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
                },
            )
        after = rows[-1]["id"]


def restore_cost_payloads(connection) -> None:
    """Restore JSON compatibility before revision 0035 removes typed results.

    Only one execution and bounded scalar pages are materialized at a time.
    Full summaries are also restored for normalized result snapshots so the
    downgrade caller can assemble range execution arrays inside SQLite.
    """
    snapshots = connection.execute(
        sa.text(
            "SELECT id FROM performance_snapshot WHERE cost_schema_version>=2 OR result_schema_version>=2 ORDER BY id"
        )
    ).scalars()
    for snapshot_id in snapshots:
        _restore_executions(connection, snapshot_id)
        _restore_trades(connection, snapshot_id)
        connection.execute(
            sa.text("UPDATE performance_snapshot SET cost_schema_version=1 WHERE id=:id AND cost_schema_version>=2"),
            {"id": snapshot_id},
        )
