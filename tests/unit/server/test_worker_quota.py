"""报单确认、真实子进程退出和原生额度补写的回归。"""

import asyncio
from contextlib import closing
from multiprocessing import Pipe
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from axile.executor.account_control.decorators import run_controlled_call
from axile.executor.account_control.quota_state import AccountControlQuotaState
from axile.server.account_control.store import AccountControlStore
from axile.server.execution.worker_backend import manager as manager_module
from axile.server.execution.worker_backend import quota
from tests.sqlite_oracle import connect as oracle_connect
from tests.unit.executor.test_account_control_quota_state import guard as shared_guard
from tests.unit.server._business_facts_test_support import seed_account
from tests.unit.server.test_gm_worker_manager import _FakeHandle


def _quota_worker(connection, account_id, _control, _catalog, _monitor, _orders, quota_connection):
    from axile.server.core import db
    from axile.server.execution.execution_account_control import flush_account_control_records

    path, mode = connection.recv()
    db.database_path = lambda: path
    current = shared_guard(AccountControlQuotaState(), "quota-worker", symbol_limit=10)
    current.counter_mirror = quota.QuotaClient(quota_connection)
    for _ in range(2):
        current.begin_operation("place_order", symbol="IF2601").record_outcome("submitted")
    deltas, events = current.flush_records()
    if mode == "committed":
        adapter = SimpleNamespace(
            export_account_control_records=current.flush_records, get_account_control_guard=lambda: current
        )
        asyncio.run(flush_account_control_records(adapter))
    elif mode == "commit_without_clear":
        asyncio.run(AccountControlStore().flush_execution_records(counter_deltas=deltas, events=events))
    connection.send(deltas)
    connection.recv()


@pytest.fixture
def worker_quota(monkeypatch, native_database):
    seed_account(native_database)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    monkeypatch.setattr(manager_module, "run_worker_backend_loop", _quota_worker)
    manager = manager_module.WorkerBackendManager()
    try:
        yield manager, native_database
    finally:
        manager.close()


def _start(manager, path, mode="pending"):
    handle = manager._get_or_create_worker(1)
    handle.connection.send((str(path), mode))
    assert handle.connection.poll(5), "额度 worker 未就绪"
    return handle, handle.connection.recv()


def _persisted(path):
    with closing(oracle_connect(path)) as connection:
        return dict(
            connection.execute(
                "SELECT delta_uid,delta_count FROM account_control_counter_delta WHERE execution_id='quota-worker'"
            )
        )


@pytest.mark.parametrize("mode", ["pending", "committed", "commit_without_clear"])
def test_real_worker_kill_recovers_latest_counts_without_duplicates(worker_quota, mode):
    manager, path = worker_quota
    handle, deltas = _start(manager, path, mode)
    expected = {delta.delta_uid: delta.delta_count for delta in deltas}
    assert len(expected) == 4 and set(expected.values()) == {2}
    assert _persisted(path) == ({} if mode == "pending" else expected)
    manager._force_drop_worker(1, handle)
    manager._force_drop_worker(1, handle)
    assert _persisted(path) == expected
    assert not manager._retiring_workers


def test_recovery_failure_blocks_replacement_but_not_other_accounts(worker_quota, monkeypatch):
    manager, path = worker_quota
    handle, deltas = _start(manager, path)
    original_flush = quota._flush_quota

    async def fail_write(_deltas):
        assert manager._lock.acquire(blocking=False)
        manager._lock.release()
        raise OSError("database unavailable")

    monkeypatch.setattr(quota, "_flush_quota", fail_write)
    manager._force_drop_worker(1, handle)
    with pytest.raises(manager_module.WorkerBackendExecutionError, match="额度尚未恢复"):
        manager._get_or_create_worker(1)
    assert manager._retiring_workers[1] == [handle]
    spawn = Mock(return_value=_FakeHandle())
    monkeypatch.setattr(manager, "_spawn_worker", spawn)
    assert manager._get_or_create_worker(2) is spawn.return_value
    monkeypatch.setattr(quota, "_flush_quota", original_flush)
    manager._get_or_create_worker(1)
    assert _persisted(path) == {delta.delta_uid: delta.delta_count for delta in deltas}


