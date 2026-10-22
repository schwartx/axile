"""Bounded publication and JSON-free reads of normalized performance results."""

from collections.abc import Iterator, Mapping

import sqlalchemy as sa

from axile.server.analysis_cost_reader import read_execution_results
from axile.server.db.models.analysis_result import (
    RESULT_TABLES as RESULT_TABLES,
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

RESULT_SCHEMA_VERSION = 2
POINT_FIELDS = tuple(column.name for column in result_point.c if column.name not in ("snapshot_id", "range_key", "idx"))
RANGE_COUNTERS = (
    "record_count",
    "observation_count",
    "used_record_count",
    "target_used_record_count",
    "invalid_asset_count",
)
DAILY_FIELDS = (
    "value",
    "cost",
    "lossBp",
    "coverage",
    "covered",
    "count",
    "amountComplete",
    "feeCovered",
    "coveredValue",
    "estimated",
)
DAILY_COLUMNS = (
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
)


def _result_rows(snapshot_id: str, ranges: Mapping) -> Iterator[tuple[sa.Table, dict]]:
    """Flatten each result once while retaining only the current scalar row."""
    for range_key, result in ranges.items():
        base = {"snapshot_id": snapshot_id, "range_key": range_key}
        performance = result["performance"]
        calendar = performance["calendar"]
        yield (
            result_range,
            {
                **base,
                "backtest_included": performance.get("backtest_included", True),
                "engine_version": performance["engine_version"],
                "baseline": performance.get("baseline"),
                "end": performance.get("end"),
                **{name: performance.get(name, 0) for name in RANGE_COUNTERS},
                "sizing_fallback_count": performance.get("sizing_fallback_count"),
                "calendar_status": calendar["status"],
                "calendar_id": calendar.get("calendar_id"),
                "calendar_label": calendar.get("label"),
                "event_count": result.get("event_count", len(result.get("events", []))),
            },
        )
        for index, point in enumerate(performance.get("points", [])):
            yield (
                result_point,
                {
                    **base,
                    "idx": index,
                    **{name: point.get(name, False if name == "sizing_fallback" else None) for name in POINT_FIELDS},
                },
            )
        for kind, name in (("portfolio", "skips"), ("target", "target_skips")):
            skips = performance.get(name)
            if skips is not None:
                yield (
                    result_skip,
                    {
                        **base,
                        "kind": kind,
                        **{field: skips.get(field, 0) for field in ("count", "missing_target", "missing_ticks")},
                        **{field: skips.get(field) for field in ("first_time", "last_time")},
                    },
                )
        for index, fallback in enumerate(performance.get("sizing_fallback_ranges", [])):
            yield (
                result_fallback,
                {**base, "idx": index, **{name: fallback[name] for name in ("start", "end", "count")}},
            )
        for index, binding in enumerate(performance.get("bindings", [])):
            yield (
                result_binding,
                {**base, "idx": index, "time": binding["time"], "portfolio_id": binding["portfolio_id"]},
            )
        for kind, name in (("closed", "closed_ranges"), ("unavailable", "unavailable_ranges")):
            for index, interval in enumerate(calendar.get(name, [])):
                yield (
                    result_calendar_range,
                    {**base, "kind": kind, "idx": index, "start": interval["start"], "end": interval["end"]},
                )
        for index, event in enumerate(result.get("events", [])[:100]):
            yield (
                result_event,
                {
                    **base,
                    "idx": index,
                    "time": event["time"],
                    "tag": event["tag"],
                    "text": event["text"],
                    "execution_id": event.get("executionId"),
                },
            )
        for day, summary in result.get("daily_costs", {}).items():
            yield (
                result_daily_cost,
                {
                    **base,
                    "day": day,
                    "present": sum(1 << index for index, name in enumerate(DAILY_FIELDS) if name in summary),
                    **{
                        column: summary.get(name, False if name == "amountComplete" else None)
                        for name, column in zip(DAILY_FIELDS, DAILY_COLUMNS, strict=True)
                    },
                },
            )
            for currency, value in summary.get("fees", {}).items():
                yield result_daily_fee, {**base, "day": day, "currency": currency, "value": value}


def iter_result_fact_batches(
    snapshot_id: str, ranges: Mapping, batch_size: int = 500
) -> Iterator[tuple[sa.Table, list[dict]]]:
    """Publish normalized results in bounded batches inside the caller's transaction."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    pending: dict[sa.Table, list[dict]] = {}
    for table, row in _result_rows(snapshot_id, ranges):
        batch = pending.setdefault(table, [])
        batch.append(row)
        if len(batch) >= batch_size:
            yield table, batch
            pending[table] = []
    for table, batch in pending.items():
        if batch:
            yield table, batch


def _public(row: Mapping, names: tuple[str, ...]) -> dict:
    return {name: row[name] for name in names}


async def _range_rows(
    session, table: sa.Table, snapshot_id: str, range_key: str, *ordering, limit: int | None = None
) -> list:
    statement = sa.select(table).where(table.c.snapshot_id == snapshot_id, table.c.range_key == range_key)
    if ordering:
        statement = statement.order_by(*ordering)
    if limit is not None:
        statement = statement.limit(limit)
    return (await session.execute(statement)).mappings().all()


async def read_result_range(
    session, snapshot_id: str, range_key: str, settings: dict, engine_version: str | None = None
) -> dict | None:
    """Reconstruct one public range from scalar tables without touching legacy JSON."""
    headers = await _range_rows(session, result_range, snapshot_id, range_key)
    if not headers:
        return None
    row = headers[0]
    performance = {
        "settings": settings,
        "range": range_key,
        "engine_version": row["engine_version"] if engine_version is None else engine_version,
        "backtest_included": row["backtest_included"],
        **_public(row, ("baseline", "end", *RANGE_COUNTERS, "sizing_fallback_count")),
        "skips": None,
        "target_skips": None,
        "sizing_fallback_ranges": [],
        "points": [],
        "bindings": [],
        "executions": [],
        "calendar": {
            "status": row["calendar_status"],
            "calendar_id": row["calendar_id"],
            "label": row["calendar_label"],
            "closed_ranges": [],
            "unavailable_ranges": [],
        },
    }
    performance["points"] = [
        _public(point, POINT_FIELDS)
        for point in await _range_rows(session, result_point, snapshot_id, range_key, result_point.c.idx)
    ]
    for skipped in await _range_rows(session, result_skip, snapshot_id, range_key):
        name = "target_skips" if skipped["kind"] == "target" else "skips"
        performance[name] = _public(skipped, ("count", "missing_target", "missing_ticks", "first_time", "last_time"))
    performance["sizing_fallback_ranges"] = [
        _public(item, ("start", "end", "count"))
        for item in await _range_rows(session, result_fallback, snapshot_id, range_key, result_fallback.c.idx)
    ]
    performance["bindings"] = [
        _public(item, ("time", "portfolio_id"))
        for item in await _range_rows(session, result_binding, snapshot_id, range_key, result_binding.c.idx)
    ]
    for interval in await _range_rows(
        session, result_calendar_range, snapshot_id, range_key, result_calendar_range.c.idx
    ):
        key = "closed_ranges" if interval["kind"] == "closed" else "unavailable_ranges"
        performance["calendar"][key].append(_public(interval, ("start", "end")))
    daily = {}
    for cost in await _range_rows(session, result_daily_cost, snapshot_id, range_key, result_daily_cost.c.day):
        daily[cost["day"]] = {
            **{
                name: cost[column]
                for index, (name, column) in enumerate(zip(DAILY_FIELDS, DAILY_COLUMNS, strict=True))
                if cost["present"] & (1 << index)
            },
            "fees": {},
        }
    for fee in await _range_rows(session, result_daily_fee, snapshot_id, range_key):
        daily[fee["day"]]["fees"][fee["currency"]] = fee["value"]
    events = [
        {**_public(item, ("time", "tag", "text")), "executionId": item["execution_id"]}
        for item in await _range_rows(session, result_event, snapshot_id, range_key, result_event.c.idx, limit=100)
    ]
    performance["executions"] = await read_execution_results(
        session, snapshot_id, performance["baseline"], performance["end"]
    )
    return {"performance": performance, "daily_costs": daily, "events": events, "event_count": row["event_count"]}


async def read_result_points(session, snapshot_ids: list[str], range_key: str = "all") -> dict[str, list[dict]]:
    """Read only curve scalars for dashboard cards, never legacy snapshot payloads."""
    result: dict[str, list[dict]] = {identity: [] for identity in snapshot_ids}
    if not snapshot_ids:
        return result
    rows = (
        await session.execute(
            sa.select(result_point)
            .where(result_point.c.snapshot_id.in_(snapshot_ids), result_point.c.range_key == range_key)
            .order_by(result_point.c.snapshot_id, result_point.c.idx)
        )
    ).mappings()
    for point in rows:
        result[point["snapshot_id"]].append(_public(point, POINT_FIELDS))
    return result
