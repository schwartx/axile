"""真实原生数据库上的版本并发、取消与恢复回归。"""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from axile.server.db.models.account import AccountSnapshot
from axile.server.execution import account_runtime_sync as runtime
from axile.server.native_store import native_operation
from tests.unit.server._runtime_db_support import runtime_database, runtime_rows, runtime_sync


@pytest.mark.parametrize("old_fails", [False, True, "pending"])
@pytest.mark.parametrize("wal", [False, True])
def test_old_result_cannot_overwrite_new_revision(tmp_path, monkeypatch, old_fails, wal):
    """旧成功和旧失败都只审计旧版本，随后使用最新账户快照继续对齐。"""

    async def scenario():
        async with runtime_database(tmp_path / "runtime.db", wal=wal) as path:
            entered, release = asyncio.Event(), asyncio.Event()
            observed = []
            finish = runtime._finish_attempt

            async def observe_finish(*args, **kwargs):
                result = await finish(*args, **kwargs)
                if len(observed) == 1:
                    assert result.revision == 2 and result.status == "pending"
                    assert result.last_error is None and result.reset_worker is True
                return result

            monkeypatch.setattr(runtime, "_finish_attempt", observe_finish)

            async def prepare(account, *, reset, last_execution=""):
                assert isinstance(account, AccountSnapshot)
                # 独立连接可以提交，运行态等待期间不能留原生事务。
                runtime_rows(path, "UPDATE account SET remark='worker wrote' WHERE id=2")
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
                    current = runtime_sync(path)
                    assert current.status == "pending" and current.last_error is None

            monkeypatch.setattr(runtime, "reconcile_china_channel_account", prepare)
            task = asyncio.create_task(runtime.reconcile_account_runtime(1, MagicMock()))
            await asyncio.wait_for(entered.wait(), 5)
            # 配置和新 revision 由账户业务操作一次提交。
            await native_operation(
                "account_update",
                {
                    "account_id": 1,
                    "now": runtime.now_str(),
                    "patch": {"name": "latest", "account_config": {"password": "changed"}},
                },
            )
            release.set()
            sync = await asyncio.wait_for(task, 5)
            assert observed == [("ctp-sim", False), ("latest", True)]
            assert (sync.revision, sync.attempts, sync.status, sync.reset_worker) == (2, 2, "synchronized", False)
            attempts = runtime_rows(path, "SELECT revision,succeeded FROM accountruntimesyncattempt ORDER BY id")
            assert [tuple(row) for row in attempts] == [(1, not old_fails), (2, True)]

    asyncio.run(scenario())


def test_failure_and_cancellation_release_lock_and_retry_converges(tmp_path, monkeypatch):
    """失败不回滚配置；取消也有持久尝试，pending 可由原入口重试。"""

    async def scenario():
        async with runtime_database(tmp_path / "runtime.db") as path:
            entered = asyncio.Event()
            mode = "fail"

            async def prepare(*_args, **_kwargs):
                if mode == "fail":
                    raise TimeoutError("worker timeout")
                if mode == "cancel":
                    entered.set()
                    await asyncio.Event().wait()

            monkeypatch.setattr(runtime, "reconcile_china_channel_account", prepare)
            failed = await runtime.reconcile_account_runtime(1, MagicMock())
            assert failed.status == "failed"
            assert "worker timeout" in failed.last_error and "Traceback" in failed.last_error
            mode = "cancel"
            task = asyncio.create_task(runtime.reconcile_account_runtime(1, MagicMock()))
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert runtime_sync(path).status == "pending"
            assert runtime_rows(path, "SELECT id FROM account WHERE id=1")
            attempts = runtime_rows(path, "SELECT succeeded FROM accountruntimesyncattempt ORDER BY id")
            assert [row[0] for row in attempts] == [False, False]
            mode = "success"
            recovered = await asyncio.wait_for(runtime.reconcile_account_runtime(1, MagicMock()), 5)
            assert (recovered.status, recovered.attempts) == ("synchronized", 3)

    asyncio.run(scenario())


