"""实际资源声明与录制意愿分离；SDK 工厂隔离，SQLite 和 MCAP 使用真实实现。"""

import json
import threading
from dataclasses import fields, replace
from types import SimpleNamespace

import pytest

from axile.common.serialization import copy_data, from_payload, to_payload
from axile.executor.connection_state import ConnectionMonitor
from axile.server._native import _core
from axile.server.core import db
from axile.server.db.models.account import Account, AccountCreate, AccountSnapshot
from axile.server.execution.resident_connection import disconnect_resident_executor
from axile.server.execution.worker_backend import replay, worker_state
from tests.unit.server.test_execution_replay_recorder import eventually as wait_for


class Executor:
    """只替代 SDK 连接；保留真实连接宿主与资源绑定顺序。"""

    def __init__(self, account):
        self.channel = str(account.trade_channel)
        self.credentials = account.account_config.copy()
        self.ready = False
        self._connection_monitor = ConnectionMonitor()

    def _verify_connection(self):
        return self.ready

    def close(self):
        self.ready = False


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    database = tmp_path / "accounts.db"
    _core.migration_upgrade(str(database), json.dumps({"public": "head"}))
    account = Account(
        name="worker-context",
        market="future",
        trade_channel="ctp",
        brokerage="fixture",
        account_config={"fixture": "old"},
        algorithm={"method": "SINGLE-MAKER", "params": {}},
        replay_recording_enabled=True,
    )
    old = from_payload(
        AccountSnapshot,
        _core.account_create(
            str(database),
            json.dumps(
                {
                    "account": to_payload(account, mode="json", include={item.name for item in fields(AccountCreate)}),
                    "now": "2026-10-31T01:00:00Z",
                }
            ),
        )["account"],
    )
    monkeypatch.setattr(db, "database_path", lambda: str(database))
    monkeypatch.setattr(worker_state, "create_executor_instance", lambda account, **_kwargs: Executor(account))
    monkeypatch.setattr(
        worker_state, "_initialize_worker_connection", lambda _state, executor: setattr(executor, "ready", True)
    )
    monkeypatch.setattr(worker_state, "_prepare_executor", lambda **_kwargs: None)
    state = worker_state._WorkerBackendState(account_id=old.id)

    def update(**patch):
        return from_payload(
            AccountSnapshot,
            _core.account_update(
                str(database), json.dumps({"account_id": old.id, "patch": patch, "now": "2026-10-31T01:00:01Z"})
            )["account"],
        )

    def resolve(account):
        return worker_state._resolve_prepared_executor(
            state=state, account=account, execution_id=None, audit_context={}
        )

    yield SimpleNamespace(state=state, old=old, update=update, resolve=resolve, root=tmp_path / "replay")
    if state.replay_recorder is not None:
        state.replay_recorder.close()


def allow_fixture_replay(channel, monkeypatch, *, supported=True):
    original = replay.get_channel

    def get_channel(name):
        registration = original(name)
        descriptor = registration.descriptor
        if name == channel:
            descriptor = copy_data(
                descriptor, update={"replay": copy_data(descriptor.replay, update={"supported": supported})}
            )
        return SimpleNamespace(descriptor=descriptor)

    monkeypatch.setattr(replay, "get_channel", get_channel)


def recording(runtime, execution):
    recorder = runtime.state.replay_recorder
    replay.begin_worker_replay_execution(runtime.state, execution, ("X",))
    wait_for(lambda: recorder.status()["state"] in {"recording", "error"})
    assert recorder.status()["state"] == "recording", recorder.status()
    recorder.observe(
        "/order",
        {"symbol": "X", "order_id": execution, "price": 100, "quantity": 1, "status": "working"},
    )
    replay.end_worker_replay_execution(runtime.state, "complete")
    return recorder


