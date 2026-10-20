"""Publish typed cost facts entirely inside the Python-owned SQLite transaction."""

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import SQLModel

from axile.server.analysis_cost_facts import COST_FACT_SCHEMA_VERSION, COST_FACT_TABLES
from axile.server.db.models.analysis import (
    analysis_snapshot,
    cost_attempt,
    cost_execution,
    cost_execution_symbol,
    cost_position,
    cost_trade,
    cost_transaction,
    cost_transaction_fee,
)
from axile.server.db.models.analysis_result import RESULT_TABLES
from axile.server.db.models.analysis_source import (
    source_attempt,
    source_execution,
    source_execution_symbol,
    source_position,
    source_trade,
    source_transaction,
    source_transaction_fee,
)

SNAPSHOT_CHILD_TABLES = (*RESULT_TABLES, *COST_FACT_TABLES)

# Explicit columns keep publication independent of source-only diagnostics and of
# deprecated JSON compatibility fields. No historical row is materialized here.
_EXECUTION_COLUMNS = (
    "record_id",
    "time",
    "success",
    "noop",
    "key",
    "execution_id",
    "created_at",
    "is_success",
    "duration_sec",
    "trade_count",
    "symbol_count",
    "position_count",
    "positions_present",
    "reason",
    "raw_present",
    "has_symbol_results",
    "raw_status",
    "raw_task_status",
    "raw_error",
    "raw_outcome",
    "raw_outcome_reason",
    "raw_reason_code",
    "raw_msg",
    "raw_execution_kind",
)
_TRADE_COLUMNS = (
    "record_id",
    "symbol",
    "time",
    "day",
    "value",
    "cost",
    "loss_bp",
    "fee",
    "fee_currency",
    "estimated",
    "side",
    "quantity",
    "price",
    "reference",
    "reference_source",
)
_CHILD_COPIES = (
    (
        source_execution_symbol,
        cost_execution_symbol,
        ("record_id", "symbol", "present", "status", "error", "outcome", "outcome_reason", "reason_code"),
    ),
    (source_position, cost_position, ("record_id", "idx", "symbol", "quantity", "direction")),
    (source_attempt, cost_attempt, ("record_id", "idx", "symbol", "planned", "filled", "reason")),
    (
        source_transaction,
        cost_transaction,
        (
            "record_id",
            "idx",
            "symbol",
            "side",
            "quantity",
            "price",
            "reference",
            "reference_source",
            "time",
            "end_time",
            "time_estimated",
            "before",
            "target",
            "value",
            "cost",
            "loss_bp",
            "coverage",
            "covered",
            "count",
            "amount_complete",
            "fee_covered",
        ),
    ),
    (source_transaction_fee, cost_transaction_fee, ("record_id", "transaction_idx", "currency", "value")),
)


def _account_sources(account_id: int) -> sa.Select:
    """Restrict every copied header and child to existing account-owned records."""
    records = SQLModel.metadata.tables["executerecord"]
    return (
        sa.select(source_execution.c.record_id)
        .join(records, records.c.id == source_execution.c.record_id)
        .where(records.c.account_id == account_id, source_execution.c.account_id == account_id)
    )


async def _validate_sources(session: AsyncSession, snapshot_id: str, account_id: int) -> None:
    """Reject stale or unsupported projections before writing any snapshot children."""
    snapshot = await session.scalar(
        sa.select(analysis_snapshot.c.id).where(
            analysis_snapshot.c.id == snapshot_id,
            analysis_snapshot.c.account_id == account_id,
            analysis_snapshot.c.cost_schema_version == COST_FACT_SCHEMA_VERSION,
        )
    )
    if snapshot is None:
        raise ValueError("Snapshot must belong to the account and use the typed cost schema")
    records = SQLModel.metadata.tables["executerecord"]
    invalid = await session.scalar(
        sa.select(records.c.id)
        .select_from(records.outerjoin(source_execution, source_execution.c.record_id == records.c.id))
        .where(
            records.c.account_id == account_id,
            sa.or_(
                source_execution.c.record_id.is_(None),
                source_execution.c.projection_version != COST_FACT_SCHEMA_VERSION,
                source_execution.c.account_id.is_distinct_from(records.c.account_id),
                source_execution.c.execution_id.is_distinct_from(records.c.execution_id),
                source_execution.c.created_at.is_distinct_from(records.c.created_at),
                source_execution.c.is_success.is_distinct_from(records.c.is_success),
                source_execution.c.time.is_(None),
                source_execution.c.trade_count.is_(None),
                source_execution.c.trade_count
                != (
                    sa.select(sa.func.count())
                    .select_from(source_trade)
                    .where(source_trade.c.record_id == records.c.id)
                    .correlate(records)
                    .scalar_subquery()
                ),
            ),
        )
        .limit(1)
    )
    if invalid is not None:
        raise ValueError(f"Missing, unsupported or stale source cost facts for record {invalid}")


async def copy_source_cost_facts(session: AsyncSession, snapshot_id: str, account_id: int) -> None:
    """Copy immutable structured costs without decoding payloads or committing.

    The caller must acquire the SQLite writer transaction, check its source
    version, insert the snapshot, and publish the pointer in the same transaction.
    Unsupported historical projections fail publication instead of silently
    coercing their values or reading their legacy JSON again.
    """
    await _validate_sources(session, snapshot_id, account_id)
    account_sources = _account_sources(account_id)
    await session.execute(
        cost_execution.insert().from_select(
            ["snapshot_id", *_EXECUTION_COLUMNS, "payload"],
            sa.select(
                sa.literal(snapshot_id),
                *(source_execution.c[name] for name in _EXECUTION_COLUMNS),
                sa.literal("{}"),
            ).where(source_execution.c.record_id.in_(account_sources)),
        )
    )
    # IDs remain snapshot-local and preserve the existing primary-key history
    # iteration order, including records inserted retroactively.
    trade_id = sa.func.row_number().over(order_by=(source_trade.c.record_id, source_trade.c.trade_index)) - 1
    await session.execute(
        cost_trade.insert().from_select(
            ["snapshot_id", "id", *_TRADE_COLUMNS, "payload"],
            sa.select(
                sa.literal(snapshot_id),
                trade_id,
                *(source_trade.c[name] for name in _TRADE_COLUMNS),
                sa.literal("{}"),
            ).where(source_trade.c.record_id.in_(account_sources)),
        )
    )
    for source, destination, columns in _CHILD_COPIES:
        await session.execute(
            destination.insert().from_select(
                ["snapshot_id", *columns],
                sa.select(sa.literal(snapshot_id), *(source.c[name] for name in columns)).where(
                    source.c.record_id.in_(account_sources)
                ),
            )
        )
