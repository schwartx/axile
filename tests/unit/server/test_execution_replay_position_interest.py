"""实际 MCAP 验证持仓分项兴趣，明确零量不等于未知分项。"""

import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from axile.server.execution_replay import recorder as module
from axile.server.execution_replay.recorder import TAIL_US
from axile.server.execution_replay.service import replay_window
from tests.unit.server.test_execution_replay_native import (
    TIME,
    drained,
    finished,
    wait_for,
    window,
)
from tests.unit.server.test_execution_replay_native import (
    make_recording as make_recording,
)


def position(quantity, side="NET", *, symbol="X", **extra):
    return {"symbol": symbol, "position_side": side, "quantity": quantity, **extra}


def observe_positions(recorder, rows, *, complete=False, symbols=None, **extra):
    recorder.observe(
        "/account_observation",
        {
            "scope": {"kind": "account" if symbols is None else "symbols", "symbols": symbols or []},
            "scope_complete": complete,
            "positions": rows,
            "position_basis": {"status": "unverified", "proof": None},
            **extra,
        },
    )


def observe_quote(recorder, now, symbol="X"):
    recorder.observe(
        f"/quote/{symbol}",
        {
            "symbol": symbol,
            "bids": [{"price": 99, "quantity": 2}],
            "asks": [{"price": 100, "quantity": 3}],
            "book_valid": True,
            "valid_until_us": now[0] + TAIL_US,
        },
    )


def read_messages(recorder, now, root, recording_id):
    finished(recorder)
    return replay_window(root, 7, "E", {"recording_id": recording_id, "start_us": TIME, "end_us": now[0] + 2})[
        "messages"
    ]


def test_partial_net_zero_releases_quotes_after_tail_and_keeps_zero_observation(make_recording):
    recorder, now, root, recording_id = make_recording()
    recorder.begin_execution("E", ["X"])
    observe_positions(recorder, [position(2)])
    now[0] += 1
    observe_positions(recorder, [position(0)])
    recorder.end_execution("E")
    now[0] += TAIL_US - 1
    observe_quote(recorder, now)
    observe_positions(recorder, [position(0)])
    now[0] += 2
    observe_quote(recorder, now)
    messages = read_messages(recorder, now, root, recording_id)
    assert len([row for row in messages if row["topic"] == "/quote/X"]) == 1
    state = window(root, "E", recording_id, now[0] + 1, now[0] + 2)["state_before_start"]
    assert state["observed_positions"]['["X","NET"]']["quantity"] == 0


def test_partial_hedge_zero_keeps_the_other_side_until_its_own_zero(make_recording):
    recorder, now, root, recording_id = make_recording()
    recorder.begin_execution("E", [])
    observe_positions(recorder, [position(2, "LONG"), position(3, "SHORT")])
    observe_positions(recorder, [position(0, "LONG")])
    now[0] += TAIL_US + 1
    observe_quote(recorder, now)
    now[0] += 1
    observe_positions(recorder, [position(0, "SHORT")])
    observe_quote(recorder, now)
    now[0] += TAIL_US + 1
    observe_quote(recorder, now)
    messages = read_messages(recorder, now, root, recording_id)
    assert len([row for row in messages if row["topic"] == "/quote/X"]) == 2


@pytest.mark.parametrize(
    "row",
    [
        position(None),
        position(0, field_quality={"quantity": "invalid"}),
        position(0, field_quality={"quantity": "missing"}),
        position(0, "unknown"),
    ],
)
def test_unknown_quantity_or_side_does_not_release_a_known_position(make_recording, row):
    recorder, now, root, recording_id = make_recording()
    recorder.begin_execution("E", [])
    observe_positions(recorder, [position(2)])
    observe_positions(recorder, [row])
    now[0] += TAIL_US + 1
    observe_quote(recorder, now)
    messages = read_messages(recorder, now, root, recording_id)
    assert len([item for item in messages if item["topic"] == "/quote/X"]) == 1


def test_complete_scope_clears_only_its_parts_and_duplicate_empty_does_not_extend_tail(make_recording):
    recorder, now, root, recording_id = make_recording()
    recorder.begin_execution("E", [])
    observe_positions(recorder, [position(2), position(3, "SHORT", symbol="Y")])
    now[0] += 1
    observe_positions(recorder, [], complete=True, symbols=["X"])
    now[0] += TAIL_US + 1
    observe_quote(recorder, now)
    observe_quote(recorder, now, "Y")
    observe_positions(recorder, [], complete=True)
    now[0] += TAIL_US - 1
    observe_positions(recorder, [], complete=True)
    now[0] += 2
    observe_quote(recorder, now, "Y")
    messages = read_messages(recorder, now, root, recording_id)
    assert [row["topic"] for row in messages if row["topic"].startswith("/quote/")] == ["/quote/Y"]


