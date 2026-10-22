"""Freeze each execution's typed analysis facts once at the Python write boundary."""

import math
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert
from sqlmodel import SQLModel

from axile.executor.session_closed import session_closed_scope
from axile.server.analysis_cost_facts import execution_fact, iter_execution_child_batches, trade_fact
from axile.server.db.models.analysis_source import (
    source_attempt,
    source_execution,
    source_execution_symbol,
    source_position,
    source_trade,
    source_transaction,
    source_transaction_fee,
)
from axile.server.execution.legacy_compat import normalize_legacy_result
from axile.server.performance_costs import mapping, project_execution, timestamp

SOURCE_FACT_SCHEMA_VERSION = 1
SOURCE_FACT_TABLES = (
    source_transaction_fee,
    source_transaction,
    source_attempt,
    source_position,
    source_execution_symbol,
    source_trade,
    source_execution,
)
_PREVIEW_TABLES = {table.name.removeprefix("analysis_source_"): table for table in SOURCE_FACT_TABLES[:-2]}
_PROJECTION_ERRORS = (ValueError, TypeError, KeyError, AttributeError, OverflowError)


@dataclass(frozen=True, slots=True)
class SourceBackfillProgress:
    """A bounded batch result; unsupported markers require explicit legacy handling."""

    processed: int
    complete: bool
    unsupported: int


def _source_event(record: Any) -> tuple[str | None, str | None]:
    """Keep the existing execution event wording without holding historical records."""
    if record.is_success == 1:
        return None, None
    raw = mapping(normalize_legacy_result(record.result))
    status = raw.get("status")
    labels: dict[str | None, str] = {
        "SUCCEEDED": "调仓完成",
        "NOOP": "无需调仓",
        "PARTIAL": "执行未全部完成",
        "FAILED": "执行失败",
        "BLOCKED": "未执行",
    }
    label = labels.get(status, "执行状态未知")
    scope = session_closed_scope(raw)
    if scope == "all":
        label = "休市跳过"
    elif scope == "partial":
        label = "部分品种休市 · 执行未全部完成"
    if raw.get("task_status") == "TERMINATED":
        label = "执行已终止"
    reason = raw.get("error") if status in {"FAILED", "BLOCKED", "PARTIAL"} else None
    return label, (f"{label} · {reason}" if reason else label)[:500]


def _validate(table: sa.Table, values: dict) -> None:
    """Reject unsupported scalar types before SQLite can silently coerce them."""
    for name, value in values.items():
        column = table.c[name]
        if value is None:
            if not column.nullable:
                raise ValueError(f"Missing required source fact {table.name}.{name}")
            continue
        if isinstance(column.type, sa.Boolean):
            valid = isinstance(value, bool) or type(value) is int and value in (0, 1)
        elif isinstance(column.type, sa.Integer):
            valid = type(value) is int and -(1 << 63) <= value < (1 << 63)
        elif isinstance(column.type, sa.Float):
            valid = type(value) in (int, float) and math.isfinite(value)
        elif isinstance(column.type, sa.Text):
            valid = isinstance(value, str)
        else:
            raise TypeError(f"Unsupported source fact column {table.name}.{name}")
        if not valid:
            raise ValueError(f"Unsupported source fact scalar {table.name}.{name}")


def source_fact_values(record: Any) -> tuple[dict, dict[sa.Table, list[dict]]]:
    """Project one execution, preserving typed headers, preview evidence and trade order.

    Notes
    -----
    No raw input, raw result, JSON payload or snapshot identifier is retained in
    the returned facts. The source row is immutable until its parent changes.
    """
    if record.id is None:
        raise ValueError("Execution record must be flushed before writing source facts")
    payload, fills = project_execution(record)
    projected = {
        "record_id": record.id,
        "time": timestamp(record.created_at),
        "success": record.is_success == 1,
        "noop": payload["noop"],
        "payload": payload,
    }
    scalar = {
        key: value for key, value in execution_fact(projected, "").items() if key not in {"snapshot_id", "payload"}
    }
    event_tag, event_text = _source_event(record)
    scalar.update(
        account_id=record.account_id,
        projection_version=SOURCE_FACT_SCHEMA_VERSION,
        projection_error=None,
        event_tag=event_tag,
        event_text=event_text,
    )
    children: dict[sa.Table, list[dict]] = {}
    for table, batch in iter_execution_child_batches("", [projected], batch_size=100):
        destination = _PREVIEW_TABLES[table.name.removeprefix("performance_cost_")]
        children.setdefault(destination, []).extend(
            {key: value for key, value in row.items() if key != "snapshot_id"} for row in batch
        )
    for index, fill in enumerate(fills):
        row = trade_fact(
            {
                "record_id": record.id,
                "symbol": fill["symbol"],
                "time": fill["time"],
                "day": fill["day"],
                "value": fill["value"],
                "cost": fill["cost"],
                "loss_bp": fill["lossBp"],
                "fee": fill["fee"],
                "fee_currency": fill["feeCurrency"],
                "estimated": fill["timeEstimated"],
                "payload": fill,
            },
            "",
        )
        values = {key: value for key, value in row.items() if key not in {"snapshot_id", "payload"}}
        children.setdefault(source_trade, []).append({**values, "trade_index": index})
    _validate(source_execution, scalar)
    for table, rows in children.items():
        for row in rows:
            _validate(table, row)
    return scalar, children


