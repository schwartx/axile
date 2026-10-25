"""分析编排不持有 Python DB 会话；提交确认按本次 claim 而非回执判断。"""

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from axile.server import analysis_calendar
from axile.server import performance_analysis as analysis


@pytest.fixture
def claim():
    return {"account_id": 7, "source_version": 11, "running_token": "claim-token"}


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setattr(analysis, "database_path", lambda: "/isolated/analysis.sqlite")
    return analysis.AnalysisManager()


def test_native_capabilities_require_preparation_and_commit_confirmation(monkeypatch):
    monkeypatch.setattr(analysis._core, "ANALYSIS_FACT_SCHEMA", 40)
    for name in (
        "publish_store",
        "account_public",
        "connection_accounts",
        "execution_read",
        "supplement_has_execution_steps",
        "persist_execution_state",
        "freeze_execution_input",
        "freeze_execution_after_assets",
        "queue_enqueue",
        "queue_status",
        "queue_recover",
        "queue_claim",
        "queue_fail",
        "queue_release",
        "analysis_context",
        "analysis_calendar_save",
        "analysis_publication_committed",
    ):
        monkeypatch.setattr(analysis._core, name, lambda *args: None, raising=False)
    analysis.ensure_native_capabilities()
    for name in ("account_public", "connection_accounts", "execution_read", "supplement_has_execution_steps"):
        with monkeypatch.context() as scoped:
            scoped.delattr(analysis._core, name)
            with pytest.raises(RuntimeError, match=name):
                analysis.ensure_native_capabilities()
    monkeypatch.delattr(analysis._core, "analysis_publication_committed")
    with pytest.raises(RuntimeError, match="analysis_publication_committed"):
        analysis.ensure_native_capabilities()
    monkeypatch.delattr(analysis._core, "ANALYSIS_FACT_SCHEMA")
    with pytest.raises(RuntimeError, match="schema 0034"):
        analysis.ensure_native_capabilities()


@pytest.mark.parametrize("published", [False, True])
def test_native_dispatch_prepares_calendar_before_publication(manager, claim, monkeypatch, published):
    prepared = {
        "trade_channel": "ctp",
        "settings": {"backtest_fee_rate": 0.01},
        "first_day": "2026-01-01",
        "last_day": "2026-01-03",
    }
    operation = analysis.native_operation

    async def dispatch(name, request, **kwargs):
        if name == "analysis_context":
            return prepared
        return await operation(name, request, **kwargs)

    native = AsyncMock(side_effect=dispatch)
    calendar = AsyncMock(return_value=3)
    requests = []

    def publish(path, payload):
        calendar.assert_awaited_once_with("ctp", "2026-01-01", "2026-01-03")
        requests.append((path, json.loads(payload)))
        return {"published": published}

    monkeypatch.setattr(analysis, "native_operation", native)
    monkeypatch.setattr(analysis_calendar, "ensure_calendar_facts", calendar)
    monkeypatch.setattr(analysis._core, "publish_store", publish)
    asyncio.run(manager._run_native(claim))
    assert native.await_args_list[0].args == ("analysis_context", {"account_id": 7})
    assert native.await_args_list[1].args[0] == "publish_store"
    assert native.await_count == 2
    path, request = requests[0]
    assert path == "/isolated/analysis.sqlite"
    assert request["snapshot_id"] == request["claim_token"] == "claim-token"
    assert request["expected_source_version"] == 11
    assert request["settings"] == {"backtest_weight_type": "ts", "backtest_fee_rate": 0.01}


def test_deleted_account_is_not_published(manager, claim, monkeypatch):
    native = AsyncMock(return_value=None)
    calendar = AsyncMock()
    monkeypatch.setattr(analysis, "native_operation", native)
    monkeypatch.setattr(analysis_calendar, "ensure_calendar_facts", calendar)
    monkeypatch.setattr(analysis._core, "publish_store", lambda *args: pytest.fail("account was deleted"))
    asyncio.run(manager._run_native(claim))
    calendar.assert_not_awaited()


def test_confirmation_uses_exact_claim_and_versions(manager, claim, monkeypatch):
    native = AsyncMock(return_value=True)
    monkeypatch.setattr(analysis, "native_operation", native)
    assert asyncio.run(manager._native_publication_committed(claim))
    native.assert_awaited_once_with(
        "analysis_publication_committed",
        {**claim, "logic_version": analysis.LOGIC_VERSION, "engine_version": analysis.ENGINE_VERSION},
    )
    native.reset_mock()
    assert not asyncio.run(manager._native_publication_committed({**claim, "running_token": None}))
    native.assert_not_awaited()


