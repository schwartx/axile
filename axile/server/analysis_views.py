"""Small, version-bound HTTP read models over frozen analysis scalar tables."""

from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from fastapi import HTTPException

from axile.server.db.models.analysis import (
    analysis_snapshot as snapshots,
)
from axile.server.db.models.analysis import (
    analysis_state as states,
)
from axile.server.db.models.analysis import (
    cost_attempt,
    cost_execution,
    cost_execution_symbol,
    cost_position,
    cost_range,
    cost_transaction,
    cost_transaction_fee,
)
from axile.server.db.models.analysis_result import (
    result_binding,
    result_calendar_range,
    result_daily_cost,
    result_daily_fee,
    result_event,
    result_fallback,
    result_point,
    result_range,
    result_skip,
)
from axile.server.db.models.analysis_view import execution_fee, execution_summary
from axile.server.performance_analysis import ENGINE_VERSION, LOGIC_VERSION, RETRY_DELAYS

SCHEMA_VERSION = 1
READ_VIEW_SCHEMA_VERSION = 1
SUMMARY_COLUMNS = (
    "record_id",
    "execution_id",
    "time",
    "created_at",
    "is_success",
    "noop",
    "has_transactions",
    "duration_sec",
    "symbol_count",
    "position_count",
    "positions_present",
    "reason",
    "value",
    "cost",
    "loss_bp",
    "coverage",
    "covered",
    "count",
    "fee_covered",
    "amount_complete",
    "covered_value",
    "estimated",
)
DAILY_COLUMNS = (
    "day",
    "value",
    "cost",
    "loss_bp",
    "coverage",
    "covered",
    "count",
    "amount_complete",
    "fee_covered",
    "covered_value",
    "estimated",
    "present",
)
RANGE_COLUMNS = (
    "engine_version",
    "backtest_included",
    "baseline",
    "end",
    "record_count",
    "observation_count",
    "used_record_count",
    "target_used_record_count",
    "invalid_asset_count",
    "sizing_fallback_count",
    "event_count",
)


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message})


def _columns(table: sa.Table, *excluded: str) -> tuple[str, ...]:
    return tuple(column.name for column in table.c if column.name not in excluded)


def _table(columns: Sequence[str], rows: Sequence) -> dict:
    return {"columns": list(columns), "rows": [list(row) for row in rows]}


async def _rows(session, table: sa.Table, names: Sequence[str], filters: Sequence, ordering: Sequence = ()):
    statement = sa.select(*(table.c[name] for name in names)).where(*filters)
    if ordering:
        statement = statement.order_by(*ordering)
    return (await session.execute(statement)).all()


async def _snapshot(session, account_id: int, snapshot_id: str) -> Mapping[str, Any]:
    """Check ownership and schema without selecting either historical JSON column."""
    names = _columns(snapshots, "settings", "ranges")
    row = (
        (await session.execute(sa.select(*(snapshots.c[name] for name in names)).where(snapshots.c.id == snapshot_id)))
        .mappings()
        .first()
    )
    if row is None:
        raise _error(410, "SNAPSHOT_EXPIRED", "绩效版本已过期，请重新获取发布状态")
    if row["account_id"] != account_id:
        raise _error(404, "SNAPSHOT_NOT_FOUND", "绩效版本不存在")
    if (
        row["result_schema_version"] != 2
        or row["cost_schema_version"] != 1
        or row["read_view_schema_version"] != READ_VIEW_SCHEMA_VERSION
    ):
        raise _error(409, "ANALYSIS_READ_VIEW_UNAVAILABLE", "此绩效版本需要刷新后才能读取")
    return row


def _status(row: Mapping | None, has_snapshot: bool) -> str:
    if row is None:
        return "empty"
    if row["error"]:
        return "failed"
    active = row["requested"] or row["running_version"] is not None
    if active or row["published_version"] != row["source_version"]:
        return "stale" if has_snapshot else "pending" if active else "empty"
    return "ready" if has_snapshot else "empty"


