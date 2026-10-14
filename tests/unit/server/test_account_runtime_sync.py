"""真实数据库上的版本并发、取消与恢复回归。"""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlmodel import select

from axile.server.db.models import Account, AccountRuntimeSync
from axile.server.db.models.account_runtime_sync import AccountRuntimeSyncAttempt
from axile.server.execution import account_runtime_sync as runtime
from tests.unit.server._runtime_db_support import runtime_database


@pytest.mark.parametrize("old_fails", [False, True, "pending"])
@pytest.mark.parametrize("wal", [False, True])
def test_old_result_cannot_overwrite_new_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old_fails: bool | str, wal: bool
) -> None:
    """旧成功和旧失败都只审计旧版本，随后使用最新账户快照继续对齐。"""

    async def scenario() -> None:
        async with runtime_database(tmp_path / "runtime.db", wal=wal) as (factory, sessions):
            entered, release = asyncio.Event(), asyncio.Event()
            observed: list[tuple[str, bool]] = []
            finish = runtime._finish_attempt

            async def observe_finish(*args, **kwargs):
                result = await finish(*args, **kwargs)
                if len(observed) == 1:
                    assert result.revision == 2 and result.status == "pending"
                    assert result.last_error is None and result.reset_worker is True
                return result

            monkeypatch.setattr(runtime, "_finish_attempt", observe_finish)

            async def prepare(account: Account, *, reset: bool, last_execution: str = "") -> None:
                assert all(not session.in_transaction() for session in sessions)
                observed.append((account.name, reset))
                if len(observed) == 1:
                    entered.set()
                    await release.wait()
                    if old_fails == "pending":
                        from axile.server.execution.worker_backend.manager import WorkerBackendExecutionError

                        raise WorkerBackendExecutionError(
                            "未初始化",
                            execution_error="交易柜台尚未初始化，账户通道暂未就绪",
                            reason_code="CTP_NOT_INITIALIZED",
                            error_id=7,
                        )
                    if old_fails:
                        raise RuntimeError("old prepare failed")
                else:
                    async with factory() as session:
                        sync = await session.scalar(
                            select(AccountRuntimeSync).where(AccountRuntimeSync.account_id == 1)
                        )
                        assert sync is not None and sync.status == "pending" and sync.last_error is None

            monkeypatch.setattr(runtime, "reconcile_china_channel_account", prepare)
            task = asyncio.create_task(runtime.reconcile_account_runtime(1, MagicMock(), session_factory=factory))
            await asyncio.wait_for(entered.wait(), 5)
            async with factory() as session, session.begin():
                account = await session.get(Account, 1)
                assert account is not None
                account.name = "latest"
                await runtime.enqueue_account_runtime_sync(session, 1, reset_worker=True)
            release.set()
            sync = await asyncio.wait_for(task, 5)
            assert observed == [("ctp-sim", False), ("latest", True)]
            assert (sync.revision, sync.attempts, sync.status, sync.reset_worker) == (2, 2, "synchronized", False)
            async with factory() as session:
                attempts = list(
                    (
                        await session.scalars(select(AccountRuntimeSyncAttempt).order_by(AccountRuntimeSyncAttempt.id))
                    ).all()
                )
            assert [(row.revision, row.succeeded) for row in attempts] == [(1, not old_fails), (2, True)]

    asyncio.run(scenario())


def test_failure_and_cancellation_release_lock_and_retry_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败不回滚配置，取消保留 pending；两者都能通过原有手动入口重试。"""

    async def scenario() -> None:
        async with runtime_database(tmp_path / "runtime.db") as (factory, _sessions):
            entered = asyncio.Event()
            mode = "fail"

            async def prepare(*_args: object, **_kwargs: object) -> None:
                if mode == "fail":
                    raise TimeoutError("worker timeout")
                if mode == "cancel":
                    entered.set()
                    await asyncio.Event().wait()

            monkeypatch.setattr(runtime, "reconcile_china_channel_account", prepare)
            failed = await runtime.reconcile_account_runtime(1, MagicMock(), session_factory=factory)
            assert failed.status == "failed"
            assert "worker timeout" in failed.last_error
            assert "Traceback" in failed.last_error
            mode = "cancel"
            task = asyncio.create_task(runtime.reconcile_account_runtime(1, MagicMock(), session_factory=factory))
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            async with factory() as session:
                pending = await session.scalar(select(AccountRuntimeSync).where(AccountRuntimeSync.account_id == 1))
                assert pending is not None and pending.status == "pending"
                assert await session.get(Account, 1) is not None
            mode = "success"
            recovered = await asyncio.wait_for(
                runtime.reconcile_account_runtime(1, MagicMock(), session_factory=factory), 5
            )
            assert (recovered.status, recovered.attempts) == ("synchronized", 3)

    asyncio.run(scenario())


def test_same_account_serializes_without_blocking_other_accounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重复重试排队，但其他账户仍能完成自己的数据库写入和准备。"""

    async def scenario() -> None:
        async with runtime_database(tmp_path / "runtime.db", wal=True) as (factory, _sessions):
            entered, release = asyncio.Event(), asyncio.Event()
            calls: list[int | None] = []

            async def prepare(account: Account, **_kwargs: object) -> None:
                calls.append(account.id)
                if calls == [1]:
                    entered.set()
                    await release.wait()

            monkeypatch.setattr(runtime, "reconcile_china_channel_account", prepare)
            first = asyncio.create_task(runtime.reconcile_account_runtime(1, MagicMock(), session_factory=factory))
            await asyncio.wait_for(entered.wait(), 5)
            duplicate = asyncio.create_task(runtime.reconcile_account_runtime(1, MagicMock(), session_factory=factory))
            other = await asyncio.wait_for(
                runtime.reconcile_account_runtime(2, MagicMock(), session_factory=factory), 5
            )
            assert other.status == "synchronized" and calls == [1, 2]
            release.set()
            await asyncio.wait_for(asyncio.gather(first, duplicate), 5)
            assert calls == [1, 2, 1]

    asyncio.run(scenario())


