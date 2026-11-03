"""录制保持开启的 worker 交接；控制管道与 MCAP 均使用真实实现。"""

import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from axile.executor.replay import capture_receipt, observe_receipt
from axile.server._native import _core
from axile.server.execution_replay import recorder as recorder_module
from axile.server.execution_replay.recorder import ReplayRecorder
from axile.server.execution_replay.service import replay_window
from tests.unit.server.test_execution_replay_control_delivery import eventually
from tests.unit.server.test_execution_replay_control_delivery import runtime as runtime
from tests.unit.server.test_execution_replay_recorder import FakeWriter
from tests.unit.server.test_execution_replay_retiring_worker import retiring


def test_on_worker_handoff_resumes_without_a_new_configuration_version(runtime, monkeypatch):
    created = []

    def spawn(account_id):
        created.append(runtime.spawn())
        return created[-1].handle

    monkeypatch.setattr(runtime.backend, "_spawn_worker", spawn)
    try:
        with retiring(runtime):
            runtime.backend._get_or_create_worker(runtime.account.id)
            latest = created[-1]
            result = runtime.backend._replay_control_blocking(runtime.account, True)
            eventually(lambda: latest.state.replay_recorder is not None)
            recorder = latest.state.replay_recorder
            eventually(lambda: recorder.status().get("reason") is not None or recorder.status()["state"] == "error")
            pending = recorder.status()
            assert pending["state"] == "starting", pending
            assert pending["effective_version"] is None
            assert "error" not in pending
            assert result["desired"] is True and result["effective_version"] is None
            identity = pending["recording_id"]
            recorder.begin_execution("handoff", ["X"])
            recorder.observe("/command", {"command_id": "queued", "operation": "place_order", "phase": "request"})
        eventually(lambda: recorder.status()["state"] == "recording")
        recovered = runtime.backend._replay_control_blocking(runtime.account, False)
        assert recovered["state"] == "recording"
        assert recovered["effective_version"] == runtime.account.replay_config_version
        assert recorder.status()["recording_id"] == identity
        recorder.end_execution("handoff")
        assert recorder.close()["state"] == "disabled"
        page = replay_window(
            runtime.root,
            runtime.account.id,
            "handoff",
            {
                "recording_id": identity,
                "start_us": 0,
                "end_us": 2**53 - 1,
            },
        )
        assert any(message["data"].get("command_id") == "queued" for message in page["messages"])
        assert len({message["capture_seq"] for message in page["messages"]}) == len(page["messages"])
    finally:
        # 替身 process 始终存活，测试结束不将其留给 manager 的 atexit 关闭。
        runtime.backend._retiring_workers.clear()


@pytest.mark.parametrize("release", ["finish", "crash"])
def test_real_process_writer_lock_handoff(tmp_path, release):
    child = """
import json,sys
sys.path.insert(0,sys.argv[2])
from axile.server._native import _core
w=_core.ExecutionReplayWriter(sys.argv[1],7,'old','{"schema_version":1}')
w.append(json.dumps({'schema_version':1,'recording_id':'old','capture_seq':1,'time_us':1,
                    'topic':'/execution','data':{'execution_id':'old','phase':'start'}}))
print('ready',flush=True)
input()
w.finish()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", child, str(tmp_path), str(Path(__file__).parents[3])],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    recorder = ReplayRecorder(7, tmp_path, now_us=lambda: 100)
    try:
        assert process.stdout.readline().strip() == "ready"
        recorder.attach(SimpleNamespace())
        recorder.configure(True, 1)
        eventually(lambda: recorder.status().get("reason") is not None or recorder.status()["state"] == "error")
        assert recorder.status()["state"] == "starting"
        recorder.begin_execution("new", ["X"])
        recorder.observe("/command", {"command_id": "queued", "phase": "request"})
        identity = recorder.status()["recording_id"]
        if release == "finish":
            process.communicate("release\n", timeout=3)
        else:
            process.kill()
            process.communicate(timeout=3)
        eventually(lambda: recorder.status()["state"] == "recording")
        with pytest.raises(BlockingIOError):
            recorder_module.native_writer(str(tmp_path), 7, "third", '{"schema_version":1}')
        recorder.end_execution("new")
        assert recorder.close()["state"] == "disabled"
        page = replay_window(tmp_path, 7, "new", {"recording_id": identity, "start_us": 0, "end_us": 101})
        assert any(message["data"].get("command_id") == "queued" for message in page["messages"])
    finally:
        recorder.close()
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=3)


@pytest.mark.parametrize("action", ["off", "disconnect"])
def test_starting_cancel_before_first_writer_attempt(tmp_path, action):
    entered, release = threading.Event(), threading.Event()
    attempts = []

    def metadata():
        entered.set()
        assert release.wait(2)
        return {"environment": "demo"}

    recorder = ReplayRecorder(7, tmp_path, writer_factory=lambda *_: attempts.append(True))
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
        assert not session.queue and session.queued_bytes == 0
        assert not attempts and not list(tmp_path.iterdir())
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


@pytest.mark.parametrize("switch", ["off_on", "resource"])
def test_waiting_segment_cannot_capture_into_its_replacement(tmp_path, switch):
    held = _core.ExecutionReplayWriter(str(tmp_path), 7, "held", '{"schema_version":1}')
    owner = SimpleNamespace()
    recorder = ReplayRecorder(7, tmp_path, now_us=lambda: 100)
    try:
        recorder.attach(owner)
        recorder.configure(True, 1)
        eventually(lambda: recorder.status().get("reason") is not None)
        previous = recorder.status()["recording_id"]
        receipt = capture_receipt(owner)
        recorder.begin_execution("E", ["X"])
        recorder.observe("/command", {"command_id": "old-queued", "phase": "request"})
        if switch == "off_on":
            recorder.configure(False, 2)
            eventually(lambda: recorder.status()["state"] == "disabled")
            recorder.configure(True, 3)
        else:
            recorder.attach(SimpleNamespace(), {"channel": "fresh"})
        eventually(
            lambda: recorder.status().get("recording_id") != previous and recorder.status().get("reason") is not None
        )
        identity = recorder.status()["recording_id"]
        observe_receipt(receipt, "/command", {"command_id": "late-old", "phase": "returned"})
        held.finish()
        eventually(lambda: recorder.status()["state"] == "recording")
        recorder.observe("/command", {"command_id": "fresh", "phase": "request"})
        recorder.end_execution("E")
        recorder.close()
        page = replay_window(tmp_path, 7, "E", {"recording_id": identity, "start_us": 0, "end_us": 101})
        assert [message["data"]["command_id"] for message in page["messages"] if message["topic"] == "/command"] == [
            "fresh"
        ]
    finally:
        held.finish()
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
