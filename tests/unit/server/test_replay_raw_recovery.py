"""Recovery retains the logical task's consumed budget and original deadline."""

import pytest

from axile.server.execution_replay.reader import ReadError, Readers


def test_restart_resumes_immediately_with_signed_old_owner_without_refilling_root(tmp_path):
    clock = [0.0]
    old = Readers(lambda: clock[0])
    token = old.open(7, "E", tmp_path)
    clock[0] = 12
    session, root = old.admit(7, "E", {**token, "root_seq": 1, "attempt": 1}, 100)
    old.release(session, root, 100, 80, 300)
    # Releasing the native lock is the stopping proof. An overlapping replacement cannot open.
    new = Readers(lambda: clock[0])
    with pytest.raises(ReadError, match="OWNER_UNAVAILABLE"):
        new.open(7, "E", tmp_path)
    old._owner = None
    resumed = new.open(
        7,
        "E",
        tmp_path,
        resume={
            **token,
            "root_seq": 1,
            "attempt": 2,
            "profile": "point",
            "elapsed_ms": 5000,
            "spent_bytes": 80,
            "spent_steps": 300,
            "total_bytes": 80,
        },
    )
    root = new.sessions[resumed["session"]].root
    assert root.bytes == 80 and root.index_steps == 300 and root.started == 7
    assert new.control(7, "E", {**token, "operation": "status", "root_seq": 1})["old_owner_stopped"]
    with pytest.raises(ReadError, match="STOP_PROOF"):
        new.control(7, "E", {**token, "session_proof": "forged", "operation": "status", "root_seq": 1})
    session, task = new.admit(7, "E", {**resumed, "root_seq": 1, "attempt": 2}, 10)
    new.release(session, task, 10, 5, 100)
    assert task.bytes == 85 and task.index_steps == 400
    clock[0] = 68
    with pytest.raises(ReadError, match="ROOT_DEADLINE"):
        new.admit(7, "E", {**resumed, "root_seq": 1, "attempt": 2}, 10)


def test_unknown_retirement_does_not_prove_an_arbitrary_session_stopped():
    owner = Readers()
    token = owner.open(7, "E")
    with pytest.raises(ReadError, match="STOP_PROOF"):
        owner.control(7, "E", {**token, "session": "other", "root_seq": 1, "operation": "status"})


def test_delayed_resume_watermark_cannot_refund_known_cost_or_discard_transport_on_invalid_input():
    clock = [0.0]
    owner = Readers(lambda: clock[0])
    token = owner.open(7, "E")
    clock[0] = 12
    session, task = owner.admit(7, "E", {**token, "root_seq": 1, "attempt": 1}, 100)
    owner.release(session, task, 100, 80, 300, 60, 20)
    clock[0] = 17
    resume = {
        **token,
        "root_seq": 1,
        "attempt": 2,
        "profile": "point",
        "elapsed_ms": 0,
        "spent_bytes": 0,
        "spent_steps": 0,
        "total_bytes": 0,
    }
    with pytest.raises(ReadError, match="RECOVERY_BUDGET"):
        owner.open(7, "E", resume={**resume, "attempt": 1})
    assert owner.sessions[token["session"]].root is task and not task.sealed
    recovered = owner.open(7, "E", resume=resume)
    current = owner.sessions[recovered["session"]]
    assert current.root.started == 12 and current.root.bytes == 80 and current.root.index_steps == 300
    assert current.total_bytes == 80 and current.total_index_steps == 300
    assert current.raw_bytes == 60 and current.total_index_bytes == 20


def test_resume_retires_old_transport_and_import_maintenance_retires_actual_owner(tmp_path):
    clock = [0.0]
    owner = Readers(lambda: clock[0])
    old = owner.open(7, "E", tmp_path)
    clock[0] = 12
    session, root = owner.admit(7, "E", {**old, "root_seq": 1, "attempt": 1}, 100)
    owner.release(session, root, 100, 80, 300)
    new = owner.open(
        7,
        "E",
        tmp_path,
        resume={
            **old,
            "root_seq": 1,
            "attempt": 2,
            "profile": "point",
            "elapsed_ms": 5000,
            "spent_bytes": 80,
            "spent_steps": 300,
            "total_bytes": 80,
        },
    )
    with pytest.raises(ReadError, match="READER_SESSION"):
        owner.admit(7, "E", {**old, "root_seq": 1, "attempt": 1}, 10)
    with owner.maintenance():
        assert owner._owner is None and not owner.sessions
        with pytest.raises(ReadError, match="READ_WAIT"):
            owner.open(7, "E", tmp_path)
    owner.open(7, "E", tmp_path)
    assert owner.control(7, "E", {**new, "root_seq": 1, "operation": "status"})["old_owner_stopped"]
