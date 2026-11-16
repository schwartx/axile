"""录制保持开启的 worker 交接；控制管道与 MCAP 均使用真实实现。"""

import threading
from types import SimpleNamespace

import pytest

from axile.server._native import _core
from axile.server.execution_replay.recorder import ReplayRecorder
from tests.unit.server.test_execution_replay_control_delivery import eventually
from tests.unit.server.test_execution_replay_control_delivery import runtime as runtime
from tests.unit.server.test_execution_replay_recorder import FakeWriter


@pytest.mark.parametrize("action", ["off", "disconnect"])
def test_starting_cancel_before_first_writer_attempt(tmp_path, action):
    entered, release = threading.Event(), threading.Event()
    writer = FakeWriter()

    def metadata():
        entered.set()
        assert release.wait(2)
        return {"environment": "demo"}

    recorder = ReplayRecorder(7, tmp_path, writer_factory=lambda *_: writer)
    recorder.attach(SimpleNamespace(replay_context_metadata=metadata))
    recorder.configure(True, 1)
    try:
        assert entered.wait(1)
        assert recorder.status()["state"] == "starting"
        session = recorder._session
        recorder.begin_execution("canceled", ["X"])
        assert session.queue
        if action == "off":
            recorder.configure(False, 2)
        else:
            recorder.attach(None)
        assert recorder.status()["state"] == "closing"
        release.set()
        session.thread.join(1)
        assert not session.thread.is_alive()
        assert recorder.status()["state"] == ("disabled" if action == "off" else "waiting_connection")
        assert not session.queue
        assert writer.finished.is_set()
        assert any(message["data"].get("execution_id") == "canceled" for message in writer.messages)
    finally:
        release.set()
        recorder.close()


@pytest.mark.parametrize("action", ["off", "disconnect", "close"])
def test_waiting_handoff_is_cancelable(tmp_path, action):
    attempts = []
    entered = threading.Event()

    def busy(*_args):
        attempts.append(True)
        entered.set()
        raise BlockingIOError("replay_writer_busy")

    recorder = ReplayRecorder(7, tmp_path, writer_factory=busy)
    recorder.attach(SimpleNamespace())
    recorder.configure(True, 1)
    try:
        assert entered.wait(1)
        eventually(lambda: recorder.status().get("reason") is not None)
        waiting_thread = recorder._session.thread
        if action == "off":
            recorder.configure(False, 2)
        elif action == "disconnect":
            recorder.attach(None)
        elif action == "close":
            recorder.close()
        eventually(lambda: not waiting_thread.is_alive())
        state = recorder.status()
        assert (
            state["state"]
            == {
                "off": "disabled",
                "disconnect": "waiting_connection",
                "close": "disabled",
            }[action]
        )
        assert "reason" not in state
        assert not list(tmp_path.iterdir())
    finally:
        recorder.close()


@pytest.mark.parametrize("failure", ["native_io", "no_writer"])
def test_native_failure_is_not_retried_as_handoff(tmp_path, monkeypatch, failure):
    attempts = []

    def fail(*_args):
        attempts.append(True)
        if failure == "no_writer":
            return None
        raise _core.NativeError("REPLAY_IO", "isolated I/O error")

    monkeypatch.setattr(_core, "ExecutionReplayWriter", fail)
    recorder = ReplayRecorder(7, tmp_path)
    recorder.attach(SimpleNamespace())
    recorder.configure(True, 1)
    try:
        eventually(lambda: recorder.status()["state"] == "error")
        assert recorder.status()["error"] == "replay_writer_error"
        assert len(attempts) == 1
    finally:
        recorder.close()


def test_off_during_writer_creation_is_not_acknowledged_before_drain(tmp_path):
    entered, open_writer, finishing, finish_writer = (threading.Event() for _ in range(4))
    writer = FakeWriter()

    def factory(*_args):
        entered.set()
        assert open_writer.wait(3)
        return writer

    def finish():
        finishing.set()
        assert finish_writer.wait(3)

    writer.finish = finish
    recorder = ReplayRecorder(7, tmp_path, writer_factory=factory)
    recorder.attach(SimpleNamespace())
    recorder.configure(True, 1)
    try:
        assert entered.wait(1)
        recorder.configure(False, 2)
        open_writer.set()
        assert finishing.wait(1)
        pending = recorder.status()
        assert pending["state"] == "closing"
        assert pending["effective_version"] != 2
        finish_writer.set()
        eventually(lambda: recorder.status()["state"] == "disabled")
        assert recorder.status()["effective_version"] == 2
    finally:
        open_writer.set()
        finish_writer.set()
        recorder.close()