def test_switch_to_unsupported_channel_does_not_keep_old_supported_declaration(runtime, monkeypatch):
    allow_fixture_replay("gm", monkeypatch, supported=False)
    current = runtime.update(trade_channel="gm", account_config={"fixture": "new"})
    stale = replay.current_replay_config(runtime.old)
    fresh = replay.current_replay_config(current)
    assert not fresh.supported and stale.config_version == fresh.config_version
    replay.configure_worker_replay(runtime.state, stale)
    runtime.resolve(runtime.old)
    recorder = recording(runtime, "A")
    replay.configure_worker_replay(runtime.state, fresh)
    second = runtime.resolve(current)
    wait_for(lambda: recorder.status()["state"] != "closing")
    assert second.channel == "gm" and second.ready
    assert recorder.status()["state"] == "unsupported"
    assert recorder._context["channel"] == "gm"
    assert getattr(second, "_replay_observer", None) is None


def test_new_resource_snapshot_cannot_restore_an_old_on_intent(runtime):
    current = runtime.update(trade_channel="gm", account_config={"fixture": "new"}, replay_recording_enabled=False)
    off = replay.current_replay_config(current)
    replay.configure_worker_replay(runtime.state, off)
    replay.configure_worker_replay(runtime.state, replay.replay_config(runtime.old))
    runtime.resolve(runtime.old)
    runtime.resolve(current)
    assert runtime.state.replay_recorder is None
    assert replay.worker_replay_status(runtime.state)["desired"] is False
    assert runtime.state.replay_config.config_version == current.replay_config_version
    assert not runtime.root.exists()


@pytest.mark.parametrize("advance_version", [False, True])
def test_control_context_cannot_override_an_already_bound_resource(runtime, monkeypatch, advance_version):
    allow_fixture_replay("tq", monkeypatch)
    current = runtime.update(trade_channel="tq", account_config={"fixture": "new"})
    stale = replay.current_replay_config(runtime.old)
    replay.configure_worker_replay(runtime.state, replay.current_replay_config(current))
    owner = runtime.resolve(current)
    recorder = recording(runtime, "B")
    recording_id = recorder.status()["recording_id"]
    if advance_version:
        stale = replace(stale, config_version=stale.config_version + 1)
    replay.configure_worker_replay(runtime.state, stale)
    assert recorder.status()["recording_id"] == recording_id
    assert recorder._owner is owner and recorder._context["channel"] == "tq"
    assert recorder.status()["config_version"] == stale.config_version


def test_missing_or_mismatched_binding_does_not_attach_control_context_to_a_new_owner(runtime):
    selected = replay.current_replay_config(runtime.old)
    unbound = Executor(runtime.old)
    unbound.ready = True
    unbound._connection_monitor.succeeded()
    runtime.state.executor, runtime.state.connected_once = unbound, True
    replay.configure_worker_replay(runtime.state, selected)
    recorder = runtime.state.replay_recorder
    assert recorder.status()["state"] == "waiting_connection"
    assert getattr(unbound, "_replay_observer", None) is None
    worker_state._configure_executor_resources(runtime.state, runtime.old, unbound)
    wait_for(lambda: recorder.status()["state"] == "recording")
    other = Executor(runtime.old)
    runtime.state.executor = other
    replay.sync_worker_replay_connection(runtime.state, True)
    assert recorder._owner is None
    assert getattr(unbound, "_replay_observer", None) is None
    assert getattr(other, "_replay_observer", None) is None


def test_metadata_failure_isolated_from_connection_and_recovers_at_a_valid_resource_bind(runtime, monkeypatch):
    selected = replay.current_replay_config(runtime.old)
    replay.configure_worker_replay(runtime.state, selected)
    original = replay.replay_config

    def fail(_account):
        raise ValueError("fixture declaration error")

    monkeypatch.setattr(replay, "replay_config", fail)
    executor = runtime.resolve(runtime.old)
    assert executor.ready
    assert runtime.state.replay_error
    assert getattr(executor, "_replay_observer", None) is None
    assert not list(runtime.root.rglob("*.mcap"))
    monkeypatch.setattr(replay, "replay_config", original)
    assert runtime.resolve(runtime.old) is executor
    wait_for(lambda: runtime.state.replay_recorder.status()["state"] == "recording")
    assert runtime.state.replay_error is None


