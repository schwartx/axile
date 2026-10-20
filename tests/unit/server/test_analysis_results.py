"""Normalized result storage preserves public performance semantics without JSON."""

import asyncio
from contextlib import asynccontextmanager

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server.analysis_results import iter_result_fact_batches, read_result_points, read_result_range
from axile.server.db.models.analysis import analysis_snapshot as snapshots
from axile.server.db.models.analysis_result import RESULT_TABLES
from axile.server.db.models.performance import AccountPerformance

SETTINGS = {"backtest_weight_type": "cs", "backtest_fee_rate": 0.0002}
ENGINE_VERSION = "equity-ledger-1"


def _range(range_key="all", *, empty=False):
    performance = AccountPerformance(
        settings=SETTINGS,
        engine_version=ENGINE_VERSION,
        range=range_key,
        backtest_included=False,
        baseline=None if empty else "2026-01-01T09:00:00",
        end=None if empty else "2026-01-02T09:00:00",
        record_count=0 if empty else 6,
        observation_count=0 if empty else 5,
        used_record_count=0 if empty else 4,
        target_used_record_count=0 if empty else 3,
        invalid_asset_count=0 if empty else 1,
        skips=None
        if empty
        else {
            "count": 2,
            "missing_target": 1,
            "missing_ticks": 1,
            "first_time": "2026-01-01T10:00:00",
            "last_time": "2026-01-01T11:00:00",
        },
        target_skips=None
        if empty
        else {
            "count": 1,
            "missing_target": 0,
            "missing_ticks": 1,
            "first_time": None,
            "last_time": None,
        },
        sizing_fallback_count=None if empty else 2,
        sizing_fallback_ranges=[]
        if empty
        else [
            {"start": "2026-01-01T09:00:00", "end": "2026-01-02T09:00:00", "count": 2},
        ],
        points=[]
        if empty
        else [
            {
                "date": "2026-01-01",
                "observed_at": "2026-01-01T09:00:00",
                "record_id": 7,
                "execution_id": "execution-7",
                "account_return": 0,
                "account_equity": 100,
                "portfolio_return": 0,
                "account_daily_return": None,
                "portfolio_daily_return": None,
                "target_portfolio_return": 0,
                "target_portfolio_daily_return": None,
                "sizing_fallback": False,
                "difference": 0,
            },
            {
                "date": "2026-01-02",
                "observed_at": "2026-01-02T09:00:00",
                "record_id": None,
                "execution_id": None,
                "account_return": 0.03,
                "account_equity": 103,
                "portfolio_return": -0.02,
                "account_daily_return": 0.03,
                "portfolio_daily_return": -0.02,
                "target_portfolio_return": None,
                "target_portfolio_daily_return": None,
                "sizing_fallback": True,
                "difference": 0.05,
            },
        ],
        bindings=[]
        if empty
        else [
            {"time": "2026-01-01T09:00:00", "portfolio_id": 3},
            {"time": "2026-01-02T09:00:00", "portfolio_id": None},
        ],
        executions=[],
        calendar={
            "status": "unavailable" if empty else "partial",
            "calendar_id": None if empty else "CN_FUTURES",
            "label": None if empty else "期货交易日历",
            "closed_ranges": [] if empty else [{"start": "2026-01-03", "end": "2026-01-04"}],
            "unavailable_ranges": [] if empty else [{"start": "2026-01-05", "end": "2026-01-06"}],
        },
    ).model_dump()
    return {
        "performance": performance,
        "daily_costs": {}
        if empty
        else {
            "2026-01-01": {
                "value": None,
                "cost": None,
                "lossBp": None,
                "coverage": None,
                "covered": 0,
                "count": 1,
                "amountComplete": False,
                "fees": {},
                "feeCovered": 0,
            },
            "2026-01-02": {
                "value": 1000,
                "cost": 0,
                "lossBp": 0,
                "coverage": 1,
                "covered": 2,
                "count": 2,
                "amountComplete": True,
                "fees": {"CNY": 0, "USD": 1.5},
                "feeCovered": 2,
                "coveredValue": None,
                "estimated": 0,
            },
        },
        "events": []
        if empty
        else [
            {"time": "2026-01-02T09:00:00", "tag": "binding", "text": "解绑组合", "executionId": None},
            {"time": "2026-01-01T09:00:00", "tag": "execution", "text": "已执行", "executionId": "execution-7"},
        ],
        "event_count": 0 if empty else 17,
    }


@asynccontextmanager
async def _database(tmp_path, snapshots_by_id):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'results.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        async with sessions() as session:
            for snapshot_id, ranges in snapshots_by_id.items():
                await session.execute(
                    snapshots.insert().values(
                        id=snapshot_id,
                        account_id=2,
                        source_version=3,
                        logic_version="test",
                        engine_version=ENGINE_VERSION,
                        computed_at="2026-01-07T00:00:00",
                        settings=SETTINGS,
                        ranges={},
                    )
                )
                for table, rows in iter_result_fact_batches(snapshot_id, ranges, batch_size=2):
                    await session.execute(table.insert(), rows)
            await session.commit()
        yield sessions
    finally:
        await engine.dispose()


