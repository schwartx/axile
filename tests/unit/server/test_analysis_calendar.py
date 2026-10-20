"""冻结日历事实的账户范围、批次提交与重新评估契约。"""

import asyncio
import sqlite3
from datetime import date

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server import analysis_calendar
from axile.server.analysis_calendar import ensure_calendar_facts
from axile.server.db.models.analysis import analysis_state
from axile.server.db.models.analysis_calendar import analysis_calendar_day as calendar_days
from axile.server.db.models.analysis_replay import replay_observation as observations
from axile.server.trading_calendar import CalendarDayDecision, CalendarDecisionStatus
from tests.unit.server._business_analysis_support import analysis_record, persist_records
from tests.unit.server._execution_test_support import build_account


async def database(tmp_path, dates=()):
    path = tmp_path / "calendar.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        session.add_all([build_account(id=1), build_account(id=2)])
        await session.flush()
        for record_id, (account_id, timestamp) in enumerate(dates, 1):
            await persist_records(
                session,
                [
                    analysis_record(
                        id=record_id,
                        account_id=account_id,
                        execution_id=None,
                        is_success=1,
                        created_at=timestamp,
                        input={},
                        result={},
                    )
                ],
            )
            await session.flush()
            await session.execute(
                observations.insert().values(
                    record_id=record_id,
                    account_id=account_id,
                    time=timestamp,
                    sizing_fallback=False,
                    target_present=False,
                    target_weight_present=False,
                    target_units_present=False,
                    target_quantities_present=False,
                )
            )
        await session.execute(analysis_state.insert().values(account_id=1, source_version=7))
        await session.commit()
    return engine, sessions, path


def decision(day, status=CalendarDecisionStatus.AVAILABLE_OPEN):
    return CalendarDayDecision(channel="ctp", day=day, status=status, calendar_id="SHFE", label="期货交易日历")


@pytest.mark.parametrize("channel", ["ctp", None])
def test_empty_history_does_not_evaluate_or_write_calendar(tmp_path, monkeypatch, channel):
    def unexpected(*args):
        pytest.fail("无观测或无渠道时不应读取交易日历")

    monkeypatch.setattr(analysis_calendar, "evaluate_channel_calendar_day", unexpected)

    async def check():
        engine, sessions, _ = await database(tmp_path)
        try:
            async with sessions() as session:
                assert await ensure_calendar_facts(session, 1, channel) == 0
                assert await session.scalar(sa.select(sa.func.count()).select_from(calendar_days)) == 0
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_account_bounds_include_baseline_and_gaps_without_reading_other_accounts(tmp_path, monkeypatch):
    evaluated = []

    def evaluate(channel, day):
        evaluated.append((channel, day))
        return decision(
            day,
            CalendarDecisionStatus.AVAILABLE_CLOSED if day.day == 3 else CalendarDecisionStatus.AVAILABLE_OPEN,
        )

    monkeypatch.setattr(analysis_calendar, "evaluate_channel_calendar_day", evaluate)

    async def check():
        engine, sessions, _ = await database(
            tmp_path,
            [
                (1, "2026-01-04T15:00:00.000000"),
                (2, "2025-01-01T09:00:00.000000"),
                (1, "2026-01-02T09:00:00.000000"),
                (2, "2027-01-01T09:00:00.000000"),
            ],
        )
        try:
            async with sessions() as session:
                assert await ensure_calendar_facts(session, 1, "ctp", batch_size=2) == 3
                rows = (await session.execute(sa.select(calendar_days).order_by(calendar_days.c.day))).mappings().all()
                assert [(row["day"], row["status"]) for row in rows] == [
                    ("2026-01-02", "available_open"),
                    ("2026-01-03", "available_closed"),
                    ("2026-01-04", "available_open"),
                ]
                assert all(row["calendar_id"] == "SHFE" and row["label"] == "期货交易日历" for row in rows)
            assert evaluated == [("ctp", date(2026, 1, day)) for day in (2, 3, 4)]
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_batches_are_committed_before_later_days_are_evaluated(tmp_path, monkeypatch):
    visible = []

    async def check():
        engine, sessions, path = await database(
            tmp_path, [(1, "2026-01-01T09:00:00.000000"), (1, "2026-01-05T09:00:00.000000")]
        )

        def evaluate(channel, day):
            with sqlite3.connect(path) as reader:
                visible.append(reader.execute("SELECT count(*) FROM analysis_calendar_day").fetchone()[0])
            return decision(day)

        monkeypatch.setattr(analysis_calendar, "evaluate_channel_calendar_day", evaluate)
        try:
            async with sessions() as session:
                assert await ensure_calendar_facts(session, 1, "ctp", batch_size=2) == 5
                assert not session.in_transaction()
            assert visible == [0, 0, 2, 2, 4]
            with sqlite3.connect(path) as reader:
                assert reader.execute("SELECT count(*) FROM analysis_calendar_day").fetchone()[0] == 5
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_reevaluation_updates_existing_days_without_changing_business_version(tmp_path, monkeypatch):
    status = CalendarDecisionStatus.AVAILABLE_OPEN
    monkeypatch.setattr(analysis_calendar, "evaluate_channel_calendar_day", lambda channel, day: decision(day, status))

    async def check():
        nonlocal status
        engine, sessions, _ = await database(tmp_path, [(1, "2026-01-02T09:00:00.000000")])
        try:
            async with sessions() as session:
                assert await ensure_calendar_facts(session, 1, "ctp") == 1
                status = CalendarDecisionStatus.UNAVAILABLE
                assert await ensure_calendar_facts(session, 1, "ctp") == 1
                assert await session.scalar(sa.select(sa.func.count()).select_from(calendar_days)) == 1
                assert await session.scalar(sa.select(calendar_days.c.status)) == "unavailable"
                assert await session.scalar(sa.select(analysis_state.c.source_version)) == 7
        finally:
            await engine.dispose()

    asyncio.run(check())


@pytest.mark.parametrize("unregistered", [False, True])
def test_terminal_channel_decisions_fill_all_days_after_one_evaluation(tmp_path, monkeypatch, unregistered):
    calls = []

    def evaluate(channel, day):
        calls.append(day)
        if unregistered:
            raise KeyError(channel)
        return decision(day, CalendarDecisionStatus.NOT_REQUIRED)

    monkeypatch.setattr(analysis_calendar, "evaluate_channel_calendar_day", evaluate)

    async def check():
        engine, sessions, _ = await database(
            tmp_path, [(1, "2026-01-01T09:00:00.000000"), (1, "2026-01-05T09:00:00.000000")]
        )
        try:
            async with sessions() as session:
                assert await ensure_calendar_facts(session, 1, "ctp", batch_size=2) == 5
                rows = (await session.execute(sa.select(calendar_days))).mappings().all()
                expected = "unregistered" if unregistered else "not_required"
                assert len(rows) == 5
                assert {row["status"] for row in rows} == {expected}
                assert all(row["calendar_id"] is None and row["label"] is None for row in rows)
                assert calls == [date(2026, 1, 1)]
        finally:
            await engine.dispose()

    asyncio.run(check())


@pytest.mark.parametrize("batch_size", [0, 1001])
def test_invalid_batch_size_fails_before_querying(batch_size):
    with pytest.raises(ValueError, match="batch_size"):
        asyncio.run(ensure_calendar_facts(None, 1, "ctp", batch_size=batch_size))
