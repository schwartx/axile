"""查询证明在完成或退出后回收，历史查询次数不限制持续录制。"""

import threading
from copy import deepcopy

import pytest

from axile.executor import replay
from axile.server.execution_replay import recorder as module
from tests.unit.server.test_execution_replay_native import (
    TIME,
    drained,
    finished,
    wait_for,
    window,
)
from tests.unit.server.test_execution_replay_native import (
    make_recording as make_recording,
)
from tests.unit.server.test_execution_replay_recorder import eventually
from tests.unit.server.test_execution_replay_recorder import running as running


def order_set(recorder, token, scope, **fields):
    recorder.observe(
        "/order_set",
        {**token, "scope": scope, "scope_complete": True, "orders": [], **fields},
    )


def test_more_than_4096_completed_queries_keep_recording_real_mcap(make_recording):
    recorder, _, root, recording_id = make_recording()
    recorder.begin_execution("E", ["X"])
    for index in range(module.MAX_IDENTITIES + 2):
        token = recorder.begin_query(["X"])
        assert token is not None, (index, recorder.status())
        order_set(recorder, token, ["X"], synchronized=True)
        if index % 128 == 0:
            wait_for(lambda: drained(recorder))
    assert recorder.status()["state"] == "recording"
    finished(recorder)
    state = window(root, "E", recording_id, TIME + 1, TIME + 2)["state_before_start"]
    assert state["last_set_revision"]["X"] == module.MAX_IDENTITIES + 2
    assert len(list(root.rglob("*.mcap"))) >= 1


def test_more_than_4096_failed_queries_release_their_pending_proofs(running):
    recorder, _, _, _ = running
    for _ in range(module.MAX_IDENTITIES + 2):
        token = recorder.begin_query(["A"])
        assert token is not None
        recorder.finish_query(token)
    assert recorder.status()["state"] == "recording"


def test_only_completed_proofs_are_evicted_to_preserve_pending_queries(running, monkeypatch):
    recorder, _, writers, _ = running
    monkeypatch.setattr(module, "MAX_IDENTITIES", 3)
    completed = recorder.begin_query(["A"])
    order_set(recorder, completed, ["A"])
    pending = [(symbol, recorder.begin_query([symbol])) for symbol in ("B", "C", "D")]
    assert all(token is not None for _, token in pending)
    order_set(recorder, completed, ["A"])
    for symbol, token in pending:
        order_set(recorder, token, [symbol])
    recorder.close()
    proofs = [message["data"]["query_basis"] for message in writers[0].messages]
    assert proofs[1] is None
    assert proofs[2:] == [token["query_basis"] for _, token in pending]


def test_real_pending_query_limit_still_stops_recording(running, monkeypatch):
    recorder, _, _, _ = running
    monkeypatch.setattr(module, "MAX_IDENTITIES", 3)
    for _ in range(3):
        assert recorder.begin_query(["A"]) is not None
    assert recorder.begin_query(["A"]) is None
    assert recorder.status()["error"] == "replay_query_budget"


@pytest.mark.parametrize("field", ["recording_id", "request_revision", "start_seq", "sync_generation"])
def test_foreign_finish_cannot_discard_a_matching_pending_query(running, field):
    recorder, _, writers, _ = running
    token = recorder.begin_query(["A"])
    invalid = deepcopy(token)
    if field == "recording_id":
        invalid["query_basis"][field] = "other-recording"
    elif field == "sync_generation":
        invalid[field] += 1
    else:
        invalid["query_basis"][field] += 1
    recorder.finish_query(invalid)
    order_set(recorder, token, ["A"])
    recorder.close()
    assert writers[0].messages[0]["data"]["query_basis"] == token["query_basis"]


def test_missing_or_malformed_finish_is_harmless_and_does_not_consume_a_query(running):
    recorder, _, writers, _ = running
    token = recorder.begin_query(["A"])
    for invalid in (None, {}, {"query_basis": []}, {**token, "sync_generation": True}):
        recorder.finish_query(invalid)
    order_set(recorder, token, ["A"])
    recorder.close()
    assert writers[0].messages[0]["data"]["query_basis"] == token["query_basis"]


def test_finished_query_cannot_later_claim_a_complete_result(running):
    recorder, _, writers, _ = running
    finished_token = recorder.begin_query(["A"])
    recorder.finish_query(finished_token)
    current = recorder.begin_query(["A"])
    order_set(recorder, finished_token, ["A"], synchronized=True)
    order_set(recorder, current, ["A"], synchronized=True)
    recorder.close()
    assert [message["data"]["query_basis"] for message in writers[0].messages] == [
        None,
        current["query_basis"],
    ]


