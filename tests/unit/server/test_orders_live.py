"""委托 IPC 镜像、实例隔离和纯内存读取回归。"""

import asyncio
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from axile.executor.account_orders import AccountOrderBook
from axile.executor.algorithms.utils.clock import clock_now_iso
from axile.executor.connection_state import ConnectionObservation
from axile.server import account_orders, connection_runtime, orders_live
from axile.server.connection_state import ConnectionBinding, ConnectionCollector, connection_config_key
from axile.server.execution.worker_backend.order_monitor import sample_orders
from axile.server.execution.worker_backend.worker_state import _WorkerBackendState
from tests.unit.server._execution_test_support import build_account
from tests.unit.server.test_account_orders import order


@pytest.fixture(autouse=True)
def isolate_mirrors():
    connection_runtime.clear_sessions()
    yield
    connection_runtime.clear_sessions()


def session(instance="first"):
    account = build_account()
    binding = ConnectionBinding(
        account.id, connection_config_key(account.trade_channel, account.account_config), instance
    )
    connection_runtime.register_session(binding, lambda: True)
    connection_runtime.observe_session(binding, 1, ConnectionObservation("ok", clock_now_iso()))
    return account, binding


def frame(binding, *, version=1, complete=True):
    return {
        "binding": asdict(binding),
        "snapshot": {
            "version": version,
            "instance_id": binding.instance_id,
            "worker_instance_id": binding.instance_id,
            "generation": 0,
            "complete": complete,
            "error": None,
            "data": [],
            "resolved": [],
            "sampled_at": clock_now_iso(),
        },
    }


def test_plain_reads_never_query_channel_and_config_change_is_unknown(monkeypatch):
    account, binding = session()
    orders_live.consume_orders_snapshot(frame(binding))
    command = AsyncMock(side_effect=AssertionError("普通读取不能访问渠道"))
    monkeypatch.setattr(account_orders, "_command", command)
    result = asyncio.run(account_orders.query_account_orders(account))
    assert result.complete and result.count == 0
    account.account_config = {**account.account_config, "investor_id": "other"}
    result = asyncio.run(account_orders.query_account_orders(account))
    assert not result.complete and result.count is None
    command.assert_not_called()


def test_old_worker_cannot_replace_new_snapshot_and_repeated_sampling_has_no_event():
    _, old = session("old")
    _, new = session("new")
    current = frame(new)
    orders_live.consume_orders_snapshot(current)
    version = orders_live.orders_hub.version
    orders_live.consume_orders_snapshot(current)
    orders_live.consume_orders_snapshot(frame(old, version=999))
    assert orders_live.orders_hub.version == version
    assert orders_live._frames[new.account_id][0] == new


def test_sampling_only_reads_memory_and_connection_failure_preserves_last_orders():
    account, binding = session()
    book = AccountOrderBook()
    book.replace([order()], 0)
    executor = SimpleNamespace(account_orders=book)
    state = _WorkerBackendState(
        executor=executor,
        orders_executor=executor,
        account_id=account.id,
        connection_collector=ConnectionCollector(binding),
        orders_binding=binding,
    )
    snapshot = sample_orders(state)
    assert snapshot["snapshot"]["complete"]
    assert "secret" not in str(snapshot)
    state.disconnected = True
    snapshot = sample_orders(state)
    assert not snapshot["snapshot"]["complete"]
    assert snapshot["snapshot"]["data"][0]["remaining_volume"] == 2


def test_monitor_does_not_wait_for_connection_replacement():
    from threading import Event, Thread

    account, binding = session()
    state = _WorkerBackendState(
        executor=SimpleNamespace(account_orders=AccountOrderBook()),
        account_id=account.id,
        connection_collector=ConnectionCollector(binding),
        orders_binding=binding,
    )
    ready, release = Event(), Event()

    def replace():
        with state.resource_lock:
            ready.set()
            release.wait(2)

    thread = Thread(target=replace)
    thread.start()
    assert ready.wait(1)
    try:
        assert sample_orders(state) is None
    finally:
        release.set()
        thread.join(1)