def _unsupported_marker(record: Any, error: Exception) -> dict:
    values: dict[str, Any] = {column.name: None for column in source_execution.c if column.nullable}
    values.update(
        record_id=record.id,
        account_id=record.account_id,
        projection_version=0,
        projection_error=f"{type(error).__name__}: {error}"[:300],
        execution_id=record.execution_id if isinstance(record.execution_id, str) else None,
        created_at=record.created_at if isinstance(record.created_at, str) else None,
        success=False,
        noop=False,
        positions_present=False,
        raw_present=0,
        has_symbol_results=False,
    )
    return values


async def _replace_source(session, scalar: dict, children: dict[sa.Table, list[dict]]) -> None:
    statement = insert(source_execution).values(**scalar)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[source_execution.c.record_id],
            set_={key: statement.excluded[key] for key in scalar if key != "record_id"},
        )
    )
    for table in SOURCE_FACT_TABLES[:-1]:
        await session.execute(table.delete().where(table.c.record_id == scalar["record_id"]))
    for table, rows in children.items():
        for start in range(0, len(rows), 100):
            await session.execute(table.insert(), rows[start : start + 100])


async def write_source_facts(session, record: Any) -> bool:
    """Write typed facts in the caller's transaction; unsupported evidence gets a marker.

    Returns
    -------
    bool
        Whether this record has a complete supported projection. False leaves an
        explicit version-zero row, so native analysis cannot silently skip it.
    """
    await session.flush()
    if record.id is None:
        raise ValueError("Execution record must be flushed before writing source facts")
    try:
        scalar, children = source_fact_values(record)
        supported = True
    except _PROJECTION_ERRORS as error:
        scalar, children = _unsupported_marker(record, error), {}
        supported = False
    await _replace_source(session, scalar, children)
    return supported


def _record_scope(records: sa.Table, account_id: int, through_record_id: int | None) -> list:
    conditions = [records.c.account_id == account_id]
    if through_record_id is not None:
        conditions.append(records.c.id <= through_record_id)
    return conditions


async def _unsupported_count(session, account_id: int, through_record_id: int | None) -> int:
    conditions = [
        source_execution.c.account_id == account_id,
        source_execution.c.projection_version != SOURCE_FACT_SCHEMA_VERSION,
    ]
    if through_record_id is not None:
        conditions.append(source_execution.c.record_id <= through_record_id)
    return await session.scalar(sa.select(sa.func.count()).select_from(source_execution).where(*conditions)) or 0


async def backfill_source_facts(
    session, account_id: int, batch_size: int = 100, *, through_record_id: int | None = None
) -> SourceBackfillProgress:
    """Read at most 100 missing canonical records in the caller's transaction."""
    from axile.server.analysis_history import read_analysis_record

    if not 1 <= batch_size <= 100:
        raise ValueError("batch_size must be between 1 and 100")
    records = SQLModel.metadata.tables["executerecord"]
    missing = ~sa.exists(sa.select(1).where(source_execution.c.record_id == records.c.id))
    conditions = [*_record_scope(records, account_id, through_record_id), missing]
    rows = (await session.execute(sa.select(records).where(*conditions).order_by(records.c.id).limit(batch_size))).all()
    for record in rows:
        await write_source_facts(session, await read_analysis_record(session, record))
    remaining = await session.scalar(sa.select(sa.exists().where(*conditions)))
    unsupported = await _unsupported_count(session, account_id, through_record_id)
    return SourceBackfillProgress(processed=len(rows), complete=not remaining, unsupported=unsupported)


async def ensure_source_facts(session, account_id: int, batch_size: int = 100) -> SourceBackfillProgress:
    """Commit bounded source backfills using a dedicated session and a fixed record horizon.

    Notes
    -----
    Completion means every parent has either valid facts or an unsupported marker.
    Callers must require ``unsupported == 0`` before publishing native results.
    Each batch holds the writer slot while deriving facts, preventing stale input
    from replacing a concurrently changed parent's projection.
    """
    if not 1 <= batch_size <= 100:
        raise ValueError("batch_size must be between 1 and 100")
    records = SQLModel.metadata.tables["executerecord"]
    upper = await session.scalar(sa.select(sa.func.max(records.c.id)).where(records.c.account_id == account_id))
    await session.commit()
    if upper is None:
        return SourceBackfillProgress(processed=0, complete=True, unsupported=0)
    processed = 0
    while True:
        await session.execute(sa.text("BEGIN IMMEDIATE"))
        try:
            progress = await backfill_source_facts(session, account_id, batch_size, through_record_id=upper)
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
        processed += progress.processed
        if progress.complete:
            return SourceBackfillProgress(processed=processed, complete=True, unsupported=progress.unsupported)
