"""录制意愿读取当前数据库版本，不继承在途交易的旧设置。"""

import threading
from types import SimpleNamespace

import pytest

from axile.server import connection_state, native_store
from axile.server.core import db
from axile.server.db.models.account import Account, AccountCreate, AccountSnapshot
from axile.server.execution.worker_backend import manager, replay, worker, worker_state
from axile.server.execution.worker_backend.protocol import WorkerBackendRequest
from axile.server.execution_replay.service import replay_manifest
from tests.conftest import native_store_call


@pytest.fixture
def saved_account(native_database, monkeypatch):
    monkeypatch.setattr(db, "database_path", lambda: str(native_database))
    monkeypatch.setattr(connection_state, "begin_connection_instance", lambda _: None)
    account = Account(
        name="replay-race",
        market="future",
        trade_channel="ctp",
        brokerage="fixture",
        account_config={"fixture": "frozen"},
        algorithm={"method": "SINGLE-MAKER", "params": {}},
        replay_recording_enabled=True,
    )
    value = native_store_call(
        native_database,
        "account_create",
        {
            "account": account.model_dump(mode="json", include=set(AccountCreate.model_fields)),
            "now": "2026-10-31T01:00:00Z",
        },
    )["account"]
    return native_database, AccountSnapshot.model_validate(value)


def handle():
    return SimpleNamespace(
        connection_binding=None,
        monitor=None,
        retired=False,
        process=SimpleNamespace(is_alive=lambda: True),
        connection=SimpleNamespace(closed=False),
    )


def request(account):
    return WorkerBackendRequest("old-execution", "prepare", account.model_dump(mode="json"), "E", {})


@pytest.mark.parametrize("entry", ["cold", "replacement", "new_manager"])
def test_saved_off_wins_over_old_on_snapshot_for_every_new_worker(saved_account, monkeypatch, entry):
    path, frozen = saved_account
    latest = AccountSnapshot.model_validate(
        native_store_call(
            path,
            "account_update",
            {
                "account_id": frozen.id,
                "patch": {"replay_recording_enabled": False, "name": "updated after capture"},
                "now": "2026-10-31T01:00:01Z",
            },
        )["account"]
    )
    assert latest.replay_config_version > frozen.replay_config_version
    backend = manager.WorkerBackendManager()
    spawned = []
    monkeypatch.setattr(backend, "_spawn_worker", lambda identity: spawned.append(identity) or handle())
    acknowledged = backend._replay_control_blocking(latest, True)
    assert acknowledged["state"] == "disabled"
    assert acknowledged["effective_version"] == latest.replay_config_version
    assert not spawned

    if entry == "new_manager":
        backend = manager.WorkerBackendManager()
        monkeypatch.setattr(backend, "_spawn_worker", lambda identity: spawned.append(identity) or handle())
    elif entry == "replacement":
        retired = handle()
        retired.retired = True
        backend._workers[frozen.id] = retired
        monkeypatch.setattr(backend, "_dispose_handle", lambda _: None)

    old_request = request(frozen)
    old_payload = old_request.account_payload.copy()
    try:
        fresh = backend._get_or_create_worker(frozen.id)
        snapshot = backend._bind_connection_request(fresh, old_request)
        selected = old_request.replay_config
        assert selected is not None
        assert selected.enabled is False
        assert selected.config_version == latest.replay_config_version
        assert selected.context["channel"] == str(frozen.trade_channel)
        assert snapshot.name == frozen.name
        assert old_request.account_payload == old_payload
        assert old_request.account_payload["replay_recording_enabled"] is True

        state = worker._WorkerBackendState(account_id=frozen.id, executor=SimpleNamespace(), connected_once=True)
        assert replay.configure_worker_replay(state, selected)["state"] == "disabled"
        worker_state._configure_executor_resources(state, snapshot, state.executor)
        assert state.replay_recorder is None
        assert not (path.parent / "replay").exists()
    finally:
        backend._workers.clear()
        backend._retiring_workers.clear()


def test_saved_on_also_supersedes_an_old_off_snapshot(saved_account):
    _, latest = saved_account
    frozen = latest.model_copy(update={"replay_recording_enabled": False, "replay_config_version": 0})
    selected = replay.current_replay_config(frozen)
    assert selected is not None and selected.enabled is True
    assert selected.config_version == latest.replay_config_version


def test_current_configuration_read_failure_does_not_replay_a_stale_on_intent(saved_account, monkeypatch):
    path, frozen = saved_account

    def unavailable(*args, **kwargs):
        raise OSError("fixture database read unavailable")

    monkeypatch.setattr(native_store, "native_operation_sync", unavailable)
    old_request = request(frozen)
    old_request.replay_config = replay.replay_config(frozen)
    snapshot = manager.WorkerBackendManager._bind_connection_request(handle(), old_request)
    assert snapshot.replay_recording_enabled is True
    assert old_request.replay_config is None
    installed = []
    executor = SimpleNamespace(set_instrument_catalog_provider=installed.append)
    provider = SimpleNamespace()
    state = worker._WorkerBackendState(
        account_id=frozen.id, executor=executor, connected_once=True, catalog_provider=provider
    )
    try:
        worker_state._configure_executor_resources(state, snapshot, executor)
        state.replay_connection_observer(True)
        assert installed == [provider]
        assert callable(executor._catalog_control_factory)
        assert state.replay_config is None
        assert state.replay_recorder is None
        assert not (path.parent / "replay").exists()
    finally:
        if state.replay_recorder is not None:
            state.replay_recorder.close()


def test_authoritative_on_configuration_survives_old_off_resources_and_records(saved_account):
    path, latest = saved_account
    stale = latest.model_copy(update={"replay_recording_enabled": False, "replay_config_version": 0})
    selected = replay.current_replay_config(stale)
    assert selected is not None and selected.enabled
    executor = SimpleNamespace()
    state = worker._WorkerBackendState(account_id=latest.id, executor=executor)
    try:
        assert replay.configure_worker_replay(state, selected)["state"] == "waiting_connection"
        worker_state._configure_executor_resources(state, stale, executor)
        assert state.replay_config is selected
        state.connected_once = True
        state.replay_connection_observer(True)
        recorder = state.replay_recorder
        wake = threading.Event()
        for _ in range(200):
            if recorder.status()["state"] != "starting":
                break
            wake.wait(0.01)
        assert recorder.status()["state"] == "recording"
        recorder.begin_execution("current-enabled", ["X"])
        recorder.end_execution("current-enabled")
        assert recorder.close()["state"] == "disabled"
        assert replay_manifest(path.parent / "replay", latest.id, "current-enabled")["availability"] == "available"
    finally:
        if state.replay_recorder is not None:
            state.replay_recorder.close()
