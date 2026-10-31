"""完整集合按范围的查询版本排序，不借单笔查询的交付序号规则。"""

import pytest

from axile.server.execution_replay.service import replay_window
from tests.unit.server.test_execution_replay_native import TIME, finished, window
from tests.unit.server.test_execution_replay_native import make_recording as make_recording
from tests.unit.server.test_execution_replay_recorder import running as running


def order(price):
    return {
        "symbol": "X",
        "order_id": "O",
        "status": "working",
        "confirmed_book": True,
        "quantity": 1,
        "remaining_quantity": 1,
        "filled_quantity": 0,
        "price": price,
    }


def complete(recorder, token, scope, rows):
    recorder.observe(
        "/order_set", {**token, "scope": scope, "scope_complete": True, "synchronized": True, "orders": rows}
    )


@pytest.mark.parametrize("older_scope", ["symbols", "account"])
def test_global_complete_query_rejects_older_sets_without_registering_unknown_scopes(running, older_scope):
    recorder, _, writers, _ = running
    scope = {"kind": older_scope, "symbols": ["X"] if older_scope == "symbols" else []}
    older = recorder.begin_query(scope["symbols"])
    account = recorder.begin_query([])
    fresh = recorder.begin_query(["X"])
    complete(recorder, account, {"kind": "account", "symbols": []}, [])
    fences = recorder._order_set_observation.copy()
    complete(recorder, older, scope, [order(100)])
    assert recorder._orders == set()
    assert recorder._order_set_revisions == {}
    assert recorder._order_set_observation == fences
    assert fresh["query_basis"]["start_seq"] < fences[""][2]
    complete(recorder, fresh, {"kind": "symbols", "symbols": ["X"]}, [order(102)])
    assert recorder._orders == {("X", "O")}
    assert recorder._order_set_revisions == {"X": fresh["query_basis"]["request_revision"]}
    recorder.close()
    raw = writers[0].messages[1]["data"]
    assert raw["orders"][0]["price"] == 100
    assert raw["query_basis"] == older["query_basis"]


@pytest.mark.parametrize("older_scope", ["symbols", "account"])
def test_actual_mcap_global_set_fences_old_unknown_orders_but_accepts_newer_full_query(make_recording, older_scope):
    recorder, now, root, recording_id = make_recording()
    recorder.begin_execution("E", [])
    scope = {"kind": older_scope, "symbols": ["X"] if older_scope == "symbols" else []}
    older, account, fresh = (
        recorder.begin_query(scope["symbols"]),
        recorder.begin_query([]),
        recorder.begin_query(["X"]),
    )
    now[0] += 1
    complete(recorder, account, {"kind": "account", "symbols": []}, [])
    now[0] += 1
    complete(recorder, older, scope, [order(100)])
    assert recorder._orders == set()
    now[0] += 1
    complete(recorder, fresh, {"kind": "symbols", "symbols": ["X"]}, [order(102)])
    assert recorder._orders == {("X", "O")}
    now[0] += 1
    recorder.end_execution("E")
    finished(recorder)
    before = window(root, "E", recording_id, TIME + 3, TIME + 5)["state_before_start"]
    assert before["orders"] == {}
    assert "X" not in before["sync_generation"]
    assert "X" not in before["last_set_revision"]
    assert before["last_set_observation"] == {"": [account["sync_generation"], 2, 2]}
    after = window(root, "E", recording_id, TIME + 4, TIME + 5)["state_before_start"]
    assert after["orders"]['["X","O"]']["price"] == 102
    assert after["orders"]['["X","O"]']["confirmed_book"] is True
    full = replay_window(root, 7, "E", {"recording_id": recording_id, "start_us": TIME, "end_us": TIME + 5})
    sets = [event["data"] for event in full["messages"] if event["topic"] == "/order_set"]
    assert [rows["orders"][0]["price"] for rows in sets if rows["orders"]] == [100, 102]
