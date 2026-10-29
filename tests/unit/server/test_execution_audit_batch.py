"""固定阶段审计批次的真实 SQLite 隔离、提交与通知边界。"""

import asyncio
import threading

import pytest

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import (
    ExecutionArtifactType,
    ExecutionEventStatus,
    ExecutionEventType,
    ExecutionReasonFamily,
)
from axile.server.execution import audit_batch
from axile.server.execution_audit import build_execution_event


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
        execution_id="batch-execution", artifact_type=ExecutionArtifactType.ENVIRONMENT_SNAPSHOT, content=content
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


def test_batch_sends_good_rows_once_and_isolates_unserializable_payloads(monkeypatch):
    trace = []

    async def persist(name, request):
        assert name == "append_execution_audit_batch"
        assert len(request["entries"]) == 4
        assert [entry["kind"] for entry in request["entries"]] == ["event", "invalid", "invalid", "event"]
        trace.append("commit")
        return {
            "count": 2,
            "failures": [],
            "notifications": [
                {
                    key: request["entries"][index]["row"].get(key)
                    for key in ("execution_id", "account_id", "event_type", "symbol", "seq")
                }
                for index in (0, 3)
            ],
        }

    monkeypatch.setattr(audit_batch, "native_operation", persist)
    monkeypatch.setattr(audit_batch, "_notify_live_hub", lambda **kw: trace.append(("notify", kw["seq"])))
    batch = audit_batch.ExecutionAuditBatch(max_entries=8)
    batch.add_event(_event())
    batch.add_artifact(
        execution_id="batch-execution",
        artifact_type=ExecutionArtifactType.STANDARD_INPUT,
        content={"invalid_json": object()},
    )
    batch.add_artifact(
        execution_id="batch-execution",
        artifact_type=ExecutionArtifactType.ENVIRONMENT_SNAPSHOT,
        content={"nonfinite": float("nan")},
    )
    batch.add_event(_event(seq=3))
    assert asyncio.run(audit_batch.append_execution_audit_batch(batch)) == 2
    assert trace == ["commit", ("notify", 1), ("notify", 3)]


@pytest.mark.parametrize("cancel", [False, True])
def test_batch_failure_returns_zero_without_notifications_or_propagates_cancellation(monkeypatch, cancel):
    notifications = []

    async def fail(name, request):
        if cancel:
            raise asyncio.CancelledError
        raise RuntimeError("native commit failed")

    monkeypatch.setattr(audit_batch, "native_operation", fail)
    monkeypatch.setattr(audit_batch, "_notify_live_hub", lambda **kw: notifications.append(kw))
    batch = audit_batch.ExecutionAuditBatch(max_entries=3)
    batch.add_event(_event())
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(audit_batch.append_execution_audit_batch(batch))
    else:
        assert asyncio.run(audit_batch.append_execution_audit_batch(batch)) == 0
    assert notifications == []


def test_sync_batch_notifies_in_caller_thread_after_database_thread_returns(monkeypatch):
    trace = []

    async def persist(name, request):
        trace.append(("commit", threading.get_ident()))
        event = request["entries"][0]["row"]
        return {
            "count": 1,
            "failures": [],
            "notifications": [
                {key: event.get(key) for key in ("execution_id", "account_id", "event_type", "symbol", "seq")}
            ],
        }

    monkeypatch.setattr(audit_batch, "native_operation", persist)
    monkeypatch.setattr(audit_batch, "_notify_live_hub", lambda **_: trace.append(("notify", threading.get_ident())))

    async def check():
        batch = audit_batch.ExecutionAuditBatch(max_entries=3)
        batch.add_event(_event())
        assert await asyncio.to_thread(audit_batch.append_execution_audit_batch_sync, batch) == 1
        assert trace[0][0] == "commit" and trace[0][1] != threading.get_ident()
        assert trace[1] == ("notify", trace[0][1])

    asyncio.run(check())


def test_sync_batch_soft_fails_bridge_error(monkeypatch):
    def fail(_factory):
        raise TimeoutError("synthetic deadline")

    monkeypatch.setattr(audit_batch, "_run_coroutine_sync", fail)
    batch = audit_batch.ExecutionAuditBatch(max_entries=3)
    batch.add_event(_event())
    assert audit_batch.append_execution_audit_batch_sync(batch) == 0
