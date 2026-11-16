"""查询证明在完成或退出后回收，历史查询次数不限制持续录制。"""

from copy import deepcopy

import pytest

from axile.server.execution_replay import recorder as module
from tests.unit.server.test_execution_replay_recorder import eventually
from tests.unit.server.test_execution_replay_recorder import running as running


def order_set(recorder, token, scope, **fields):
    recorder.observe(
        "/order_set",
        {**token, "scope": scope, "scope_complete": True, "orders": [], **fields},
    )


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


def test_pending_queries_outgrow_completed_cache_budget_without_stopping(running, monkeypatch):
    recorder, _, _, _ = running
    monkeypatch.setattr(module, "MAX_IDENTITIES", 3)
    for _ in range(3):
        assert recorder.begin_query(["A"]) is not None
    assert recorder.begin_query(["A"]) is not None
    assert recorder.status()["state"] == "recording"
    assert len(recorder._session.queries) == 4


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
