"""公共报撤的各个阶段固定入口录制身份，不跨段补造撤单意图。"""

import json
import threading
from types import SimpleNamespace

import pytest

from axile.executor.replay import (
    capture_command_receipt,
    capture_receipt,
    current_command_id,
    observe_receipt,
    replay_command,
    set_replay_observer,
)
from axile.executor.replay_actions import replay_operation
from tests.unit.server.test_execution_replay_native import TIME, finished, wait_for, window
from tests.unit.server.test_execution_replay_native import make_recording as make_recording


class Operations:
    @replay_operation("cancel_order")
    def cancel_order(self, symbol, order_id):
        return self.operation_body()


def working(recorder):
    recorder.observe(
        "/order",
        {
            "symbol": "X",
            "order_id": "O",
            "execution_id": "E",
            "status": "working",
            "confirmed_book": True,
            "price": 100,
            "quantity": 1,
            "remaining_quantity": 1,
        },
    )


def switch_recording(recorder, first_id):
    recorder.configure(False, 2)
    wait_for(lambda: recorder.status()["state"] == "disabled")
    recorder.configure(True, 3)
    wait_for(lambda: recorder.status()["state"] == "recording" and recorder.status()["recording_id"] != first_id)
    return recorder.status()["recording_id"]


@pytest.mark.parametrize("fails", [False, True])
def test_blocked_operation_return_or_error_never_enters_a_new_recording(make_recording, fails):
    recorder, now, root, first_id = make_recording()
    owner = recorder._owner
    recorder.begin_execution("E", ["X"])
    entered, release = threading.Event(), threading.Event()
    outcome = {}

    def body():
        entered.set()
        assert release.wait(3)
        if fails:
            raise ValueError("private business failure")
        return True

    def run():
        try:
            outcome["result"] = Operations.cancel_order(owner, "X", "O")
        except BaseException as error:
            outcome["error"] = error

    owner.operation_body = body
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(1)
        recording_id = switch_recording(recorder, first_id)
        now[0] += 1
        working(recorder)
        release.set()
        thread.join(3)
        assert not thread.is_alive()
        if fails:
            assert type(outcome["error"]) is ValueError
            assert str(outcome["error"]) == "private business failure"
        else:
            assert outcome["result"] is True
        now[0] += 1
        recorder.end_execution("E")
        finished(recorder)
        page = window(root, "E", recording_id, TIME, now[0] + 1)
        assert [event for event in page["messages"] if event["topic"] == "/command"] == []
        state = window(root, "E", recording_id, now[0] + 1, now[0] + 2)["state_before_start"]
        assert state["orders"]['["X","O"]']["cancel_pending"] is False
        assert state["orders"]['["X","O"]']["confirmed_book"] is True
    finally:
        release.set()
        thread.join(3)


def test_wrapper_pins_identity_before_action_context_can_switch_recording(make_recording):
    recorder, now, root, first_id = make_recording()
    owner = recorder._owner
    recorder.begin_execution("E", ["X"])
    selected = {}

    def get_guard():
        selected["recording_id"] = switch_recording(recorder, first_id)
        return None

    owner.get_account_control_guard = get_guard
    owner.operation_body = lambda: True
    assert Operations.cancel_order(owner, "X", "O") is True
    now[0] += 1
    working(recorder)
    recorder.end_execution("E")
    finished(recorder)
    page = window(root, "E", selected["recording_id"], TIME, now[0] + 1)
    assert not any(event["topic"] == "/command" for event in page["messages"])


@pytest.mark.parametrize("entry", ["context", "operation"])
def test_disabled_entry_does_not_sample_a_later_enabled_recording(make_recording, entry):
    recorder, now, root, _ = make_recording()
    owner = recorder._owner
    recorder.begin_execution("E", ["X"])
    recorder.configure(False, 2)
    wait_for(lambda: recorder.status()["state"] == "disabled")

    def enable():
        recorder.configure(True, 3)
        wait_for(lambda: recorder.status()["state"] == "recording")
        return True

    if entry == "context":
        with replay_command(owner, "cancel_order", {"symbol": "X", "order_id": "O"}) as identity:
            assert identity is None
            assert enable() is True
    else:
        owner.operation_body = enable
        assert Operations.cancel_order(owner, "X", "O") is True
    recording_id = recorder.status()["recording_id"]
    now[0] += 1
    working(recorder)
    recorder.end_execution("E")
    finished(recorder)
    page = window(root, "E", recording_id, TIME, now[0] + 1)
    assert not any(event["topic"] == "/command" for event in page["messages"])


