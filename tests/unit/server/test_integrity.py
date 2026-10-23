"""在位性：可执行数量口径与权重回退."""

from axile.channels.cn_futures import canonicalize_cn_futures_symbol, quantize_cn_futures_quantity
from axile.server.integrity import REBALANCE_THRESHOLD, plan_executable_target

TONIGHT_EQUITY = 992_670.6124999999
TONIGHT_POSITIONS = [
    {
        "symbol": "c2611",
        "volume": 1.0,
        "market_value": 22800.0,
        "direction": "多头",
        "extra": {"net_position": 1.0},
    },
    {
        "symbol": "rb2610",
        "volume": 4.0,
        "market_value": 123100.0,
        "direction": "多头",
        "extra": {"net_position": 4.0},
    },
    {
        "symbol": "TA701",
        "volume": 2.0,
        "market_value": 55000.0,
        "direction": "空头",
        "extra": {"net_position": -2.0},
    },
    {
        "symbol": "m2701",
        "volume": 1.0,
        "market_value": 32960.0,
        "direction": "空头",
        "extra": {"net_position": -1.0},
    },
]
TONIGHT_TARGET = {"TA2701": -0.08, "c2611": 0.03, "m2701": -0.06, "rb2610": 0.13}


def _futures_plan(positions: list[object], target: dict[str, float], equity: float):
    return plan_executable_target(
        positions,
        target,
        equity,
        canonicalize_symbol=lambda symbol: canonicalize_cn_futures_symbol(symbol, reference_year=2026),
        quantize_target_quantity=quantize_cn_futures_quantity,
    )


def test_rebalance_threshold_kept_for_weight_fallback() -> None:
    """无数量口径时仍用 0.5 个百分点，与前端缺 quantities 时一致。"""
    assert REBALANCE_THRESHOLD == 0.5


def test_tonight_book_is_aligned_after_lot_quantize() -> None:
    """手数已截断到位：TA2701 与 TA701 合并后 off=0。"""
    plan = _futures_plan(TONIGHT_POSITIONS, TONIGHT_TARGET, TONIGHT_EQUITY)
    assert plan.weights == {"TA701": -0.08, "c2611": 0.03, "m2701": -0.06, "rb2610": 0.13}
    assert plan.quantities == {"TA701": -2.0, "c2611": 1.0, "m2701": -1.0, "rb2610": 4.0}
    assert plan.off_symbol_count == 0


def test_alias_does_not_split_one_contract() -> None:
    """同一郑商所合约不会因为四位/三位代码各算一只。"""
    plan = _futures_plan(TONIGHT_POSITIONS, TONIGHT_TARGET, TONIGHT_EQUITY)
    assert "TA2701" not in plan.weights
    assert "TA701" in plan.weights


def test_czce_option_alias_merges_like_futures() -> None:
    """郑商所期权四位年与三位年也合成一只。"""
    positions = [
        {
            "symbol": "TA701C5000",
            "volume": 1.0,
            "market_value": 2500.0,
            "direction": "多头",
            "extra": {"net_position": 1.0},
        }
    ]
    plan = _futures_plan(positions, {"TA2701C5000": 0.01}, 250_000.0)
    assert plan.weights == {"TA701C5000": 0.01}
    assert plan.quantities == {"TA701C5000": 1.0}
    assert plan.off_symbol_count == 0


def test_leftover_and_unsized_opens_count_as_off() -> None:
    """遗留空头 + 四个未持有目标（无一手名义）仍是 5 只待调整。"""
    positions = [{"symbol": "RM611", "market_value": 46000, "direction": "空头"}]
    target = {"TA701": -0.07, "c2611": 0.03, "m2701": -0.06, "rb2610": 0.12}
    plan = _futures_plan(positions, target, 993_114.59)
    assert plan.off_symbol_count == 5


def test_one_tradable_lot_delta_is_off() -> None:
    """现持 3 手、目标截成 4 手 → 真能买，计 1 只待调整。"""
    positions = [
        {
            "symbol": "rb2610",
            "volume": 3.0,
            "market_value": 92310.0,
            "direction": "多头",
            "extra": {"net_position": 3.0},
        }
    ]
    plan = _futures_plan(positions, {"rb2610": 0.13}, TONIGHT_EQUITY)
    assert plan.quantities == {"rb2610": 4.0}
    assert plan.off_symbol_count == 1
    assert plan.rows[0].symbol == "rb2610"
    assert plan.rows[0].action == "increase"
    assert plan.rows[0].side == "buy"
    assert not plan.rows[0].aligned


def test_plan_weight_fallback_without_canonicalization_keeps_alias() -> None:
    """权重回退不合并别名，保持旧口径。"""
    positions = [{"symbol": "RM611", "market_value": 46000, "direction": "空头"}]
    target = {"TA701": -0.07, "c2611": 0.03, "m2701": -0.06, "rb2610": 0.12}
    assert plan_executable_target(positions, target, 993_114.59).off_symbol_count == 5


def test_plan_weight_fallback_aligned_within_threshold() -> None:
    """当前权重与目标相差不超过 0.5 个百分点视为到位。"""
    positions = [{"symbol": "rb2610", "market_value": 120, "direction": "多头"}]
    assert plan_executable_target(positions, {"rb2610": 0.12}, 1000).off_symbol_count == 0


def test_plan_weight_fallback_ignores_both_zero() -> None:
    positions = [{"symbol": "ghost", "market_value": 0, "direction": "多头"}]
    assert plan_executable_target(positions, {"ghost": 0.0}, 1000).off_symbol_count == 0


def test_plan_weight_fallback_short_direction_english() -> None:
    positions = [{"symbol": "RM611", "market_value": 50_000, "direction": "short"}]
    assert plan_executable_target(positions, {}, 100_000).off_symbol_count == 1