async def read_status(session, account_id: int) -> dict:
    """Read publication metadata independently of any chart currently displayed."""
    async with session.begin_nested():
        fields = (
            "id",
            "source_version",
            "logic_version",
            "engine_version",
            "computed_at",
            "data_until",
            "read_view_schema_version",
        )
        row = (
            (
                await session.execute(
                    sa.select(states, *(snapshots.c[name].label("snapshot_" + name) for name in fields))
                    .select_from(
                        states.outerjoin(
                            snapshots,
                            sa.and_(
                                states.c.current_snapshot == snapshots.c.id,
                                snapshots.c.account_id == states.c.account_id,
                            ),
                        )
                    )
                    .where(states.c.account_id == account_id)
                )
            )
            .mappings()
            .first()
        )
        compatible = bool(
            row
            and row["snapshot_id"]
            and row["snapshot_logic_version"] == LOGIC_VERSION
            and row["snapshot_engine_version"] == ENGINE_VERSION
        )
        has_snapshot = bool(row and row["snapshot_id"])
        state = "pending" if has_snapshot and not compatible else _status(row, compatible)
        return {
            "schema_version": SCHEMA_VERSION,
            "status": state,
            "source_version": row["source_version"] if row else 0,
            "snapshot_id": row["snapshot_id"] if compatible else None,
            "snapshot_version": row["snapshot_source_version"] if compatible else None,
            "logic_version": row["snapshot_logic_version"] if compatible else LOGIC_VERSION,
            "engine_version": row["snapshot_engine_version"] if compatible else ENGINE_VERSION,
            "computed_at": row["snapshot_computed_at"] if compatible else None,
            "data_until": row["snapshot_data_until"] if compatible else None,
            "error": row["error"] if row else None,
            "retry_at": row["retry_at"] if row and row["error"] and row["failures"] <= len(RETRY_DELAYS) else None,
            "read_view_schema_version": row["snapshot_read_view_schema_version"] if compatible else 0,
        }


async def _range_header(session, snapshot_id: str, range_key: str) -> dict:
    filters = [result_range.c.snapshot_id == snapshot_id, result_range.c.range_key == range_key]
    row = (await session.execute(sa.select(result_range).where(*filters))).mappings().first()
    if row is None:
        raise _error(503, "ANALYSIS_READ_VIEW_INCOMPLETE", "已发布的绩效版本缺少区间结果")
    skipped = (
        (
            await session.execute(
                sa.select(result_skip).where(
                    result_skip.c.snapshot_id == snapshot_id,
                    result_skip.c.range_key == range_key,
                )
            )
        )
        .mappings()
        .all()
    )
    skips = {
        item["kind"]: {
            key: item[key]
            for key in (
                "count",
                "missing_target",
                "missing_ticks",
                "first_time",
                "last_time",
            )
        }
        for item in skipped
    }
    return {
        "range": range_key,
        **{key: row[key] for key in RANGE_COLUMNS},
        "skips": skips.get("portfolio"),
        "target_skips": skips.get("target"),
        "calendar": {
            "status": row["calendar_status"],
            "calendar_id": row["calendar_id"],
            "label": row["calendar_label"],
        },
    }


async def _range_filters(session, snapshot_id: str, range_key: str, start=None, end=None) -> list:
    bounds = (
        await session.execute(
            sa.select(cost_range.c.baseline, cost_range.c.end).where(
                cost_range.c.snapshot_id == snapshot_id,
                cost_range.c.range_key == range_key,
            )
        )
    ).first()
    if bounds is None:
        raise _error(503, "ANALYSIS_READ_VIEW_INCOMPLETE", "已发布的绩效版本缺少区间边界")
    lower, upper = bounds
    filters = [execution_summary.c.snapshot_id == snapshot_id]
    if lower is None or upper is None:
        filters.append(sa.false())
    else:
        filters.extend((execution_summary.c.time >= lower, execution_summary.c.time <= upper))
    if start is not None:
        filters.extend((execution_summary.c.time >= start, execution_summary.c.time <= end))
    return filters


