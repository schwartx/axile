"""Version-bound cost pagination over compact indexed projections."""

import base64
import hashlib
import json
from typing import Literal

import sqlalchemy as sa
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from axile.server.analysis_cost_facts import COST_FACT_SCHEMA_VERSION
from axile.server.analysis_cost_reader import read_execution_payloads, scalar_columns, trade_payload
from axile.server.db.models.analysis import analysis_snapshot as snapshots
from axile.server.db.models.analysis import cost_execution as executions
from axile.server.db.models.analysis import cost_range as ranges
from axile.server.db.models.analysis import cost_trade as trades
from axile.server.db.models.performance import RangeKey
from axile.server.performance_costs import execution_list_payload, timestamp


class CostQuery(BaseModel):
    """An immutable snapshot plus exact filter and ordering scope."""

    model_config = ConfigDict(extra="forbid")
    snapshot_id: str
    range: RangeKey = "all"
    dimension: Literal["execution", "symbol", "trade", "summary"] = "execution"
    sort: Literal["time", "cost"] = "time"
    day: str | None = None
    start: float | None = Field(default=None, allow_inf_nan=False)
    end: float | None = Field(default=None, allow_inf_nan=False)
    record_id: int | None = None
    symbol: str | None = None
    symbol_search: str | None = None
    side: Literal["buy", "sell", "none"] | None = None
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def boundaries(self):
        """Reject ambiguous or malformed time selectors."""
        if (self.start is None) != (self.end is None):
            raise ValueError("start and end must define a nonempty interval")
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("start and end must define a nonempty interval")
        if self.day:
            if self.start is not None:
                raise ValueError("day and interval cannot be combined")
            from datetime import date

            if date.fromisoformat(self.day).isoformat() != self.day:
                raise ValueError("day must be YYYY-MM-DD")
        return self


def _cursor(query: CostQuery, account_id: int) -> tuple[str, int]:
    scope = query.model_dump(exclude={"cursor"}) | {"account_id": account_id}
    digest = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
    offset = 0
    if query.cursor:
        try:
            payload = json.loads(base64.urlsafe_b64decode(query.cursor))
            offset = payload["offset"]
            if payload["scope"] != digest or type(offset) is not int or offset < 0:
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise HTTPException(
                400, detail={"code": "INVALID_COST_CURSOR", "message": "游标与查询条件不匹配"}
            ) from None
    return digest, offset


def _filters(query: CostQuery, result: dict, typed: bool = False):
    start = result["baseline"] if typed else timestamp(result["baseline"]) if result["baseline"] else None
    end = result["end"] if typed else timestamp(result["end"]) if result["end"] else None
    start = start if start is not None else 0
    end = end if end is not None else -1
    execution_filter = [
        executions.c.snapshot_id == query.snapshot_id,
        executions.c.time >= start,
        executions.c.time <= end,
    ]
    if query.record_id is not None:
        execution_filter.append(executions.c.record_id == query.record_id)
    record_ids = sa.select(executions.c.record_id).where(*execution_filter)
    trade_filter = [trades.c.snapshot_id == query.snapshot_id, trades.c.record_id.in_(record_ids)]
    time_filter = None
    if query.day:
        trade_filter.append(trades.c.day == query.day)
        day_start = timestamp(query.day)
        time_filter = sa.and_(executions.c.time >= day_start, executions.c.time < day_start + 86400000)
    elif query.start is not None:
        trade_filter.extend([trades.c.time > query.start, trades.c.time <= query.end])
        time_filter = sa.and_(executions.c.time > query.start, executions.c.time <= query.end)
    if query.symbol is not None:
        trade_filter.append(trades.c.symbol == query.symbol)
    if query.side is not None:
        trade_filter.append(_side_column(typed) == query.side)
    if query.symbol_search:
        trade_filter.append(trades.c.symbol.icontains(query.symbol_search, autoescape=True))
    has_symbol = query.symbol is not None or bool(query.symbol_search) or query.side is not None
    if has_symbol or time_filter is not None:
        matching = executions.c.record_id.in_(sa.select(trades.c.record_id).where(*trade_filter))
        execution_filter.append(
            sa.or_(matching, time_filter) if time_filter is not None and not has_symbol else matching
        )
    return execution_filter, trade_filter


