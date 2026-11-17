"""持仓对账只比较已记录的目标数量。"""

import pytest

from axile.channels.cn_futures import canonicalize_cn_futures_symbol
from axile.server.integrity import plan_executable_target


def _plan(positions, weights, quantities, equity=1_000_000):
    return plan_executable_target(
        positions,
        weights,
        quantities,
        equity,
        canonicalize_symbol=lambda symbol: canonicalize_cn_futures_symbol(symbol, reference_year=2026),
    )


@pytest.mark.parametrize("weight", [-0.12, 0.02])
def test_quantized_zero_without_position_is_aligned(weight):
    plan = _plan([], {"SM701": weight}, {"SM701": 0})
    assert plan.off_symbol_count == 0
    row = plan.rows[0]
    assert row.current_quantity == row.target_quantity == 0
    assert row.target_weight == weight
    assert row.aligned
    assert (row.action, row.side) == ("aligned", "none")


@pytest.mark.parametrize(
    ("current", "target", "action", "side"),
    [
        (1, 0, "close", "sell"),
        (-1, 0, "close", "buy"),
        (3, 4, "increase", "buy"),
        (4, 3, "reduce", "sell"),
        (-3, -7, "increase", "sell"),
        (-7, -3, "reduce", "buy"),
        (1, -1, "flip", "sell"),
        (-1, 1, "flip", "buy"),
    ],
)
def test_actions_use_quantity_even_when_weight_suggests_otherwise(current, target, action, side):
    positions = [
        {"symbol": "m2701", "volume": abs(current), "market_value": 1, "direction": "多头" if current > 0 else "空头"}
    ]
    plan = _plan(positions, {"m2701": 0.9}, {"m2701": target})
    row = plan.rows[0]
    assert plan.off_symbol_count == 1
    assert (row.action, row.side) == (action, side)


def test_aliases_merge_positions_and_recorded_quantities():
    positions = [
        {"symbol": "TA2701", "volume": 1, "direction": "空头", "market_value": 25000},
        {"symbol": "TA701", "volume": 1, "direction": "空头", "market_value": 25000},
    ]
    plan = _plan(positions, {"TA2701": -0.08}, {"TA2701": -2})
    assert plan.weights == {"TA701": -0.08}
    assert plan.quantities == {"TA701": -2}
    assert len(plan.rows) == 1
    assert plan.rows[0].current_quantity == -2
    assert plan.off_symbol_count == 0


def test_recorded_quantity_stays_aligned_despite_new_equity_and_price():
    positions = [{"symbol": "rb2610", "volume": 4, "market_value": 200000, "direction": "多头"}]
    plan = _plan(positions, {"rb2610": 0.13}, {"rb2610": 4}, equity=500000)
    assert plan.rows[0].current_weight == 0.4
    assert plan.off_symbol_count == 0


def test_removed_symbol_needs_closing():
    positions = [{"symbol": "m2701", "volume": 2, "market_value": 60000, "direction": "空头"}]
    plan = _plan(positions, {}, {})
    assert plan.off_symbol_count == 1
    assert plan.rows[0].target_quantity == 0
    assert (plan.rows[0].action, plan.rows[0].side) == ("close", "buy")


def test_empty_target_and_book_are_ready():
    plan = _plan([], {}, {})
    assert plan.off_symbol_count == 0
    assert plan.rows == ()


@pytest.mark.parametrize("quantities", [{}, {"SM701": float("nan")}, {"SM701": float("inf")}, {"SM701": True}])
def test_missing_or_invalid_target_quantity_is_rejected(quantities):
    with pytest.raises(ValueError, match="目标数量"):
        _plan([], {"SM701": 0.02}, quantities)


@pytest.mark.parametrize(
    "position",
    [
        {"symbol": "SM701", "market_value": 100},
        {"symbol": "SM701", "volume": "bad", "market_value": 100},
        {"symbol": "SM701", "volume": float("nan"), "market_value": 100},
    ],
)
def test_missing_actual_quantity_is_not_assumed_zero(position):
    plan = _plan([position], {"SM701": 0.02}, {"SM701": 0})
    assert plan.off_symbol_count is None
    assert plan.rows == ()


def test_net_position_takes_precedence_over_volume():
    positions = [{"symbol": "m2701", "volume": 9, "market_value": 60000, "extra": {"net_position": -2}}]
    plan = _plan(positions, {"m2701": -0.06}, {"m2701": -2})
    assert plan.off_symbol_count == 0
