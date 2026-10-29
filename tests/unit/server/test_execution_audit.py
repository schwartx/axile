"""执行审计持久化与同步封装器的测试。"""

import asyncio
import subprocess
import sys
import threading

import pytest

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import (
    ExecutionArtifactType,
    ExecutionEventStatus,
    ExecutionEventType,
    ExecutionReasonFamily,
)
from axile.server import execution_audit
from axile.server.execution import audit_batch
from tests.unit.server._runtime_db_support import runtime_database, runtime_rows


def _event_kwargs():
    return {
        "execution_id": "exec-1",
        "account_id": 1,
        "channel": TradeChannel.CTP,
        "algorithm": "SINGLE-MAKER",
        "event_type": ExecutionEventType.SYMBOL_SKIPPED,
        "status": ExecutionEventStatus.WARNING,
        "reason_family": ExecutionReasonFamily.MARKET_RULE,
        "reason_code": "EXTERNAL.BELOW_MIN_NOTIONAL",
        "symbol": "ag2612",
        "seq": 2,
    }


@pytest.mark.parametrize("inserted", [True, False])
def test_event_notifies_only_after_successful_native_commit(monkeypatch, inserted):
    trace = []

    async def persist(name, row):
        assert name == "append_execution_event"
        assert row["event_type"] == "symbol_skipped"
        assert row["event_uid"] == execution_audit.build_event_uid(
            "exec-1", ExecutionEventType.SYMBOL_SKIPPED, symbol="ag2612", seq=2
        )
        trace.append("commit")
        return inserted

    monkeypatch.setattr(execution_audit, "native_operation", persist)
    monkeypatch.setattr(execution_audit, "_notify_live_hub", lambda **_: trace.append("notify"))
    assert asyncio.run(execution_audit.append_execution_event(**_event_kwargs())) is inserted
    assert trace == (["commit", "notify"] if inserted else ["commit"])


@pytest.mark.parametrize("inserted", [True, False])
def test_artifact_returns_native_duplicate_receipt(monkeypatch, inserted):
    async def persist(name, row):
        assert name == "append_execution_artifact"
        assert row["artifact_type"] == "environment_snapshot"
        assert row["content"] == {"summary": {"filled": 1}}
        return inserted

    monkeypatch.setattr(execution_audit, "native_operation", persist)
    assert (
        asyncio.run(
            execution_audit.append_execution_artifact(
                execution_id="exec-1",
                artifact_type=ExecutionArtifactType.ENVIRONMENT_SNAPSHOT,
                content={"summary": {"filled": 1}},
            )
        )
        is inserted
    )


@pytest.mark.parametrize("raise_on_error", [False, True])
def test_native_commit_failure_never_broadcasts(monkeypatch, raise_on_error):
    notifications = []

    async def fail(name, row):
        raise RuntimeError("native commit failed")

    monkeypatch.setattr(execution_audit, "native_operation", fail)
    monkeypatch.setattr(execution_audit, "_notify_live_hub", lambda **kw: notifications.append(kw))
    if raise_on_error:
        with pytest.raises(RuntimeError, match="native commit failed"):
            asyncio.run(execution_audit.append_execution_event(**_event_kwargs(), raise_on_error=True))
    else:
        assert asyncio.run(execution_audit.append_execution_event(**_event_kwargs())) is False
    assert notifications == []


@pytest.mark.parametrize("kind", ["event", "artifact", "batch"])
def test_sync_audit_rejects_running_loop_before_starting_write(monkeypatch, kind):
    from axile.server.execution import audit_batch

    async def unexpected(*_args, **_kwargs):
        pytest.fail("循环内同步误用不应开始写入")

    monkeypatch.setattr(execution_audit, "append_execution_event", unexpected)
    monkeypatch.setattr(execution_audit, "append_execution_artifact", unexpected)
    monkeypatch.setattr(audit_batch, "_persist_batch", unexpected)

    async def check():
        heartbeat = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(heartbeat.set_result, None)
        with pytest.raises(RuntimeError, match="await"):
            if kind == "event":
                execution_audit.append_execution_event_sync(**_event_kwargs())
            elif kind == "artifact":
                execution_audit.append_execution_artifact_sync()
            else:
                audit_batch.append_execution_audit_batch_sync(audit_batch.ExecutionAuditBatch(max_entries=1))
        await asyncio.wait_for(heartbeat, 1)

    asyncio.run(check())


def test_sync_audit_keeps_worker_coroutine_deadline():
    with pytest.raises(TimeoutError):
        execution_audit._run_coroutine_sync(lambda: asyncio.sleep(1), timeout=0.01)