def _summary_statement(filters: Sequence):
    columns = [
        (execution_summary if name in execution_summary.c else cost_execution).c[name] for name in SUMMARY_COLUMNS
    ]
    return (
        sa.select(*columns)
        .select_from(
            execution_summary.join(
                cost_execution,
                sa.and_(
                    execution_summary.c.snapshot_id == cost_execution.c.snapshot_id,
                    execution_summary.c.record_id == cost_execution.c.record_id,
                ),
            )
        )
        .where(*filters)
        .order_by(execution_summary.c.time, execution_summary.c.record_id)
    )


async def _fees(session, snapshot_id: str, record_ids) -> dict:
    names = ("record_id", "currency", "value", "count")
    filters = [execution_fee.c.snapshot_id == snapshot_id, execution_fee.c.record_id.in_(record_ids)]
    rows = await _rows(session, execution_fee, names, filters, (execution_fee.c.record_id, execution_fee.c.currency))
    return _table(names, rows)


async def read_chart(session, account_id: int, snapshot_id: str, range_key: str) -> dict:
    """Read the selected curve and flat execution index in one SQLite read snapshot."""
    async with session.begin_nested():
        snapshot = await _snapshot(session, account_id, snapshot_id)
        header = await _range_header(session, snapshot_id, range_key)
        resources = (
            ("points", result_point, _columns(result_point, "snapshot_id", "range_key", "idx"), (result_point.c.idx,)),
            ("bindings", result_binding, ("time", "portfolio_id"), (result_binding.c.idx,)),
            ("fallbacks", result_fallback, ("start", "end", "count"), (result_fallback.c.idx,)),
            (
                "calendar_ranges",
                result_calendar_range,
                ("kind", "start", "end"),
                (result_calendar_range.c.kind, result_calendar_range.c.idx),
            ),
            ("daily_costs", result_daily_cost, DAILY_COLUMNS, (result_daily_cost.c.day,)),
            (
                "daily_fees",
                result_daily_fee,
                ("day", "currency", "value"),
                (result_daily_fee.c.day, result_daily_fee.c.currency),
            ),
        )
        tables = {}
        for name, table, columns, order in resources:
            filters = (table.c.snapshot_id == snapshot_id, table.c.range_key == range_key)
            tables[name] = _table(columns, await _rows(session, table, columns, filters, order))
        event_columns = ("time", "tag", "text", "execution_id")
        events = (
            await session.execute(
                sa.select(*(result_event.c[name] for name in event_columns))
                .where(
                    result_event.c.snapshot_id == snapshot_id,
                    result_event.c.range_key == range_key,
                )
                .order_by(result_event.c.idx)
                .limit(100)
            )
        ).all()
        tables["events"] = _table(event_columns, events)
        filters = await _range_filters(session, snapshot_id, range_key)
        tables["execution_index"] = _table(SUMMARY_COLUMNS, (await session.execute(_summary_statement(filters))).all())
        record_ids = sa.select(execution_summary.c.record_id).where(*filters)
        tables["execution_fees"] = await _fees(session, snapshot_id, record_ids)
        return {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "account_id": account_id,
            "range": range_key,
            "snapshot_version": snapshot["source_version"],
            "computed_at": snapshot["computed_at"],
            "data_until": snapshot["data_until"],
            "settings": {
                "backtest_weight_type": snapshot["backtest_weight_type"],
                "backtest_fee_rate": snapshot["backtest_fee_rate"],
            },
            "header": header,
            "tables": tables,
        }