def test_hot_control_does_not_wait_for_connection_resource_lock(runtime):
    selected = replay.current_replay_config(runtime.old)
    replay.configure_worker_replay(runtime.state, selected)
    runtime.resolve(runtime.old)
    recording(runtime, "A")
    done = threading.Event()
    results = []

    def turn_off():
        results.append(
            replay.configure_worker_replay(runtime.state, replace(selected, enabled=False, config_version=2))
        )
        done.set()

    with runtime.state.resource_lock:
        thread = threading.Thread(target=turn_off)
        thread.start()
        completed_while_locked = done.wait(1)
    thread.join(1)
    assert completed_while_locked and not thread.is_alive()
    assert results[0]["desired"] is False


@pytest.mark.parametrize("initially_recording", [False, True])
def test_enabling_after_connection_failure_waits_for_actual_recovery(runtime, initially_recording):
    account = runtime.update(replay_recording_enabled=initially_recording)
    replay.configure_worker_replay(runtime.state, replay.current_replay_config(account))
    executor = runtime.resolve(account)
    assert runtime.state.connected_once
    assert not runtime.state.connection_collector.needs_reconciliation
    if initially_recording:
        wait_for(lambda: replay.worker_replay_status(runtime.state)["state"] == "recording")

    executor.ready = False
    executor._connection_monitor.failed(ConnectionError("fixture connection lost"))
    assert runtime.state.connection_collector.needs_reconciliation
    if initially_recording:
        wait_for(lambda: replay.worker_replay_status(runtime.state)["state"] == "waiting_connection")
        account = runtime.update(replay_recording_enabled=False)
        replay.configure_worker_replay(runtime.state, replay.current_replay_config(account))
        wait_for(lambda: replay.worker_replay_status(runtime.state)["state"] == "disabled")

    account = runtime.update(replay_recording_enabled=True)
    replay.configure_worker_replay(runtime.state, replay.current_replay_config(account))
    recorder = runtime.state.replay_recorder
    assert recorder.status()["state"] == "waiting_connection"
    assert recorder._owner is None
    assert getattr(executor, "_replay_observer", None) is None
    if not initially_recording:
        assert not list(runtime.root.rglob("*.mcap"))

    executor.ready = True
    executor._connection_monitor.succeeded()
    wait_for(lambda: recorder.status()["state"] == "recording")
    assert recorder._owner is executor
    assert runtime.resolve(account) is executor
    assert not runtime.state.connection_collector.needs_reconciliation


def test_enabling_after_recovery_observed_while_disabled_reuses_connection(runtime):
    account = runtime.update(replay_recording_enabled=False)
    replay.configure_worker_replay(runtime.state, replay.current_replay_config(account))
    executor = runtime.resolve(account)
    executor.ready = False
    executor._connection_monitor.failed(ConnectionError("fixture connection lost"))
    executor.ready = True
    executor._connection_monitor.succeeded()
    assert runtime.state.replay_recorder is None

    account = runtime.update(replay_recording_enabled=True)
    replay.configure_worker_replay(runtime.state, replay.current_replay_config(account))
    recorder = runtime.state.replay_recorder
    wait_for(lambda: recorder.status()["state"] == "recording")
    assert recorder._owner is executor


def test_enabling_during_manual_disconnect_does_not_reattach_the_closing_executor(runtime):
    account = runtime.update(replay_recording_enabled=False)
    replay.configure_worker_replay(runtime.state, replay.current_replay_config(account))
    executor = runtime.resolve(account)
    entered, release = threading.Event(), threading.Event()

    def logout():
        entered.set()
        assert release.wait(3)

    executor.graceful_logout = logout
    disconnect = threading.Thread(
        target=disconnect_resident_executor,
        args=(runtime.state,),
        kwargs={"close": lambda _executor: None},
    )
    disconnect.start()
    try:
        assert entered.wait(1)
        account = runtime.update(replay_recording_enabled=True)
        replay.configure_worker_replay(runtime.state, replay.current_replay_config(account))
        recorder = runtime.state.replay_recorder
        assert recorder.status()["state"] == "waiting_connection"
        assert recorder._owner is None
    finally:
        release.set()
        disconnect.join(1)
        assert not disconnect.is_alive()