def test_same_account_serializes_without_blocking_other_accounts(tmp_path, monkeypatch):
    """重复重试排队，其他账户仍可完成独立准备与提交。"""

    async def scenario():
        async with runtime_database(tmp_path / "runtime.db", wal=True):
            entered, release = asyncio.Event(), asyncio.Event()
            calls = []

            async def prepare(account, **_kwargs):
                calls.append(account.id)
                if calls == [1]:
                    entered.set()
                    await release.wait()

            monkeypatch.setattr(runtime, "reconcile_china_channel_account", prepare)
            first = asyncio.create_task(runtime.reconcile_account_runtime(1, MagicMock()))
            await asyncio.wait_for(entered.wait(), 5)
            duplicate = asyncio.create_task(runtime.reconcile_account_runtime(1, MagicMock()))
            other = await asyncio.wait_for(runtime.reconcile_account_runtime(2, MagicMock()), 5)
            assert other.status == "synchronized" and calls == [1, 2]
            release.set()
            await asyncio.wait_for(asyncio.gather(first, duplicate), 5)
            assert calls == [1, 2, 1]

    asyncio.run(scenario())


def test_enqueue_is_atomic_and_preserves_reset_request(tmp_path):
    """独立连接同时登记目标不丢版本、reset 或创建幂等键。"""

    async def scenario():
        async with runtime_database(tmp_path / "runtime.db", wal=True) as path:
            await native_operation(
                "runtime_sync_enqueue",
                {"account_id": 1, "reset_worker": True, "create_request_key": "create-1", "now": "2026-10-01"},
            )
            await asyncio.gather(
                *(
                    native_operation(
                        "runtime_sync_enqueue", {"account_id": 1, "reset_worker": False, "now": "2026-10-01"}
                    )
                    for _ in range(4)
                )
            )
            sync = runtime_sync(path)
            assert (sync.revision, sync.reset_worker, sync.create_request_key) == (5, True, "create-1")

    asyncio.run(scenario())


def test_startup_retries_synchronized_accounts_and_isolates_failure(tmp_path, monkeypatch):
    """重启重建内存态，即使上次成功也准备；单账户失败不影响后续提交。"""

    async def scenario():
        async with runtime_database(tmp_path / "runtime.db") as path:
            await native_operation(
                "runtime_sync_enqueue", {"account_id": 1, "reset_worker": False, "now": "2026-10-01"}
            )
            runtime_rows(path, "UPDATE accountruntimesync SET status='synchronized' WHERE account_id=1")
            calls = []

            async def prepare(account, **_kwargs):
                calls.append(account.id)
                if account.id == 1:
                    raise RuntimeError("offline")

            monkeypatch.setattr(runtime, "reconcile_china_channel_account", prepare)
            await runtime.recover_account_runtime_on_startup(MagicMock())
            assert sorted(calls) == [1, 2]
            rows = runtime_rows(path, "SELECT status FROM accountruntimesync ORDER BY account_id")
            assert [row[0] for row in rows] == ["failed", "synchronized"]

    asyncio.run(scenario())


