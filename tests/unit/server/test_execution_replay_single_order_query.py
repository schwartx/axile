"""单笔查询只证明自身前缀，不发布完整订单集合。"""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from axile.server.execution_replay import recorder as module
from tests.unit.server.test_execution_replay_native import TIME, finished, window
from tests.unit.server.test_execution_replay_native import make_recording as make_recording
from tests.unit.server.test_execution_replay_recorder import eventually
from tests.unit.server.test_execution_replay_recorder import running as running


def order(identity="O", price=100):
    return {
        "symbol": "X",
        "order_id": identity,
        "price": price,
        "quantity": 2,
        "remaining_quantity": 2,
        "status": "working",
        "confirmed_book": True,
    }


def query_order(recorder, token, row=None, scope=None):
    recorder.observe(
        "/order",
        {**(row or order()), **token, "scope": scope or {"kind": "symbols", "symbols": ["X"]}},
    )


def test_account_query_rows_keep_pending_proof_until_the_caller_finishes(running):
    recorder, _, writers, _ = running
    token = recorder.begin_query([])
    scope = {"kind": "account", "symbols": []}
    query_order(recorder, token, order("first"), scope)
    query_order(recorder, token, order("second"), scope)
    assert token["query_basis"]["request_revision"] in recorder._session.queries
    assert recorder._session.completed_queries == {}
    assert recorder._order_set_revisions == {}
    assert recorder._orders == {("X", "first"), ("X", "second")}
    recorder.finish_query(token)
    query_order(recorder, token, order("finished"), scope)
    assert recorder._orders == {("X", "first"), ("X", "second")}
    recorder.close()
    assert [message["data"]["query_basis"] for message in writers[0].messages] == [
        token["query_basis"],
        token["query_basis"],
        None,
    ]
    assert writers[0].messages[-1]["data"]["confirmed_book"] is False


@pytest.mark.parametrize("field", ["recording_id", "request_revision", "start_seq", "scope", "explicit_null"])
def test_invalid_declared_query_proof_cannot_confirm_or_consume_the_real_query(running, field):
    recorder, _, writers, _ = running
    token = recorder.begin_query(["X"])
    invalid = deepcopy(token)
    scope = {"kind": "symbols", "symbols": ["X"]}
    if field == "scope":
        scope["symbols"] = ["Y"]
    elif field == "explicit_null":
        invalid["query_basis"] = None
    elif field == "recording_id":
        invalid["query_basis"][field] = "other-recording"
    else:
        invalid["query_basis"][field] += 1
    query_order(recorder, invalid, order("invalid"), scope)
    assert recorder._orders == set()
    assert token["query_basis"]["request_revision"] in recorder._session.queries
    query_order(recorder, token, order("valid"))
    recorder.observe("/order", order("live"))
    assert recorder._orders == {("X", "valid"), ("X", "live")}
    recorder.close()
    assert writers[0].messages[0]["data"]["query_basis"] is None
    assert writers[0].messages[0]["data"]["confirmed_book"] is False
    assert "query_basis" not in writers[0].messages[-1]["data"]
    assert writers[0].messages[-1]["data"]["confirmed_book"] is True


def test_a_live_update_fences_older_single_query_without_changing_its_recorded_facts(running):
    recorder, _, writers, _ = running
    token = recorder.begin_query(["X"])
    recorder.observe("/order", order(price=101))
    live_seq = recorder._session.sequence
    query_order(recorder, token, order(price=100))
    assert recorder._order_changes[("X", "O")] == live_seq
    newer = recorder.begin_query(["X"])
    query_order(recorder, newer, order(price=102))
    assert recorder._order_changes[("X", "O")] == recorder._session.sequence
    recorder.close()
    assert [message["data"]["price"] for message in writers[0].messages] == [101, 100, 102]
    assert writers[0].messages[1]["data"]["query_basis"] == token["query_basis"]


@pytest.mark.parametrize("first_return", [0, 1])
def test_overlapping_queries_keep_the_first_observation_until_a_new_query_starts(running, first_return):
    recorder, _, writers, _ = running
    tokens = [recorder.begin_query(["X"]), recorder.begin_query(["X"])]
    query_order(recorder, tokens[first_return], order(price=101))
    accepted_seq = recorder._session.sequence
    query_order(recorder, tokens[1 - first_return], order(price=100))
    assert recorder._order_changes[("X", "O")] == accepted_seq
    third = recorder.begin_query(["X"])
    query_order(recorder, third, order(price=102))
    assert recorder._order_changes[("X", "O")] == recorder._session.sequence
    assert recorder._session.completed_queries == {}
    recorder.close()
    assert [message["data"]["price"] for message in writers[0].messages] == [101, 100, 102]


@pytest.mark.parametrize("recovery", ["query", "live"])
@pytest.mark.parametrize("single_first", [True, False])
@pytest.mark.parametrize("symbols", [["X"], []])
@pytest.mark.parametrize("prior,present", [(True, False), (True, True), (False, False)])
def test_a_complete_observation_fences_all_inflight_single_queries(
    running, recovery, single_first, symbols, prior, present
):
    recorder, _, writers, _ = running
    if prior:
        recorder.observe("/order", order())
    first, second = ["X"], symbols
    if not single_first:
        first, second = second, first
    tokens = recorder.begin_query(first), recorder.begin_query(second)
    single, complete = tokens if single_first else tokens[::-1]
    scope = {"kind": "symbols", "symbols": symbols} if symbols else {"kind": "account", "symbols": []}
    recorder.observe(
        "/order_set",
        {
            **complete,
            "scope": scope,
            "scope_complete": True,
            "synchronized": True,
            "orders": [order(price=101)] if present else [],
        },
    )
    expected = {("X", "O")} if present else set()
    assert recorder._orders == expected
    observed_scopes = {"X"} if symbols else {""} | ({"X"} if prior or present else set())
    assert recorder._order_set_observation == {
        symbol: (
            complete["sync_generation"],
            complete["query_basis"]["request_revision"],
            recorder._session.sequence,
        )
        for symbol in observed_scopes
    }
    changes = recorder._order_changes.copy()
    query_order(recorder, single)
    assert recorder._orders == expected
    assert recorder._order_changes == changes
    if recovery == "query":
        query_order(recorder, recorder.begin_query(["X"]), order(price=102))
    else:
        recorder.observe("/order", order(price=102))
    assert recorder._orders == {("X", "O")}
    recorder.close()
    raw = next(
        message for message in writers[0].messages if message["data"].get("query_basis") == single["query_basis"]
    )
    assert raw["data"]["confirmed_book"] is True
    assert raw["data"]["price"] == 100


