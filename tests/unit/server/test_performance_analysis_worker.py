"""Durable queue wiring; native transaction semantics are tested in Rust."""

import asyncio
import importlib.util
import json
import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server import performance_analysis as analysis
from axile.server.db.models.analysis import analysis_state as states
from tests.unit.server._business_analysis_support import install_analysis_triggers, persist_records, record
from tests.unit.server._execution_test_support import build_account


@asynccontextmanager
async def database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'analysis.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(build_account(id=1))
        await session.commit()
        await analysis.enqueue(session, 1)
    yield factory
    await engine.dispose()


def with_database(test):
    def run(tmp_path, monkeypatch):
        async def check():
            async with database(tmp_path) as sessions:
                await test(sessions, monkeypatch)

        asyncio.run(check())

    return run


@with_database
async def test_real_native_publication_ready_and_readable(sessions, monkeypatch):
    path = os.environ.get("AXILE_TEST_NATIVE_EXTENSION")
    native = analysis._core
    if path:
        spec = importlib.util.spec_from_file_location("_core", path)
        native = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(native)
    if not hasattr(native, "publish_store"):
        pytest.skip("Rebuild native extension or set AXILE_TEST_NATIVE_EXTENSION")
    monkeypatch.setattr(analysis, "_core", native)
    async with sessions() as session:
        connection = await session.connection()
        await connection.run_sync(install_analysis_triggers)
        execution = record()
        execution.account_id = 1
        await persist_records(session, [execution])
        await session.commit()
    manager = analysis.AnalysisManager(sessions)
    assert await manager.run_once()
    row = await state(sessions)
    assert row["current_snapshot"] is not None, row["error"]
    assert row["published_version"] == row["source_version"]
    assert row["running_token"] is None and row["failures"] == 0
    async with sessions() as session:
        assert await analysis.read_snapshot_status(session, 1) == "ready"
        store = native.Store(session.get_bind().url.database)
    payload = native.snapshot(store, 1, "all", analysis.LOGIC_VERSION, analysis.ENGINE_VERSION)
    if isinstance(payload, bytes):
        payload = json.loads(payload)
    assert payload["status"] == "ready"
    async with sessions() as session:
        assert await session.scalar(sa.text("SELECT count(*) FROM performance_cost_execution")) == 1
        assert await session.scalar(sa.text("SELECT count(*) FROM performance_cost_trade")) == 1
    assert not await manager.run_once()


async def state(sessions):
    async with sessions() as session:
        return (await session.execute(sa.select(states))).mappings().one()


@with_database
async def test_native_dispatch_uses_claim_and_file_database(sessions, monkeypatch):
    requests = []

    def publish(path, payload):
        requests.append((path, json.loads(payload)))
        return {"published": False, "reason": "source_changed"}

    monkeypatch.setattr(analysis._core, "publish_store", publish, raising=False)
    manager = analysis.AnalysisManager(sessions)
    assert await manager.run_once()
    path, request = requests[0]
    assert path.endswith("analysis.sqlite")
    assert request["command"] == "publish_store"
    assert request["claim_token"] == request["snapshot_id"] == (await state(sessions))["running_token"]
    assert request["expected_source_version"] == 1
    assert request["settings"]["backtest_fee_rate"] == 0


@with_database
async def test_failure_retry_and_changed_source_release(sessions, monkeypatch):
    manager = analysis.AnalysisManager(sessions)
    monkeypatch.setattr(manager, "_run_native", AsyncMock(side_effect=ValueError("broken")))
    assert await manager.run_once()
    row = await state(sessions)
    assert row["failures"] == 1 and row["requested"] and row["running_token"] is None
    assert not await manager.run_once()
    async with sessions() as session:
        await session.execute(states.update().values(retry_at=0))
        await session.commit()
    claimed = await manager._claim()
    async with sessions() as session:
        await session.execute(states.update().values(source_version=2, failures=0))
        await session.commit()
    await manager._fail(claimed)
    row = await state(sessions)
    assert row["failures"] == 0 and row["requested"] and row["running_token"] is None


@with_database
async def test_restart_recovers_claim_and_null_engine_version(sessions, monkeypatch):
    manager = analysis.AnalysisManager(sessions)
    await manager._claim()
    async with sessions() as session:
        await session.execute(states.update().values(current_snapshot="old", failures=4))
        await session.commit()
    monkeypatch.setattr(analysis._core, "publish_store", lambda *args: None, raising=False)
    monkeypatch.setattr(manager, "_loop", AsyncMock())
    await manager.start()
    await manager.start()
    await manager.stop()
    row = await state(sessions)
    assert row["source_version"] == 2 and row["requested"]
    assert row["failures"] == 0 and row["running_token"] is None


@with_database
async def test_concurrent_runs_serialize_and_shutdown_drains(sessions, monkeypatch):
    manager = analysis.AnalysisManager(sessions)
    entered, release = asyncio.Event(), asyncio.Event()

    async def publish(row):
        entered.set()
        await release.wait()

    monkeypatch.setattr(analysis._core, "publish_store", lambda *args: None, raising=False)
    monkeypatch.setattr(manager, "_run_native", publish)
    await manager.start()
    await entered.wait()
    stopped = asyncio.create_task(manager.stop())
    await asyncio.sleep(0)
    assert not stopped.done()
    release.set()
    await stopped
    assert not await manager.run_once()


@with_database
async def test_old_claim_cannot_fail_new_claim_or_deleted_account(sessions, monkeypatch):
    manager = analysis.AnalysisManager(sessions)
    claimed = await manager._claim()
    async with sessions() as session:
        await session.execute(states.update().values(running_token="new"))
        await session.commit()
    await manager._fail(claimed)
    assert (await state(sessions))["running_token"] == "new"
    async with sessions() as session:
        await session.execute(states.delete())
        await session.commit()
    await manager._fail(claimed)
    async with sessions() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(states)) == 0
