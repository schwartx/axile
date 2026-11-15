"""New raw protocol fences and bounded resource accounting; no v1 comparison."""

import pytest

from axile.server.execution_replay import reader
from axile.server.execution_replay.reader import LEASE, ROOT_BYTES, ReadError, Readers


@pytest.fixture
def owner():
    clock = [0.0]
    readers = Readers(lambda: clock[0])
    token = readers.open(7, "E")
    clock[0] = 12
    return readers, clock, {**token, "root_seq": 1, "attempt": 1}


def test_canceled_attempt_cannot_read_and_late_cancel_cannot_cancel_replacement(owner):
    readers, _, token = owner
    session, root = readers.admit(7, "E", token, 10)
    readers.control(7, "E", {**token, "operation": "cancel"})
    with pytest.raises(ReadError, match="REPLAY_READ_CANCELED"):
        readers.check(session, root, 1)
    with pytest.raises(ReadError):
        readers.admit(7, "E", {**token, "attempt": 2}, 10)
    readers.release(session, root, 10, 7, 100)
    replacement = {**token, "attempt": 2}
    second, task = readers.admit(7, "E", replacement, 10)
    with pytest.raises(ReadError, match="REPLAY_ATTEMPT_RETIRED"):
        readers.admit(7, "E", token, 10)
    readers.control(7, "E", {**token, "operation": "cancel"})
    readers.check(second, task, 2)
    readers.release(second, task, 10, 3, 100)
    receipt = readers.control(7, "E", {**token, "operation": "seal"})
    assert receipt["bytes"] == 10
    assert receipt["index_steps"] == 200


def test_ack_pruning_keeps_root_watermark_and_control_idempotence(owner):
    readers, _, token = owner
    # Seal is valid before the first data request can arrive.
    assert readers.control(7, "E", {**token, "operation": "seal"})["state"] == "settled"
    readers.control(7, "E", {**token, "operation": "ack"})
    assert readers.sessions[token["session"]].root is None
    with pytest.raises(ReadError, match="REPLAY_ROOT_RETIRED"):
        readers.admit(7, "E", token, 1)
    assert readers.control(7, "E", {**token, "operation": "status"})["state"] == "retired"
    session, root = readers.admit(7, "E", {**token, "root_seq": 2}, 1)
    readers.control(7, "E", {**token, "operation": "cancel"})
    readers.check(session, root, 1)
    readers.release(session, root, 1, 1, 0)


def test_cancel_before_first_request_fences_attempt_one(owner):
    readers, _, token = owner
    readers.control(7, "E", {**token, "operation": "cancel"})
    with pytest.raises(ReadError, match="REPLAY_ATTEMPT_RETIRED"):
        readers.admit(7, "E", token, 1)
    session, root = readers.admit(7, "E", {**token, "attempt": 2}, 1)
    readers.release(session, root, 1, 1, 0)


def test_no_budget_reset_on_retry_or_new_session_and_expiry_does_not_release_active_io(owner):
    readers, clock, token = owner
    session, root = readers.admit(7, "E", token, ROOT_BYTES)
    readers.release(session, root, ROOT_BYTES, ROOT_BYTES, 100)
    readers.control(7, "E", {**token, "operation": "cancel"})
    with pytest.raises(ReadError, match="REPLAY_ROOT_BUDGET"):
        readers.admit(7, "E", {**token, "attempt": 2}, 1)
    assert readers.accounts[7].total_bytes == ROOT_BYTES
    other = {**readers.open(7, "E"), "root_seq": 1, "attempt": 1}
    current, task = readers.admit(7, "E", other, 1)
    clock[0] += LEASE + 1
    readers._expire()
    assert other["session"] in readers.sessions
    assert readers.control(7, "E", {**other, "operation": "status"})["state"] == "stopping"
    readers.release(current, task, 1, 1, 0)
    readers._expire()
    assert readers.control(7, "E", {**other, "operation": "status"}) == {"state": "retired", "estimated": True}


def test_startup_waits_entire_window_and_tokens_do_not_cross_owner_lifetimes():
    clock = [0.0]
    first = Readers(lambda: clock[0])
    token = {**first.open(7, "E"), "root_seq": 1, "attempt": 1}
    with pytest.raises(ReadError, match="REPLAY_READ_WAIT"):
        first.admit(7, "E", token, 1)
    second = Readers(lambda: clock[0])
    clock[0] = 12
    with pytest.raises(ReadError, match="REPLAY_READER_EPOCH"):
        second.admit(7, "E", token, 1)


def test_account_index_work_is_not_reset_by_new_session(owner, monkeypatch):
    readers, _, token = owner
    monkeypatch.setattr(reader, "WINDOW_INDEX_STEPS", 200_000)
    session, root = readers.admit(7, "E", token, 1)
    readers.release(session, root, 1, 1, 190_000)
    other = {**readers.open(7, "E"), "root_seq": 1, "attempt": 1}
    with pytest.raises(ReadError, match="REPLAY_INDEX_RATE"):
        readers.admit(7, "E", other, 1)
    assert readers.accounts[7].total_index_steps == 190_000


def test_large_root_keeps_per_read_reservations_and_account_rate(owner):
    readers, clock, token = owner
    for _ in range(9):
        session, task = readers.admit(7, "E", token, 1)
        assert task.reserved_steps == reader.REQUEST_INDEX_STEPS
        readers.release(session, task, 1, 1, 190_000)
    assert task.index_steps == 1_710_000 > reader.REQUEST_INDEX_STEPS
    other = {**readers.open(7, "E"), "root_seq": 1, "attempt": 1}
    session, task = readers.admit(7, "E", token, 1)
    with pytest.raises(ReadError, match="REPLAY_INDEX_RATE"):
        readers.admit(7, "E", other, 1)
    readers.release(session, task, 1, 1, 190_000)
    with pytest.raises(ReadError, match="REPLAY_INDEX_RATE"):
        readers.admit(7, "E", token, 1)
    clock[0] += 11
    session, task = readers.admit(7, "E", token, 1)
    assert task.index_steps == 1_900_000
    readers.release(session, task, 1, 1, 100)


def test_large_root_deadline_and_lifetime_work_are_still_finite(owner):
    readers, clock, token = owner
    session, root = readers.admit(7, "E", token, 1)
    readers.release(session, root, 1, 1, 100)
    root.index_steps = reader.ROOT_INDEX_STEPS - 99
    with pytest.raises(ReadError, match="REPLAY_ROOT_BUDGET"):
        readers.admit(7, "E", token, 1)
    root.index_steps = 100
    clock[0] += reader.ROOT_SECONDS - 1
    session, root = readers.admit(7, "E", token, 1)
    readers.release(session, root, 1, 1, 100)
    clock[0] += 1
    with pytest.raises(ReadError, match="REPLAY_ROOT_DEADLINE"):
        readers.admit(7, "E", token, 1)
