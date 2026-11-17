"""目标数量事实的就绪、历史复用与模式隔离。"""

import pytest

from axile.server.target_sizing import resolve_account_target


def snapshot(execution_id="new", *, mode="weight", targets=None):
    return {
        "id": 2,
        "account_id": 1,
        "portfolio_id": 7,
        "target_mode": mode,
        "raw_weights": {"SM2701": 0.02} if targets is None else targets,
        "normalized_weights": {"SM2701": 0.02} if targets is None else targets,
        "source": "execution" if execution_id else "manual",
        "execution_id": execution_id,
        "calculated_at": "2026-11-17T10:00:00",
    }


def facts(quantity=0, *, mode="weight", ready=True):
    return {
        "record_id": 1 if ready else None,
        "input_id": "input",
        "artifact_versions": {},
        "target_context": {"weight_precision": 0.01},
        "sizings": {
            "SM701": {"reason_code": "COMMON.SIZING.QUANTIZED", "sizing_mode": mode, "target_quantity": quantity}
        },
    }


def canonical(symbol):
    return "SM701" if symbol == "SM2701" else symbol


@pytest.mark.parametrize("quantity", [0, -1, 0.015])
def test_recorded_quantities_and_zero_are_ready(quantity):
    result = resolve_account_target(
        {"snapshot": snapshot(), "previous": [], "executions": {"new": facts(quantity)}}, canonical
    )
    assert result["quantities"] == {"SM701": quantity}
    assert result["sizing"]["status"] == "available"


@pytest.mark.parametrize("quantity", [None, float("nan"), float("inf")])
def test_invalid_quantities_do_not_produce_conclusions(quantity):
    result = resolve_account_target(
        {"snapshot": snapshot(), "previous": [], "executions": {"new": facts(quantity)}}, canonical
    )
    assert result["quantities"] is None


@pytest.mark.parametrize("mode", ["weight", "quantity"])
def test_inflight_does_not_fall_back(mode):
    result = resolve_account_target(
        {
            "snapshot": snapshot(mode=mode),
            "previous": [snapshot("old", mode=mode)],
            "executions": {"new": facts(mode=mode, ready=False), "old": facts(mode=mode)},
        },
        canonical,
    )
    assert result["quantities"] is None
    assert result["sizing"]["status"] == "pending_execution"
    assert result["sizing"]["execution_id"] == "new"


@pytest.mark.parametrize("mode", ["weight", "quantity"])
def test_manual_reuses_one_same_mode_target(mode):
    result = resolve_account_target(
        {
            "snapshot": snapshot(None, mode=mode),
            "previous": [
                snapshot("wrong", mode="quantity" if mode == "weight" else "weight"),
                snapshot("old", mode=mode),
            ],
            "executions": {"old": facts(mode=mode)},
        },
        canonical,
    )
    assert result["quantities"] == {"SM701": 0}
    assert result["sizing"]["execution_id"] == "old"
    assert result["execution_id"] is None


def test_incomplete_history_is_not_joined():
    current = snapshot(None, targets={"SM2701": 0.02, "rb2610": 0.1})
    other = facts()
    other["sizings"] = {"rb2610": {"reason_code": "COMMON.SIZING.EXACT", "target_quantity": 1}}
    result = resolve_account_target(
        {
            "snapshot": current,
            "previous": [
                {**current, "execution_id": "a", "source": "execution"},
                {**current, "execution_id": "b", "source": "execution"},
            ],
            "executions": {"a": facts(), "b": other},
        },
        canonical,
    )
    assert result["quantities"] is None


def test_empty_executed_target_is_ready():
    evidence = facts()
    evidence["sizings"] = {}
    result = resolve_account_target(
        {"snapshot": snapshot(targets={}), "previous": [], "executions": {"new": evidence}}, canonical
    )
    assert result["quantities"] == {}
    assert result["sizing"]["status"] == "available"


@pytest.mark.parametrize("value", [[], {"SM2701": None}, {"SM2701": True}, {"SM2701": float("inf")}])
def test_invalid_target_snapshot_is_unavailable(value):
    result = resolve_account_target({"snapshot": snapshot(targets=value), "previous": [], "executions": {}}, canonical)
    assert result["quantities"] is None
    assert result["sizing"]["status"] == "unavailable"


def test_malformed_extra_sizing_does_not_imply_close_to_zero():
    evidence = facts()
    evidence["sizings"]["rb2610"] = {"target_quantity": 1}
    result = resolve_account_target(
        {"snapshot": snapshot(), "previous": [], "executions": {"new": evidence}}, canonical
    )
    assert result["quantities"] is None


def test_unavailable_leftover_cannot_be_inferred_zero():
    evidence = facts()
    evidence["sizings"]["rb2610"] = {"reason_code": "COMMON.SIZING.MISSING_MARKET_DATA", "target_quantity": None}
    result = resolve_account_target(
        {"snapshot": snapshot(), "previous": [], "executions": {"new": evidence}}, canonical
    )
    assert result["quantities"] is None
