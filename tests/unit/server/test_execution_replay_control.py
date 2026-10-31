"""录制控制独立于长交易，失败不回收交易 worker。"""

import threading
from multiprocessing import Pipe
from types import SimpleNamespace

from axile.server.db.models.account import Account
from axile.server.execution.worker_backend import manager, replay, worker
from axile.server.execution.worker_backend.protocol import WorkerReplayConfig, WorkerTerminationSignal


class Recorder:
    """只核验控制协议，不访问录制文件或原生实现。"""

    def __init__(self, *args):
        self.desired, self.version = False, 0
        self.owner = None
        self.calls = []

    def configure(self, enabled, version):
        self.calls.append(("configure", enabled, version))
        self.desired, self.version = enabled, version

    def attach(self, owner, context, *, supported):
        self.owner = owner

    def begin_execution(self, execution_id, symbols):
        self.calls.append(("begin", execution_id, tuple(symbols)))

    def end_execution(self, execution_id, data):
        self.calls.append(("end", execution_id, data))

    def status(self):
        return {
            "desired": self.desired,
            "state": "waiting_connection" if self.desired else "disabled",
            "config_version": self.version,
            "effective_version": self.version,
        }


def config(enabled, version):
    return WorkerReplayConfig(enabled, version, "/unused/replay", {"channel": "external"}, True)


def test_control_during_busy_trade_is_versioned_and_preserves_termination(monkeypatch):
    from axile.server.execution_replay import recorder

    monkeypatch.setattr(recorder, "ReplayRecorder", Recorder)
    state = worker._WorkerBackendState(account_id=1)
    parent, child = Pipe()
    thread = threading.Thread(target=worker._run_termination_control_loop, args=(child, state))
    thread.start()
    backend = manager.WorkerBackendManager()
    handle = manager._WorkerBackendHandle(
        1, SimpleNamespace(is_alive=lambda: True), SimpleNamespace(closed=False), parent
    )
    backend._workers[1] = handle
    account = Account(id=1, name="test", market="future", trade_channel="ctp", brokerage="test")
    selected = config(True, 5)
    monkeypatch.setattr(replay, "replay_config", lambda _: selected)
    try:
        with state.resource_lock, handle.request_lock:
            assert backend._replay_control_blocking(account, True)["effective_version"] == 5
            selected = config(False, 4)
            assert backend._replay_control_blocking(account, True)["desired"] is True
            assert state.replay_recorder.desired is True
            selected = config(False, 5)
            backend._replay_control_blocking(account, True)
            assert state.replay_recorder.desired is True
            selected = config(False, 6)
            assert backend._replay_control_blocking(account, True)["effective_version"] == 6
            parent.send(WorkerTerminationSignal("execution", "stop", None))
            backend._replay_control_blocking(account, False)
            assert state.pending_terminations["execution"].reason == "stop"
    finally:
        parent.send(None)
        thread.join(timeout=1)
        backend._workers.clear()
        parent.close()
        child.close()
    assert not thread.is_alive()


def test_disabled_control_creates_no_recorder_and_waiting_does_not_spawn(monkeypatch):
    from axile.server.execution_replay import recorder

    def forbidden(*args):
        raise AssertionError("disabled recording must not allocate a writer")

    monkeypatch.setattr(recorder, "ReplayRecorder", forbidden)
    state = worker._WorkerBackendState(account_id=1)
    assert replay.configure_worker_replay(state, config(False, 1))["state"] == "disabled"
    assert state.replay_recorder is None
    backend = manager.WorkerBackendManager()
    monkeypatch.setattr(replay, "replay_config", lambda _: config(True, 2))
    monkeypatch.setattr(backend, "_spawn_worker", forbidden)
    account = Account(id=1, name="test", market="future", trade_channel="ctp", brokerage="test")
    assert backend._replay_control_blocking(account, True)["state"] == "waiting_connection"


def test_recorder_failure_does_not_change_executor_or_connection(monkeypatch):
    from axile.server.execution_replay import recorder

    def fail(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(recorder, "ReplayRecorder", fail)
    executor = object()
    state = worker._WorkerBackendState(account_id=1, executor=executor, connected_once=True)
    assert replay.configure_worker_replay(state, config(True, 1))["state"] == "error"
    assert state.executor is executor
    assert state.connected_once


def test_enable_mid_execution_registers_carry_in_before_starting_recorder(monkeypatch):
    from axile.server.execution_replay import recorder

    monkeypatch.setattr(recorder, "ReplayRecorder", Recorder)
    executor = object()
    state = worker._WorkerBackendState(account_id=1, executor=executor, connected_once=True)
    replay.begin_worker_replay_execution(state, "execution", ("A",))
    assert state.replay_recorder is None
    replay.configure_worker_replay(state, config(True, 1))
    assert state.replay_recorder.calls[:2] == [("begin", "execution", ("A",)), ("configure", True, 1)]
    replay.end_worker_replay_execution(state, "terminated")
    assert state.replay_recorder.calls[-1] == ("end", "execution", {"ending_reason": "terminated"})
    assert state.replay_execution_id is None
    assert state.replay_recorder.owner is executor
