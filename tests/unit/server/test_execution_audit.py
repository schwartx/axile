"""执行审计持久化与同步封装器的测试。"""

import asyncio
from types import TracebackType

import pytest
from sqlalchemy.exc import IntegrityError

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import (
    ExecutionArtifactType,
    ExecutionEventStatus,
    ExecutionEventType,
    ExecutionReasonFamily,
)
from axile.server import execution_audit


class _FakeSession:
    commit_error: Exception | None
    rollback_called: bool
    commit_called: bool

    def __init__(self, commit_error: Exception | None = None) -> None:
        """构造一个可选提交失败的伪异步会话。"""
        self.commit_error = commit_error
        self.added: list[object] = []
        self.rollback_called = False
        self.commit_called = False

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commit_called = True
        if self.commit_error is not None:
            raise self.commit_error

    async def rollback(self) -> None:
        self.rollback_called = True


def test_append_execution_event_ignores_duplicate_event_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """事务回滚后，应忽略重复的事件 id。"""
    duplicate_error = IntegrityError(
        "INSERT INTO execution_event",
        {},
        Exception("UNIQUE constraint failed: execution_event.event_uid"),
    )
    fake_session = _FakeSession(commit_error=duplicate_error)
    monkeypatch.setattr(execution_audit, "SessionLocal", lambda: fake_session)

    result = asyncio.run(
        execution_audit.append_execution_event(
            execution_id="exec-1",
            account_id=1,
            channel=TradeChannel.CTP,
            algorithm="SINGLE-MAKER",
            event_type=ExecutionEventType.SYMBOL_SKIPPED,
            status=ExecutionEventStatus.WARNING,
            reason_family=ExecutionReasonFamily.MARKET_RULE,
            reason_code="EXTERNAL.BELOW_MIN_NOTIONAL",
            symbol="ag2612",
            seq=2,
        )
    )

    assert result is False
    assert fake_session.rollback_called is True


def test_append_execution_artifact_ignores_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """事务回滚后，应忽略重复的附件 key。"""
    duplicate_error = IntegrityError(
        "INSERT INTO execution_artifact",
        {},
        Exception("UNIQUE constraint failed: execution_artifact.execution_id, execution_artifact.artifact_type"),
    )
    fake_session = _FakeSession(commit_error=duplicate_error)
    monkeypatch.setattr(execution_audit, "SessionLocal", lambda: fake_session)

    result = asyncio.run(
        execution_audit.append_execution_artifact(
            execution_id="exec-1",
            artifact_type=ExecutionArtifactType.EXECUTION_SUMMARY,
            content={"summary": {"filled": 1}},
        )
    )

    assert result is False
    assert fake_session.rollback_called is True


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
