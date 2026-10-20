"""Compact on-demand account returns preserve metadata and avoid ORM history loads."""

import asyncio
import json
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server import native_account_performance as module
from axile.server.db.models import ExecuteRecord, PortfolioAccount
from axile.server.db.models.analysis import analysis_state as states
from axile.server.db.models.analysis_replay import replay_observation as facts
from axile.server.db.models.performance import AccountPerformance, PerformanceSettings
from axile.server.native_analysis import NativeAnalysisError
from axile.server.performance import calculate_performance, observation
from tests.unit.server._execution_test_support import build_account


def records():
    return [
        ExecuteRecord(
            id=index,
            account_id=7,
            execution_id=f"account-{index}",
            created_at=f"2026-01-0{index}T09:00:00",
            raw_input={"curr_target": {}},
            raw_result={"account_assets": {"total_asset": asset, "positions": []}},
            is_success=1,
        )
        for index, asset in enumerate((100, 110, 121), 1)
    ]


async def database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'account.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        session.add(build_account(id=7, backtest_fee_rate=0.003))
        session.add_all(records())
        session.add_all(
            [
                PortfolioAccount(account_id=7, portfolio_id=None, created_at=when)
                for when in ("2026-01-04T09:00:00", "2026-01-02T01:00:00+00:00", "2025-12-31T09:00:00")
            ]
        )
        await session.flush()
        await session.execute(states.insert().values(account_id=7, source_version=1))
        await session.commit()
    return engine, sessions


@pytest.mark.parametrize("range_key", ["30", "90", "all"])
def test_native_account_returns_keep_curve_and_metadata_without_point_models(tmp_path, monkeypatch, range_key):
    async def check():
        engine, sessions = await database(tmp_path)
        settings = PerformanceSettings(backtest_weight_type="cs", backtest_fee_rate=0.003)
        expected = calculate_performance([observation(item) for item in records()], settings, range_key, False, "ctp")
        calls = []
        prepared = []
        ensure = module.ensure_observation_facts

        async def track_ensure(session, account_id):
            prepared.append(session)
            return await ensure(session, account_id)

        async def query(command, **fields):
            assert command == "performance"
            assert fields["account_id"] == 7 and fields["range"] == range_key
            assert fields["settings"] == settings.model_dump()
            assert fields["include_backtest"] is False
            assert fields["expected_source_version"] == 1
            assert not prepared[-1].in_transaction()
            calls.append(fields)
            return expected.model_dump_json().encode()

        def reject_models(*args, **kwargs):
            raise AssertionError("native curve must not be rebuilt as point models")

        monkeypatch.setattr(module, "get_native_analysis", lambda session: SimpleNamespace(query=query))
        monkeypatch.setattr(module, "ensure_observation_facts", track_ensure)
        monkeypatch.setattr(AccountPerformance, "model_validate", reject_models)
        try:
            async with sessions() as session:
                # The existing read transaction must close before bounded backfill writes.
                await session.execute(sa.text("BEGIN"))
                actual = json.loads(await module.native_account_performance(session, 7, range_key))
                assert prepared[0] is not session
                assert not session.in_transaction()
            assert actual["points"] == expected.model_dump()["points"]
            assert actual["calendar"] == expected.calendar.model_dump()
            assert actual["bindings"] == [{"time": "2026-01-02T09:00:00", "portfolio_id": None}]
            assert actual["backtest_included"] is False
            assert len(calls) == 1
            async with sessions() as session:
                assert await session.scalar(sa.select(sa.func.count()).select_from(facts)) == 3
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_unavailable_native_does_not_rollback_or_backfill(monkeypatch):
    monkeypatch.setattr(module, "get_native_analysis", lambda session: None)

    async def forbidden(*args, **kwargs):
        raise AssertionError("compatibility detection must not mutate transaction state")

    monkeypatch.setattr(module, "ensure_observation_facts", forbidden)
    session = SimpleNamespace(rollback=forbidden)
    assert asyncio.run(module.native_account_performance(session, 7, "all")) is None


@pytest.mark.parametrize("always_changes", [False, True])
def test_source_change_retries_once_with_fresh_metadata(tmp_path, monkeypatch, always_changes):
    async def check():
        engine, sessions = await database(tmp_path)
        calls = []
        empty = calculate_performance(
            [], PerformanceSettings(backtest_weight_type="cs", backtest_fee_rate=0.003), "all", False
        )

        async def query(command, **fields):
            calls.append(fields["expected_source_version"])
            if len(calls) == 1 or always_changes:
                async with sessions() as writer:
                    await writer.execute(states.update().values(source_version=states.c.source_version + 1))
                    await writer.commit()
                raise NativeAnalysisError("source changed", code="SOURCE_VERSION_CHANGED")
            return empty.model_dump_json().encode()

        monkeypatch.setattr(module, "get_native_analysis", lambda session: SimpleNamespace(query=query))
        try:
            async with sessions() as session:
                if always_changes:
                    with pytest.raises(NativeAnalysisError) as caught:
                        await module.native_account_performance(session, 7, "all")
                    assert caught.value.code == "SOURCE_VERSION_CHANGED"
                else:
                    result = json.loads(await module.native_account_performance(session, 7, "all"))
                    assert result["points"] == []
                    assert result["bindings"] == []
            assert calls == [1, 2]
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_non_version_native_failure_is_not_retried(tmp_path, monkeypatch):
    async def check():
        engine, sessions = await database(tmp_path)
        calls = []

        async def query(command, **fields):
            calls.append(command)
            raise NativeAnalysisError("failed", code="SQLITE_ERROR")

        monkeypatch.setattr(module, "get_native_analysis", lambda session: SimpleNamespace(query=query))
        try:
            async with sessions() as session:
                with pytest.raises(NativeAnalysisError):
                    await module.native_account_performance(session, 7, "all")
            assert calls == ["performance"]
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_compiled_worker_computes_account_only_from_structured_facts(tmp_path, monkeypatch):
    from pathlib import Path

    from axile.server.native_analysis import NativeAnalysis

    binary = Path(__file__).resolve().parents[3] / "rust" / "analysis" / "target" / "debug" / "axile-analysis"
    if not binary.is_file():
        pytest.skip("Build the native analysis worker to exercise the end-to-end contract")

    async def check():
        engine, sessions = await database(tmp_path)
        bridge = NativeAnalysis(binary, tmp_path / "account.db")
        monkeypatch.setattr(module, "get_native_analysis", lambda session: bridge)
        settings = PerformanceSettings(backtest_weight_type="cs", backtest_fee_rate=0.003)
        expected = calculate_performance([observation(item) for item in records()], settings, "all", False, "ctp")
        try:
            async with sessions() as session:
                actual = json.loads(await module.native_account_performance(session, 7, "all"))
            assert actual["range"] == "all"
            assert actual["backtest_included"] is False
            assert actual["record_count"] == expected.record_count
            assert actual["calendar"] == expected.calendar.model_dump()
            assert actual["bindings"] == [{"time": "2026-01-02T09:00:00", "portfolio_id": None}]
            assert len(actual["points"]) == len(expected.points)
            for point, reference in zip(actual["points"], expected.points, strict=True):
                for key, value in reference.model_dump().items():
                    assert point[key] == (pytest.approx(value) if isinstance(value, float) else value)
        finally:
            await engine.dispose()

    asyncio.run(check())
