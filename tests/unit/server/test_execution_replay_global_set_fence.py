"""完整集合按范围的查询版本排序，不借单笔查询的交付序号规则。"""

import pytest

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