def test_command_sampling_cannot_advance_monitor_watermark():
    account, binding = session()
    book = AccountOrderBook()
    book.replace([], 0)
    executor = SimpleNamespace(account_orders=book)
    state = _WorkerBackendState(
        executor=executor,
        orders_executor=executor,
        account_id=account.id,
        connection_collector=ConnectionCollector(binding),
        orders_binding=binding,
    )
    assert "snapshot" in sample_orders(state)
    assert sample_orders(state)["heartbeat"]
    book.apply(order())
    assert "snapshot" in sample_orders(state, force=True)
    # 即使命令响应超时被丢弃，独立监控下一帧仍能送达变化。
    assert "snapshot" in sample_orders(state)
    assert sample_orders(state)["heartbeat"]


def test_expired_mirror_is_uncertain_and_heartbeat_recovers_without_broker(monkeypatch):
    account, binding = session()
    moment = [100.0]
    monkeypatch.setattr(orders_live, "clock_monotonic", lambda: moment[0])
    orders_live.consume_orders_snapshot(frame(binding))
    moment[0] += 6
    assert not orders_live.read_orders_snapshot(account)["complete"]
    version = orders_live.orders_hub.version
    orders_live.consume_orders_snapshot({"binding": asdict(binding), "heartbeat": True})
    assert orders_live.read_orders_snapshot(account)["complete"]
    assert orders_live.orders_hub.version == version + 1


def test_replacement_between_identity_check_and_cache_write_rejects_old_frame(monkeypatch):
    account, binding = session("old")
    original = connection_runtime.matches_session
    first = True

    def replaces(source):
        nonlocal first
        if first:
            first = False
            valid = original(source)
            session("new")
            return valid
        return original(source)

    monkeypatch.setattr(connection_runtime, "matches_session", replaces)
    orders_live._frames.pop(account.id, None)
    orders_live.consume_orders_snapshot(frame(binding))
    assert account.id not in orders_live._frames


def test_recovery_stays_bounded_and_never_runs_during_execution_or_disconnect(monkeypatch):
    from unittest.mock import Mock

    from axile.server.execution.worker_backend import order_monitor, worker_state

    account, binding = session()
    state = _WorkerBackendState(
        executor=SimpleNamespace(account_orders=AccountOrderBook()),
        account_id=account.id,
        connection_collector=ConnectionCollector(binding),
        orders_account=account,
        orders_recovery_enabled=True,
        orders_binding=binding,
    )
    thread = Mock()
    thread.is_alive.return_value = False
    factory = Mock(return_value=thread)
    monkeypatch.setattr(order_monitor, "Thread", factory)
    monkeypatch.setattr(order_monitor, "clock_monotonic", lambda: 100)
    state.executor.connection_recovery_in_progress = True
    order_monitor._schedule_recovery(state)
    factory.assert_not_called()
    assert state.orders_retry_at == 0
    state.executor.connection_recovery_in_progress = False
    order_monitor._schedule_recovery(state)
    order_monitor._schedule_recovery(state)
    assert factory.call_count == 1 and state.orders_retry_at == 130
    prepare = Mock()
    finalize = Mock()
    monkeypatch.setattr(worker_state, "_resolve_prepared_executor", prepare)
    monkeypatch.setattr(worker_state, "_finalize_executor", finalize)
    for field in ("request_active", "active_execution_id", "disconnected"):
        setattr(state, field, "active" if field == "active_execution_id" else True)
        order_monitor._recover(state)
        setattr(state, field, None if field == "active_execution_id" else False)
    prepare.assert_not_called()
    finalize.assert_not_called()
    order_monitor._recover(state)
    prepare.assert_called_once()
    finalize.assert_called_once()


