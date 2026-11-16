"""账户录制持续运行时的身份和文件边界。"""

from axile.server.execution_replay import recorder as module
from tests.unit.server.test_execution_replay_recorder import running as running


def test_old_terminal_history_cannot_evict_a_live_order_query_fence(running, monkeypatch):
    recorder, _, _, _ = running
    monkeypatch.setattr(module, "MAX_IDENTITIES", 2)
    query = recorder.begin_query(["X"])
    recorder.observe("/order", {"symbol": "X", "order_id": "LIVE", "status": "working"})
    for identity in ("O1", "O2", "O3"):
        recorder.observe("/order", {"symbol": "X", "order_id": identity, "status": "canceled"})
    assert ("X", "LIVE") in recorder._order_changes
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
    assert ("X", "LIVE") in recorder._orders
    assert recorder.status()["state"] == "recording"


def test_live_orders_do_not_stop_recording_when_the_recent_cache_is_full(running, monkeypatch):
    recorder, _, _, _ = running
    monkeypatch.setattr(module, "MAX_IDENTITIES", 2)
    for order_id in ("O1", "O2", "O3"):
        recorder.observe("/order", {"symbol": "X", "order_id": order_id, "status": "working"})
    assert recorder.status()["state"] == "recording"
    assert all(("X", order_id) in recorder._orders for order_id in ("O1", "O2", "O3"))


def test_seed_and_concurrent_queries_are_not_limited_by_recent_cache(running, monkeypatch):
    recorder, owner, _, _ = running
    monkeypatch.setattr(module, "MAX_IDENTITIES", 2)
    owner.replay_seed_observations = lambda: iter(
        ("/order", {"symbol": "X", "order_id": f"O{index}", "status": "working"}) for index in range(3)
    )
    recorder._seed_cached_observations(recorder._session)
    tokens = [recorder.begin_query(["A", "B", "C"]) for _ in range(3)]
    assert all(token is not None for token in tokens)
    assert len(recorder._orders) == 3
    assert recorder.status()["state"] == "recording"