async def read_execution_summary(
    session,
    account_id: int,
    snapshot_id: str,
    range_key: str,
    *,
    limit: int = 200,
    offset: int = 0,
    start: float | None = None,
    end: float | None = None,
) -> dict:
    """Page frozen execution summaries without aggregating trades at request time."""
    async with session.begin_nested():
        await _snapshot(session, account_id, snapshot_id)
        filters = await _range_filters(session, snapshot_id, range_key, start, end)
        rows = (await session.execute(_summary_statement(filters).offset(offset).limit(limit + 1))).all()
        page = rows[:limit]
        fees = await _fees(session, snapshot_id, [row[0] for row in page])
        return {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "range": range_key,
            "table": _table(SUMMARY_COLUMNS, page),
            "fees": fees,
            "next_offset": offset + limit if len(rows) > limit else None,
        }


async def _detail_table(session, table: sa.Table, snapshot_id: str, record_id: int, ordering, offset: int, limit: int):
    columns = _columns(table, "snapshot_id", "record_id")
    rows = (
        await session.execute(
            sa.select(*(table.c[name] for name in columns))
            .where(
                table.c.snapshot_id == snapshot_id,
                table.c.record_id == record_id,
            )
            .order_by(ordering)
            .offset(offset)
            .limit(limit + 1)
        )
    ).all()
    return _table(columns, rows[:limit]), offset + limit if len(rows) > limit else None


async def read_execution_detail(
    session,
    account_id: int,
    snapshot_id: str,
    record_id: int,
    *,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    """Read one execution's typed evidence, with explicit child pagination."""
    async with session.begin_nested():
        await _snapshot(session, account_id, snapshot_id)
        columns = _columns(cost_execution, "snapshot_id", "payload")
        header = (
            (
                await session.execute(
                    sa.select(*(cost_execution.c[name] for name in columns)).where(
                        cost_execution.c.snapshot_id == snapshot_id,
                        cost_execution.c.record_id == record_id,
                    )
                )
            )
            .mappings()
            .first()
        )
        if header is None:
            raise _error(404, "EXECUTION_NOT_FOUND", "此绩效版本中没有该执行记录")
        summary = (
            (
                await session.execute(
                    sa.select(execution_summary).where(
                        execution_summary.c.snapshot_id == snapshot_id,
                        execution_summary.c.record_id == record_id,
                    )
                )
            )
            .mappings()
            .first()
        )
        if summary is None:
            raise _error(503, "ANALYSIS_READ_VIEW_INCOMPLETE", "已发布的绩效版本缺少执行摘要")
        fees = await _fees(session, snapshot_id, [record_id])
        summary_result = {
            key: value for key, value in summary.items() if key not in {"snapshot_id", "record_id", "time"}
        }
        summary_result["fees"] = {row[1]: row[2] for row in fees["rows"]}
        tables, next_offsets = {}, {}
        for name, table, ordering in (
            ("positions", cost_position, cost_position.c.idx),
            ("attempts", cost_attempt, cost_attempt.c.idx),
            ("symbols", cost_execution_symbol, cost_execution_symbol.c.symbol),
            ("transactions", cost_transaction, cost_transaction.c.idx),
        ):
            tables[name], next_offsets[name] = await _detail_table(
                session, table, snapshot_id, record_id, ordering, offset, limit
            )
        transaction_ids = [row[0] for row in tables["transactions"]["rows"]]
        fee_columns = ("transaction_idx", "currency", "value")
        tables["transaction_fees"] = _table(
            fee_columns,
            await _rows(
                session,
                cost_transaction_fee,
                fee_columns,
                (
                    cost_transaction_fee.c.snapshot_id == snapshot_id,
                    cost_transaction_fee.c.record_id == record_id,
                    cost_transaction_fee.c.transaction_idx.in_(transaction_ids),
                ),
                (cost_transaction_fee.c.transaction_idx, cost_transaction_fee.c.currency),
            ),
        )
        total = await session.scalar(
            sa.select(sa.func.count())
            .select_from(cost_transaction)
            .where(
                cost_transaction.c.snapshot_id == snapshot_id,
                cost_transaction.c.record_id == record_id,
            )
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "record_id": record_id,
            "header": dict(header),
            "summary": summary_result,
            "tables": tables,
            "total": total,
            "next_offset": next_offsets.pop("transactions"),
            "next_offsets": next_offsets,
        }
