"""执行审计持久化与同步封装器的测试。"""

import asyncio

import pytest

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import (
    ExecutionArtifactType,
    ExecutionEventStatus,
    ExecutionEventType,
    ExecutionReasonFamily,
)
from axile.server import execution_audit


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
        assert row["artifact_type"] == "execution_summary"
        assert row["content"] == {"summary": {"filled": 1}}
        return inserted

    monkeypatch.setattr(execution_audit, "native_operation", persist)
    assert (
        asyncio.run(
            execution_audit.append_execution_artifact(
                execution_id="exec-1",
                artifact_type=ExecutionArtifactType.EXECUTION_SUMMARY,
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


def test_run_coroutine_sync_times_out_with_running_loop() -> None:
    """有 loop 分支：从运行中的事件循环同步调用，挂起协程也应在超时内抛 TimeoutError。"""

    async def _hang() -> None:
        await asyncio.sleep(5)

    async def _driver() -> None:
        # 在运行中的 loop 内调用，触发临时线程分支。
        execution_audit._run_coroutine_sync(lambda: _hang(), timeout=0.1)

    with pytest.raises(TimeoutError):
        asyncio.run(_driver())


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
        artifact_type=ExecutionArtifactType.EXECUTION_SUMMARY,
        content={"summary": {"filled": 1}},
    )

    assert result is False
