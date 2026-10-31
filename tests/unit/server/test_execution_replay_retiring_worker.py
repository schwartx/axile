"""退出中的旧 worker 仍接收录制意愿，所有存活实例都确认后才报生效。"""

import threading
from contextlib import contextmanager
from multiprocessing import Pipe
from types import SimpleNamespace

import pytest

from axile.server.execution.worker_backend import replay
from axile.server.execution.worker_backend.protocol import WorkerBackendResponse
from axile.server.execution_replay.service import replay_window
from tests.unit.server.test_execution_replay_control_delivery import eventually
from tests.unit.server.test_execution_replay_control_delivery import runtime as runtime


@contextmanager
def retiring(runtime):
    instance = runtime.first
    business, peer = Pipe()
    alive = [True]
    instance.handle.connection = business
    instance.handle.process = SimpleNamespace(
        is_alive=lambda: alive[0], join=lambda **kwargs: None, terminate=lambda: alive.__setitem__(0, False)
    )

    def shutdown():
        request = peer.recv()
        replay.close_worker_replay(instance.state)
        peer.send(WorkerBackendResponse(request.request_id, "result", {"shutdown": True}))
        alive[0] = False

    server = threading.Thread(target=shutdown, daemon=True)
    drop = threading.Thread(target=runtime.backend._drop_account_blocking, args=(runtime.account.id,))
    instance.handle.request_lock.acquire()
    server.start()
    drop.start()
    eventually(lambda: runtime.account.id not in runtime.backend._workers)
    try:
        yield instance
    finally:
        instance.handle.request_lock.release()
        drop.join(3)
        server.join(3)
        business.close()
        peer.close()
        assert not drop.is_alive() and not server.is_alive()


@pytest.mark.parametrize("replacement", [False, True])
def test_off_reaches_the_retiring_worker_before_its_busy_trade_finishes(runtime, replacement):
    recorder = runtime.first.state.replay_recorder
    identity = recorder.status()["recording_id"]
    with retiring(runtime):
        if replacement:
            latest = runtime.spawn()
        off = runtime.save(False)
        with recorder._condition:
            result = runtime.backend._replay_control_blocking(off, True)
            assert result["desired"] is False
            assert result["config_version"] == off.replay_config_version
            # 旧实例尚未确认，不能用新实例的 disabled 或“注册表已空”冒充完成。
            assert result["effective_version"] is None
        eventually(lambda: recorder.status()["state"] == "disabled")
        recorder.observe("/command", {"execution_id": "E", "command_id": "after-off", "phase": "request"})
        if replacement:
            assert latest.state.replay_recorder is None
            assert latest.state.replay_config.enabled is False
    result = runtime.backend._replay_control_blocking(off, False)
    assert result["effective_version"] == off.replay_config_version
    page = replay_window(
        runtime.root, runtime.account.id, "E", {"recording_id": identity, "start_us": 0, "end_us": 2**53 - 1}
    )
    assert all(message["data"].get("command_id") != "after-off" for message in page["messages"])


def test_forced_retirement_remains_pending_until_the_process_has_really_exited(runtime, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    instance = runtime.first
    alive = [True]
    instance.handle.process = SimpleNamespace(is_alive=lambda: alive[0])

    def terminate(handle):
        entered.set()
        assert release.wait(3)
        replay.close_worker_replay(instance.state)
        runtime.backend._close_control(handle)
        alive[0] = False

    monkeypatch.setattr(runtime.backend, "_terminate_process", terminate)
    drop = threading.Thread(target=runtime.backend._force_drop_worker, args=(runtime.account.id, instance.handle))
    drop.start()
    try:
        assert entered.wait(3)
        off = runtime.save(False)
        pending = runtime.backend._replay_control_blocking(off, True)
        assert pending["desired"] is False and pending["effective_version"] is None
        assert instance.state.replay_recorder.status()["desired"] is True
    finally:
        release.set()
        drop.join(3)
        assert not drop.is_alive()
    assert runtime.backend._replay_control_blocking(off, False)["effective_version"] == off.replay_config_version
    assert not runtime.backend._retiring_workers


def test_returning_from_dispose_does_not_forget_an_unconfirmed_live_process(runtime, monkeypatch):
    instance = runtime.first
    monkeypatch.setattr(runtime.backend, "_dispose_handle", lambda handle: None)
    runtime.backend._drop_account_blocking(runtime.account.id)
    assert runtime.backend._retiring_workers[runtime.account.id] == [instance.handle]
    off = runtime.save(False)
    try:
        result = runtime.backend._replay_control_blocking(off, True)
        if result["effective_version"] == off.replay_config_version:
            assert instance.state.replay_recorder.status()["desired"] is False
        eventually(lambda: instance.state.replay_recorder.status()["state"] == "disabled")
    finally:
        instance.handle.process = SimpleNamespace(is_alive=lambda: False)
    assert runtime.backend._replay_control_blocking(off, False)["effective_version"] == off.replay_config_version
    assert not runtime.backend._retiring_workers


@pytest.mark.parametrize("active_version, previous_version, previous_state", [(1, 2, "closing"), (3, 2, "disabled")])
def test_newer_worker_intent_is_not_hidden_by_an_older_acknowledged_worker(
    runtime, monkeypatch, active_version, previous_version, previous_state
):
    active = runtime.first.handle
    previous = SimpleNamespace(process=SimpleNamespace(is_alive=lambda: True), replay_sent_version=-1)
    runtime.backend._retiring_workers[runtime.account.id] = [previous]
    monkeypatch.setattr(runtime.backend, "_queue_replay_config", lambda handle, config: None)
    monkeypatch.setattr(
        runtime.backend,
        "_replay_handle_status",
        lambda handle, deadline: {
            "desired": handle is active,
            "state": "recording" if handle is active else previous_state,
            "config_version": active_version if handle is active else previous_version,
            "effective_version": active_version if handle is active else 1 if previous_state == "closing" else 2,
        },
    )
    try:
        result = runtime.backend._replay_control_blocking(runtime.account, False)
        assert result["desired"] is (active_version > previous_version)
        assert result["config_version"] == max(active_version, previous_version)
        assert result["state"] == "recording"
        assert result["effective_version"] == min(active_version, 1 if previous_state == "closing" else 2)
    finally:
        runtime.backend._retiring_workers.clear()


def test_replaced_unhealthy_worker_remains_controlled_until_its_process_exits(runtime, monkeypatch):
    previous = runtime.first
    previous.handle.connection.closed = True
    monkeypatch.setattr(runtime.backend, "_dispose_handle", lambda handle: None)
    monkeypatch.setattr(runtime.backend, "_spawn_worker", lambda account_id: runtime.spawn().handle)
    replacement = runtime.backend._get_or_create_worker(runtime.account.id)
    assert replacement is not previous.handle
    assert runtime.backend._retiring_workers[runtime.account.id] == [previous.handle]
    try:
        off = runtime.save(False)
        result = runtime.backend._replay_control_blocking(off, True)
        assert result["effective_version"] is None
        eventually(lambda: previous.state.replay_recorder.status()["state"] == "disabled")
    finally:
        previous.handle.process = SimpleNamespace(is_alive=lambda: False)
    assert runtime.backend._replay_control_blocking(off, False)["effective_version"] == off.replay_config_version
    assert not runtime.backend._retiring_workers
