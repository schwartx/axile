"""真实控制管道超时后仍交付已保存配置，不触发交易连接。"""

import threading
from dataclasses import replace
from multiprocessing import Pipe
from time import perf_counter
from types import SimpleNamespace

import pytest

from axile.server.core import db
from axile.server.db.models.account import Account, AccountCreate
from axile.server.execution.worker_backend import manager, replay, worker
from axile.server.execution.worker_backend.protocol import (
    WorkerReplayControl,
    WorkerReplayStatus,
    WorkerTerminationSignal,
)
from axile.server.execution_replay.service import replay_window
from tests.conftest import native_store_call


def eventually(predicate):
    wake = threading.Event()
    for _ in range(300):
        if predicate():
            return
        wake.wait(0.01)
    assert predicate()


@pytest.fixture
def runtime(native_database, monkeypatch):
    monkeypatch.setattr(db, "database_path", lambda: str(native_database))
    account = Account(
        name="recording-control",
        market="future",
        trade_channel="ctp",
        brokerage="fixture",
        account_config={"fixture": "frozen"},
        algorithm={"method": "SINGLE-MAKER", "params": {}},
        replay_recording_enabled=True,
    )
    saved = native_store_call(
        native_database,
        "account_create",
        {
            "account": account.model_dump(mode="json", include=set(AccountCreate.model_fields)),
            "now": "2026-10-31T01:00:00Z",
        },
    )["account"]
    account = Account.model_validate(saved)
    backend = manager.WorkerBackendManager()
    active = []

    def spawn(enabled=False):
        state = worker._WorkerBackendState(account_id=account.id, executor=SimpleNamespace(), connected_once=True)
        if enabled:
            replay.configure_worker_replay(state, replay.replay_config(account))
            eventually(lambda: state.replay_recorder.status()["state"] == "recording")
            state.replay_recorder.begin_execution("E", ["X"])
            state.replay_recorder.observe(
                "/command", {"execution_id": "E", "command_id": "before-off", "phase": "request"}
            )
        parent, child = Pipe()
        thread = threading.Thread(target=worker._run_termination_control_loop, args=(child, state), daemon=True)
        handle = manager._WorkerBackendHandle(
            account.id, SimpleNamespace(is_alive=lambda: True), SimpleNamespace(closed=False), parent
        )
        backend._workers[account.id] = handle
        thread.start()
        instance = SimpleNamespace(state=state, handle=handle, parent=parent, child=child, thread=thread)
        active.append(instance)
        return instance

    def save(enabled):
        value = native_store_call(
            native_database,
            "account_update",
            {"account_id": account.id, "patch": {"replay_recording_enabled": enabled}, "now": "2026-10-31T01:00:01Z"},
        )["account"]
        return Account.model_validate(value)

    first = spawn(True)
    yield SimpleNamespace(
        backend=backend,
        account=account,
        first=first,
        spawn=spawn,
        save=save,
        path=native_database,
        root=native_database.parent / "replay",
    )
    backend._workers.clear()
    for instance in active:
        eventually(lambda: instance.handle.control_thread is None)
        instance.parent.send(None)
        instance.thread.join(2)
        assert not instance.thread.is_alive()
        instance.parent.close()
        instance.child.close()
        if instance.state.replay_recorder is not None:
            instance.state.replay_recorder.close()


def test_saved_off_is_delivered_after_pending_timeout_without_any_status_poll(runtime):
    backend, first = runtime.backend, runtime.first
    recorder = first.state.replay_recorder
    recording = recorder.status()["recording_id"]
    results = {}
    with recorder._condition:
        old = threading.Thread(
            target=lambda: results.setdefault("old", backend._replay_control_blocking(runtime.account, False))
        )
        old.start()
        eventually(lambda: first.handle.replay_pending is not None)
        off = runtime.save(False)
        update = threading.Thread(target=lambda: results.setdefault("off", backend._replay_control_blocking(off, True)))
        update.start()
        old.join(3)
        update.join(3)
        assert not old.is_alive() and not update.is_alive()
        assert results["off"]["effective_version"] is None
    # 不再调用 manager GET；排队配置必须独立于旧状态回复生效。
    eventually(lambda: recorder.status()["state"] == "disabled")
    assert recorder.status()["config_version"] == off.replay_config_version
    recorder.observe("/command", {"execution_id": "E", "command_id": "after-off", "phase": "request"})
    page = replay_window(
        runtime.root, runtime.account.id, "E", {"recording_id": recording, "start_us": 0, "end_us": 2**53 - 1}
    )
    identities = [event["data"].get("command_id") for event in page["messages"]]
    assert "before-off" in identities and "after-off" not in identities
    confirmed = backend._replay_control_blocking(off, False)
    assert confirmed["desired"] is False and confirmed["state"] == "disabled"
    assert confirmed["effective_version"] == off.replay_config_version