def _summary_columns():
    """Share the existing aggregate expressions between totals and page groups."""
    valid = sa.and_(trades.c.cost.is_not(None), trades.c.value.is_not(None))
    return (
        sa.func.count().label("count"),
        sa.func.count(trades.c.value).label("valued"),
        sa.func.sum(trades.c.value).label("value"),
        sa.func.sum(trades.c.cost).label("cost"),
        sa.func.count(sa.case((valid, 1))).label("covered"),
        sa.func.sum(sa.case((valid, trades.c.value), else_=0)).label("covered_value"),
        sa.func.sum(sa.case((valid, trades.c.loss_bp * trades.c.value), else_=0)).label("weighted_loss"),
        sa.func.sum(sa.cast(trades.c.estimated, sa.Integer)).label("estimated"),
    )


def _summary_result(row, fee_rows):
    """Keep null, completeness and fee semantics identical for totals and groups."""
    complete = row["count"] == row["valued"]
    return {
        "value": row["value"],
        "cost": row["cost"],
        "lossBp": row["weighted_loss"] / row["covered_value"] if row["covered_value"] else None,
        "coverage": row["covered_value"] / row["value"] if complete and row["value"] else None,
        "covered": row["covered"],
        "coveredValue": row["covered_value"],
        "count": row["count"],
        "amountComplete": complete,
        "fees": {currency: value for currency, value, _ in fee_rows},
        "feeCovered": sum(count for _, _, count in fee_rows),
        "estimated": row["estimated"] or 0,
    }


async def summary(session, filters: list) -> dict:
    """Aggregate projected numeric columns without loading per-trade JSON."""
    row = (await session.execute(sa.select(*_summary_columns()).select_from(trades).where(*filters))).mappings().one()
    fee_rows = (
        await session.execute(
            sa.select(trades.c.fee_currency, sa.func.sum(trades.c.fee), sa.func.count())
            .where(*filters, trades.c.fee.is_not(None), trades.c.fee_currency.is_not(None))
            .group_by(trades.c.fee_currency)
        )
    ).all()
    return _summary_result(row, fee_rows)


def _side_column(typed: bool):
    return trades.c.side if typed else trades.c.payload["side"].as_string()


def _quantity_column(typed: bool):
    return trades.c.quantity if typed else trades.c.payload["quantity"].as_float()


async def _execution_page(session, filters, trade_filter, query, offset, typed=False):
    cost = (
        sa.select(trades.c.record_id, sa.func.sum(trades.c.cost).label("cost"))
        .where(*trade_filter)
        .group_by(trades.c.record_id)
        .subquery()
    )
    stmt = (
        sa.select(*(scalar_columns(executions) if typed else [executions.c.payload]), cost.c.cost)
        .select_from(executions.outerjoin(cost, executions.c.record_id == cost.c.record_id))
        .where(*filters)
    )
    ordering = [executions.c.time.desc(), executions.c.record_id.desc()]
    if query.sort == "cost":
        ordering.insert(0, cost.c.cost.desc().nulls_last())
    rows = (await session.execute(stmt.order_by(*ordering).offset(offset).limit(query.limit))).mappings().all()
    payloads = (
        await read_execution_payloads(session, query.snapshot_id, rows) if typed else [row["payload"] for row in rows]
    )
    data = []
    for payload in payloads:
        row_filter = [*trade_filter, trades.c.record_id == payload["record"]["id"]]
        row_summary = await summary(session, row_filter)
        symbol_count = await session.scalar(
            sa.select(sa.func.count(sa.distinct(trades.c.symbol))).where(
                *row_filter, _quantity_column(typed).is_not(None)
            )
        )
        data.append({**execution_list_payload(payload), "symbolCount": symbol_count, "summary": row_summary})
    return data


