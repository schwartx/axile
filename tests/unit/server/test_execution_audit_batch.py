"""固定阶段审计批次的真实 SQLite 隔离、提交与通知边界。"""

import asyncio
import threading

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import (
    ExecutionArtifactType,
    ExecutionEventStatus,
    ExecutionEventType,
    ExecutionReasonFamily,
)
from axile.server.business_facts import assets, repository
from axile.server.db.models import ExecutionArtifact, ExecutionEvent
from axile.server.execution import audit_batch, input_persistence
from axile.server.execution_audit import build_execution_event
from tests.unit.server._execution_test_support import build_account


def _event(seq=1, **kwargs):
    return build_execution_event(
        execution_id="batch-execution",
        account_id=1,
        channel=TradeChannel.CTP,
        algorithm="SINGLE-MAKER",
        event_type=ExecutionEventType.EXECUTION_STARTED,
        status=ExecutionEventStatus.INFO,
        reason_family=ExecutionReasonFamily.SYSTEM,
        reason_code="COMMON.EXECUTION_STARTED",
        seq=seq,
        **kwargs,
    )


def test_batch_enforces_hard_limit_and_reuses_payload_references():
    batch = audit_batch.ExecutionAuditBatch(max_entries=3)
    content = {"large_payload": ["synthetic"] * 1000}
    details = {}
    entry = _event(details=details)
    assert batch.add_event(entry) is None
    batch.add_artifact(
        execution_id="batch-execution", artifact_type=ExecutionArtifactType.EXECUTION_SUMMARY, content=content
    )
    batch.add_event(_event(seq=2))
    entries = tuple(batch)
    assert entries[0] is entry
    assert entry.details is details
    assert entries[1].content is content
    with pytest.raises(ValueError, match="固定上限"):
        batch.add_event(_event(seq=3))
    with pytest.raises(ValueError):
        audit_batch.ExecutionAuditBatch(max_entries=9)


def test_batch_isolates_duplicates_and_bad_rows_and_notifies_after_commit(tmp_path, monkeypatch):
    async def check():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'isolation.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(audit_batch, "SessionLocal", sessions)
        trace = []
        statements = []
        event.listen(engine.sync_engine, "commit", lambda *_: trace.append("commit"))
        event.listen(engine.sync_engine, "before_cursor_execute", lambda _c, _u, s, *_: statements.append(s))
        monkeypatch.setattr(audit_batch, "_notify_live_hub", lambda **kw: trace.append(("notify", kw["seq"])))
        try:
            async with engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            batch = audit_batch.ExecutionAuditBatch(max_entries=8)
            batch.add_event(_event())
            batch.add_event(_event())
            batch.add_artifact(
                execution_id="batch-execution", artifact_type=ExecutionArtifactType.STANDARD_INPUT, content={}
            )
            batch.add_artifact(
                execution_id="batch-execution", artifact_type=ExecutionArtifactType.STANDARD_INPUT, content={}
            )
            bad_event = _event(seq=2)
            bad_event.reason_code = None
            batch.add_event(bad_event)
            batch.add_artifact(
                execution_id="batch-execution",
                artifact_type=ExecutionArtifactType.EXECUTION_SUMMARY,
                content={"invalid_json": object()},
            )
            batch.add_event(_event(seq=3))
            trace.clear()
            statements.clear()
            assert await audit_batch.append_execution_audit_batch(batch) == 3
            assert trace == ["commit", ("notify", 1), ("notify", 3)]
            assert sum(s.startswith("SAVEPOINT ") for s in statements) == 7
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(ExecutionEvent)) == 2
                assert await session.scalar(select(func.count()).select_from(ExecutionArtifact)) == 1
        finally:
            await engine.dispose()

    asyncio.run(check())


@pytest.mark.parametrize("failure", ["commit", "cancel"])
def test_batch_failure_rolls_back_audit_but_keeps_committed_business_facts(tmp_path, monkeypatch, failure):
    async def check():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rollback.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(input_persistence, "SessionLocal", sessions)
        before = {"total_asset": 100.0, "positions": []}
        after = {"total_asset": 101.0, "positions": []}
        payload = {"curr_target": {"A": 0.0}}
        notifications = []
        monkeypatch.setattr(audit_batch, "_notify_live_hub", lambda **kw: notifications.append(kw))
        entered_commit = asyncio.Event()
        rollbacks = []

        class FailingSession(AsyncSession):
            async def commit(self):
                entered_commit.set()
                if failure == "commit":
                    raise RuntimeError("synthetic commit failure")
                await asyncio.Event().wait()

            async def rollback(self):
                rollbacks.append(True)
                await super().rollback()

        try:
            async with engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            async with sessions() as session:
                session.add(build_account(id=1, account_control_preset="default"))
                await session.commit()
            input_id = await input_persistence._persist_pre_execute_input(
                account_id=1, execution_id="batch-execution", payload=payload, before_account_assets=before
            )
            await input_persistence._persist_post_execute_assets(
                account_id=1, execution_id="batch-execution", payload={"account_assets": after}
            )
            monkeypatch.setattr(
                audit_batch, "SessionLocal", async_sessionmaker(engine, class_=FailingSession, expire_on_commit=False)
            )
            batch = audit_batch.ExecutionAuditBatch(max_entries=3)
            batch.add_event(_event())
            batch.add_artifact(
                execution_id="batch-execution", artifact_type=ExecutionArtifactType.STANDARD_INPUT, content={}
            )
            task = asyncio.create_task(audit_batch.append_execution_audit_batch(batch))
            if failure == "cancel":
                await asyncio.wait_for(entered_commit.wait(), timeout=1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                assert await task == 0
            assert rollbacks == [True]
            assert notifications == []
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(ExecutionEvent)) == 0
                assert await session.scalar(select(func.count()).select_from(ExecutionArtifact)) == 0
                assert await repository.read_execution_input(session, input_id) == payload
                stored_assets = list((await session.execute(select(assets.asset_snapshot))).mappings())
                assert len(stored_assets) == 2
                assert {row["total_asset"] for row in stored_assets} == {100.0, 101.0}
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_sync_batch_notifies_in_caller_thread_after_database_thread_returns(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'thread.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(audit_batch, "SessionLocal", sessions)
    trace = []
    event.listen(engine.sync_engine, "commit", lambda *_: trace.append(("commit", threading.get_ident())))
    monkeypatch.setattr(audit_batch, "_notify_live_hub", lambda **_kw: trace.append(("notify", threading.get_ident())))

    async def check():
        try:
            async with engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            trace.clear()
            caller = threading.get_ident()
            batch = audit_batch.ExecutionAuditBatch(max_entries=3)
            batch.add_event(_event())
            assert audit_batch.append_execution_audit_batch_sync(batch) == 1
            assert trace[0][0] == "commit"
            assert trace[0][1] != caller
            assert trace[1] == ("notify", caller)
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_sync_batch_soft_fails_bridge_error(monkeypatch):
    def fail(_factory):
        raise TimeoutError("synthetic deadline")

    monkeypatch.setattr(audit_batch, "_run_coroutine_sync", fail)
    batch = audit_batch.ExecutionAuditBatch(max_entries=3)
    batch.add_event(_event())
    assert audit_batch.append_execution_audit_batch_sync(batch) == 0