def test_lock_timeout_does_not_drop_off_or_claim_an_unacknowledged_version(runtime):
    off = runtime.save(False)
    with runtime.first.handle.replay_lock:
        result = runtime.backend._replay_control_blocking(off, True)
        assert result["effective_version"] is None
        assert result["state"] == "starting"
    eventually(lambda: runtime.first.state.replay_recorder.status()["state"] == "disabled")


def test_latest_saved_version_survives_an_older_control_call_and_a_replaced_worker(runtime):
    off = runtime.save(False)
    assert runtime.backend._replay_control_blocking(off, True)["desired"] is False
    runtime.first.state.replay_recorder.close()
    runtime.first.handle.retired = True
    replacement = runtime.spawn()
    result = runtime.backend._replay_control_blocking(runtime.account, False)
    assert result["desired"] is False and result["config_version"] == off.replay_config_version
    assert result["effective_version"] == off.replay_config_version
    assert replacement.state.replay_config.enabled is False
    assert replacement.state.replay_recorder is None


def test_latest_saved_version_is_retained_even_before_a_worker_exists(runtime):
    runtime.first.state.replay_recorder.close()
    runtime.backend._workers.clear()
    off = runtime.save(False)
    assert runtime.backend._replay_control_blocking(off, True)["effective_version"] == off.replay_config_version
    replacement = runtime.spawn()
    result = runtime.backend._replay_control_blocking(runtime.account, False)
    assert result["desired"] is False and result["config_version"] == off.replay_config_version
    assert replacement.state.replay_recorder is None


def test_saved_channel_change_advances_version_and_replaces_cached_context(runtime):
    runtime.backend._replay_control_blocking(runtime.account, False)
    changed = Account.model_validate(
        native_store_call(
            runtime.path,
            "account_update",
            {
                "account_id": runtime.account.id,
                "patch": {"trade_channel": "gm"},
                "now": "2026-10-31T01:00:01Z",
            },
        )["account"]
    )
    assert changed.replay_config_version > runtime.account.replay_config_version
    runtime.first.handle.retired = True
    replacement = runtime.spawn()
    result = runtime.backend._replay_control_blocking(changed, False)
    assert result["config_version"] == changed.replay_config_version
    assert replacement.state.replay_config.context["channel"] == "gm"
    assert runtime.backend._replay_configs[changed.id].context["channel"] == "gm"


def test_late_status_ack_does_not_overwrite_a_newer_saved_intent(runtime, monkeypatch):
    received, release = threading.Event(), threading.Event()
    original = runtime.backend._receive_replay_status
    results = {}

    def paused(handle):
        result = original(handle)
        received.set()
        assert release.wait(2)
        return result

    monkeypatch.setattr(runtime.backend, "_receive_replay_status", paused)
    old = threading.Thread(
        target=lambda: results.setdefault("old", runtime.backend._replay_control_blocking(runtime.account, False))
    )
    old.start()
    assert received.wait(2)
    off = runtime.save(False)
    update = threading.Thread(
        target=lambda: results.setdefault("off", runtime.backend._replay_control_blocking(off, True))
    )
    update.start()
    eventually(lambda: runtime.first.state.replay_recorder.status()["desired"] is False)
    release.set()
    old.join(3)
    update.join(3)
    assert not old.is_alive() and not update.is_alive()
    assert results["old"]["desired"] is False
    assert results["old"]["config_version"] == off.replay_config_version
    assert results["old"]["effective_version"] == runtime.account.replay_config_version


def test_same_configuration_is_not_continuously_resent(runtime):
    messages = []
    parent = runtime.first.parent

    def send(message):
        messages.append(message)
        parent.send(message)

    runtime.first.handle.control_connection = SimpleNamespace(send=send, poll=parent.poll, recv=parent.recv)
    runtime.backend._replay_control_blocking(runtime.account, False)
    runtime.backend._replay_control_blocking(runtime.account, True)
    runtime.backend._replay_control_blocking(runtime.account, False)
    assert len([message for message in messages if message.request_id is None]) == 1
    assert len([message for message in messages if message.request_id is not None]) == 3