@pytest.mark.parametrize("committed", [False, True])
def test_receipt_loss_only_fails_an_uncommitted_claim(manager, claim, monkeypatch, committed):
    monkeypatch.setattr(manager, "_claim", AsyncMock(return_value=claim))
    monkeypatch.setattr(manager, "_run_native", AsyncMock(side_effect=RuntimeError("receipt lost")))
    monkeypatch.setattr(manager, "_native_publication_committed", AsyncMock(return_value=committed))
    fail = AsyncMock()
    monkeypatch.setattr(manager, "_fail", fail)
    assert asyncio.run(manager.run_once())
    if committed:
        fail.assert_not_awaited()
    else:
        fail.assert_awaited_once_with(claim)


def test_cancelled_calculation_releases_claim_and_propagates(manager, claim, monkeypatch):
    monkeypatch.setattr(manager, "_claim", AsyncMock(return_value=claim))
    monkeypatch.setattr(manager, "_run_native", AsyncMock(side_effect=asyncio.CancelledError()))
    release = AsyncMock()
    monkeypatch.setattr(manager, "_release_claim", release)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(manager.run_once())
    release.assert_awaited_once_with(claim)


@pytest.mark.parametrize("committed", [False, True])
def test_cancelled_publication_holds_lock_and_claim_until_native_thread_finishes(
    manager, claim, monkeypatch, committed
):
    entered, release, finished = (threading.Event() for _ in range(3))
    releases = []
    operation = analysis.native_operation

    async def dispatch(name, request, **kwargs):
        if name == "analysis_context":
            return {"settings": {}, "trade_channel": "ctp", "first_day": "2026-01-01", "last_day": "2026-01-02"}
        if name == "analysis_publication_committed":
            assert finished.is_set()
            return committed
        return await operation(name, request, **kwargs)

    def publish(*_args):
        entered.set()
        assert release.wait(2)
        finished.set()
        return {"published": committed}

    def release_claim(*args):
        assert finished.is_set()
        releases.append(args)

    monkeypatch.setattr(manager, "_claim", AsyncMock(return_value=claim))
    monkeypatch.setattr(analysis, "native_operation", dispatch)
    monkeypatch.setattr(analysis_calendar, "ensure_calendar_facts", AsyncMock())
    monkeypatch.setattr(analysis._core, "publish_store", publish)
    monkeypatch.setattr(analysis._core, "queue_release", release_claim)

    async def run():
        caller = asyncio.create_task(manager.run_once())
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            caller.cancel()
            await asyncio.sleep(0)
            caller.cancel()
            await asyncio.sleep(0)
            assert not caller.done()
            assert manager.compute_lock.locked()
            assert releases == []
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert finished.is_set()
        assert not manager.compute_lock.locked()

    asyncio.run(run())
    assert len(releases) == 1


def test_release_and_fail_delegate_once_without_commit_preread(manager, claim, monkeypatch):
    confirmation = AsyncMock()
    monkeypatch.setattr(manager, "_native_publication_committed", confirmation)
    calls = []
    monkeypatch.setattr(analysis._core, "queue_release", lambda *args: calls.append(("release", args)))
    monkeypatch.setattr(analysis._core, "queue_fail", lambda *args: calls.append(("fail", args)))
    monkeypatch.setattr(analysis, "get_default_clock", lambda: SimpleNamespace(time=lambda: 123.0))

    async def check():
        await manager._release_claim(claim)
        await manager._fail(claim)

    asyncio.run(check())
    confirmation.assert_not_awaited()
    assert calls == [
        ("release", ("/isolated/analysis.sqlite", 7, 11, "claim-token")),
        ("fail", ("/isolated/analysis.sqlite", 7, 11, "claim-token", 123.0)),
    ]


def test_concurrent_runs_keep_one_calculation_slot(manager, claim, monkeypatch):
    monkeypatch.setattr(manager, "_claim", AsyncMock(side_effect=[claim, claim]))
    entered, release = asyncio.Event(), asyncio.Event()
    active = maximum = 0

    async def publish(row):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        entered.set()
        await release.wait()
        active -= 1

    monkeypatch.setattr(manager, "_run_native", publish)

    async def check():
        first = asyncio.create_task(manager.run_once())
        await entered.wait()
        second = asyncio.create_task(manager.run_once())
        await asyncio.sleep(0)
        assert not second.done()
        release.set()
        assert await first and await second

    asyncio.run(check())
    assert maximum == 1