def test_append_execution_event_sync_swallows_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """审计写入超时应被吞成 False（尽力而为），不向调用方抛异常拖死执行。"""

    def _raise_timeout(_factory: object) -> bool:
        raise TimeoutError("audit db hang")

    monkeypatch.setattr(execution_audit, "_run_coroutine_sync", _raise_timeout)

    result = execution_audit.append_execution_event_sync(
        execution_id="exec-1",
        account_id=1,
        channel=TradeChannel.CTP,
        algorithm="SINGLE-MAKER",
        event_type=ExecutionEventType.EXECUTION_STARTED,
        status=ExecutionEventStatus.INFO,
        reason_family=ExecutionReasonFamily.SYSTEM,
        reason_code="COMMON.EXECUTION_STARTED",
    )

    assert result is False


def test_append_execution_artifact_sync_swallows_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """附件写入超时同样应被吞成 False。"""

    def _raise_timeout(_factory: object) -> bool:
        raise TimeoutError("audit db hang")

    monkeypatch.setattr(execution_audit, "_run_coroutine_sync", _raise_timeout)

    result = execution_audit.append_execution_artifact_sync(
        execution_id="exec-1",
        artifact_type=ExecutionArtifactType.ENVIRONMENT_SNAPSHOT,
        content={"summary": {"filled": 1}},
    )

    assert result is False


def test_native_audit_sync_async_and_batch_commit_before_notification(native_database, monkeypatch):
    notifications = []

    def notify(**row):
        assert runtime_rows(native_database, "SELECT id FROM execution_event WHERE seq=?", (row["seq"],))
        notifications.append(row["seq"])

    monkeypatch.setattr(execution_audit, "_notify_live_hub", notify)
    monkeypatch.setattr(audit_batch, "_notify_live_hub", notify)

    async def check():
        async with runtime_database(native_database, wal=True):
            kwargs = _event_kwargs()
            assert await execution_audit.append_execution_event(**kwargs)
            assert not await execution_audit.append_execution_event(**kwargs)
            assert await asyncio.to_thread(execution_audit.append_execution_event_sync, **(kwargs | {"seq": 3}))
            artifact = dict(
                execution_id="exec-1", artifact_type=ExecutionArtifactType.ENVIRONMENT_SNAPSHOT, content={"ok": True}
            )
            assert await execution_audit.append_execution_artifact(**artifact)
            assert not await asyncio.to_thread(execution_audit.append_execution_artifact_sync, **artifact)
            assert await asyncio.to_thread(
                execution_audit.append_execution_artifact_sync,
                **(artifact | {"artifact_type": ExecutionArtifactType.STANDARD_INPUT}),
            )
            batch = audit_batch.ExecutionAuditBatch(max_entries=3)
            batch.add_event(execution_audit.build_execution_event(**(kwargs | {"seq": 4})))
            batch.add_artifact(**(artifact | {"execution_id": "exec-batch"}))
            batch.add_artifact(**(artifact | {"content": {"invalid": object()}}))
            assert await asyncio.to_thread(audit_batch.append_execution_audit_batch_sync, batch) == 2
            assert await audit_batch.append_execution_audit_batch(batch) == 0

    asyncio.run(check())
    assert notifications == [2, 3, 4]
    assert len(runtime_rows(native_database, "SELECT id FROM execution_event")) == 3
    assert len(runtime_rows(native_database, "SELECT id FROM execution_artifact")) == 3


def test_cancelled_native_audit_waits_for_locked_database_and_releases_writer(native_database, monkeypatch):
    from axile.server.native_store import _core

    entered, finished = threading.Event(), threading.Event()
    native_write = _core.append_execution_event

    def write(*args):
        entered.set()
        try:
            return native_write(*args)
        finally:
            finished.set()

    monkeypatch.setattr(_core, "append_execution_event", write)
    # 独立进程持锁，避免 Python 与原生 SQLite 动态库共享进程的文件锁干扰。
    script = """
import sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
connection.execute('BEGIN IMMEDIATE')
print('locked', flush=True)
sys.stdin.readline()
connection.rollback()
connection.close()
"""

    async def check():
        async with runtime_database(native_database, wal=True):
            with subprocess.Popen(
                [sys.executable, "-c", script, str(native_database)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            ) as locker:
                try:
                    assert await asyncio.wait_for(asyncio.to_thread(locker.stdout.readline), 2) == "locked\n"
                    task = asyncio.create_task(execution_audit.append_execution_event(**_event_kwargs()))
                    assert await asyncio.to_thread(entered.wait, 2)
                    task.cancel()
                    _, pending = await asyncio.wait({task}, timeout=0.03)
                    assert task in pending and not finished.is_set()
                finally:
                    locker.stdin.write("release\n")
                    locker.stdin.flush()
                    await asyncio.to_thread(locker.wait, 2)
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
                assert finished.is_set()
            # 取消完成时原生调用已经退出，后续 writer 不受遗留占用阻塞。
            assert await execution_audit.append_execution_event(**(_event_kwargs() | {"seq": 3}))

    asyncio.run(check())
    assert len(runtime_rows(native_database, "SELECT id FROM execution_event")) == 2