def test_old_generation_cannot_clear_interest_but_a_current_zero_can(make_recording):
    recorder, now, root, recording_id = make_recording()
    recorder.begin_execution("E", [])
    observe_positions(recorder, [position(2)])
    old_generation = recorder._sync_generation
    recorder.observe("/connection", {"connected": False})
    observe_positions(recorder, [], complete=True, sync_generation=old_generation)
    observe_positions(recorder, [position(0)], sync_generation=old_generation)
    now[0] += TAIL_US + 1
    observe_quote(recorder, now)
    observe_positions(recorder, [position(0)], sync_generation=recorder._sync_generation)
    now[0] += TAIL_US + 1
    observe_quote(recorder, now)
    messages = read_messages(recorder, now, root, recording_id)
    assert len([row for row in messages if row["topic"] == "/quote/X"]) == 1


def test_same_owner_recording_switch_keeps_interest_without_copying_position_facts(make_recording):
    recorder, now, root, first_recording_id = make_recording()
    recorder.begin_execution("E", [])
    observe_positions(recorder, [position(2)])
    old_observer = recorder._owner._replay_observer
    recorder.configure(False, 2)
    recorder.configure(True, 3)
    wait_for(
        lambda: recorder.status()["state"] == "recording" and recorder.status()["recording_id"] != first_recording_id
    )
    recording_id = recorder.status()["recording_id"]
    old_observer("/account_observation", {"positions": [position(0)]})
    now[0] += 1
    observe_quote(recorder, now)
    now[0] += 1
    observe_positions(recorder, [position(0)])
    now[0] += TAIL_US + 1
    observe_quote(recorder, now)
    messages = read_messages(recorder, now, root, recording_id)
    assert len([row for row in messages if row["topic"] == "/quote/X"]) == 1
    observations = [row for row in messages if row["topic"] == "/account_observation"]
    assert [row["data"]["positions"][0]["quantity"] for row in observations] == [0]


def test_partial_zero_does_not_clear_unknown_seed_but_complete_scope_does(make_recording):
    recorder, now, root, first_id = make_recording()
    recorder.begin_execution("E", [])
    owner = SimpleNamespace(replay_interest_symbols=lambda: {"positions": ["X"]})
    recorder.attach(owner)
    wait_for(lambda: recorder.status().get("recording_id") != first_id and "X" in recorder._interest_positions)
    recording_id = recorder.status()["recording_id"]
    observe_positions(recorder, [position(0, "LONG")])
    now[0] += TAIL_US + 1
    observe_quote(recorder, now)
    observe_positions(recorder, [], complete=True, symbols=["X"])
    now[0] += TAIL_US + 1
    observe_quote(recorder, now)
    messages = read_messages(recorder, now, root, recording_id)
    assert len([row for row in messages if row["topic"] == "/quote/X"]) == 1


def test_new_owner_discards_old_position_parts_and_seeds_its_own_interest(make_recording):
    recorder, now, root, first_id = make_recording()
    recorder.begin_execution("E", [])
    observe_positions(recorder, [position(2, "LONG")])
    owner = SimpleNamespace(replay_interest_symbols=lambda: {"positions": ["Y"]})
    recorder.attach(owner)
    wait_for(lambda: recorder.status().get("recording_id") != first_id and "Y" in recorder._interest_positions)
    recording_id = recorder.status()["recording_id"]
    observe_quote(recorder, now)
    observe_quote(recorder, now, "Y")
    messages = read_messages(recorder, now, root, recording_id)
    assert [row["topic"] for row in messages if row["topic"].startswith("/quote/")] == ["/quote/Y"]


def test_position_parts_share_the_existing_identity_budget(make_recording, monkeypatch):
    recorder, _, _, _ = make_recording()
    monkeypatch.setattr(module, "MAX_IDENTITIES", 2)
    observe_positions(recorder, [position(2, "LONG"), position(3, "SHORT")])
    assert recorder.status()["state"] == "recording"
    observe_positions(recorder, [position(4, symbol="Y")])
    assert recorder.status()["error"] == "replay_identity_budget"


@contextmanager
def blocked_seed(recorder, groups):
    entered, release = threading.Event(), threading.Event()

    def seed():
        entered.set()
        assert release.wait(3)
        if isinstance(groups, Exception):
            raise groups
        return groups

    recorder.attach(SimpleNamespace(replay_interest_symbols=seed))
    try:
        assert entered.wait(3)
        yield recorder.status()["recording_id"], release
    finally:
        release.set()