@pytest.mark.parametrize("fails", [False, True])
def test_same_recording_keeps_original_generation_and_real_command_outcome(make_recording, fails):
    recorder, now, root, recording_id = make_recording()
    owner = recorder._owner
    recorder.begin_execution("E", ["X"])
    working(recorder)
    original_generation = owner._replay_sync_generation

    def body():
        now[0] += 1
        recorder.observe("/connection", {"connected": False})
        working(recorder)
        if fails:
            raise ValueError("private business failure")
        return True

    owner.operation_body = body
    if fails:
        with pytest.raises(ValueError, match="private business failure"):
            Operations.cancel_order(owner, "X", "O")
    else:
        assert Operations.cancel_order(owner, "X", "O") is True
    assert current_command_id() is None
    assert owner._replay_sync_generation == original_generation + 1
    now[0] += 1
    recorder.end_execution("E")
    finished(recorder)
    page = window(root, "E", recording_id, TIME, now[0] + 1)
    commands = [event["data"] for event in page["messages"] if event["topic"] == "/command"]
    assert {event["sync_generation"] for event in commands} == {original_generation}
    assert [event["phase"] for event in commands] == (
        ["request", "error"] if fails else ["request", "returned", "result"]
    )
    assert "private business failure" not in json.dumps(commands)
    state = window(root, "E", recording_id, now[0] + 1, now[0] + 2)["state_before_start"]
    assert state["orders"]['["X","O"]']["cancel_pending"] is (not fails)
    assert state["orders"]['["X","O"]']["confirmed_book"] is True


def test_new_operation_after_enable_still_records_all_three_stages(make_recording):
    recorder, now, root, recording_id = make_recording()
    owner = recorder._owner
    generation = owner._replay_sync_generation
    recorder.begin_execution("E", ["X"])
    working(recorder)
    owner.operation_body = lambda: True
    assert Operations.cancel_order(owner, "X", "O") is True
    now[0] += 1
    recorder.end_execution("E")
    finished(recorder)
    commands = [
        event["data"]
        for event in window(root, "E", recording_id, TIME, now[0] + 1)["messages"]
        if event["topic"] == "/command"
    ]
    assert [event["phase"] for event in commands] == ["request", "returned", "result"]
    assert len({event["command_id"] for event in commands}) == 1
    assert {event["sync_generation"] for event in commands} == {generation}
    state = window(root, "E", recording_id, now[0] + 1, now[0] + 2)["state_before_start"]
    assert state["orders"]['["X","O"]']["cancel_pending"] is True


def test_explicit_disabled_receipt_is_not_resampled_and_unknown_generation_stays_unknown():
    observed = []
    owner = SimpleNamespace(_replay_observer=lambda topic, data: observed.append(dict(data)))
    with replay_command(owner, "cancel_order", {"symbol": "X", "order_id": "O"}, receipt=None) as identity:
        assert identity is None
    observe_receipt(None, "/command", {"phase": "result"})
    assert observed == []
    receipt = capture_receipt(owner)
    owner._replay_sync_generation = 9
    observe_receipt(receipt, "/command", {"phase": "result"})
    assert observed == [{"phase": "result", "sync_generation": None}]


def test_direct_context_keeps_its_observer_and_generation_after_owner_changes():
    first, second = [], []
    owner = SimpleNamespace(_replay_sync_generation=1)
    set_replay_observer(owner, lambda topic, data: first.append(dict(data)))
    with replay_command(owner, "cancel_order", {"symbol": "X", "order_id": "O"}) as identity:
        assert identity is not None
        set_replay_observer(owner, lambda topic, data: second.append(dict(data)))
        owner._replay_sync_generation = 2
    assert [event["phase"] for event in first] == ["request", "returned"]
    assert {event["sync_generation"] for event in first} == {1}
    assert second == []


def test_command_receipt_distinguishes_no_context_from_a_disabled_nested_operation():
    owner = SimpleNamespace(_replay_observer=lambda *_args: None, _replay_sync_generation=1)
    original = capture_receipt(owner)
    assert capture_command_receipt(owner) == original
    with replay_command(owner, "cancel_order", {}, receipt=original) as identity:
        owner._replay_observer = lambda *_args: None
        owner._replay_sync_generation = 2
        assert capture_command_receipt(owner) == original
        with replay_command(owner, "cancel_order", {}, receipt=None):
            assert current_command_id() is None
            assert capture_command_receipt(owner) is None
        assert current_command_id() == identity
        assert capture_command_receipt(owner) == original
    assert capture_command_receipt(owner) == capture_receipt(owner)
    assert capture_command_receipt(owner) != original