def test_enqueue_is_atomic_and_preserves_reset_request(tmp_path: Path) -> None:
    """独立连接同时登记目标不丢版本、不清除待处理 reset 或创建幂等键。"""

    async def scenario() -> None:
        async with runtime_database(tmp_path / "runtime.db", wal=True) as (factory, _sessions):
            async with factory() as session, session.begin():
                await runtime.enqueue_account_runtime_sync(session, 1, reset_worker=True, create_request_key="create-1")

            async def enqueue() -> None:
                async with factory() as session, session.begin():
                    await runtime.enqueue_account_runtime_sync(session, 1, reset_worker=False)

            await asyncio.gather(*(enqueue() for _ in range(4)))
            async with factory() as session:
                sync = await session.scalar(select(AccountRuntimeSync).where(AccountRuntimeSync.account_id == 1))
                assert sync is not None
                assert (sync.revision, sync.reset_worker, sync.create_request_key) == (5, True, "create-1")

    asyncio.run(scenario())


def test_startup_retries_synchronized_accounts_and_isolates_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重启重建内存态，即使上次成功也准备；首个账户失败不污染后续连接。"""

    async def scenario() -> None:
        async with runtime_database(tmp_path / "runtime.db") as (factory, _sessions):
            async with factory() as session, session.begin():
                session.add(AccountRuntimeSync(account_id=1, status="synchronized"))
            calls: list[int | None] = []

            async def prepare(account: Account, **_kwargs: object) -> None:
                calls.append(account.id)
                if account.id == 1:
                    raise RuntimeError("offline")

            monkeypatch.setattr(runtime, "reconcile_china_channel_account", prepare)
            await runtime.recover_account_runtime_on_startup(MagicMock(), session_factory=factory)
            assert sorted(calls) == [1, 2]
            async with factory() as session:
                rows = list(
                    (await session.scalars(select(AccountRuntimeSync).order_by(AccountRuntimeSync.account_id))).all()
                )
                assert [row.status for row in rows] == ["failed", "synchronized"]

    asyncio.run(scenario())


@pytest.mark.parametrize("next_result", ["success", "pending", "failed"])
def test_uninitialized_waits_and_session_prepare_updates_sync(tmp_path, monkeypatch, next_result):
    from loguru import logger

    from axile.server.execution import ctp_channels
    from axile.server.execution.worker_backend.manager import WorkerBackendExecutionError

    reason = "交易柜台尚未初始化，账户通道暂未就绪"
    records = []

    async def scenario():
        async with runtime_database(tmp_path / "runtime.db") as (factory, sessions):
            monkeypatch.setattr(runtime, "SessionLocal", factory)
            mode = "pending"
            resets = []

            async def prepare(account, *, reset, last_execution=""):
                assert all(not session.in_transaction() for session in sessions)
                resets.append(reset)
                if mode == "pending":
                    raise WorkerBackendExecutionError(
                        reason,
                        execution_error=reason,
                        reason_code="CTP_NOT_INITIALIZED",
                        error_id=7,
                    )
                if mode == "failed":
                    raise WorkerBackendExecutionError("technical", execution_error="CTP 登录校验失败")

            monkeypatch.setattr(runtime, "reconcile_china_channel_account", prepare)
            async with factory() as session, session.begin():
                await runtime.enqueue_account_runtime_sync(session, 1, reset_worker=True)
                account = await session.get(Account, 1)
            sync = await runtime.reconcile_account_runtime(1, MagicMock(), session_factory=factory)
            assert (sync.status, sync.reset_worker) == ("pending", True)
            assert reason in sync.last_error
            assert "Traceback" in sync.last_error
            assert sync.synchronized_at is None
            warnings = [record for record in records if record["level"].name == "WARNING"]
            assert len(warnings) == 1 and warnings[0]["exception"] is None
            assert not any(record["level"].name in {"ERROR", "CRITICAL"} for record in records)
            mode = next_result
            await asyncio.wait_for(ctp_channels._prepare_accounts([account], "day"), 5)
            async with factory() as session:
                sync = await session.scalar(select(AccountRuntimeSync).where(AccountRuntimeSync.account_id == 1))
                attempts = list((await session.scalars(select(AccountRuntimeSyncAttempt))).all())
            assert sync.status == {"success": "synchronized", "pending": "pending", "failed": "failed"}[mode]
            if mode == "success":
                assert sync.last_error is None
            else:
                assert {"pending": reason, "failed": "CTP 登录校验失败"}[mode] in sync.last_error
                assert "Traceback" in sync.last_error
            assert sync.reset_worker is (mode != "success")
            assert bool(sync.synchronized_at) is (mode == "success")
            assert [attempt.succeeded for attempt in attempts] == [False, mode == "success"]
            assert reason in attempts[0].error
            assert resets == [True, True]

    sink = logger.add(lambda message: records.append(message.record))
    try:
        asyncio.run(scenario())
    finally:
        logger.remove(sink)


@pytest.mark.parametrize("boundary", ["scheduler", "other_channel", "unknown", "retryable"])
def test_only_explicit_ctp_prepare_error_is_pending(tmp_path, monkeypatch, boundary):
    from axile.common.trade_channel import TradeChannel
    from axile.server.execution.worker_backend.manager import WorkerBackendExecutionError

    async def scenario():
        async with runtime_database(tmp_path / "runtime.db") as (factory, _sessions):
            if boundary == "other_channel":
                async with factory() as session, session.begin():
                    account = await session.get(Account, 1)
                    account.trade_channel = TradeChannel.TQ

            async def fail(*args, **kwargs):
                raise WorkerBackendExecutionError(
                    "private SDK detail",
                    execution_error="账户通道准备未完成",
                    reason_code="CTP_NOT_INITIALIZED" if boundary in {"scheduler", "other_channel"} else None,
                    retryable=boundary == "retryable",
                )

            monkeypatch.setattr(
                runtime, "_apply_account_job" if boundary == "scheduler" else "reconcile_china_channel_account", fail
            )
            sync = await runtime.reconcile_account_runtime(1, MagicMock(), session_factory=factory)
            assert sync.status == "failed"
            assert "账户通道准备未完成" in sync.last_error
            assert "SDK" in sync.last_error

    asyncio.run(scenario())


@pytest.mark.parametrize("wal", [False, True])
def test_slow_startup_account_does_not_block_another_account(tmp_path, monkeypatch, wal):
    """首个账户卡在柜台时，其他账户仍独立恢复并提交 synchronized 状态。"""

    async def scenario():
        async with runtime_database(tmp_path / "runtime.db", wal=wal) as (factory, _sessions):
            slow_started, release_slow, healthy_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
            original_finish = runtime._finish_attempt

            async def prepare(account, **_kwargs):
                if account.id == 1:
                    slow_started.set()
                    await release_slow.wait()
                    raise RuntimeError("柜台不可用")

            async def finish(account_id, *args, **kwargs):
                sync = await original_finish(account_id, *args, **kwargs)
                if account_id == 2:
                    healthy_finished.set()
                return sync

            monkeypatch.setattr(runtime, "reconcile_china_channel_account", prepare)
            monkeypatch.setattr(runtime, "_finish_attempt", finish)
            recovery = asyncio.create_task(
                runtime.recover_account_runtime_on_startup(MagicMock(), session_factory=factory)
            )
            try:
                await asyncio.wait_for(slow_started.wait(), 2)
                await asyncio.wait_for(healthy_finished.wait(), 2)
                assert not recovery.done()
                async with factory() as session:
                    states = {
                        row.account_id: row.status for row in (await session.scalars(select(AccountRuntimeSync))).all()
                    }
                assert states == {1: "pending", 2: "synchronized"}
            finally:
                release_slow.set()
                await asyncio.wait_for(recovery, 2)
            async with factory() as session:
                states = {
                    row.account_id: row.status for row in (await session.scalars(select(AccountRuntimeSync))).all()
                }
            assert states == {1: "failed", 2: "synchronized"}

    asyncio.run(scenario())


def test_runtime_target_keeps_latest_execution_time_for_catalog_preference(tmp_path):
    from axile.server.db.models.execution import ExecuteRecord

    async def scenario():
        async with runtime_database(tmp_path / "runtime.db") as (factory, _sessions):
            async with factory() as session, session.begin():
                session.add(ExecuteRecord(account_id=1, is_success=1, created_at="2026-10-13 20:00:00"))
                session.add(ExecuteRecord(account_id=1, is_success=0, created_at="2026-10-13 21:00:00"))
            target = await runtime._begin_attempt(1, factory)
            assert target.last_execution == "2026-10-13 21:00:00"

    asyncio.run(scenario())
