"""Typed SQLite facts consumed by the Rust cost reader without JSON decoding."""

from collections.abc import Iterable, Iterator
from typing import Any

import sqlalchemy as sa

from tests.fixtures.rust_store.oracle_tables import (
    cost_attempt,
    cost_execution,
    cost_execution_symbol,
    cost_position,
    cost_trade,
    cost_transaction,
    cost_transaction_fee,
)
from tests.unit.server._performance_costs_oracle import execution_list_payload, mapping, timestamp

COST_FACT_SCHEMA_VERSION = 1
RAW_FIELDS = ("status", "task_status", "error", "outcome", "outcome_reason", "reason_code", "msg", "execution_kind")
SYMBOL_FIELDS = ("status", "error", "outcome", "outcome_reason", "reason_code")
# Delete children before their source rows even on connections without foreign_keys.
COST_FACT_TABLES = (
    cost_transaction_fee,
    cost_transaction,
    cost_attempt,
    cost_position,
    cost_execution_symbol,
    cost_trade,
    cost_execution,
)


def _present(value: dict, fields: tuple[str, ...]) -> int:
    return sum(1 << index for index, name in enumerate(fields) if name in value)


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise ValueError("Cost fact text fields must contain strings or null")


def execution_fact(row: dict, snapshot_id: str) -> dict:
    """Extract scalar headers, retaining missing versus null status fields."""
    payload = execution_list_payload(row["payload"])
    record = mapping(payload.get("record"))
    raw = mapping(record.get("raw_result"))
    return {
        **row,
        "snapshot_id": snapshot_id,
        "key": _text(payload.get("key")),
        "execution_id": _text(record.get("execution_id")),
        "created_at": _text(record.get("created_at")),
        "is_success": record.get("is_success"),
        "duration_sec": payload.get("durationSec"),
        "trade_count": payload.get("tradeCount"),
        "symbol_count": payload.get("symbolCount"),
        "position_count": payload.get("positionCount"),
        "positions_present": isinstance(payload.get("positions"), list),
        "reason": _text(payload.get("reason")),
        "raw_present": _present(raw, RAW_FIELDS),
        "has_symbol_results": isinstance(raw.get("symbol_results"), dict),
        **{"raw_" + name: _text(raw.get(name)) for name in RAW_FIELDS},
    }


def trade_fact(row: dict, snapshot_id: str) -> dict:
    """Store every trade API field as a scalar while preserving legacy payloads."""
    payload = row["payload"]
    side = payload.get("side", "none")
    if side not in ("buy", "sell", "none"):
        raise ValueError("Cost fact side must be buy, sell or none")
    return {
        **row,
        "snapshot_id": snapshot_id,
        "side": side,
        "quantity": payload.get("quantity"),
        "price": payload.get("price"),
        "reference": payload.get("reference"),
        "reference_source": _text(payload.get("referenceSource")),
    }


def _execution_children(row: dict, snapshot_id: str) -> Iterator[tuple[sa.Table, dict]]:
    payload = execution_list_payload(row["payload"])
    base = {"snapshot_id": snapshot_id, "record_id": row["record_id"]}
    raw = mapping(mapping(payload.get("record")).get("raw_result"))
    for symbol, result in mapping(raw.get("symbol_results")).items():
        result = mapping(result)
        yield (
            cost_execution_symbol,
            {
                **base,
                "symbol": _text(symbol),
                "present": _present(result, SYMBOL_FIELDS),
                **{name: _text(result.get(name)) for name in SYMBOL_FIELDS},
            },
        )
    for index, position in enumerate(payload.get("positions") or []):
        yield (
            cost_position,
            {
                **base,
                "idx": index,
                "symbol": _text(position.get("symbol")),
                "quantity": position.get("quantity"),
                "direction": _text(position.get("direction")),
            },
        )
    for index, attempt in enumerate(payload.get("attempts") or []):
        yield (
            cost_attempt,
            {
                **base,
                "idx": index,
                "symbol": _text(attempt.get("symbol")),
                "planned": attempt.get("planned"),
                "filled": attempt.get("filled"),
                "reason": _text(attempt.get("reason")),
            },
        )
    for index, transaction in enumerate(payload.get("transactions") or []):
        summary = transaction["summary"]
        yield (
            cost_transaction,
            {
                **base,
                "idx": index,
                **{
                    name: transaction.get(name)
                    for name in ("quantity", "price", "reference", "time", "before", "target")
                },
                "symbol": _text(transaction.get("symbol")),
                "side": _text(transaction.get("side")),
                "reference_source": _text(transaction.get("referenceSource")),
                "end_time": transaction.get("endTime"),
                "time_estimated": transaction.get("timeEstimated", False),
                **{name: summary.get(name) for name in ("value", "cost", "coverage", "covered", "count")},
                "loss_bp": summary.get("lossBp"),
                "amount_complete": summary["amountComplete"],
                "fee_covered": summary.get("feeCovered"),
            },
        )
        for currency, value in summary["fees"].items():
            yield cost_transaction_fee, {**base, "transaction_idx": index, "currency": _text(currency), "value": value}


def range_facts(snapshot_id: str, ranges: dict) -> Iterator[dict]:
    """保留历史0034迁移调用的纯转换，不注册或写入范围副本。"""
    for key, value in ranges.items():
        performance = value["performance"]
        baseline, end = performance["baseline"], performance["end"]
        yield {
            "snapshot_id": snapshot_id,
            "range_key": key,
            "baseline": timestamp(baseline) if baseline else None,
            "end": timestamp(end) if end else None,
        }


def iter_cost_fact_batches(
    snapshot_id: str,
    projected: Iterable[dict],
    cost_rows: Iterable[dict],
    ranges: dict,
    batch_size: int = 500,
) -> Iterator[tuple[sa.Table, list[dict]]]:
    """Bound temporary publication objects; callers keep all inserts in one transaction."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    pending: dict[sa.Table, list[dict]] = {}

    def rows() -> Iterator[tuple[sa.Table, dict]]:
        for row in projected:
            yield cost_execution, execution_fact(row, snapshot_id)
            yield from _execution_children(row, snapshot_id)
        for row in cost_rows:
            yield cost_trade, trade_fact(row, snapshot_id)

    for table, row in rows():
        batch = pending.setdefault(table, [])
        batch.append(row)
        if len(batch) >= batch_size:
            yield table, batch
            pending[table] = []
    for table, batch in pending.items():
        if batch:
            yield table, batch


def iter_execution_child_batches(
    snapshot_id: str, projected: Iterable[dict], batch_size: int = 500
) -> Iterator[tuple[sa.Table, list[dict]]]:
    """Backfill preview rows without copying historical execution payloads."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    pending: dict[sa.Table, list[dict]] = {}
    for row in projected:
        for table, value in _execution_children(row, snapshot_id):
            batch = pending.setdefault(table, [])
            batch.append(value)
            if len(batch) >= batch_size:
                yield table, batch
                pending[table] = []
    for table, batch in pending.items():
        if batch:
            yield table, batch
