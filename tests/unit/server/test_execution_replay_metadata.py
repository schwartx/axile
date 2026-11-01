"""录制元数据白名单及历史上下文保持不可变，使用真实 MCAP。"""

import json
from types import SimpleNamespace

import pytest

from axile.executor.tq.tq_execute import TQExecutor
from axile.server.execution_replay.recorder import ReplayRecorder
from axile.server.execution_replay.service import replay_manifest
from tests.unit.server.test_execution_replay_native import wait_for


@pytest.mark.parametrize("mode", ["live", "kq", "sim"])
def test_account_mode_is_frozen_in_manifest_and_old_recording_survives_config_change(tmp_path, mode):
    config = SimpleNamespace(account_mode=mode, tq_password="never-copy")
    owner = SimpleNamespace(account_config=config, _config=lambda: config)
    calls = []

    def metadata():
        calls.append(config.account_mode)
        return TQExecutor.replay_context_metadata(owner)

    owner.replay_context_metadata = metadata
    now = [1_700_000_000_000_000]
    recorder = ReplayRecorder(7, tmp_path, {"channel": "tq"}, now_us=lambda: now[0])
    try:
        recorder.attach(owner)
        recorder.configure(True, 1)
        wait_for(lambda: recorder.status()["state"] == "recording")
        recorder.begin_execution("old", ["rb2610"])
        now[0] += 1
        recorder.end_execution("old")
        recorder.configure(False, 2)
        wait_for(lambda: recorder._session is None)
        old = replay_manifest(tmp_path, 7, "old")["recordings"][0]["context"]
        assert old["account_mode"] == mode
        assert old["environment"] == ("live" if mode == "live" else "demo")
        assert "never-copy" not in json.dumps(old)

        next_mode = "sim" if mode != "sim" else "live"
        config.account_mode = next_mode
        recorder.configure(True, 3)
        wait_for(lambda: recorder.status()["state"] == "recording")
        recorder.begin_execution("new", ["rb2610"])
        now[0] += 1
        recorder.end_execution("new")
        assert recorder.close()["state"] == "disabled", recorder.status()
        assert replay_manifest(tmp_path, 7, "old")["recordings"][0]["context"] == old
        assert replay_manifest(tmp_path, 7, "new")["recordings"][0]["context"]["account_mode"] == next_mode
        assert calls == [mode, next_mode]
    finally:
        recorder.close()


@pytest.mark.parametrize(
    "metadata",
    [
        {"environment": "demo", "account_mode": "unknown-mode"},
        {"environment": "demo", "account_mode": "sim", "password": "never-copy"},
    ],
)
def test_invalid_mode_metadata_is_not_frozen(tmp_path, metadata):
    recorder = ReplayRecorder(7, tmp_path, {"channel": "tq"})
    try:
        recorder.attach(SimpleNamespace(replay_context_metadata=lambda: metadata))
        recorder.configure(True, 1)
        wait_for(lambda: recorder.status()["state"] == "recording")
        recorder.begin_execution("E", ["rb2610"])
        recorder.end_execution("E")
        assert recorder.close()["state"] == "disabled", recorder.status()
        context = replay_manifest(tmp_path, 7, "E")["recordings"][0]["context"]
        assert context["environment"] == "unknown" and "account_mode" not in context
        assert "never-copy" not in json.dumps(context)
    finally:
        recorder.close()
