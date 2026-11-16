"""纯 Python 采集边界；fake writer 不代替真实 MCAP 验收。"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
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
from axile.server.execution_replay.contracts import freeze_data
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
    assert message["schema_version"] == 2
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
    eventually(lambda: len(writer.messages) == 3)
    recorder.close()
    assert [message["topic"] for message in writer.messages] == ["/account_observation", "/capture", "/quote/A"]
    assert [message["capture_seq"] for message in writer.messages] == [1, 2, 3]


def test_enabling_during_execution_seeds_carry_in_without_claiming_start(tmp_path):
    writer = FakeWriter()
    recorder = ReplayRecorder(7, tmp_path, writer_factory=lambda *_: writer)
    recorder.attach(SimpleNamespace())
    recorder.begin_execution("already-running", ["A"])
    recorder.configure(True, 1)
    eventually(lambda: bool(writer.messages))
    recorder.close()
    assert writer.messages[0]["data"]["phase"] == "carry_in"


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