def test_no_parent_ack_prevents_order_and_closes_quota_channel():
    current = shared_guard(AccountControlQuotaState(), "no-ack")
    connection = Mock()
    connection.poll.return_value = False
    current.counter_mirror = quota.QuotaClient(connection)
    send_order = Mock()
    with pytest.raises(RuntimeError, match="额度同步失败"):
        run_controlled_call(guard=current, operation="place_order", call=send_order)
    send_order.assert_not_called()
    connection.close.assert_called_once()
    assert current.flush_records()[0] == []


def test_shutdown_reports_unpersisted_quota_and_keeps_it_for_retry(worker_quota, monkeypatch):
    manager, path = worker_quota
    handle, deltas = _start(manager, path)

    async def fail_write(_deltas):
        raise OSError("database unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(quota, "_flush_quota", fail_write)
        manager._force_drop_worker(1, handle)
        with pytest.raises(manager_module.WorkerBackendExecutionError, match="关停时仍有报单额度未恢复"):
            manager.close()
        assert manager._retiring_workers[1] == [handle]
    manager.close()
    assert _persisted(path) == {delta.delta_uid: delta.delta_count for delta in deltas}


def test_preparing_worker_executor_attaches_the_quota_client(monkeypatch):
    from axile.server.execution.worker_backend import worker_state
    from tests.unit.server._execution_test_support import build_account

    current = shared_guard(AccountControlQuotaState(), "prepared")
    monkeypatch.setattr(worker_state, "build_account_control_guard", AsyncMock(return_value=current))
    executor = Mock()
    client = Mock()
    worker_state._prepare_executor(
        executor=executor, account=build_account(id=1), execution_id="prepared", audit_context={}, quota_client=client
    )
    executor.set_account_control_guard.assert_called_once_with(current)
    current.begin_operation("place_order")
    assert client.call_args.args[0] == current.instance_id
    assert {delta.delta_count for delta in client.call_args.args[1]} == {1}


@pytest.mark.parametrize("pending", [False, True])
def test_live_retiring_worker_can_only_be_replaced_after_quota_is_complete(monkeypatch, native_database, pending):
    seed_account(native_database)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    current = shared_guard(AccountControlQuotaState(), "draining")
    parent, child = Pipe()
    handle = _FakeHandle(join_stops=False)
    handle.replay_draining = True
    handle.quota_mirror = quota.QuotaMirror(parent, 1, handle.process)
    client = quota.QuotaClient(child)
    current.counter_mirror = client
    if pending:
        current.begin_operation("place_order")
    child.close()
    manager = manager_module.WorkerBackendManager()
    manager._retiring_workers[1] = [handle]
    replacement = _FakeHandle()
    spawn = Mock(return_value=replacement)
    monkeypatch.setattr(manager, "_spawn_worker", spawn)
    try:
        if pending:
            with pytest.raises(manager_module.WorkerBackendExecutionError, match="额度尚未恢复"):
                manager._get_or_create_worker(1)
            spawn.assert_not_called()
        else:
            assert manager._get_or_create_worker(1) is replacement
        assert manager._retiring_workers[1] == [handle]
    finally:
        handle.process._is_alive = False
        manager.close()


def test_quota_mirror_rejects_another_accounts_counters():
    current = shared_guard(AccountControlQuotaState(), "wrong-account")
    parent, child = Pipe()
    mirror = quota.QuotaMirror(parent, 2, SimpleNamespace(is_alive=lambda: False))
    current.counter_mirror = quota.QuotaClient(child)
    with pytest.raises(RuntimeError, match="额度同步失败"):
        current.begin_operation("place_order")
    assert mirror.recover()