@pytest.mark.parametrize("next_result", ["success", "pending", "failed"])
def test_uninitialized_waits_and_session_prepare_updates_sync(tmp_path, monkeypatch, next_result):
    from loguru import logger

    from axile.server.execution import ctp_channels
    from axile.server.execution.worker_backend.manager import WorkerBackendExecutionError

    reason = "交易柜台尚未初始化，账户通道暂未就绪"
    records = []

    async def scenario():
        async with runtime_database(tmp_path / "runtime.db") as path:
            mode = "pending"
            resets = []

            async def prepare(account, *, reset, last_execution=""):
                runtime_rows(path, "UPDATE account SET remark='worker wrote' WHERE id=2")
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
            await native_operation("runtime_sync_enqueue", {"account_id": 1, "reset_worker": True, "now": "2026-10-01"})
            sync = await runtime.reconcile_account_runtime(1, MagicMock())
            assert (sync.status, sync.reset_worker) == ("pending", True)
            assert reason in sync.last_error and "Traceback" in sync.last_error
            assert sync.synchronized_at is None
            warnings = [record for record in records if record["level"].name == "WARNING"]
            assert len(warnings) == 1 and warnings[0]["exception"] is None
            assert not any(record["level"].name in {"ERROR", "CRITICAL"} for record in records)
            mode = next_result
            await asyncio.wait_for(ctp_channels._prepare_accounts([1], "day"), 5)
            sync = runtime_sync(path)
            attempts = runtime_rows(path, "SELECT succeeded,error FROM accountruntimesyncattempt ORDER BY id")
            assert sync.status == {"success": "synchronized", "pending": "pending", "failed": "failed"}[mode]
            if mode == "success":
                assert sync.last_error is None
            else:
                assert {"pending": reason, "failed": "CTP 登录校验失败"}[mode] in sync.last_error
                assert "Traceback" in sync.last_error
            assert sync.reset_worker is (mode != "success")
            assert bool(sync.synchronized_at) is (mode == "success")
            assert [row[0] for row in attempts] == [False, mode == "success"]
            assert reason in attempts[0][1] and resets == [True, True]

    sink = logger.add(lambda message: records.append(message.record))
    try:
        asyncio.run(scenario())
    finally:
        logger.remove(sink)


@pytest.mark.parametrize("boundary", ["scheduler", "other_channel", "unknown", "retryable"])
def test_only_explicit_ctp_prepare_error_is_pending(tmp_path, monkeypatch, boundary):
    from axile.server.execution.worker_backend.manager import WorkerBackendExecutionError

    async def scenario():
        async with runtime_database(tmp_path / "runtime.db") as path:
            if boundary == "other_channel":
                runtime_rows(path, "UPDATE account SET trade_channel='tq' WHERE id=1")

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
            sync = await runtime.reconcile_account_runtime(1, MagicMock())
            assert sync.status == "failed"
            assert "账户通道准备未完成" in sync.last_error and "SDK" in sync.last_error

    asyncio.run(scenario())


@pytest.mark.parametrize("wal", [False, True])
def test_slow_startup_account_does_not_block_another_account(tmp_path, monkeypatch, wal):
    """首个账户卡在柜台时，其他账户仍独立恢复并提交 synchronized 状态。"""

    async def scenario():
        async with runtime_database(tmp_path / "runtime.db", wal=wal) as path:
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
            recovery = asyncio.create_task(runtime.recover_account_runtime_on_startup(MagicMock()))
            try:
                await asyncio.wait_for(slow_started.wait(), 2)
                await asyncio.wait_for(healthy_finished.wait(), 2)
                assert not recovery.done()
                rows = runtime_rows(path, "SELECT account_id,status FROM accountruntimesync")
                assert dict(rows) == {1: "pending", 2: "synchronized"}
            finally:
                release_slow.set()
                await asyncio.wait_for(recovery, 2)
            assert dict(runtime_rows(path, "SELECT account_id,status FROM accountruntimesync")) == {
                1: "failed",
                2: "synchronized",
            }

    asyncio.run(scenario())


def test_runtime_target_keeps_latest_execution_time_for_catalog_preference(tmp_path: Path):
    async def scenario():
        async with runtime_database(tmp_path / "runtime.db") as path:
            for succeeded, at in [(1, "2026-10-13 20:00:00"), (0, "2026-10-13 21:00:00")]:
                runtime_rows(
                    path, "INSERT INTO executerecord(account_id,is_success,created_at) VALUES (1,?,?)", (succeeded, at)
                )
            target = await runtime._begin_attempt(1)
            assert target.last_execution == "2026-10-13 21:00:00"
            assert runtime_rows(path, "SELECT id FROM accountruntimesyncattempt WHERE id=?", (target.attempt_id,))

    asyncio.run(scenario())
