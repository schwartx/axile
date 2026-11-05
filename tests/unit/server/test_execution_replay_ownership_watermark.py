"""归属补全不充当新的场内证据；真实报单入口和 MCAP 恢复回归。"""

import json
import threading
from types import SimpleNamespace

from axile.executor.order_submission_context import order_submission_context
from axile.executor.replay_actions import replay_operation
from tests.unit.server.test_execution_replay_native import TIME, finished, trade, window
from tests.unit.server.test_execution_replay_native import make_recording as make_recording


class Operations:
    @replay_operation("place_order")
    def place_order(self, symbol, direction, volume, price, order_type):
        return self.operation_body()


def test_late_place_result_cannot_override_a_complete_order_query(make_recording):
    recorder, now, root, recording_id = make_recording()
    owner = recorder._owner
    recorder.begin_execution("E", ["X"])
    entered, release = threading.Event(), threading.Event()
    outcome = {}

    def body():
        entered.set()
        assert release.wait(3)
        return SimpleNamespace(order_id="O")

    def place():
        try:
            with order_submission_context({"execution_id": "E"}):
                outcome["result"] = Operations.place_order(owner, "X", "BUY", 2, 100, "LIMIT")
        except BaseException as error:
            outcome["error"] = error

    owner.operation_body = body
    thread = threading.Thread(target=place)
    thread.start()
    try:
        assert entered.wait(1)
        now[0] += 1
        recorder.observe(
            "/order",
            {
                "symbol": "X",
                "order_id": "O",
                "status": "working",
                "confirmed_book": True,
                "quantity": 2,
                "filled_quantity": 0,
                "remaining_quantity": 2,
                "price": 100,
            },
        )
        now[0] += 1
        recorder.observe("/trade", trade("T", 1, order_id="O", origin="unknown"))
        query = recorder.begin_query(["X"])
        physical_seq = query["query_basis"]["start_seq"]
        now[0] += 1
        release.set()
        thread.join(3)
        assert not thread.is_alive()
        assert "error" not in outcome
        assert outcome["result"].order_id == "O"
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
        assert not recorder._orders
        assert recorder._order_changes[("X", "O")] == physical_seq
        now[0] += 1
        recorder.end_execution("E")
        now[0] += 6_000_000
        recorder.observe("/quote/X", {"symbol": "X", "bids": [], "asks": [], "book_valid": False})
        finished(recorder)
    finally:
        release.set()
        thread.join(3)

    full = window(root, "E", recording_id, TIME, now[0] + 1)
    restored = window(root, "E", recording_id, TIME + 5, now[0] + 1)
    state = restored["state_before_start"]
    order = state["orders"]['["X","O"]']
    assert order["confirmed_book"] is False
    assert order["confirmation_state"] == "absent"
    assert order["execution_id"] == "E"
    assert order["filled_quantity"] == 1
    assert state["order_change_seq"]['["X","O"]'] == physical_seq
    own = state["trade_accounting"]["own_records"]["T"]
    assert own["data"]["execution_id"] == "E"
    assert own["last_fact_seq"] == own["first_capture_seq"] == physical_seq
    assert state["trade_accounting"]["execution_totals"]["E"]["by_symbol"]["X"]["quantity"] == 1
    assert not any(event["topic"] == "/quote/X" for event in full["messages"])
    assert [event["data"]["phase"] for event in full["messages"] if event["topic"] == "/command"] == [
        "request",
        "returned",
        "result",
    ]
    (root.parent / "ownership-query-truth.json").write_text(json.dumps({"full": full, "restored": restored}))