def test_all_public_result_fields_round_trip_without_snapshot_json(tmp_path):
    """All result metadata and nullable point fields survive normalized storage."""
    ranges = {key: _range(key) for key in ("all", "30", "90")}

    async def check():
        async with _database(tmp_path, {"snapshot": ranges}) as sessions:
            async with sessions() as session:
                for range_key, expected in ranges.items():
                    actual = await read_result_range(session, "snapshot", range_key, SETTINGS, ENGINE_VERSION)
                    assert actual == expected
                    AccountPerformance.model_validate(actual["performance"])
                # An intentionally empty legacy blob cannot supply any of these values.
                legacy = (await session.execute(sa.select(snapshots.c.ranges))).scalar_one()
                assert legacy == {}

    asyncio.run(check())


def test_empty_result_preserves_nulls_and_required_calendar(tmp_path):
    """No observations still has a complete result contract rather than invented zeroes."""
    expected = _range(empty=True)

    async def check():
        async with _database(tmp_path, {"snapshot": {"all": expected}}) as sessions:
            async with sessions() as session:
                actual = await read_result_range(session, "snapshot", "all", SETTINGS, ENGINE_VERSION)
                assert actual == expected
                assert actual["performance"]["skips"] is None
                assert actual["performance"]["sizing_fallback_count"] is None
                assert actual["performance"]["calendar"]["status"] == "unavailable"

    asyncio.run(check())


def test_daily_costs_keep_unknown_money_zero_fees_and_optional_presence(tmp_path):
    """Unknown amounts, explicit zero fees and absent optional fields stay distinct."""
    expected = _range()
    expected["daily_costs"]["2026-01-03"] = {"fees": {"X": 0}}

    async def check():
        async with _database(tmp_path, {"snapshot": {"all": expected}}) as sessions:
            async with sessions() as session:
                actual = await read_result_range(session, "snapshot", "all", SETTINGS, ENGINE_VERSION)
                costs = actual["daily_costs"]
                assert costs == expected["daily_costs"]
                assert costs["2026-01-01"]["value"] is None
                assert "coveredValue" not in costs["2026-01-01"]
                assert "estimated" not in costs["2026-01-01"]
                assert costs["2026-01-02"]["coveredValue"] is None
                assert costs["2026-01-02"]["estimated"] == 0
                assert costs["2026-01-02"]["fees"]["CNY"] == 0
                assert costs["2026-01-03"] == {"fees": {"X": 0}}

    asyncio.run(check())


def test_result_facts_have_bounded_batches_and_no_json_columns():
    """Large point series are inserted in bounded typed batches."""
    expected = _range()
    first = expected["performance"]["points"][0]
    expected["performance"]["points"] = [dict(first, record_id=index) for index in range(19)]
    batches = list(iter_result_fact_batches("snapshot", {"all": expected}, batch_size=3))
    assert batches
    assert all(0 < len(rows) <= 3 for _, rows in batches)
    point_rows = [row for table, rows in batches if table.name == "performance_result_point" for row in rows]
    assert len(point_rows) == 19
    assert [row["record_id"] for row in point_rows] == list(range(19))
    assert all(not isinstance(column.type, sa.JSON) for table in RESULT_TABLES for column in table.columns)
    with pytest.raises(ValueError, match="batch_size"):
        list(iter_result_fact_batches("snapshot", {"all": expected}, batch_size=0))


def test_events_are_capped_without_losing_total_count(tmp_path):
    """The timeline remains bounded while its total count describes the full history."""
    expected = _range()
    events = [
        {"time": "2026-01-01T09:00:00", "tag": "execution", "text": str(index), "executionId": None}
        for index in range(117)
    ]
    expected.update(events=events, event_count=117)

    async def check():
        async with _database(tmp_path, {"snapshot": {"all": expected}}) as sessions:
            async with sessions() as session:
                actual = await read_result_range(session, "snapshot", "all", SETTINGS, ENGINE_VERSION)
                assert actual["events"] == events[:100]
                assert actual["event_count"] == 117
                count = await session.scalar(sa.text("SELECT count(*) FROM performance_result_event"))
                assert count == 100

    asyncio.run(check())


def test_summary_points_read_multiple_snapshots_and_selected_range(tmp_path):
    """Cards batch-read ordered points and handle missing versions without legacy JSON."""
    all_range = _range()
    recent_range = _range("90")
    recent_range["performance"]["points"] = recent_range["performance"]["points"][-1:]
    empty_range = _range(empty=True)

    async def check():
        async with _database(
            tmp_path,
            {"snapshot-a": {"all": all_range, "90": recent_range}, "snapshot-b": {"all": empty_range}},
        ) as sessions:
            async with sessions() as session:
                points = await read_result_points(session, ["snapshot-a", "snapshot-b", "missing"])
                assert points == {
                    "snapshot-a": all_range["performance"]["points"],
                    "snapshot-b": [],
                    "missing": [],
                }
                recent = await read_result_points(session, ["snapshot-a", "snapshot-b"], range_key="90")
                assert recent == {"snapshot-a": recent_range["performance"]["points"], "snapshot-b": []}
                assert await read_result_points(session, []) == {}

    asyncio.run(check())