async def _symbol_page(session, filters, query, offset, typed=False):
    cost = sa.func.sum(trades.c.cost)
    stmt = sa.select(trades.c.symbol).where(*filters).group_by(trades.c.symbol)
    ordering = [cost.desc().nulls_last(), trades.c.symbol] if query.sort == "cost" else [trades.c.symbol]
    symbols = (await session.execute(stmt.order_by(*ordering).offset(offset).limit(query.limit))).scalars().all()
    if not symbols:
        return []
    page_filter = [*filters, trades.c.symbol.in_(symbols)]
    side, quantity = _side_column(typed), _quantity_column(typed)
    rows = (
        (
            await session.execute(
                sa.select(
                    trades.c.symbol,
                    *_summary_columns(),
                    sa.func.max(trades.c.time).label("last_time"),
                    sa.func.sum(sa.case((side == "buy", quantity), else_=0)).label("buy"),
                    sa.func.sum(sa.case((side == "sell", quantity), else_=0)).label("sell"),
                    sa.func.count(sa.case((sa.or_(side == "none", quantity.is_(None)), 1))).label("incomplete"),
                )
                .where(*page_filter)
                .group_by(trades.c.symbol)
            )
        )
        .mappings()
        .all()
    )
    fees = {symbol: [] for symbol in symbols}
    fee_rows = (
        await session.execute(
            sa.select(trades.c.symbol, trades.c.fee_currency, sa.func.sum(trades.c.fee), sa.func.count())
            .where(*page_filter, trades.c.fee.is_not(None), trades.c.fee_currency.is_not(None))
            .group_by(trades.c.symbol, trades.c.fee_currency)
        )
    ).all()
    for symbol, currency, amount, count in fee_rows:
        fees[symbol].append((currency, amount, count))
    by_symbol = {
        row["symbol"]: {
            "symbol": row["symbol"],
            "lastTime": row["last_time"],
            "summary": _summary_result(row, fees[row["symbol"]]),
            "buy": row["buy"] or 0,
            "sell": row["sell"] or 0,
            "quantityIncomplete": bool(row["incomplete"]),
        }
        for row in rows
    }
    return [by_symbol[symbol] for symbol in symbols]


def _cost_bounds(batch):
    """Keep missing boundaries distinct from explicitly null historical values."""
    values = {}
    for index, name in enumerate(("baseline", "end")):
        if batch[name + "_kind"] is None:
            raise KeyError(name)
        values[name] = batch["bounds"][index]
    return values


def _execution_identifier(identity, kind):
    """Retain the original missing-key error instead of manufacturing null."""
    if kind is None:
        raise KeyError("execution_id")
    return identity[0]


async def _legacy_bounds(session, query: CostQuery) -> dict:
    """Keep exact JSON numeric tokens and missing-key behavior for schema zero."""
    path = f'$."{query.range}".performance'
    batch = (
        (
            await session.execute(
                sa.select(
                    sa.func.json_extract(snapshots.c.ranges, path + ".baseline", path + ".end", type_=sa.JSON()).label(
                        "bounds"
                    ),
                    sa.func.json_type(snapshots.c.ranges, path + ".baseline").label("baseline_kind"),
                    sa.func.json_type(snapshots.c.ranges, path + ".end").label("end_kind"),
                ).where(snapshots.c.id == query.snapshot_id)
            )
        )
        .mappings()
        .one()
    )
    return _cost_bounds(batch)


