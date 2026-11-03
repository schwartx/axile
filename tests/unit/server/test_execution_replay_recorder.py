"""纯 Python 采集边界；fake writer 不代替真实 MCAP 验收。"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from axile.executor.replay import (
    begin_query,
    current_command_id,
    observe,
    replay_command,
    replay_enabled,
    set_replay_observer,
)
from axile.server.execution_replay import recorder as module
from axile.server.execution_replay.contracts import DATA_FIELDS, freeze_data
from axile.server.execution_replay.recorder import ReplayRecorder


class FakeWriter:
    def __init__(self, *_args):
        self.messages = []
        self.seals = 0
        self.finished = threading.Event()
        self.appended = threading.Event()
        self.gate = None

    def append(self, value):
        if self.gate is not None:
            self.gate.wait(2)
        self.messages.append(json.loads(value))
        self.appended.set()

    def seal(self):
        self.seals += 1

    def poll(self, _now):
        pass

    def finish(self):
        self.finished.set()


def eventually(predicate):
    wake = threading.Event()
    for _ in range(100):
        if predicate():
            return
        wake.wait(0.01)
    assert predicate()


@pytest.fixture
def running(tmp_path):
    writers = []
    now = [1_000_000]

    def factory(*args):
        writer = FakeWriter(*args)
        writers.append(writer)
        return writer

    recorder = ReplayRecorder(7, tmp_path, writer_factory=factory, now_us=lambda: now[0])
    owner = SimpleNamespace()
    recorder.attach(owner)
    recorder.configure(True, 1)
    eventually(lambda: recorder.status()["state"] == "recording")
    yield recorder, owner, writers, now
    recorder.close()


def test_disabled_and_waiting_create_no_writer_or_thread(tmp_path):
    created = []
    recorder = ReplayRecorder(7, tmp_path, writer_factory=lambda *_: created.append(True))
    owner = SimpleNamespace()
    recorder.attach(owner)
    assert not replay_enabled(owner)
    recorder.observe("/trade", {"api_key": "do not retain"})
    assert recorder._session is None
    assert recorder.status()["state"] == "disabled"
    recorder.attach(None)
    assert recorder.configure(True, 1)["state"] == "waiting_connection"
    assert recorder._session is None
    assert not created and not list(tmp_path.iterdir())
    recorder.close()


def test_native_failure_does_not_escape_or_retry(tmp_path):
    def fail(*_args):
        raise RuntimeError("private native failure text")

    recorder = ReplayRecorder(7, tmp_path, writer_factory=fail)
    recorder.attach(SimpleNamespace())
    recorder.configure(True, 1)
    eventually(lambda: recorder.status()["state"] == "error")
    assert recorder.status()["error"] == "replay_writer_error"
    recorder.observe("/command", {"phase": "request"})
    recorder.close()


def test_low_disk_space_is_reported_without_affecting_the_account(tmp_path):
    def fail(*_args):
        raise RuntimeError("REPLAY_DISK_SPACE", "private storage details")

    recorder = ReplayRecorder(7, tmp_path, writer_factory=fail)
    recorder.attach(SimpleNamespace())
    recorder.configure(True, 1)
    eventually(lambda: recorder.status()["state"] == "error")
    assert recorder.status()["error"] == "replay_disk_space_low"
    recorder.observe("/command", {"phase": "request"})
    recorder.close()


def test_frozen_payload_is_not_changed_by_later_callback_mutation(running):
    recorder, _, writers, _ = running
    original = {"phase": "request", "actual_parameters": {"price": 12, "quantity": 2}}
    recorder.observe("/command", original)
    original["actual_parameters"]["price"] = 999
    eventually(lambda: bool(writers[0].messages))
    message = writers[0].messages[0]
    assert message["data"]["actual_parameters"]["price"] == 12
    assert message["schema_version"] == 1
    assert message["time_us"] == 1_000_000


def test_concurrent_sources_share_one_sequence_without_reordering(running):
    recorder, _, writers, _ = running
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda number: recorder.observe("/command", {"request_id": str(number)}), range(40)))
    eventually(lambda: len(writers[0].messages) == 40)
    messages = writers[0].messages
    assert [message["capture_seq"] for message in messages] == list(range(1, 41))
    assert {message["time_us"] for message in messages} == {1_000_000}


@pytest.mark.parametrize(
    "data,code",
    [
        ({"price": float("nan")}, "replay_nonfinite_number"),
        ({"price": object()}, "replay_non_json_value"),
        ({"actual_parameters": {"password": "secret"}}, "replay_forbidden_field"),
        ({"extra": {}}, "replay_unknown_data_field"),
    ],
)
def test_invalid_or_sensitive_input_stops_only_recorder(running, data, code):
    recorder, owner, writers, _ = running
    observe(owner, "/command", data)
    assert recorder.status()["error"] == code
    assert not replay_enabled(owner)
    eventually(lambda: recorder._session is None)
    assert writers[0].finished.is_set()


def test_missing_quality_and_declared_false_zero_survive():
    payload = {
        "direction": None,
        "quantity": 0,
        "confirmed_book": False,
        "field_quality": {"direction": "missing", "quantity": "known"},
        "attributes": {"non_private_flag": False},
    }
    assert freeze_data("/order", payload, {"attributes": {"order": ["non_private_flag"]}}) == payload
    with pytest.raises(ValueError, match="undeclared_extension"):
        freeze_data("/order", payload, {})


def test_clock_rollback_stops_new_messages(running):
    recorder, _, writers, now = running
    recorder.observe("/command", {"phase": "request"})
    now[0] -= 1
    recorder.observe("/command", {"phase": "returned"})
    assert recorder.status()["error"] == "replay_clock_rollback"
    assert all(message["capture_seq"] == 1 for message in writers[0].messages)


def test_queue_accepts_more_than_4096_messages_without_waiting_for_native(tmp_path):
    gate = threading.Event()
    entered = threading.Event()
    writer = FakeWriter()

    def factory(*_args):
        entered.set()
        gate.wait(2)
        return writer

    recorder = ReplayRecorder(7, tmp_path, writer_factory=factory)
    recorder.attach(SimpleNamespace())
    recorder.configure(True, 1)
    assert entered.wait(1)
    try:
        for _ in range(5_000):
            recorder.observe("/command", {"phase": "request"})
        assert len(recorder._session.queue) == 5_000
        assert recorder.status()["state"] != "error"
    finally:
        gate.set()
        recorder.close(timeout=10)
    assert len(writer.messages) == 5_000


def test_queue_accepts_more_than_16_mib_without_stopping(tmp_path):
    gate = threading.Event()
    entered = threading.Event()
    writer = FakeWriter()

    def factory(*_args):
        entered.set()
        gate.wait(10)
        return writer

    recorder = ReplayRecorder(7, tmp_path, writer_factory=factory)
    recorder.attach(SimpleNamespace())
    recorder.configure(True, 1)
    assert entered.wait(1)
    try:
        for _ in range(2_200):
            recorder.observe("/command", {"phase": "request", "source": "X" * 8_000})
        assert len(recorder._session.queue) == 2_200
        assert recorder.status()["state"] != "error"
    finally:
        gate.set()
        recorder.close(timeout=10)
    assert len(writer.messages) == 2_200


def test_disable_returns_while_native_append_is_blocked(running):
    recorder, owner, writers, _ = running
    gate = threading.Event()
    writers[0].gate = gate
    recorder.observe("/command", {"phase": "request"})
    assert recorder.configure(False, 2)["state"] == "closing"
    assert not replay_enabled(owner)
    assert recorder.configure(True, 1)["desired"] is False
    gate.set()
    eventually(lambda: recorder.status()["state"] == "disabled")
    assert writers[0].finished.is_set()


def test_attach_none_unbinds_and_waits_for_normal_connection(running):
    recorder, owner, writers, _ = running
    recorder.attach(None)
    assert not replay_enabled(owner)
    assert not hasattr(owner, "_replay_account_id")
    eventually(lambda: recorder.status()["state"] == "waiting_connection")
    replacement = SimpleNamespace()
    recorder.attach(replacement)
    eventually(lambda: len(writers) == 2 and recorder.status()["state"] == "recording")
    assert replay_enabled(replacement)
    assert replacement._replay_account_id == 7


def test_related_symbols_share_execution_tail_and_duplicates_do_not_extend(running):
    recorder, _, writers, now = running
    recorder.begin_execution("E1", ["A"])
    recorder.begin_execution("E2", ["A", "B"])
    recorder.end_execution("E1")
    now[0] += module.TAIL_US + 1
    recorder.observe("/quote/A", {"symbol": "A", "bids": [], "asks": []})
    recorder.end_execution("E2")
    trade = {"symbol": "A", "trade_key": "stable-T", "identity_quality": "stable", "quantity": 1}
    recorder.observe("/trade", trade)
    now[0] += module.TAIL_US - 1
    recorder.observe("/trade", trade)
    now[0] += 2
    recorder.observe("/quote/A", {"symbol": "A", "bids": [], "asks": []})
    recorder.close()
    quotes = [message for message in writers[0].messages if message["topic"] == "/quote/A"]
    assert len(quotes) == 1
    assert len([message for message in writers[0].messages if message["topic"] == "/trade"]) == 2


def test_known_order_execution_survives_execution_end(running):
    recorder, _, writers, _ = running
    recorder.begin_execution("E1", ["A"])
    recorder.observe("/order", {"symbol": "A", "order_id": "O1", "execution_id": "E1", "status": "working"})
    recorder.end_execution("E1")
    recorder.observe("/trade", {"symbol": "A", "order_id": "O1", "trade_key": "T1", "identity_quality": "stable"})
    recorder.close()
    assert writers[0].messages[-1]["data"]["execution_id"] == "E1"


def test_command_lifecycle_preserves_exception_and_disabled_allocates_no_identity():
    owner = SimpleNamespace()
    with replay_command(owner, "place_order", {}) as command_id:
        assert command_id is None
    messages = []
    set_replay_observer(owner, lambda topic, data: messages.append((topic, data)))
    with pytest.raises(ValueError, match="private error"):
        with replay_command(owner, "place_order", {"symbol": "A"}) as command_id:
            assert current_command_id() == command_id
            raise ValueError("private error")
    assert [item[1]["phase"] for item in messages] == ["request", "error"]
    assert "private error" not in json.dumps(messages)
    assert current_command_id() is None


def test_observer_failure_never_changes_business_call():
    owner = SimpleNamespace()

    def broken(*_args):
        raise RuntimeError("observer failure")

    set_replay_observer(owner, broken)
    with replay_command(owner, "cancel_order", {}):
        observe(owner, "/order", {})


def test_query_start_tokens_order_same_sequence_queries_and_cross_generation(running):
    recorder, owner, writers, _ = running
    first = begin_query(owner, ["A"])
    second = begin_query(owner, ["A", "B"])
    assert first["query_basis"]["start_seq"] == second["query_basis"]["start_seq"] == 0
    assert second["query_basis"]["request_revision"] == first["query_basis"]["request_revision"] + 1
    recorder.observe("/order_set", {**first, "scope": ["A"], "scope_complete": True, "orders": []})
    recorder.observe("/connection", {"ready": False})
    assert owner._replay_sync_generation == first["sync_generation"] + 1
    recorder.observe("/order_set", {**first, "scope": ["A"], "scope_complete": True, "orders": []})
    recorder.close()
    sets = [message for message in writers[0].messages if message["topic"] == "/order_set"]
    assert sets[0]["data"]["query_basis"] == first["query_basis"]
    assert sets[1]["data"]["query_basis"] is None


def test_foreign_recording_query_proof_becomes_unknown(running):
    recorder, owner, writers, _ = running
    proof = begin_query(owner, ["A"])
    proof["query_basis"]["recording_id"] = "another-recording"
    recorder.observe("/order_set", {**proof, "scope": ["A"], "scope_complete": True, "orders": []})
    recorder.close()
    assert writers[0].messages[0]["data"]["query_basis"] is None
    assert begin_query(owner, ["A"]) is None


def test_native_time_dictionary_is_bounded_and_does_not_accept_sdk_objects():
    assert freeze_data("/trade", {"source_times": {"timestamp_ns": "1793318400123456789"}}, {})
    with pytest.raises(ValueError, match="invalid_source_times"):
        freeze_data("/trade", {"source_times": {"timestamp_ns": {"sdk": 1}}}, {})


def test_reader_preserves_legal_native_cursor_budget(monkeypatch):
    from axile.server.execution_replay import service

    captured = []
    monkeypatch.setattr(service, "_read", lambda *args: captured.append(json.loads(args[-1])) or {})
    cursor = "A" * 16_000
    service.replay_window("/server/replay", 7, "E1", {"recording_id": "R1", "cursor": cursor})
    assert captured[0]["cursor"] == cursor
    with pytest.raises(ValueError, match="invalid_replay_cursor"):
        service.replay_window("/server/replay", 7, "E1", {"recording_id": "R1", "cursor": "A" * (512 * 1024 + 1)})


def test_healthy_transport_does_not_restore_pre_disconnect_query_proof(running):
    recorder, owner, writers, _ = running
    account_token = begin_query(owner, [])
    recorder.observe("/connection", {"connected": False})
    recorder.observe("/connection", {"connected": True, "synchronized": True})
    recorder.observe(
        "/order_set",
        {
            **account_token,
            "scope": {"kind": "account", "symbols": []},
            "scope_complete": True,
            "synchronized": True,
            "orders": [],
        },
    )
    recorder.close()
    assert writers[0].messages[-1]["data"]["query_basis"] is None


def test_query_token_cannot_expand_symbol_scope_to_account(running):
    recorder, owner, writers, _ = running
    token = begin_query(owner, ["A"])
    recorder.observe(
        "/order_set",
        {
            **token,
            "scope": {"kind": "account", "symbols": []},
            "scope_complete": True,
            "synchronized": True,
            "orders": [],
        },
    )
    recorder.close()
    assert writers[0].messages[-1]["data"]["query_basis"] is None


@pytest.mark.parametrize("failure", ["connected", "ready", "available", "synchronized"])
def test_each_connection_loss_revokes_queries_without_account_sync(running, failure):
    recorder, owner, writers, _ = running
    recorder.observe("/connection", {failure: False})
    recorder.observe("/connection", {"connected": True})
    first = begin_query(owner, ["A"])
    recorder.observe(
        "/order_set",
        {**first, "scope": ["A"], "scope_complete": True, "synchronized": True, "orders": []},
    )
    pending = begin_query(owner, ["A"])
    recorder.observe("/connection", {failure: False})
    assert owner._replay_sync_generation == pending["sync_generation"] + 1
    recorder.observe(
        "/order_set",
        {
            **pending,
            "scope": ["A"],
            "scope_complete": True,
            "synchronized": True,
            "orders": [{"symbol": "A", "order_id": "O", "status": "working"}],
        },
    )
    recorder.close()
    assert writers[0].messages[-1]["data"]["query_basis"] is None
    assert ("A", "O") not in recorder._orders


@pytest.mark.parametrize("rows", [[], [{"symbol": "A", "order_id": "O", "status": "canceled"}]])
def test_complete_order_set_retires_capture_interest_after_the_tail(running, rows):
    recorder, owner, writers, now = running
    recorder.begin_execution("E", ["A"])
    recorder.observe("/order", {"symbol": "A", "order_id": "O", "status": "working"})
    recorder.end_execution("E")
    token = begin_query(owner, ["A"])
    recorder.observe(
        "/order_set", {**token, "scope": ["A"], "scope_complete": True, "synchronized": True, "orders": rows}
    )
    recorder.observe("/quote/A", {"symbol": "A", "bids": [], "asks": []})
    now[0] += module.TAIL_US + 1
    recorder.observe("/quote/A", {"symbol": "A", "bids": [], "asks": []})
    recorder.close()
    assert ("A", "O") not in recorder._orders
    assert len([message for message in writers[0].messages if message["topic"] == "/quote/A"]) == 1


def test_order_set_prefix_keeps_new_orders_and_fills(running):
    recorder, owner, _, _ = running
    for identity in ("old", "filled"):
        recorder.observe("/order", {"symbol": "A", "order_id": identity, "status": "working"})
    token = begin_query(owner, ["A"])
    recorder.observe("/order", {"symbol": "A", "order_id": "new", "status": "working"})
    recorder.observe(
        "/trade", {"symbol": "A", "order_id": "filled", "trade_key": "T", "identity_quality": "stable", "quantity": 1}
    )
    recorder.observe(
        "/order_set", {**token, "scope": ["A"], "scope_complete": True, "synchronized": True, "orders": []}
    )
    assert recorder._orders == {("A", "new"), ("A", "filled")}


def test_duplicate_trade_does_not_protect_an_order_from_a_new_complete_set(running):
    recorder, owner, _, _ = running
    recorder.observe("/order", {"symbol": "A", "order_id": "O", "status": "working"})
    trade = {"symbol": "A", "order_id": "O", "trade_key": "T", "identity_quality": "stable", "quantity": 1}
    recorder.observe("/trade", trade)
    token = begin_query(owner, ["A"])
    recorder.observe("/trade", trade)
    recorder.observe(
        "/order_set", {**token, "scope": ["A"], "scope_complete": True, "synchronized": True, "orders": []}
    )
    assert ("A", "O") not in recorder._orders


def test_late_order_set_cannot_revive_removed_orders_in_an_overlapping_scope(running):
    recorder, owner, _, _ = running
    recorder.observe("/order", {"symbol": "A", "order_id": "OA", "status": "working"})
    recorder.observe("/order", {"symbol": "B", "order_id": "OB", "status": "working"})
    first = begin_query(owner, ["A", "B"])
    second = begin_query(owner, ["A"])
    assert first["query_basis"]["start_seq"] == second["query_basis"]["start_seq"]
    recorder.observe(
        "/order_set", {**second, "scope": ["A"], "scope_complete": True, "synchronized": True, "orders": []}
    )
    recorder.observe(
        "/order_set",
        {
            **first,
            "scope": ["A", "B"],
            "scope_complete": True,
            "synchronized": True,
            "orders": [
                {"symbol": "A", "order_id": "OA", "status": "working"},
                {"symbol": "B", "order_id": "OB", "status": "canceled"},
            ],
        },
    )
    assert recorder._orders == set()


@pytest.mark.parametrize("complete,synchronized", [(False, True), (True, False)])
def test_partial_order_sets_and_old_generation_cannot_remove_capture_interest(running, complete, synchronized):
    recorder, owner, _, _ = running
    recorder.observe("/order", {"symbol": "A", "order_id": "O", "status": "working"})
    token = begin_query(owner, ["A"])
    recorder.observe(
        "/order_set",
        {**token, "scope": ["A"], "scope_complete": complete, "synchronized": synchronized, "orders": []},
    )
    recorder.observe("/connection", {"connected": False})
    recorder.observe(
        "/order_set", {**token, "scope": ["A"], "scope_complete": True, "synchronized": True, "orders": []}
    )
    recorder.observe(
        "/order", {"symbol": "A", "order_id": "O", "status": "canceled", "sync_generation": token["sync_generation"]}
    )
    assert recorder._orders == {("A", "O")}


def test_new_recording_does_not_inherit_order_interest_fences(running):
    recorder, _, _, _ = running
    recorder.observe("/order", {"symbol": "A", "order_id": "O", "status": "canceled"})
    recorder.configure(False, 2)
    eventually(lambda: recorder._session is None)
    recorder.configure(True, 3)
    eventually(lambda: recorder.status()["state"] == "recording")
    recorder.observe("/order", {"symbol": "A", "order_id": "O", "status": "working"})
    assert recorder._orders == {("A", "O")}


def test_poll_seals_only_after_its_captured_queue_prefix(tmp_path):
    now = [1_000_000]
    entered = threading.Event()
    release = threading.Event()
    timeline = []

    class OrderedWriter(FakeWriter):
        def append(self, raw):
            message = json.loads(raw)
            if message["capture_seq"] == 1:
                entered.set()
                assert release.wait(2)
            timeline.append(("message", message["time_us"]))

        def poll(self, timestamp):
            timeline.append(("poll", timestamp))

    writer = OrderedWriter()
    recorder = ReplayRecorder(7, tmp_path, writer_factory=lambda *_: writer, now_us=lambda: now[0])
    recorder.attach(SimpleNamespace())
    recorder.configure(True, 1)
    recorder.observe("/command", {"phase": "request"})
    assert entered.wait(1)
    now[0] = 2_000_000
    recorder.observe("/command", {"phase": "returned"})
    with recorder._condition:
        now[0] = 3_000_000
        recorder._session.next_poll = 0
    release.set()
    eventually(lambda: ("poll", 3_000_000) in timeline)
    recorder.close()
    assert timeline.index(("message", 2_000_000)) < timeline.index(("poll", 3_000_000))


def test_close_timeout_keeps_draining_and_finishes(running):
    recorder, _, writers, _ = running
    entered = threading.Event()
    release = threading.Event()

    def blocked(_raw):
        entered.set()
        release.wait(2)

    writers[0].append = blocked
    recorder.observe("/command", {"phase": "request"})
    assert entered.wait(1)
    recorder.observe("/command", {"phase": "returned"})
    try:
        assert recorder.close(timeout=0.01)["state"] == "closing"
        threading.Event().wait(1.1)
        assert recorder.status()["state"] == "closing"
    finally:
        release.set()
    eventually(lambda: recorder._session is None)
    assert writers[0].finished.is_set()


def test_worker_shutdown_waits_for_recording_to_finish(running):
    from axile.server.execution.worker_backend.replay import close_worker_replay

    recorder, _, writers, _ = running
    entered, release = threading.Event(), threading.Event()

    def blocked(_raw):
        entered.set()
        release.wait(3)

    writers[0].append = blocked
    recorder.observe("/command", {"phase": "request"})
    assert entered.wait(1)
    state = SimpleNamespace(replay_recorder=recorder, replay_error=None)
    closing = threading.Thread(target=close_worker_replay, args=(state,))
    closing.start()
    try:
        closing.join(1.1)
        assert closing.is_alive()
        assert recorder.status()["state"] == "closing"
    finally:
        release.set()
        closing.join(3)
    assert not closing.is_alive()
    assert writers[0].finished.is_set()


def test_unlinked_trade_correction_protects_inflight_order_query(running, monkeypatch):
    recorder, _, _, _ = running
    monkeypatch.setattr(module, "MAX_IDENTITIES", 2)
    recorder.observe("/order", {"symbol": "X", "order_id": "O", "status": "working"})
    recorder.observe("/trade", {"symbol": "X", "order_id": "O", "trade_key": "T", "identity_quality": "stable"})
    for key in ("T2", "T3"):
        recorder.observe("/trade", {"symbol": "Y", "order_id": "Y", "trade_key": key, "identity_quality": "stable"})
    token = recorder.begin_query(["X"])
    recorder.observe("/trade", {"trade_key": "T", "identity_quality": "stable", "price": 12})
    recorder.observe(
        "/order_set",
        {
            **token,
            "scope": {"kind": "symbols", "symbols": ["X"]},
            "scope_complete": True,
            "synchronized": True,
            "orders": [],
        },
    )
    assert ("X", "O") in recorder._orders


def test_repeated_query_observation_keeps_its_original_coverage_proof(running):
    recorder, owner, writers, _ = running
    token = begin_query(owner, [])
    result = {
        **token,
        "scope": {"kind": "account", "symbols": []},
        "scope_complete": True,
        "synchronized": True,
        "orders": [],
    }
    recorder.observe("/order_set", result)
    recorder.observe("/order_set", result)
    recorder.close()
    assert [message["data"]["query_basis"] for message in writers[0].messages] == [token["query_basis"]] * 2


def test_cached_seed_is_optional_and_uses_same_ordered_observer(tmp_path):
    writer = FakeWriter()
    owner = SimpleNamespace(
        replay_seed_observations=lambda: iter(
            [
                (
                    "/account_observation",
                    {
                        "scope": {"kind": "account", "symbols": []},
                        "scope_complete": True,
                        "positions": [{"symbol": "A", "quantity": 2}],
                        "source": "raw_cache",
                    },
                ),
                ("/quote/A", {"symbol": "A", "bids": [], "asks": [], "source": "raw_cache"}),
            ]
        )
    )
    recorder = ReplayRecorder(7, tmp_path, writer_factory=lambda *_: writer)
    recorder.attach(owner)
    recorder.configure(True, 1)
    eventually(lambda: len(writer.messages) == 2)
    recorder.close()
    assert [message["topic"] for message in writer.messages] == ["/account_observation", "/quote/A"]
    assert [message["capture_seq"] for message in writer.messages] == [1, 2]


def test_enabling_during_execution_seeds_carry_in_without_claiming_start(tmp_path):
    writer = FakeWriter()
    recorder = ReplayRecorder(7, tmp_path, writer_factory=lambda *_: writer)
    recorder.attach(SimpleNamespace())
    recorder.begin_execution("already-running", ["A"])
    recorder.configure(True, 1)
    eventually(lambda: bool(writer.messages))
    recorder.close()
    assert writer.messages[0]["data"]["phase"] == "carry_in"


def test_canonical_canceled_order_expires_after_tail_without_hiding_open_order(running):
    recorder, _, writers, now = running
    recorder.observe("/order", {"symbol": "A", "order_id": "O1", "status": "working"})
    recorder.observe("/order", {"symbol": "B", "order_id": "O2", "status": "working"})
    recorder.observe("/order", {"symbol": "A", "order_id": "O1", "status": "canceled", "remaining_quantity": 3})
    recorder.observe("/quote/A", {"symbol": "A", "bids": [], "asks": []})
    now[0] += module.TAIL_US + 1
    recorder.observe("/quote/A", {"symbol": "A", "bids": [], "asks": []})
    recorder.observe("/quote/B", {"symbol": "B", "bids": [], "asks": []})
    recorder.close()
    assert [message["topic"] for message in writers[0].messages if message["topic"].startswith("/quote/")] == [
        "/quote/A",
        "/quote/B",
    ]


def test_command_identity_owns_early_native_order_before_public_return(running):
    recorder, _, writers, _ = running
    recorder.observe(
        "/command",
        {
            "command_id": "C1",
            "operation": "place_order",
            "phase": "request",
            "execution_id": "E1",
            "origin": "execution",
        },
    )
    recorder.observe(
        "/command",
        {"command_id": "C1", "operation": "place_order", "phase": "native_request", "symbol": "A", "order_id": "O1"},
    )
    recorder.observe(
        "/trade", {"symbol": "A", "order_id": "O1", "trade_key": "T1", "identity_quality": "stable", "quantity": 2}
    )
    recorder.close()
    assert writers[0].messages[1]["data"]["execution_id"] == "E1"
    assert writers[0].messages[2]["data"]["execution_id"] == "E1"
    assert writers[0].messages[2]["data"]["origin"] == "execution"


@pytest.mark.parametrize("original_execution,origin", [("original", "execution"), (None, "external")])
def test_cancel_keeps_original_order_ownership(running, original_execution, origin):
    recorder, _, writers, _ = running
    recorder.observe(
        "/order",
        {"symbol": "A", "order_id": "O1", "status": "working", "execution_id": original_execution, "origin": origin},
    )
    recorder.observe(
        "/command",
        {
            "command_id": "cancel",
            "operation": "cancel_order",
            "phase": "request",
            "execution_id": "cancellation-execution",
            "symbol": "A",
            "order_id": "O1",
        },
    )
    recorder.observe(
        "/command",
        {
            "command_id": "cancel",
            "operation": "cancel_order",
            "phase": "native_request",
            "symbol": "A",
            "order_id": "O1",
        },
    )
    recorder.observe(
        "/trade", {"symbol": "A", "order_id": "O1", "trade_key": "T1", "identity_quality": "stable", "quantity": 2}
    )
    recorder.close()
    assert writers[0].messages[2]["data"]["execution_id"] == "cancellation-execution"
    assert writers[0].messages[3]["data"].get("execution_id") == original_execution
    assert writers[0].messages[3]["data"]["origin"] == origin


def test_only_active_execution_is_not_ownership_proof(running):
    recorder, _, writers, _ = running
    recorder.begin_execution("only-execution", ["A"])
    recorder.observe(
        "/trade",
        {"symbol": "A", "order_id": "external-order", "trade_key": "T1", "identity_quality": "stable", "quantity": 2},
    )
    recorder.close()
    assert writers[0].messages[-1]["data"].get("execution_id") is None


def test_nested_rows_use_declared_fields_and_position_extensions():
    with pytest.raises(ValueError, match="unknown_row_field"):
        freeze_data("/account_observation", {"positions": [{"undocumented_sdk_field": "private"}]}, {})
    with pytest.raises(ValueError, match="undeclared_extension"):
        freeze_data("/account_observation", {"positions": [{"symbol": "A", "attributes": {"cost_basis": 2}}]}, {})
    value = {"positions": [{"symbol": "A", "attributes": {"cost_basis": 2}}]}
    assert freeze_data("/account_observation", value, {"attributes": {"position": ["cost_basis"]}}) == value


def test_python_allowed_fields_match_packaged_native_schema():
    schema_path = Path(module.__file__).with_name("schemas") / "replay-v1.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert DATA_FIELDS == set(schema["$defs"]["data"]["properties"])
    scope = schema["$defs"]["scope"]
    assert any("kind" in shape.get("properties", {}) for shape in scope["oneOf"])


def test_environment_metadata_is_read_only_when_recording_starts(tmp_path):
    contexts = []
    reads = []
    environment = ["demo"]

    def metadata():
        reads.append(True)
        return {"environment": environment[0]}

    def factory(_root, _account, _recording, context):
        contexts.append(json.loads(context))
        return FakeWriter()

    recorder = ReplayRecorder(7, tmp_path, writer_factory=factory)
    recorder.attach(SimpleNamespace(replay_context_metadata=metadata))
    assert not reads
    recorder.configure(True, 1)
    eventually(lambda: len(contexts) == 1)
    recorder.configure(False, 2)
    eventually(lambda: recorder.status()["state"] == "disabled")
    environment[0] = "live"
    recorder.configure(True, 3)
    eventually(lambda: len(contexts) == 2)
    recorder.close()
    assert [context["environment"] for context in contexts] == ["demo", "live"]
    assert len(reads) == 2


@pytest.mark.parametrize(
    "payload", [{"environment": "demo", "unexpected": "never-copy"}, {"environment": "unverified"}, None]
)
def test_invalid_environment_metadata_falls_back_to_unknown(tmp_path, payload):
    contexts = []

    def metadata():
        if payload is None:
            raise RuntimeError("never-copy error text")
        return payload

    def factory(_root, _account, _recording, context):
        contexts.append(json.loads(context))
        return FakeWriter()

    recorder = ReplayRecorder(7, tmp_path, writer_factory=factory)
    recorder.attach(SimpleNamespace(replay_context_metadata=metadata))
    recorder.configure(True, 1)
    eventually(lambda: bool(contexts))
    recorder.close()
    assert contexts[0]["environment"] == "unknown"
    assert "never-copy" not in json.dumps(contexts)
