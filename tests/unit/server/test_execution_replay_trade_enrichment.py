"""成交的物理事实补全与采集兴趣、原生查询水位保持一致。"""

import pytest

from tests.unit.server.test_execution_replay_native import TIME, finished, trade, window
from tests.unit.server.test_execution_replay_native import make_recording as make_recording


@pytest.mark.parametrize(
    "first,completion,changed",
    [
        *[
            ({field: None}, {field: value}, True)
            for field, value in [
                ("symbol", "X"),
                ("order_id", "O"),
                ("price", 100),
                ("quantity", 1),
                ("direction", "BUY"),
                ("position_side", "NET"),
                ("offset", "OPEN"),
            ]
        ],
        ({"field_quality": {"quantity": "missing"}}, {"field_quality": {"quantity": "known"}}, True),
        ({"field_quality": {"quantity": "invalid"}}, {"field_quality": {"quantity": "known"}}, True),
        ({}, {}, False),
        ({}, {"origin": "execution"}, False),
        ({}, {"quantity": 1.0, "direction": "buy"}, False),
        ({}, {"quantity": 99, "field_quality": {"quantity": "missing"}}, False),
        ({}, {"quantity": 2}, False),
        ({"field_quality": {"quantity": "missing"}}, {"quantity": 2}, False),
        ({"field_quality": {"quantity": "missing"}}, {"quantity": 2, "field_quality": {"quantity": "invalid"}}, False),
    ],
)
def test_trade_completion_and_duplicates_use_the_same_query_watermark(make_recording, first, completion, changed):
    recorder, now, root, recording_id = make_recording()
    recorder.begin_execution("E", ["X"])
    now[0] += 1
    recorder.observe(
        "/order",
        {
            "symbol": "X",
            "order_id": "O",
            "status": "working",
            "quantity": 2,
            "remaining_quantity": 2,
            "filled_quantity": 0,
            "price": 100,
            "confirmed_book": True,
        },
    )
    original = {**trade("T", 1, order_id="O", execution_id="E"), **first}
    now[0] += 1
    recorder.observe("/trade", original)
    query = recorder.begin_query(["X"])
    start = query["query_basis"]["start_seq"]
    now[0] += 1
    enriched = {**original, **completion}
    if "field_quality" not in completion:
        enriched.pop("field_quality", None)
    recorder.observe("/trade", enriched)
    now[0] += 1
    recorder.observe(
        "/order_set",
        {
            **query,
            "scope": {"kind": "symbols", "symbols": ["X"]},
            "scope_complete": True,
            "synchronized": True,
            "orders": [],
        },
    )
    recorder.finish_query(query)
    tracked = ("X", "O") in recorder._orders
    tracking_sequence = recorder._order_changes[("X", "O")]
    now[0] += 1
    recorder.end_execution("E")
    now[0] += 6_000_000
    recorder.observe("/quote/X", {"symbol": "X", "bids": [], "asks": [], "book_valid": False})
    finished(recorder)
    full = window(root, "E", recording_id, TIME, now[0] + 1)
    restored = window(root, "E", recording_id, now[0], now[0] + 1)["state_before_start"]
    assert tracked is changed
    assert (tracking_sequence > start) is changed
    assert restored["orders"]['["X","O"]']["confirmed_book"] is changed
    assert (restored["order_change_seq"]['["X","O"]'] > start) is changed
    assert any(event["topic"] == "/quote/X" for event in full["messages"]) is changed
