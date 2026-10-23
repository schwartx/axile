"""原生提交失败不能提前改动队列、通知或渠道占位。"""

import asyncio
from types import SimpleNamespace

import pytest

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionKind, ExecutionTaskStatus
from axile.server.execution import intents
from axile.server.execution.registry import (
    clear_queued_execution,
    clear_running_execution,
    get_queued_execution_id,
    get_running_execution_id,
    set_queued_execution,
)


def _snapshot(execution_id: str, account_id: int = 710) -> intents.IntentSnapshot:
    return intents.IntentSnapshot(
        execution_id=execution_id,
        account_id=account_id,
        kind=ExecutionKind.REBALANCE,
        trigger_source="manual",
        status=ExecutionTaskStatus.QUEUED,
        channel=TradeChannel.CTP,
        algorithm="SINGLE-MAKER",
        payload={},
        created_at="2026-10-23T10:00:00+08:00",
        started_at=None,
        finished_at=None,
        error=None,
        cancel_requested_at=None,
        cancel_reason=None,
        terminate_mode=None,
    )


def test_failed_insert_never_publishes_a_memory_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failed_operation(_name, _request):
        raise RuntimeError("commit failed")

    account = SimpleNamespace(id=710, trade_channel=TradeChannel.CTP, algorithm={"method": "SINGLE-MAKER"})
    monkeypatch.setattr(intents, "native_operation", failed_operation)
    try:
        with pytest.raises(RuntimeError, match="commit failed"):
            asyncio.run(
                intents._insert_intent(
                    account=account, kind=ExecutionKind.REBALANCE, trigger_source="manual", payload={}
                )
            )
        assert get_queued_execution_id(710) is None
    finally:
        clear_queued_execution(710, get_queued_execution_id(710) or "")


@pytest.mark.parametrize("conflict", [False, True])
def test_failed_promotion_releases_the_channel_reservation(monkeypatch: pytest.MonkeyPatch, conflict: bool) -> None:
    async def load(_execution_id):
        return _snapshot("failed-promote")

    async def failed_cas(*_args, **_kwargs):
        assert get_running_execution_id(710) == "failed-promote"
        if conflict:
            raise RuntimeError("INTENT_CONFLICT", "running slot occupied")
        raise RuntimeError("commit failed")

    monkeypatch.setattr(intents, "get_intent", load)
    monkeypatch.setattr(intents, "cas_intent_status", failed_cas)
    try:
        with pytest.raises(RuntimeError) as caught:
            asyncio.run(intents._promote_intent_to_running("failed-promote", 710))
        if conflict:
            assert isinstance(caught.value, intents.IntentNotRunnable)
            assert caught.value.retry
        else:
            assert str(caught.value) == "commit failed"
        assert get_running_execution_id(710) is None
    finally:
        clear_running_execution(710, "failed-promote")


@pytest.mark.parametrize("conflict", [False, True])
def test_coalesce_only_clears_a_queue_proven_stale(monkeypatch: pytest.MonkeyPatch, conflict: bool) -> None:
    async def failed_operation(_name, _request):
        if conflict:
            raise RuntimeError("INTENT_CONFLICT", "intent no longer queued")
        raise RuntimeError("commit failed")

    account = SimpleNamespace(id=710, trade_channel=TradeChannel.CTP)
    monkeypatch.setattr(intents, "native_operation", failed_operation)
    set_queued_execution(710, "stale-queue")
    try:
        with pytest.raises(RuntimeError):
            asyncio.run(intents._coalesce("stale-queue", account, {}))
        assert get_queued_execution_id(710) == (None if conflict else "stale-queue")
    finally:
        clear_queued_execution(710, "stale-queue")


@pytest.mark.parametrize("changed", [False, True])
def test_terminal_wakeup_requires_a_committed_change(monkeypatch: pytest.MonkeyPatch, changed: bool) -> None:
    from axile.server.execution import notification_outbox

    calls = []

    async def finish(name, request):
        assert name == "intent_finish"
        assert request["status"] == ExecutionTaskStatus.FAILED
        assert request["finished_at"] == request["now"] == "2026-10-23T10:01:00+08:00"
        return {"changed": changed, "intent": {"account_id": 710}}

    monkeypatch.setattr(intents, "native_operation", finish)
    monkeypatch.setattr(notification_outbox, "wake_notifications", calls.append)
    asyncio.run(
        intents.mark_intent_finished(
            "failed-execution",
            ExecutionTaskStatus.FAILED,
            finished_at="2026-10-23T10:01:00+08:00",
        )
    )
    assert calls == ([710] if changed else [])


def test_submit_rechecks_admission_after_native_unique_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    async def submit(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("INTENT_CONFLICT", "concurrent queued intent")
        return intents.SubmitResult("coalesced", "winner", 710)

    monkeypatch.setattr(intents, "_submit_intent", submit)
    result = asyncio.run(intents.submit_intent(710, ExecutionKind.REBALANCE, "manual"))
    assert result.execution_id == "winner"
    assert len(calls) == 2


@pytest.mark.parametrize("stage", ["insert", "coalesce"])
def test_native_supplement_rejection_keeps_its_domain_error(monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    from axile.server._native import _core
    from axile.server.execution.supplements import SupplementNotRunnable

    native_error = _core.NativeError("SUPPLEMENT_NOT_RUNNABLE", "补发需求已取消或过期")
    payload = {"supplement_step_id": "expired-step"}
    account = SimpleNamespace(id=710, trade_channel=TradeChannel.CTP, algorithm={"method": "SINGLE-MAKER"})

    async def rejected(name, request):
        assert name == f"intent_{stage}"
        assert request["account_id"] == account.id
        assert request["payload"] == payload
        raise native_error

    monkeypatch.setattr(intents, "native_operation", rejected)
    task = (
        intents._insert_intent(
            account=account, kind=ExecutionKind.REBALANCE, trigger_source="scheduler", payload=payload
        )
        if stage == "insert"
        else intents._coalesce("queued-execution", account, payload)
    )
    with pytest.raises(SupplementNotRunnable, match="补发需求已取消或过期") as caught:
        asyncio.run(task)
    assert caught.value.args == ("补发需求已取消或过期",)
    assert caught.value.__cause__ is native_error
    assert native_error.args == ("SUPPLEMENT_NOT_RUNNABLE", "补发需求已取消或过期")
