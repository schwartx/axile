"""启动恢复：中断持久化阶段和内存队列编排。"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionKind, ExecutionTaskStatus
from axile.server.execution import dispatcher as dispatcher_module
from axile.server.execution import intents as intents_module
from axile.server.execution import records as records_module
from axile.server.execution import registry
from axile.server.execution.intents import PROCESS_INTERRUPTED_REASON, IntentSnapshot
from tests.unit.server._execution_test_support import build_account


@pytest.fixture(autouse=True)
def _no_historical_repair(monkeypatch):
    async def missing_records():
        return []

    monkeypatch.setattr(intents_module, "list_interrupted_intents_without_record", missing_records)


def _intent(execution_id: str, status: ExecutionTaskStatus) -> IntentSnapshot:
    return IntentSnapshot(
        execution_id=execution_id,
        account_id=3,
        kind=ExecutionKind.REBALANCE,
        trigger_source="scheduler",
        status=status,
        channel=TradeChannel.CTP,
        algorithm="SINGLE-MAKER",
        payload={},
        created_at="2026-10-23T10:00:00",
        started_at=None,
        finished_at=None,
        error=None,
        cancel_requested_at=None,
        cancel_reason=None,
        terminate_mode=None,
    )


@pytest.mark.parametrize("interrupted_status", [ExecutionTaskStatus.RUNNING, ExecutionTaskStatus.TERMINATING])
def test_recover_interrupts_running_and_restores_queued(monkeypatch, interrupted_status) -> None:
    """中断票随错误记录一起保存，再恢复存在账户的排队票并唤醒。"""
    interrupted = _intent("run-1", interrupted_status)
    queued = _intent("queue-1", ExecutionTaskStatus.QUEUED)
    saved: dict[str, IntentSnapshot] = {item.execution_id: item for item in (interrupted, queued)}
    calls: list[tuple[str, object]] = []
    account = build_account(id=3).snapshot()

    async def list_intents(*statuses: ExecutionTaskStatus):
        return [item for item in saved.values() if item.status in statuses]

    async def finish(execution_id: str, status: ExecutionTaskStatus, **kwargs: object):
        calls.append(("finish", execution_id))
        saved[execution_id] = replace(saved[execution_id], status=status, error=kwargs.get("error"))

    async def append(**kwargs: object):
        assert saved[kwargs["execution_id"]].status == interrupted_status
        assert kwargs["msg"] == PROCESS_INTERRUPTED_REASON
        saved[kwargs["execution_id"]] = replace(interrupted, status=ExecutionTaskStatus.FAILED)
        calls.append(("record", kwargs["execution_id"]))

    async def load(account_id: int):
        assert account_id == 3
        return account

    monkeypatch.setattr(intents_module, "list_intents_by_status", list_intents)
    monkeypatch.setattr(dispatcher_module, "mark_intent_finished", finish)
    monkeypatch.setattr(records_module, "append_error_execute_record", append)
    monkeypatch.setattr(dispatcher_module, "get_account_snapshot", load)
    monkeypatch.setattr(dispatcher_module, "sync_account_live", lambda account_id: calls.append(("live", account_id)))
    monkeypatch.setattr(
        dispatcher_module, "wake_account_dispatcher", lambda account_id: calls.append(("wake", account_id))
    )
    registry.try_register_running_execution(3, interrupted.execution_id)
    try:
        asyncio.run(dispatcher_module.recover_intents_on_startup())
        assert saved["run-1"].status == ExecutionTaskStatus.FAILED
        assert saved["queue-1"].status == ExecutionTaskStatus.QUEUED
        assert registry.get_running_execution_id(3) is None
        assert registry.get_queued_execution_id(3) == "queue-1"
        assert calls == [("record", "run-1"), ("live", 3), ("wake", 3)]
    finally:
        registry.clear_running_execution(3, interrupted.execution_id)
        registry.clear_queued_execution(3, queued.execution_id)
        registry._clear_execution_task_state(queued.execution_id)


def test_recovery_fails_queued_intent_for_deleted_account(monkeypatch) -> None:
    queued = _intent("deleted-account", ExecutionTaskStatus.QUEUED)
    finished: list[tuple[object, ...]] = []

    async def list_intents(*statuses: ExecutionTaskStatus):
        return [queued] if ExecutionTaskStatus.QUEUED in statuses else []

    async def load(_account_id: int):
        return None

    async def finish(execution_id: str, status: ExecutionTaskStatus, **kwargs: object):
        finished.append((execution_id, status, kwargs))

    monkeypatch.setattr(intents_module, "list_intents_by_status", list_intents)
    monkeypatch.setattr(dispatcher_module, "get_account_snapshot", load)
    monkeypatch.setattr(dispatcher_module, "mark_intent_finished", finish)
    monkeypatch.setattr(
        dispatcher_module, "wake_account_dispatcher", lambda _: pytest.fail("已删除账户不能启动 dispatcher")
    )
    asyncio.run(dispatcher_module.recover_intents_on_startup())
    assert finished == [(queued.execution_id, ExecutionTaskStatus.FAILED, {"error": "账户不存在"})]


def test_recovery_repairs_interrupted_record_without_finishing_intent_twice(monkeypatch):
    interrupted = replace(_intent("repair-only", ExecutionTaskStatus.FAILED), error=PROCESS_INTERRUPTED_REASON)
    appended: list[str] = []

    async def list_intents(*_statuses):
        return []

    async def repairs():
        return [interrupted]

    async def append(**kwargs):
        appended.append(kwargs["execution_id"])

    async def unexpected_finish(*_args, **_kwargs):
        pytest.fail("补写终态不得重新结算 intent 或通知")

    monkeypatch.setattr(intents_module, "list_intents_by_status", list_intents)
    monkeypatch.setattr(intents_module, "list_interrupted_intents_without_record", repairs)
    monkeypatch.setattr(records_module, "append_error_execute_record", append)
    monkeypatch.setattr(dispatcher_module, "mark_intent_finished", unexpected_finish)
    registry.try_register_running_execution(3, interrupted.execution_id)
    try:
        asyncio.run(dispatcher_module.recover_intents_on_startup())
        assert appended == [interrupted.execution_id]
        assert registry.get_running_execution_id(3) is None
    finally:
        registry.clear_running_execution(3, interrupted.execution_id)
