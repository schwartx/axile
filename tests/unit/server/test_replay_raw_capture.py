"""The recorder keeps direct evidence and finite queues, not trade projections."""

import json
import threading
from types import SimpleNamespace

import pytest

from axile.server.execution_replay import recorder as module
from axile.server.execution_replay.contracts import MAX_FREEZE_BYTES, MAX_FREEZE_NODES, freeze_json


class Writer:
    def __init__(self):
        self.messages = []
        self.gate = threading.Event()
        self.gate.set()

    def append(self, text):
        assert self.gate.wait(2)
        self.messages.append(json.loads(text))

    def seal(self):
        pass

    def poll(self, timestamp):
        pass

    def finish(self):
        pass


def start(tmp_path):
    writer = Writer()
    recorder = module.ReplayRecorder(7, tmp_path, writer_factory=lambda *args: writer, now_us=lambda: 1_000_000)
    owner = SimpleNamespace()
    recorder.attach(owner)
    recorder.configure(True, 1)
    return recorder, writer


def test_observations_are_not_merged_and_capture_start_precedes_quote(tmp_path):
    recorder, writer = start(tmp_path)
    recorder.begin_execution("E", ["S"])
    recorder.observe(
        "/trade",
        {
            "symbol": "S",
            "trade_key": "T",
            "identity_quality": "stable",
            "order_id": "O",
            "execution_id": "E",
            "price": 1,
        },
    )
    recorder.observe("/trade", {"symbol": "S", "trade_key": "T", "identity_quality": "stable", "quantity": 2})
    recorder.observe("/quote/S", {"symbol": "S", "bids": [[1, 1]], "asks": [[2, 1]]})
    recorder.close(timeout=2)
    trades = [event for event in writer.messages if event["topic"] == "/trade"]
    assert len(trades) == 2
    assert "price" not in trades[1]["data"] and "execution_id" not in trades[1]["data"]
    topics = [event["topic"] for event in writer.messages]
    assert topics.index("/capture") < topics.index("/quote/S")
    assert all(event["schema_version"] == 2 for event in writer.messages)


def test_capture_overload_stops_recording_without_waiting_for_writer(tmp_path, monkeypatch):
    recorder, writer = start(tmp_path)
    writer.gate.clear()
    monkeypatch.setattr(module, "MAX_QUEUE_BYTES", 600)
    for _ in range(10):
        recorder.observe("/trade", {"symbol": "S", "quantity": 1})
    assert recorder.status()["error"] == "replay_capture_budget"
    assert recorder._session.queue_bytes <= 600
    writer.gate.set()
    recorder.close(timeout=2)


def test_outstanding_query_metadata_is_bounded(tmp_path, monkeypatch):
    recorder, _ = start(tmp_path)
    monkeypatch.setattr(module, "MAX_IDENTITIES", 2)
    assert recorder.begin_query(["S"])
    assert recorder.begin_query(["S"])
    assert recorder.begin_query(["S"]) is None
    assert recorder.status()["error"] == "replay_query_budget"
    recorder.close(timeout=2)


def test_freeze_rejects_oversize_before_copying_whole_observation():
    with pytest.raises(ValueError, match="replay_structure_budget"):
        freeze_json([{}] * MAX_FREEZE_NODES)
    with pytest.raises(ValueError, match="replay_capture_budget"):
        freeze_json("x" * (MAX_FREEZE_BYTES + 1))
