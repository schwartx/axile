"""Task recovery must not overwrite a native publication that already committed."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server.db.models.analysis import analysis_snapshot as snapshots
from axile.server.db.models.analysis import analysis_state as states
from axile.server.db.models.analysis_view import execution_fee, execution_summary
from axile.server.db.models.performance import PerformanceSettings
from axile.server.native_analysis import NativeAnalysisProtocolError, NativeAnalysisTimeout
from axile.server.performance_analysis import LOGIC_VERSION, AnalysisManager, compute_batch, enqueue
from axile.server.performance_replay import ENGINE_VERSION
from tests.unit.server._execution_test_support import build_account


@asynccontextmanager
async def queue_database(path, **values):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    async with sessions() as session:
        session.add(build_account(id=7))
        await session.flush()
        await session.execute(
            states.insert(),
            {"account_id": 7, "source_version": 4, "published_version": 3, "running_token": "owned", **values},
        )
        await session.commit()
    try:
        yield sessions, AnalysisManager(sessions)
    finally:
        await engine.dispose()


async def state(sessions):
    async with sessions() as session:
        return dict((await session.execute(sa.select(states))).mappings().one())


async def commit_publication(sessions, row, **state_changes):
    async with sessions() as session:
        await session.execute(
            snapshots.insert(),
            {
                "id": row["running_token"],
                "account_id": row["account_id"],
                "source_version": row["source_version"],
                "logic_version": LOGIC_VERSION,
                "engine_version": ENGINE_VERSION,
                "computed_at": "2026-10-20T12:00:00+08:00",
                "settings": {},
                "ranges": {},
                "cost_schema_version": 1,
                "result_schema_version": 2,
                "read_view_schema_version": 1,
            },
        )
        await session.execute(
            states.update()
            .where(states.c.account_id == row["account_id"])
            .values(
                current_snapshot=row["running_token"],
                published_version=row["source_version"],
                running_version=None,
                running_token=None,
                requested=False,
                error=None,
                failures=0,
                **state_changes,
            )
        )
        await session.commit()


@pytest.mark.parametrize("recovery", ["failure", "cancel"])
@pytest.mark.parametrize("latest", ["committed", "new_claim", "same_version_new_claim"])
def test_recovery_preserves_committed_result_and_newer_claim(tmp_path, recovery, latest):
    async def check():
        values = (
            {"published_version": 4, "current_snapshot": "published", "running_version": None, "running_token": None}
            if latest == "committed"
            else {"source_version": 5, "running_version": 5, "running_token": "newer"}
            if latest == "new_claim"
            else {"running_version": 4, "running_token": "newer"}
        )
        async with queue_database(tmp_path / "queue.db", **values) as (sessions, manager):
            before = await state(sessions)
            row = {"account_id": 7, "source_version": 4, "failures": 0, "running_token": "owned"}
            if recovery == "failure":
                await manager._fail(row)
            else:
                await manager._release_claim(row)
            assert await state(sessions) == before

    asyncio.run(check())


@pytest.mark.parametrize("recovery", ["failure", "cancel"])
def test_source_change_requeues_only_the_old_claim_without_failure(tmp_path, recovery):
    async def check():
        async with queue_database(tmp_path / "queue.db", source_version=5, running_version=4) as (sessions, manager):
            row = {"account_id": 7, "source_version": 4, "failures": 0, "running_token": "owned"}
            if recovery == "failure":
                await manager._fail(row)
            else:
                await manager._release_claim(row)
            current = await state(sessions)
            assert current["source_version"] == 5
            assert current["running_version"] is None
            assert current["running_token"] is None
            assert current["requested"]
            assert current["failures"] == 0
            assert current["error"] is None

    asyncio.run(check())


def test_failure_of_owned_current_version_keeps_retry_semantics(tmp_path):
    async def check():
        async with queue_database(tmp_path / "queue.db", running_version=4) as (sessions, manager):
            await manager._fail({"account_id": 7, "source_version": 4, "failures": 0, "running_token": "owned"})
            current = await state(sessions)
            assert current["running_version"] is None
            assert current["running_token"] is None
            assert current["requested"]
            assert current["failures"] == 1
            assert current["retry_at"] > 0
            assert current["error"]

    asyncio.run(check())


def test_two_coordinators_cannot_claim_the_same_queued_version(tmp_path):
    async def check():
        async with queue_database(tmp_path / "queue.db", requested=True) as (sessions, manager):
            other = AnalysisManager(sessions)
            claimed = await asyncio.gather(manager._claim(), other._claim())
            assert sum(row is not None for row in claimed) == 1
            current = await state(sessions)
            assert current["running_version"] == 4
            assert len(current["running_token"]) == 32
            assert next(row for row in claimed if row is not None)["running_token"] == current["running_token"]
            assert not current["requested"]

    asyncio.run(check())


@pytest.mark.parametrize("interruption", ["receipt", "timeout", "cancel"])
def test_postcommit_failure_or_cancel_preserves_authoritative_snapshot(tmp_path, monkeypatch, interruption):
    async def check():
        async with queue_database(tmp_path / "queue.db", requested=True) as (sessions, manager):
            captured = []

            async def publish_then_interrupt(row):
                captured.append(row)
                await commit_publication(sessions, row)
                if interruption == "cancel":
                    raise asyncio.CancelledError
                if interruption == "timeout":
                    raise NativeAnalysisTimeout("receipt timed out")
                raise NativeAnalysisProtocolError("receipt lost")

            monkeypatch.setattr(manager, "_run_native", publish_then_interrupt)
            if interruption == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await manager.run_once()
            else:
                assert await manager.run_once()
            current = await state(sessions)
            assert current["current_snapshot"] == captured[0]["running_token"]
            assert current["published_version"] == 4
            assert current["running_version"] is None and current["running_token"] is None
            assert not current["requested"]
            assert current["error"] is None and current["failures"] == 0

    asyncio.run(check())


def test_native_dispatch_uses_claim_id_and_never_stages_result_rows(tmp_path, monkeypatch):
    async def check():
        async with queue_database(tmp_path / "queue.db", requested=True) as (sessions, manager):
            sent = []

            async def publish_store(**request):
                sent.append(request)
                row = {
                    "account_id": request["account_id"],
                    "source_version": request["expected_source_version"],
                    "running_token": request["snapshot_id"],
                }
                await commit_publication(sessions, row)
                return {"published": True}

            monkeypatch.setattr(
                "axile.server.native_analysis.get_native_analysis",
                lambda session: SimpleNamespace(publish_store=publish_store),
            )
            monkeypatch.setattr("axile.server.analysis_observations.ensure_observation_facts", AsyncMock())
            monkeypatch.setattr(
                "axile.server.analysis_source.ensure_source_facts",
                AsyncMock(return_value=SimpleNamespace(unsupported=0)),
            )
            monkeypatch.setattr("axile.server.analysis_calendar.ensure_calendar_facts", AsyncMock())

            def no_rows(*args, **kwargs):
                raise AssertionError("native publication must not load/stage result rows in Python")

            monkeypatch.setattr(manager, "_load", no_rows)
            monkeypatch.setattr(manager, "_publish_structured", no_rows)
            monkeypatch.setattr("axile.server.analysis_staging.stage_publication", no_rows)
            assert await manager.run_once()
            current = await state(sessions)
            assert sent[0]["snapshot_id"] == current["current_snapshot"]
            assert sent[0]["expected_source_version"] == 4
            assert sent[0]["computed_at"]
            assert set(sent[0]) == {
                "account_id",
                "expected_source_version",
                "snapshot_id",
                "logic_version",
                "engine_version",
                "computed_at",
                "settings",
                "trade_channel",
            }

    asyncio.run(check())


def test_native_failure_does_not_fall_back_to_python_calculation(tmp_path, monkeypatch):
    async def check():
        async with queue_database(tmp_path / "queue.db", requested=True) as (sessions, manager):

            async def fail_native(row):
                raise NativeAnalysisProtocolError("synthetic native error")

            def no_python(*args, **kwargs):
                raise AssertionError("native failure must not fall back to Python")

            monkeypatch.setattr(manager, "_run_native", fail_native)
            monkeypatch.setattr(manager, "_load", no_python)
            assert await manager.run_once()
            current = await state(sessions)
            assert current["failures"] == 1 and current["error"]
            assert current["running_version"] is None and current["running_token"] is None

    asyncio.run(check())


def test_legacy_publication_freezes_views_and_collects_obsolete_children(tmp_path):
    async def check():
        async with queue_database(tmp_path / "queue.db", requested=True) as (sessions, manager):
            settings = PerformanceSettings(backtest_weight_type="cs", backtest_fee_rate=0.0002)
            published = []
            for _ in range(3):
                row = await manager._claim()
                await manager._publish(row, settings, compute_batch([], [], [], [], settings))
                published.append(row["running_token"])
                async with sessions() as session:
                    marker = await session.scalar(
                        sa.select(snapshots.c.read_view_schema_version).where(snapshots.c.id == published[-1])
                    )
                    assert marker == 1
                    await session.execute(
                        execution_summary.insert(),
                        {
                            "snapshot_id": published[-1],
                            "record_id": 1,
                            "time": 1000,
                            "covered": 0,
                            "count": 0,
                            "fee_covered": 0,
                            "amount_complete": True,
                            "covered_value": 0,
                            "estimated": 0,
                            "has_transactions": False,
                        },
                    )
                    await session.execute(
                        execution_fee.insert(),
                        {"snapshot_id": published[-1], "record_id": 1, "currency": "USD", "value": 0, "count": 1},
                    )
                    await session.commit()
                    await enqueue(session, 7, force=True)
            async with sessions() as session:
                for table in (snapshots, execution_summary, execution_fee):
                    key = table.c.id if table is snapshots else table.c.snapshot_id
                    assert set((await session.scalars(sa.select(key))).all()) == set(published[-2:])

    asyncio.run(check())


def test_legacy_compare_and_publish_cannot_overwrite_another_attempt(tmp_path):
    async def check():
        async with queue_database(tmp_path / "queue.db", running_version=4, running_token="newer") as (
            sessions,
            manager,
        ):
            before = await state(sessions)
            async with sessions() as session:
                previous = await manager._claim_publication(
                    session, {"account_id": 7, "source_version": 4, "running_token": "owned"}, "candidate"
                )
                assert previous is None
                await session.commit()
            assert await state(sessions) == before

    asyncio.run(check())


def test_restart_clears_interrupted_version_and_token_together(tmp_path, monkeypatch):
    async def check():
        async with queue_database(tmp_path / "queue.db", running_version=4) as (sessions, manager):
            monkeypatch.setattr(manager, "_loop", AsyncMock())
            await manager.start()
            await manager.stop()
            current = await state(sessions)
            assert current["running_version"] is None and current["running_token"] is None
            assert current["requested"]

    asyncio.run(check())
