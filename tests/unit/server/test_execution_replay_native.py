"""真实原生扩展和 MCAP 的隔离集成；输入为手写事实，不连接交易账户。"""

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from axile.channels import get_channel
from axile.server._native import _core
from axile.server.execution_replay.recorder import ReplayRecorder
from axile.server.execution_replay.service import replay_manifest, replay_window

TIME = 1_793_404_800_000_000
MAGIC = b"\x89MCAP0\r\n"


def wait_for(predicate):
    wake = threading.Event()
    for _ in range(300):
        if predicate():
            return
        wake.wait(0.01)
    assert predicate()


@pytest.fixture
def make_recording(tmp_path):
    assert getattr(_core, "EXECUTION_REPLAY_SCHEMA", None) == 1, _core.__file__
    recorders = []

    def create(context=None):
        now = [TIME]
        root = tmp_path / str(len(recorders))
        recorder = ReplayRecorder(
            7,
            root,
            context
            or {
                "schema_version": 1,
                "channel": "external-neutral",
                "instrument": {"X": {"tick_size": 1, "quantity_unit": "lot"}},
            },
            now_us=lambda: now[0],
        )
        recorders.append(recorder)
        recorder.attach(SimpleNamespace())
        recorder.configure(True, 1)
        wait_for(lambda: recorder.status()["state"] in {"recording", "error"})
        assert recorder.status()["state"] == "recording", recorder.status()
        return recorder, now, root, recorder.status()["recording_id"]

    yield create
    for recorder in recorders:
        recorder.close()


def drained(recorder):
    with recorder._condition:
        return recorder._session is None or not recorder._session.queue


def finished(recorder):
    wait_for(lambda: drained(recorder) or recorder.status()["state"] == "error")
    assert recorder.close()["state"] == "disabled", recorder.status()


def quote():
    return {
        "symbol": "X",
        "bids": [{"price": 99, "quantity": 3}],
        "asks": [{"price": 100, "quantity": 4}],
        "book_valid": True,
        "valid_until_us": TIME + 1_000_000,
    }


def trade(identity, quantity, **fields):
    return {
        "symbol": "X",
        "trade_key": identity,
        "identity_quality": "stable",
        "price": 100,
        "quantity": quantity,
        "direction": "BUY",
        "position_side": "NET",
        "offset": "OPEN",
        **fields,
    }


def window(root, execution_id, recording_id, start, end, cursor=None):
    request = {"recording_id": recording_id, "start_us": start, "end_us": end, "symbols": ["X"]}
    if cursor is not None:
        request["cursor"] = cursor
    return replay_window(root, 7, execution_id, request)


def assert_mcap_files(root: Path, minimum=1):
    files = list(root.rglob("*.mcap"))
    assert len(files) >= minimum
    for path in files:
        payload = path.read_bytes()
        assert payload.startswith(MAGIC) and payload.endswith(MAGIC), path
    assert not list(root.rglob("*.partial"))


def test_actual_recorder_pagination_keeps_its_sealed_view(make_recording):
    recorder, now, root, recording_id = make_recording()
    recorder.begin_execution("E1", ["X"])
    for _ in range(2_100):
        recorder.observe("/quote/X", quote())
    recorder.end_execution("E1")
    wait_for(
        lambda: (
            replay_manifest(root, 7, "E1")["recordings"] and replay_manifest(root, 7, "E1")["recordings"][0]["parts"]
        )
    )
    first = window(root, "E1", recording_id, TIME, TIME + 100)
    assert len(first["messages"]) == 2_000
    assert first["complete"] is False and first["next_cursor"]
    assert first["state_before_start"]["state_through_seq"] == 0

    now[0] += 10
    recorder.begin_execution("E2", ["X"])
    recorder.observe("/quote/X", quote())
    recorder.end_execution("E2")
    finished(recorder)

    second = window(root, "E1", recording_id, TIME, TIME + 100, first["next_cursor"])
    assert second["complete"] is True
    assert second["window_id"] == first["window_id"]
    assert "state_before_start" not in second
    messages = first["messages"] + second["messages"]
    assert [message["capture_seq"] for message in messages] == list(range(1, 2_103))
    assert all(message["time_us"] == TIME for message in messages)
    fresh = window(root, "E1", recording_id, TIME, TIME + 100)
    assert fresh["window_id"] != first["window_id"]
    assert_mcap_files(root, minimum=2)