def test_new_binding_cannot_publish_old_executor_orders_before_preparation():
    account, old = session("old-config")
    _, current = session("new-config")
    book = AccountOrderBook()
    book.replace([order()], 0)
    state = _WorkerBackendState(
        executor=SimpleNamespace(account_orders=book),
        account_id=account.id,
        connection_collector=ConnectionCollector(current),
        orders_binding=old,
    )
    assert sample_orders(state) is None


def test_failed_new_configuration_disables_recovery_using_previous_account(monkeypatch):
    from axile.server.execution.worker_backend import order_monitor, worker_state

    account, binding = session()
    updated = account.model_copy(update={"account_config": {"investor_id": "different"}})
    state = _WorkerBackendState(
        orders_account=account,
        orders_binding=binding,
        orders_recovery_enabled=True,
        connection_collector=ConnectionCollector(binding),
    )

    def fail(*args, **kwargs):
        raise ConnectionError("new configuration failed")

    monkeypatch.setattr(worker_state, "ensure_resident_connection", fail)
    with pytest.raises(ConnectionError):
        worker_state._resolve_prepared_executor(state=state, account=updated, execution_id=None, audit_context={})
    assert state.orders_account is updated and not state.orders_recovery_enabled
    order_monitor._recover(state)


def test_executor_rebuild_can_restart_version_and_rejects_previous_generation(monkeypatch):
    account, binding = session()
    moment = [100.0]
    monkeypatch.setattr(orders_live, "clock_monotonic", lambda: moment[0])
    old = frame(binding, version=100)
    old["snapshot"].update(generation=1, instance_id=f"{binding.instance_id}:1")
    orders_live.consume_orders_snapshot(old)
    rebuilt = frame(binding, version=1)
    rebuilt["snapshot"].update(generation=2, instance_id=f"{binding.instance_id}:2")
    orders_live.consume_orders_snapshot(rebuilt)
    assert orders_live.read_orders_snapshot(account)["version"] == 1
    old["snapshot"]["version"] = 999
    orders_live.consume_orders_snapshot(old)
    assert orders_live.read_orders_snapshot(account)["generation"] == 2
    moment[0] += 6
    orders_live.consume_orders_snapshot({"binding": asdict(binding), "heartbeat": True, "generation": 1})
    assert not orders_live.read_orders_snapshot(account)["complete"]
    orders_live.consume_orders_snapshot({"binding": asdict(binding), "heartbeat": True, "generation": 2})
    assert orders_live.read_orders_snapshot(account)["complete"]


def test_executor_generation_advances_only_when_preparation_installs_new_instance(monkeypatch):
    from axile.server.execution.worker_backend import worker_state

    account, binding = session()
    state = _WorkerBackendState(connection_collector=ConnectionCollector(binding))
    first, second = object(), object()
    installed = [first]
    monkeypatch.setattr(worker_state, "ensure_resident_connection", lambda *args, **kwargs: installed[0])
    for expected in (1, 1, 2):
        if expected == 2:
            installed[0] = second
        worker_state._resolve_prepared_executor(state=state, account=account, execution_id=None, audit_context={})
        assert state.orders_generation == expected


def test_late_manual_response_uses_new_executor_baseline_already_received(monkeypatch):
    account, binding = session()
    old = frame(binding, version=100)["snapshot"]
    old.update(generation=1, instance_id=f"{binding.instance_id}:1")

    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def command(_account, _payload):
            started.set()
            await release.wait()
            return old

        monkeypatch.setattr(account_orders, "_command", command)
        query = asyncio.create_task(account_orders.query_account_orders(account, refresh=True))
        await started.wait()
        rebuilt = frame(binding, version=1)
        rebuilt["snapshot"].update(generation=2, instance_id=f"{binding.instance_id}:2")
        orders_live.consume_orders_snapshot(rebuilt)
        release.set()
        result = await query
        assert result.instance_id == f"{binding.instance_id}:2" and result.version == 1

    asyncio.run(run())