@pytest.mark.parametrize("invalidity", ["partial", "old_generation", "unproven"])
def test_an_unproven_or_partial_set_cannot_fence_a_valid_single_query(running, invalidity):
    recorder, _, _, _ = running
    single = recorder.begin_query(["X"])
    complete = recorder.begin_query(["X"])
    data = {
        **complete,
        "scope": {"kind": "symbols", "symbols": ["X"]},
        "scope_complete": True,
        "synchronized": True,
        "orders": [],
    }
    if invalidity == "partial":
        data["scope_complete"] = False
    elif invalidity == "old_generation":
        data["sync_generation"] -= 1
    else:
        data["query_basis"] = None
    recorder.observe("/order_set", data)
    assert recorder._order_set_revisions == {}
    assert recorder._order_set_observation == {}
    query_order(recorder, single)
    assert recorder._orders == {("X", "O")}


def test_an_older_complete_account_query_does_not_advance_its_observation_fence(running):
    recorder, _, _, _ = running
    older, newer = recorder.begin_query([]), recorder.begin_query([])
    scope = {"kind": "account", "symbols": []}
    payload = {"scope": scope, "scope_complete": True, "synchronized": True, "orders": []}
    recorder.observe("/order_set", {**payload, **newer})
    fence = recorder._order_set_observation.copy()
    single = recorder.begin_query(["X"])
    recorder.observe("/order_set", {**payload, **older})
    assert recorder._order_set_observation == fence
    query_order(recorder, single)
    assert recorder._orders == {("X", "O")}


def test_a_partially_superseded_account_set_does_not_fence_an_uncovered_single_query_or_quote(running):
    recorder, _, writers, _ = running
    recorder.observe("/order", order())
    account, newer, single = recorder.begin_query([]), recorder.begin_query(["X"]), recorder.begin_query(["Y"])
    payload = {"scope_complete": True, "synchronized": True, "orders": []}
    recorder.observe(
        "/order_set",
        {**payload, **newer, "scope": {"kind": "symbols", "symbols": ["X"]}, "orders": [order(price=101)]},
    )
    recorder.observe("/order_set", {**payload, **account, "scope": {"kind": "account", "symbols": []}})
    assert "" not in recorder._order_set_observation
    query_order(recorder, single, {**order("other"), "symbol": "Y"}, {"kind": "symbols", "symbols": ["Y"]})
    assert recorder._orders == {("X", "O"), ("Y", "other")}
    recorder.observe("/quote/Y", {"symbol": "Y", "bids": [], "asks": [], "book_valid": False})
    recorder.close()
    assert any(message["topic"] == "/quote/Y" for message in writers[0].messages)


@pytest.mark.parametrize("boundary", ["loss", "recording", "owner"])
def test_complete_scope_observation_fences_reset_with_existing_set_watermarks(running, boundary):
    recorder, _, _, _ = running
    token = recorder.begin_query([])
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
    assert "" in recorder._order_set_observation
    previous = recorder._session
    if boundary == "loss":
        recorder.observe("/connection", {"ready": False})
    elif boundary == "recording":
        recorder.configure(False, 2)
        eventually(lambda: recorder._session is None)
        recorder.configure(True, 3)
    else:
        recorder.attach(SimpleNamespace())
        eventually(lambda: recorder._session is not previous)
    assert recorder._order_set_observation == {}


def test_scope_observation_fences_are_not_limited_by_recent_identity_cache(running, monkeypatch):
    recorder, _, _, _ = running
    monkeypatch.setattr(module, "MAX_IDENTITIES", 1)
    for symbols in (["X"], []):
        token = recorder.begin_query(symbols)
        recorder.observe(
            "/order_set",
            {
                **token,
                "scope": {"kind": "symbols", "symbols": symbols} if symbols else {"kind": "account", "symbols": []},
                "scope_complete": True,
                "synchronized": True,
                "orders": [],
            },
        )
    assert recorder.status()["state"] == "recording"
    assert set(recorder._order_set_observation) == {"X", ""}


def test_actual_mcap_single_order_proof_is_retained_without_a_complete_scope_claim(make_recording):
    recorder, now, root, recording_id = make_recording()
    recorder.begin_execution("E", [])
    token = recorder.begin_query(["X"])
    query_order(recorder, token)
    recorder.finish_query(token)
    assert recorder._session.queries == {}
    assert recorder._session.completed_queries == {}
    now[0] += 1
    finished(recorder)
    full = window(root, "E", recording_id, TIME, TIME + 2)
    event = next(message for message in full["messages"] if message["topic"] == "/order")
    assert event["data"]["query_basis"] == token["query_basis"]
    assert "scope_complete" not in event["data"]
    state = window(root, "E", recording_id, TIME + 1, TIME + 2)["state_before_start"]
    assert state["last_set_revision"] == {}