def test_late_completion_does_not_replace_the_newer_completed_scope_proof(running):
    recorder, _, writers, _ = running
    first, second = recorder.begin_query(["A"]), recorder.begin_query(["A"])
    order_set(recorder, second, ["A"])
    order_set(recorder, first, ["A"])
    order_set(recorder, first, ["A"])
    order_set(recorder, second, ["A"])
    recorder.close()
    assert [message["data"]["query_basis"] for message in writers[0].messages] == [
        second["query_basis"],
        first["query_basis"],
        None,
        second["query_basis"],
    ]


def test_unsynchronized_result_can_keep_its_proof_for_ready_republication(running):
    recorder, _, writers, _ = running
    token = recorder.begin_query(["A"])
    order_set(recorder, token, ["A"], synchronized=False)
    recorder.finish_query(token)
    order_set(recorder, token, ["A"], synchronized=True)
    recorder.close()
    assert [message["data"]["query_basis"] for message in writers[0].messages] == [token["query_basis"]] * 2


def test_invalid_scope_does_not_consume_a_valid_pending_proof(running):
    recorder, _, writers, _ = running
    token = recorder.begin_query(["A"])
    order_set(recorder, token, ["B"])
    order_set(recorder, token, ["A"])
    recorder.close()
    assert [message["data"]["query_basis"] for message in writers[0].messages] == [None, token["query_basis"]]


def test_finish_from_previous_recording_cannot_discard_a_reused_revision(running):
    recorder, _, writers, _ = running
    old = recorder.begin_query(["A"])
    recorder.configure(False, 2)
    eventually(lambda: recorder.status()["state"] == "disabled")
    recorder.configure(True, 3)
    eventually(lambda: recorder.status()["state"] == "recording")
    current = recorder.begin_query(["A"])
    assert current["query_basis"]["request_revision"] == old["query_basis"]["request_revision"]
    recorder.finish_query(old)
    order_set(recorder, current, ["A"])
    recorder.close()
    assert writers[-1].messages[0]["data"]["query_basis"] == current["query_basis"]


def test_old_observer_cannot_deliver_a_delayed_frame_into_a_new_recording(make_recording):
    recorder, _, root, first_id = make_recording()
    owner = recorder._owner
    old_observer = owner._replay_observer
    recorder.begin_execution("E", ["X"])
    old_observer("/order", {"symbol": "X", "order_id": "first", "status": "working"})
    captured, release = threading.Event(), threading.Event()

    def delayed_frame():
        captured.set()
        assert release.wait(3)
        old_observer("/order", {"symbol": "X", "order_id": "delayed-old", "status": "working"})

    delayed = threading.Thread(target=delayed_frame)
    delayed.start()
    try:
        assert captured.wait(1)
        recorder.configure(False, 2)
        assert owner._replay_observer is None
        replay.observe(owner, "/order", {"symbol": "X", "order_id": "disabled", "status": "working"})
        recorder.configure(True, 3)
        wait_for(lambda: recorder.status()["state"] == "recording" and recorder.status()["recording_id"] != first_id)
        current_id = recorder.status()["recording_id"]
        current_observer = owner._replay_observer
        assert current_observer is not old_observer
        recorder.attach(owner)
        assert owner._replay_observer is current_observer
        release.set()
        delayed.join(3)
        assert not delayed.is_alive()
        current_observer("/order", {"symbol": "X", "order_id": "current", "status": "working"})
        recorder.observe("/order", {"symbol": "X", "order_id": "direct", "status": "working"})
        finished(recorder)
        state = window(root, "E", current_id, TIME + 1, TIME + 2)["state_before_start"]
        assert {row["order_id"] for row in state["orders"].values()} == {"current", "direct"}
    finally:
        release.set()
        delayed.join(3)