def test_actual_checkpoint_deduplicates_trade_and_applies_late_ownership(make_recording):
    recorder, now, root, recording_id = make_recording()
    recorder.begin_execution("E1", ["X"])
    recorder.observe("/trade", trade("T1", 4, order_id=None, association="pending", source="callback"))
    now[0] += 10
    recorder.end_execution("E1")
    now[0] += 10
    recorder.observe(
        "/command",
        {
            "command_id": "C1",
            "operation": "place_order",
            "phase": "request",
            "execution_id": "E1",
            "origin": "execution",
            "symbol": "X",
        },
    )
    recorder.observe(
        "/command",
        {"command_id": "C1", "operation": "place_order", "phase": "native_request", "symbol": "X", "order_id": "O1"},
    )
    recorder.observe("/trade", trade("T1", 4, order_id="O1", association="confirmed", source="normal_query"))
    now[0] += 10
    recorder.observe("/trade", trade("T2", 2, order_id="O1", association="confirmed", source="callback"))
    now[0] += 10
    recorder.observe("/command", {"command_id": "probe", "phase": "request", "execution_id": "E1"})
    finished(recorder)

    before = window(root, "E1", recording_id, TIME + 20, TIME + 40)["state_before_start"]
    assert before["trade_accounting"]["execution_totals"]["E1"]["complete"] is False
    after = window(root, "E1", recording_id, TIME + 40, TIME + 41)["state_before_start"]
    accounting = after["trade_accounting"]
    assert accounting["own_seen_keys"] == ["T1", "T2"]
    assert accounting["execution_totals"]["E1"]["by_symbol"]["X"]["quantity"] == 6
    assert accounting["execution_totals"]["E1"]["by_symbol"]["X"]["notional"] == 600
    assert accounting["own_records"]["T1"]["first_capture_seq"] == 2
    assert accounting["own_records"]["T1"]["first_time_us"] == TIME
    assert accounting["own_records"]["T1"]["data"]["execution_id"] == "E1"
    assert not accounting["pending"]
    assert_mcap_files(root, minimum=2)


def test_actual_same_microsecond_part_boundary_and_hot_toggle(make_recording):
    recorder, now, root, first_id = make_recording()
    recorder.begin_execution("E1", ["X"])
    recorder.end_execution("E1")
    recorder.begin_execution("E2", ["X"])
    recorder.end_execution("E2")
    wait_for(lambda: drained(recorder))
    recorder.configure(False, 2)
    wait_for(lambda: recorder.status()["state"] in {"disabled", "error"})
    assert recorder.status()["state"] == "disabled", recorder.status()
    recorder.observe("/trade", trade("disabled", 100, execution_id="E1"))
    now[0] += 10
    recorder.configure(True, 3)
    wait_for(lambda: recorder.status()["state"] in {"recording", "error"})
    assert recorder.status()["state"] == "recording", recorder.status()
    second_id = recorder.status()["recording_id"]
    assert second_id != first_id
    recorder.begin_execution("E3", ["X"])
    recorder.observe("/trade", trade("T3", 2, execution_id="E3", order_id="O3", association="confirmed"))
    recorder.end_execution("E3")
    finished(recorder)

    old = window(root, "E1", first_id, TIME, TIME + 1)
    assert old["state_before_start"]["state_through_seq"] == 0
    assert [message["capture_seq"] for message in old["messages"]] == [1, 2, 3, 4]
    assert all(message["time_us"] == TIME for message in old["messages"])
    assert {item["recording_id"] for item in replay_manifest(root, 7, "E1")["recordings"]} == {first_id}
    assert {item["recording_id"] for item in replay_manifest(root, 7, "E3")["recordings"]} == {second_id}
    with pytest.raises(Exception, match="REPLAY_NOT_RECORDED"):
        window(root, "E3", first_id, TIME, TIME + 100)
    assert_mcap_files(root, minimum=3)


def test_actual_ctp_frozen_declaration_and_attributes(make_recording):
    descriptor = get_channel("ctp").descriptor
    context = descriptor.replay.model_dump(mode="json")
    context["stream_defaults"] = context.pop("streams")
    context.update(channel="ctp", instrument={"X": {"tick_size": 1, "quantity_unit": "lot"}})
    recorder, now, root, recording_id = make_recording(context)
    recorder.begin_execution("E1", ["X"])
    recorder.observe(
        "/order",
        {
            "symbol": "X",
            "order_id": "O1",
            "execution_id": "E1",
            "status": "working",
            "confirmed_book": True,
            "price": 99,
            "quantity": 2,
            "filled_quantity": 0,
            "remaining_quantity": 2,
            "direction": "BUY",
            "offset": "OPEN",
            "attributes": {"hedge_flag": "1", "status_msg": "已受理"},
            "native_identity": {"trading_day": "20261031", "front_id": 1, "session_id": 2, "order_ref": "3"},
        },
    )
    context["display"]["order"][0]["label"] = "mutated current label"
    now[0] += 1
    recorder.observe("/command", {"command_id": "probe", "execution_id": "E1", "phase": "request"})
    finished(recorder)
    page = window(root, "E1", recording_id, TIME + 1, TIME + 2)
    order = next(iter(page["state_before_start"]["orders"].values()))
    assert order["attributes"] == {"hedge_flag": "1", "status_msg": "已受理"}
    assert order["native_identity"]["front_id"] == 1
    assert page["context"]["display"]["order"][0]["label"] == descriptor.replay.display["order"][0].label
    assert_mcap_files(root)


def test_actual_manifest_for_unrecorded_root_does_not_create_directories(tmp_path):
    absent = tmp_path / "not-created"
    assert replay_manifest(absent, 7, "never-recorded")["availability"] == "not_recorded"
    assert not absent.exists()
    assert replay_manifest(tmp_path, 7, "never-recorded")["availability"] == "not_recorded"
    assert not (tmp_path / "account-7").exists()