async def _trade_page(session, filters, ordering, query, offset, typed):
    columns = (
        [*scalar_columns(trades), executions.c.execution_id]
        if typed
        else [
            trades.c.payload,
            trades.c.id,
            trades.c.record_id,
            sa.func.json_extract(executions.c.payload, "$.record.execution_id", "$.record.id", type_=sa.JSON()).label(
                "identity"
            ),
            sa.func.json_type(executions.c.payload, "$.record.execution_id").label("kind"),
        ]
    )
    rows = (
        await session.execute(
            sa.select(*columns)
            .select_from(
                trades.join(
                    executions,
                    sa.and_(
                        trades.c.snapshot_id == executions.c.snapshot_id, trades.c.record_id == executions.c.record_id
                    ),
                )
            )
            .where(*filters)
            .order_by(*ordering)
            .offset(offset)
            .limit(query.limit)
        )
    ).mappings()
    return [
        {
            **(trade_payload(row) if typed else row["payload"]),
            "trade_id": row["id"],
            "record_id": row["record_id"],
            "execution_id": row["execution_id"] if typed else _execution_identifier(row["identity"], row["kind"]),
        }
        for row in rows
    ]


async def read_costs(session, account_id: int, query: CostQuery) -> dict:
    """Keep all page reads in one SQLite snapshot without discarding caller work."""
    async with session.begin_nested():
        return await _read_costs(session, account_id, query)


async def _read_costs(session, account_id: int, query: CostQuery) -> dict:
    """Read a stable page or explicitly reject an evicted snapshot version."""
    digest, offset = _cursor(query, account_id)
    batch = (
        (
            await session.execute(
                sa.select(
                    snapshots.c.data_until,
                    snapshots.c.logic_version,
                    snapshots.c.engine_version,
                    snapshots.c.cost_schema_version,
                ).where(snapshots.c.id == query.snapshot_id, snapshots.c.account_id == account_id)
            )
        )
        .mappings()
        .first()
    )
    from axile.server.performance_analysis import ENGINE_VERSION, LOGIC_VERSION

    if batch is None or batch["logic_version"] != LOGIC_VERSION or batch["engine_version"] != ENGINE_VERSION:
        raise HTTPException(410, detail={"code": "SNAPSHOT_EXPIRED", "message": "快照版本已失效，请重新读取绩效"})
    typed = batch["cost_schema_version"] == COST_FACT_SCHEMA_VERSION
    if typed:
        bounds = (
            (
                await session.execute(
                    sa.select(ranges.c.baseline, ranges.c.end).where(
                        ranges.c.snapshot_id == query.snapshot_id, ranges.c.range_key == query.range
                    )
                )
            )
            .mappings()
            .one()
        )
    elif batch["cost_schema_version"] == 0:
        bounds = await _legacy_bounds(session, query)
    else:
        raise HTTPException(410, detail={"code": "SNAPSHOT_EXPIRED", "message": "快照版本已失效，请重新读取绩效"})
    execution_filter, trade_filter = _filters(query, bounds, typed)
    totals = await summary(session, trade_filter)
    execution_counts = (
        await session.execute(
            sa.select(
                sa.func.count(),
                sa.func.sum(sa.cast(executions.c.success, sa.Integer)),
                sa.func.sum(sa.cast(executions.c.noop, sa.Integer)),
            ).where(*execution_filter)
        )
    ).one()
    if query.dimension == "summary":
        count, data = 0, []
    elif query.dimension == "execution":
        count = execution_counts[0]
        data = await _execution_page(session, execution_filter, trade_filter, query, offset, typed)
    elif query.dimension == "symbol":
        count = await session.scalar(sa.select(sa.func.count(sa.distinct(trades.c.symbol))).where(*trade_filter))
        data = await _symbol_page(session, trade_filter, query, offset, typed)
    else:
        count = totals["count"]
        ordering = [trades.c.time.desc(), trades.c.id]
        if query.sort == "cost":
            ordering.insert(0, trades.c.cost.desc().nulls_last())
        data = await _trade_page(session, trade_filter, ordering, query, offset, typed)
    next_cursor = (
        base64.urlsafe_b64encode(json.dumps({"scope": digest, "offset": offset + query.limit}).encode()).decode()
        if offset + query.limit < count
        else None
    )
    return {
        "snapshot_id": query.snapshot_id,
        "data_until": batch["data_until"],
        "summary": totals,
        "successful": execution_counts[1] or 0,
        "noop": execution_counts[2] or 0,
        "count": count,
        "data": data,
        "next_cursor": next_cursor,
    }