def test_send_failure_retains_latest_intent_for_a_later_status_request(runtime):
    off = runtime.save(False)
    parent = runtime.first.parent
    failed = []

    def send(message):
        if message.request_id is None and not failed:
            failed.append(True)
            raise BrokenPipeError("isolated first send failure")
        parent.send(message)

    runtime.first.handle.control_connection = SimpleNamespace(send=send, poll=parent.poll, recv=parent.recv)
    result = runtime.backend._replay_control_blocking(off, True)
    assert result["effective_version"] is None or result["effective_version"] < off.replay_config_version
    # 即使轮询在途账户快照较旧，也只能补发已保存的最新 OFF。
    result = runtime.backend._replay_control_blocking(runtime.account, False)
    assert result["desired"] is False and result["config_version"] == off.replay_config_version
    eventually(lambda: runtime.first.state.replay_recorder.status()["state"] == "disabled")


def test_unhealthy_live_worker_does_not_claim_saved_off_is_effective(runtime):
    off = runtime.save(False)
    runtime.first.handle.connection.closed = True
    result = runtime.backend._replay_control_blocking(off, True)
    assert result["effective_version"] is None
    assert result["state"] == "error"
    runtime.first.handle.connection.closed = False
    assert runtime.backend._replay_control_blocking(runtime.account, False)["desired"] is False
    eventually(lambda: runtime.first.state.replay_recorder.status()["state"] == "disabled")


def test_a_late_unmatched_reply_does_not_poison_future_status_requests(runtime):
    runtime.first.child.send(WorkerReplayStatus("old-abandoned-request", {"config_version": 0}))
    result = runtime.backend._replay_control_blocking(runtime.account, False)
    assert result["effective_version"] == runtime.account.replay_config_version
    assert result["desired"] is True


def test_backpressure_keeps_latest_off_and_termination_deadline_bounded(runtime, monkeypatch):
    handle, recorder = runtime.first.handle, runtime.first.state.replay_recorder
    parent = runtime.first.parent
    sending = {"entered": 0, "finished": 0}
    sent = []
    wake = threading.Event()

    def send(message):
        sent.append(message)
        if isinstance(message, WorkerReplayControl) and message.config is not None:
            sending["entered"] = message.config.config_version
        parent.send(message)
        sending["finished"] = sending["entered"]

    handle.control_connection = SimpleNamespace(send=send, poll=parent.poll, recv=parent.recv, close=parent.close)
    selected = replay.replay_config(runtime.account)
    results = {}
    with recorder._condition:
        old = threading.Thread(
            target=lambda: results.setdefault("old", runtime.backend._replay_control_blocking(runtime.account, False))
        )
        old.start()
        eventually(lambda: handle.replay_pending is not None)
        blocked = False
        for version in range(2, 2000):
            runtime.backend._queue_replay_config(handle, replace(selected, config_version=version))
            wake.wait(0.001)
            if sending["entered"] != sending["finished"]:
                entered = sending["entered"]
                wake.wait(0.03)
                if sending["entered"] == entered and sending["finished"] < entered:
                    blocked = True
                    break
        assert blocked, "real control Pipe never reached backpressure"
        off = runtime.account.model_copy(
            update={"replay_recording_enabled": False, "replay_config_version": version + 1}
        )
        start = perf_counter()
        result = runtime.backend._replay_control_blocking(off, True)
        assert perf_counter() - start < 1.5
        assert result["effective_version"] is None
        with handle.control_lock:
            assert handle.control_config.config.enabled is False
            assert handle.control_config.config.config_version == off.replay_config_version
        business, peer = Pipe()
        original = handle.connection
        handle.connection = business
        try:
            monkeypatch.setattr(manager, "_TERMINATION_GRACE_SECONDS", 0.05)
            controller = SimpleNamespace(is_requested=lambda: True, reason=lambda: "stop", mode=lambda: None)
            request = SimpleNamespace(request_id="business", execution_id="E")
            start = perf_counter()
            assert runtime.backend._receive_response(handle, request, 10, controller) == (None, True)
            assert perf_counter() - start < 0.5
            with handle.control_lock:
                assert isinstance(handle.control_termination, WorkerTerminationSignal)
        finally:
            handle.connection = original
            business.close()
            peer.close()
        old.join(2)
        assert not old.is_alive()
    # 管道恢复后只保留最后 OFF，无需再次访问 manager 状态接口。
    eventually(lambda: recorder.status()["desired"] is False)
    assert recorder.status()["config_version"] == off.replay_config_version
    eventually(lambda: "E" in runtime.first.state.pending_terminations)
    termination = next(index for index, message in enumerate(sent) if isinstance(message, WorkerTerminationSignal))
    final_off = next(
        index
        for index, message in enumerate(sent)
        if isinstance(message, WorkerReplayControl)
        and message.config is not None
        and message.config.config_version == off.replay_config_version
    )
    assert termination < final_off