@pytest.mark.parametrize("generation_kind", ["old", "unknown", "invalid_boolean", "future", "negative"])
def test_noncurrent_first_order_cannot_confirm_but_its_real_fill_still_counts(make_recording, generation_kind):
    recorder, now, root, recording_id = make_recording()
    owner = recorder._owner
    recorder.begin_execution("E", ["X"])
    old_generation = owner._replay_sync_generation
    now[0] += 1
    recorder.observe("/connection", {"connected": False})
    current_generation = owner._replay_sync_generation
    order = {
        "symbol": "X",
        "order_id": "O",
        "status": "working",
        "confirmed_book": True,
        "price": 100,
        "quantity": 2,
        "filled_quantity": 0,
        "remaining_quantity": 2,
        "details": {"observed": "preserved"},
    }
    supplied = {
        "old": old_generation,
        "unknown": None,
        "invalid_boolean": True,
        "future": current_generation + 1,
        "negative": -1,
    }[generation_kind]
    now[0] += 1
    recorder.observe("/order", {**order, "sync_generation": supplied})
    now[0] += 1
    recorder.observe(
        "/trade",
        {
            "symbol": "X",
            "order_id": "O",
            "trade_key": "T",
            "identity_quality": "stable",
            "price": 100,
            "quantity": 1,
            "direction": "BUY",
            "offset": "OPEN",
            "execution_id": "E",
            "origin": "execution",
            "sync_generation": old_generation,
        },
    )
    now[0] += 1
    recorder.observe("/order", {**order, "status": "partially_filled", "filled_quantity": 1, "remaining_quantity": 1})
    finished(recorder)
    after_fill = window(root, "E", recording_id, TIME + 4, TIME + 5)["state_before_start"]
    old_order = after_fill["orders"]['["X","O"]']
    assert old_order["confirmed_book"] is False
    assert old_order["details"]["observed"] == "preserved"
    if generation_kind in {"old", "unknown"}:
        assert old_order["sync_generation"] == supplied
        assert type(old_order["sync_generation"]) is type(supplied)
    else:
        assert old_order["sync_generation"] is None
        assert old_order["details"]["observed_sync_generation"] == supplied
    assert after_fill["trade_accounting"]["execution_totals"]["E"]["by_symbol"]["X"]["quantity"] == 1
    current = window(root, "E", recording_id, TIME + 5, TIME + 6)["state_before_start"]
    assert current["orders"]['["X","O"]']["confirmed_book"] is True
    assert current["orders"]['["X","O"]']["sync_generation"] == current_generation


@pytest.mark.parametrize("generation_kind", ["old", "unknown", "invalid_boolean", "future", "negative"])
def test_noncurrent_first_quote_cannot_restore_cob_but_current_quote_can(make_recording, generation_kind):
    recorder, now, root, recording_id = make_recording()
    owner = recorder._owner
    recorder.begin_execution("E", ["X"])
    old_generation = owner._replay_sync_generation
    now[0] += 1
    recorder.observe("/connection", {"connected": False})
    current_generation = owner._replay_sync_generation
    quote = {
        "symbol": "X",
        "book_valid": True,
        "bids": [{"price": 99, "quantity": 2}],
        "asks": [{"price": 100, "quantity": 3}],
        "valid_until_us": TIME + 100,
        "details": {"observed": "preserved"},
    }
    supplied = {
        "old": old_generation,
        "unknown": None,
        "invalid_boolean": True,
        "future": current_generation + 1,
        "negative": -1,
    }[generation_kind]
    now[0] += 1
    recorder.observe("/quote/X", {**quote, "sync_generation": supplied})
    now[0] += 1
    recorder.observe("/quote/X", quote)
    finished(recorder)
    before_current = window(root, "E", recording_id, TIME + 3, TIME + 4)["state_before_start"]["quotes"]["X"]
    assert before_current["available"] is False
    assert before_current["bids"] == quote["bids"] and before_current["asks"] == quote["asks"]
    assert before_current["details"]["observed"] == "preserved"
    if generation_kind in {"old", "unknown"}:
        assert before_current["sync_generation"] == supplied
        assert type(before_current["sync_generation"]) is type(supplied)
    else:
        assert before_current["sync_generation"] is None
        assert before_current["details"]["observed_sync_generation"] == supplied
    after_current = window(root, "E", recording_id, TIME + 4, TIME + 5)["state_before_start"]["quotes"]["X"]
    assert after_current["book_valid"] is True
    assert after_current["sync_generation"] == current_generation


@pytest.mark.parametrize("topic", ["/order_set", "/connection"])
def test_future_control_generation_cannot_block_a_later_current_order(make_recording, topic):
    recorder, now, root, recording_id = make_recording()
    recorder.begin_execution("E", ["X"])
    current_generation = recorder._owner._replay_sync_generation
    data = {
        "sync_generation": current_generation + 100,
        "scope": {"kind": "symbols", "symbols": ["X"]},
        "details": {"observed": "preserved"} if topic == "/order_set" else "preserved source detail",
    }
    if topic == "/order_set":
        data.update(scope_complete=True, synchronized=True, query_basis=None, orders=[])
    else:
        data.update(status="healthy", connected=True, available=True)
    now[0] += 1
    recorder.observe(topic, data)
    now[0] += 1
    recorder.observe("/order", {"symbol": "X", "order_id": "current", "status": "working", "confirmed_book": True})
    finished(recorder)
    state = window(root, "E", recording_id, TIME + 3, TIME + 4)["state_before_start"]
    assert state["sync_generation"]["X"] == current_generation
    assert state["orders"]['["X","current"]']["confirmed_book"] is True
    message = next(
        message for message in window(root, "E", recording_id, TIME, TIME + 3)["messages"] if message["topic"] == topic
    )
    assert message["data"]["sync_generation"] is None
    preserved = {"observed": "preserved"} if topic == "/order_set" else {"source_details": "preserved source detail"}
    assert message["data"]["details"] == {**preserved, "observed_sync_generation": current_generation + 100}
