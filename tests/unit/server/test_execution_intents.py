"""执行意图准入冲突表与 CAS。"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionKind, ExecutionTaskStatus, ExecutionTerminateMode
from axile.server.core import db
from axile.server.db.models import Account
from axile.server.execution import intents as intents_module
from axile.server.execution import registry as execution_registry
from axile.server.execution.intents import (
    IntentSnapshot,
    SubmitResult,
    cas_intent_status,
    get_intent,
    submit_intent,
)
from axile.server.execution.live import live_hub
from axile.server.execution.registry import (
    AccountExecutionAlreadyRunningError,
    clear_queued_execution,
    clear_running_execution,
    get_execution_task_state,
    get_queued_execution_id,
    get_running_execution_id,
    try_register_running_execution,
)
from axile.server.native_store import native_operation


def _account(*, account_id: int = 1) -> Account:
    return Account(
        id=account_id,
        name="intent-test",
        market="期货",
        trade_channel=TradeChannel.CTP,
        account_control_preset="default",
        account_config={"api_key": "k", "secret_key": "s", "is_testnet": True},
        is_started=True,
        cron_expr="*/15 * * * *",
        remark=None,
        brokerage="ctp",
        weight_precision=0.001,
        long_leverage=1.0,
        short_leverage=1.0,
        algorithm={"method": "SINGLE-MAKER"},
        empty_positions_algorithm=None,
        trade_rules={},
        forbidden_symbols=[],
        risk_symbols=[],
        feishu_key=None,
        write_empty_record=0,
    )


@asynccontextmanager
async def _intent_db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    with tempfile.TemporaryDirectory(prefix="axile-native-intent-") as directory:
        database = str(Path(directory) / "intents.sqlite")
        monkeypatch.setattr(db, "database_path", lambda: database)
        monkeypatch.setattr(intents_module, "_wake", lambda _account_id: None)
        await native_operation("migration_upgrade", {"public": "head"})
        created = await native_operation(
            "account_create", {"account": _account().model_dump(mode="json"), "now": "2026-10-23T10:00:00+08:00"}
        )
        assert created["account"]["id"] == 1
        try:
            yield None
        finally:
            clear_queued_execution(1, get_queued_execution_id(1) or "")
            clear_running_execution(1, get_running_execution_id(1) or "")


def test_submit_intent_creates_queued_rebalance(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run() -> tuple[SubmitResult, str | None]:
        async with _intent_db(monkeypatch):
            result = await submit_intent(1, ExecutionKind.REBALANCE, "manual")
            return result, get_queued_execution_id(1)

    result, queued_id = asyncio.run(_run())
    assert result.outcome == "created"
    assert result.execution_id
    assert queued_id == result.execution_id


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("memory_only", [False, True])
def test_submit_intent_coalesces_queued_rebalance(
    monkeypatch: pytest.MonkeyPatch, running: bool, memory_only: bool
) -> None:
    async def _run() -> tuple[SubmitResult, SubmitResult]:
        async with _intent_db(monkeypatch):
            if running:
                active = await submit_intent(1, ExecutionKind.REBALANCE, "manual")
                assert active.execution_id is not None
                await intents_module._promote_intent_to_running(active.execution_id, 1)
            first = await submit_intent(1, ExecutionKind.REBALANCE, "manual")
            if memory_only:
                monkeypatch.setattr(intents_module, "load_active_intents", AsyncMock(return_value=(None, None)))
            second = await submit_intent(1, ExecutionKind.REBALANCE, "scheduler")
            return first, second

    first, second = asyncio.run(_run())
    assert first.outcome == "created"
    assert second.outcome == "coalesced"
    assert second.execution_id == first.execution_id


def test_submit_intent_recovers_from_concurrent_initial_insert(monkeypatch: pytest.MonkeyPatch) -> None:
    original_insert = intents_module._insert_intent
    first_call = True

    async def competing_insert(
        *,
        account: Account,
        kind: ExecutionKind,
        trigger_source: str,
        payload: dict[str, object],
    ) -> IntentSnapshot:
        nonlocal first_call
        if first_call:
            first_call = False
            await original_insert(
                account=account,
                kind=kind,
                trigger_source=trigger_source,
                payload=payload,
            )
            raise RuntimeError("INTENT_CONFLICT", "concurrent queued intent")
        return await original_insert(
            account=account,
            kind=kind,
            trigger_source=trigger_source,
            payload=payload,
        )

    async def _run() -> SubmitResult:
        async with _intent_db(monkeypatch):
            monkeypatch.setattr(intents_module, "_insert_intent", competing_insert)
            return await submit_intent(1, ExecutionKind.REBALANCE, "manual")

    result = asyncio.run(_run())
    assert result.outcome == "coalesced"
    assert result.execution_id is not None


def test_cas_intent_status_rejects_stale_expected_status(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run() -> tuple[bool, bool, ExecutionTaskStatus | None]:
        async with _intent_db(monkeypatch):
            submitted = await submit_intent(1, ExecutionKind.REBALANCE, "manual")
            assert submitted.execution_id is not None
            terminated = await cas_intent_status(
                submitted.execution_id,
                ExecutionTaskStatus.QUEUED,
                ExecutionTaskStatus.TERMINATED,
            )
            stale_promotion = await cas_intent_status(
                submitted.execution_id,
                ExecutionTaskStatus.QUEUED,
                ExecutionTaskStatus.RUNNING,
            )
            intent = await get_intent(submitted.execution_id)
            return terminated, stale_promotion, None if intent is None else intent.status

    terminated, stale_promotion, status = asyncio.run(_run())
    assert terminated is True
    assert stale_promotion is False
    assert status == ExecutionTaskStatus.TERMINATED


def test_submit_intent_clear_replaces_queued_rebalance(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_finalize(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(
        "axile.server.execution.registry._finalize_terminated_queued_execution",
        fake_finalize,
    )

    async def _run() -> tuple[str, SubmitResult, str | None]:
        async with _intent_db(monkeypatch):
            first = await submit_intent(1, ExecutionKind.REBALANCE, "manual")
            assert first.execution_id is not None
            replaced = await submit_intent(1, ExecutionKind.CLEAR_POSITIONS, "empty_positions")
            return first.execution_id, replaced, get_queued_execution_id(1)

    old_id, replaced, queued_id = asyncio.run(_run())
    assert replaced.outcome == "created"
    assert replaced.execution_id != old_id
    assert queued_id == replaced.execution_id


def test_submit_intent_clear_conflicts_when_running(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        try_register_running_execution(1, "exec-running")

        async def _run() -> None:
            async with _intent_db(monkeypatch):
                await submit_intent(1, ExecutionKind.CLEAR_POSITIONS, "empty_positions")

        with pytest.raises(AccountExecutionAlreadyRunningError):
            asyncio.run(_run())
    finally:
        clear_running_execution(1, "exec-running")


def test_submit_intent_rebalance_queues_behind_running(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        try_register_running_execution(1, "exec-running")

        async def _run() -> tuple[SubmitResult, str | None]:
            async with _intent_db(monkeypatch):
                result = await submit_intent(1, ExecutionKind.REBALANCE, "manual")
                return result, get_queued_execution_id(1)

        result, queued_id = asyncio.run(_run())
        assert result.outcome == "created"
        assert queued_id == result.execution_id
    finally:
        clear_running_execution(1, "exec-running")
        queued_id = get_queued_execution_id(1)
        if queued_id is not None:
            clear_queued_execution(1, queued_id)


def test_cron_busy_returns_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        try_register_running_execution(1, "exec-running")
        from axile.server.execution.registry import set_queued_execution

        set_queued_execution(1, "exec-queued")

        async def _run() -> SubmitResult:
            async with _intent_db(monkeypatch):
                return await submit_intent(
                    1,
                    ExecutionKind.REBALANCE,
                    "scheduler",
                    on_conflict="skip",
                )

        result = asyncio.run(_run())
        assert result.outcome == "skipped_busy"
        assert result.execution_id is None
    finally:
        clear_running_execution(1, "exec-running")
        clear_queued_execution(1, "exec-queued")


def test_submit_intent_clear_conflicts_when_queued_rebalance_promoted(monkeypatch: pytest.MonkeyPatch) -> None:
    original_cas = intents_module.cas_intent_status

    async def promote_then_fail(
        execution_id: str,
        expected: ExecutionTaskStatus,
        target: ExecutionTaskStatus,
        **fields: object,
    ) -> bool:
        if target == ExecutionTaskStatus.TERMINATED:
            await original_cas(execution_id, ExecutionTaskStatus.QUEUED, ExecutionTaskStatus.RUNNING)
            return False
        return await original_cas(execution_id, expected, target, **fields)

    async def _run() -> tuple[ExecutionKind | None, ExecutionKind | None]:
        async with _intent_db(monkeypatch):
            first = await submit_intent(1, ExecutionKind.REBALANCE, "manual")
            assert first.execution_id is not None
            monkeypatch.setattr(intents_module, "cas_intent_status", promote_then_fail)
            with pytest.raises(AccountExecutionAlreadyRunningError):
                await submit_intent(1, ExecutionKind.CLEAR_POSITIONS, "empty_positions")
            running, queued = await intents_module.load_active_intents(1)
            return (
                None if running is None else running.kind,
                None if queued is None else queued.kind,
            )

    running_kind, queued_kind = asyncio.run(_run())
    assert running_kind == ExecutionKind.REBALANCE
    assert queued_kind is None


def test_sync_live_clears_hub_when_slots_empty() -> None:
    live_hub.publish_slots(
        account_id=91,
        execution_id="exec-stale-queued",
        kind="rebalance",
        status="queued",
        phase="queued",
        pending_execution_id=None,
        pending_kind=None,
    )
    assert live_hub.progress_for(91) is not None
    intents_module.sync_account_live(91)
    assert live_hub.progress_for(91) is None


def test_dashboard_ignores_terminal_progress_frames() -> None:
    from axile.domain.execution import ExecutionEventType

    live_hub.publish(
        execution_id="exec-done-92",
        account_id=92,
        event_type=ExecutionEventType.EXECUTION_COMPLETED,
        kind="rebalance",
    )
    progress = live_hub.progress_for(92)
    assert progress is not None
    assert progress["status"] == "done"
    live_hub.clear_live_account(92)
    assert live_hub.progress_for(92) is None


def test_promote_missing_intent_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run() -> None:
        async with _intent_db(monkeypatch):
            with pytest.raises(intents_module.IntentNotRunnable, match="不存在"):
                await intents_module.promote_intent_to_running("missing-exec", 1)

    asyncio.run(_run())


def test_terminate_queued_intent_without_memory_returns_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """DB 有 QUEUED 票、内存被拆掉时，终止应落库成功并返回状态，而不是 409。"""

    async def fake_append_execution_event(**_kwargs: object) -> bool:
        return True

    async def fake_append_terminated_execute_record(**_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(id=1, is_success=0)

    monkeypatch.setattr(execution_registry, "append_execution_event", fake_append_execution_event)
    monkeypatch.setattr(
        execution_registry,
        "append_terminated_execute_record",
        fake_append_terminated_execute_record,
    )

    async def _run() -> tuple[ExecutionTaskStatus | None, ExecutionTaskStatus | None]:
        async with _intent_db(monkeypatch):
            submitted = await submit_intent(1, ExecutionKind.REBALANCE, "manual")
            assert submitted.execution_id is not None
            execution_id = submitted.execution_id
            clear_queued_execution(1, execution_id)
            execution_registry._clear_execution_task_state(execution_id)
            assert get_execution_task_state(execution_id) is None
            try:
                state = await execution_registry.terminate_running_account_execution(
                    1,
                    reason="manual stop",
                    mode=ExecutionTerminateMode.GRACEFUL,
                )
                intent = await get_intent(execution_id)
                return (
                    None if state is None else state.status,
                    None if intent is None else intent.status,
                )
            finally:
                execution_registry.clear_running_execution(1, execution_id)
                execution_registry._clear_execution_task_state(execution_id)

    memory_status, db_status = asyncio.run(_run())
    assert memory_status == ExecutionTaskStatus.TERMINATED
    assert db_status == ExecutionTaskStatus.TERMINATED


def test_terminate_terminating_intent_without_memory_sets_cancel_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """落库已是 TERMINATING、内存缺失时，补水合并点亮 cancel_event。"""

    async def fake_append_execution_event(**_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(execution_registry, "append_execution_event", fake_append_execution_event)

    async def _run() -> bool:
        async with _intent_db(monkeypatch):
            submitted = await submit_intent(1, ExecutionKind.REBALANCE, "manual")
            assert submitted.execution_id is not None
            execution_id = submitted.execution_id
            assert await cas_intent_status(
                execution_id,
                ExecutionTaskStatus.QUEUED,
                ExecutionTaskStatus.RUNNING,
            )
            assert await intents_module.persist_intent_termination(
                execution_id,
                expected=ExecutionTaskStatus.RUNNING,
                target=ExecutionTaskStatus.TERMINATING,
                reason="stop",
                mode=ExecutionTerminateMode.GRACEFUL,
            )
            clear_queued_execution(1, execution_id)
            execution_registry._clear_execution_task_state(execution_id)
            try:
                state = await execution_registry.terminate_running_account_execution(
                    1,
                    reason="stop",
                    mode=ExecutionTerminateMode.GRACEFUL,
                )
                return bool(
                    state is not None
                    and state.status == ExecutionTaskStatus.TERMINATING
                    and state.cancel_event is not None
                    and state.cancel_event.is_set()
                )
            finally:
                execution_registry.clear_running_execution(1, execution_id)
                execution_registry._clear_execution_task_state(execution_id)

    assert asyncio.run(_run()) is True