@pytest.mark.parametrize("symbols", [None, ["X"]])
def test_complete_position_scope_wins_over_an_inflight_old_seed(make_recording, symbols):
    recorder, now, root, _ = make_recording()
    recorder.begin_execution("E", [])
    with blocked_seed(recorder, {"positions": ["X"]}) as (recording_id, release):
        observe_positions(recorder, [], complete=True, symbols=symbols)
        release.set()
        wait_for(lambda: drained(recorder))
        now[0] += TAIL_US + 1
        observe_quote(recorder, now)
        messages = read_messages(recorder, now, root, recording_id)
    assert not [row for row in messages if row["topic"] == "/quote/X"]


def test_partial_position_scope_does_not_suppress_unknown_inflight_seed(make_recording):
    recorder, now, root, _ = make_recording()
    recorder.begin_execution("E", [])
    with blocked_seed(recorder, {"positions": ["X"]}) as (recording_id, release):
        observe_positions(recorder, [position(0, "LONG")], symbols=["X"])
        release.set()
        wait_for(lambda: drained(recorder))
        now[0] += TAIL_US + 1
        observe_quote(recorder, now)
        messages = read_messages(recorder, now, root, recording_id)
    assert len([row for row in messages if row["topic"] == "/quote/X"]) == 1


@pytest.mark.parametrize("symbols", [None, ["X"]])
@pytest.mark.parametrize("valid", [False, True])
def test_only_proven_complete_order_scope_supersedes_the_inflight_seed(make_recording, symbols, valid):
    recorder, now, root, _ = make_recording()
    recorder.begin_execution("E", [])
    with blocked_seed(recorder, {"orders": ["X"]}) as (recording_id, release):
        token = recorder.begin_query(symbols or [])
        assert token is not None
        recorder.observe(
            "/order_set",
            {
                **token,
                "scope": {"kind": "account" if symbols is None else "symbols", "symbols": symbols or []},
                "scope_complete": True,
                "synchronized": valid,
                "orders": [],
            },
        )
        release.set()
        wait_for(lambda: drained(recorder))
        now[0] += TAIL_US + 1
        observe_quote(recorder, now)
        messages = read_messages(recorder, now, root, recording_id)
    assert len([row for row in messages if row["topic"] == "/quote/X"]) == (0 if valid else 1)


def test_replaced_owner_does_not_inherit_a_blocked_seed_scope_or_hints(make_recording):
    recorder, now, root, _ = make_recording()
    recorder.begin_execution("E", [])
    with blocked_seed(recorder, {"positions": ["X"]}) as (blocked_id, release):
        observe_positions(recorder, [], complete=True)
        recorder.attach(SimpleNamespace(replay_interest_symbols=lambda: {"positions": ["Y"]}))
        release.set()
        wait_for(lambda: recorder.status().get("recording_id") != blocked_id and "Y" in recorder._interest_positions)
        recording_id = recorder.status()["recording_id"]
        observe_quote(recorder, now)
        observe_quote(recorder, now, "Y")
        messages = read_messages(recorder, now, root, recording_id)
    assert [row["topic"] for row in messages if row["topic"].startswith("/quote/")] == ["/quote/Y"]


def test_seed_failure_releases_its_temporary_scope_tracking(make_recording):
    recorder, _, _, _ = make_recording()
    recorder.begin_execution("E", [])
    with blocked_seed(recorder, RuntimeError("isolated seed failure")) as (_, release):
        session = recorder._session
        observe_positions(recorder, [], complete=True, symbols=["X"])
        release.set()
        wait_for(lambda: recorder._session is None)
    assert recorder.status()["state"] == "error"
    assert session.seed_interest_scopes == {}


def test_seed_scope_tracking_is_bounded_and_cleared_after_failure(make_recording, monkeypatch):
    recorder, _, _, _ = make_recording()
    recorder.begin_execution("E", [])
    with blocked_seed(recorder, {"positions": ["X"]}) as (_, release):
        session = recorder._session
        monkeypatch.setattr(module, "MAX_IDENTITIES", 2)
        observe_positions(recorder, [], complete=True, symbols=["X", "Y", "Z"])
        assert recorder.status()["error"] == "replay_identity_budget"
        release.set()
        wait_for(lambda: recorder._session is None)
    assert session.seed_interest_scopes == {}


def test_owner_without_seed_does_not_accumulate_temporary_scope_history(make_recording):
    recorder, _, _, _ = make_recording()
    wait_for(lambda: recorder._session.seed_interest_scopes == {})
    observe_positions(recorder, [], complete=True, symbols=["X"])
    assert recorder._session.seed_interest_scopes == {}
