"""公共报撤的各个阶段固定入口录制身份，不跨段补造撤单意图。"""

from types import SimpleNamespace

from axile.executor.replay import (
    capture_command_receipt,
    capture_receipt,
    current_command_id,
    observe_receipt,
    replay_command,
    set_replay_observer,
)
from axile.executor.replay_actions import replay_operation
from tests.unit.server.test_execution_replay_recorder import eventually as wait_for


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
