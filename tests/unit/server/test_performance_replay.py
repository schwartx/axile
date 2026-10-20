"""按可手算的货币盈亏验证绩效账本，覆盖采样不变性与独立资金。"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from axile.server.db.models.performance import PerformanceSettings
from axile.server.performance import calculate_performance, observation


def snapshot(index, price, quantity=1, *, asset=100, symbol="A", multiplier=1, weight=1, **sizing):
    return observation(
        SimpleNamespace(
            id=index,
            execution_id=f"exec-{index}",
            created_at=(datetime(2026, 1, 1, 9) + timedelta(hours=index)).isoformat(),
            input={"curr_target": {symbol: weight}},
            result={
                "account_assets": {"total_asset": asset},
                "symbol_results": {
                    symbol: {
                        "first_tick": {"last_price": price},
                        "sizing": {
                            "sizing_mode": "weight",
                            "status": "SIZED",
                            "target_quantity": quantity,
                            "equity": asset,
                            "reference_price": price,
                            "unit_notional": price * multiplier,
                            **sizing,
                        },
                    }
                },
            },
        )
    )


def calculate(items, fee=0, range_key="all"):
    return calculate_performance(
        items, PerformanceSettings(backtest_weight_type="cs", backtest_fee_rate=fee), range_key
    )


@pytest.mark.parametrize("range_key", ["all", "30", "90"])
def test_intraday_fixed_quantity_matches_equity_and_is_sampling_invariant(range_key):
    items = [snapshot(0, 100), snapshot(1, 110, asset=110), snapshot(2, 121, asset=121)]
    dense = calculate(items, range_key=range_key)
    sparse = calculate([items[0], items[-1]], range_key=range_key)
    assert dense.points[-1].portfolio_return == pytest.approx(0.21)
    assert dense.points[-1].portfolio_daily_return == pytest.approx(0.21)
    assert dense.points[-1].difference == pytest.approx(0)
    assert sparse.points[-1].portfolio_return == pytest.approx(dense.points[-1].portfolio_return)


def test_quantity_and_target_weight_ledgers_ignore_later_real_equity():
    items = [snapshot(0, 100), snapshot(1, 110, asset=200), snapshot(2, 121, asset=400)]
    result = calculate(items)
    assert result.points[-1].account_return == 3
    assert result.points[-1].portfolio_return == pytest.approx(0.21)
    assert result.points[-1].target_portfolio_return == pytest.approx(0.21)
    assert result.sizing_fallback_count == 0


@pytest.mark.parametrize("direction", [1, -1])
def test_futures_multiplier_and_cross_day_equity(direction):
    items = [snapshot(i * 24, price, direction * 2, multiplier=10) for i, price in enumerate([100, 102, 101])]
    result = calculate(items)
    assert result.points[-2].portfolio_return == pytest.approx(direction * 0.4)
    assert result.points[-1].portfolio_return == pytest.approx(direction * 0.2)
    assert result.points[-1].portfolio_daily_return == pytest.approx(-direction * 20 / (100 + direction * 40))


def test_add_reduce_reverse_and_clear_charge_quantity_turnover():
    items = [
        snapshot(i, price, q)
        for i, (price, q) in enumerate([(100, 1), (110, 2), (120, 1), (115, -1), (105, 0), (90, 0)])
    ]
    result = calculate(items, fee=0.001)
    # 旧仓盈亏：10+20-5+10=35；调仓名义成交额：110+120+230+105=565。
    assert result.points[-1].portfolio_return == pytest.approx((35 - 0.565) / 100)


def test_contract_roll_closes_old_contract_and_opens_new_one():
    first = snapshot(0, 100)
    roll = snapshot(1, 200, symbol="B")
    roll.prices["A"] = 110
    last = snapshot(2, 220, symbol="B")
    result = calculate([first, roll, last], fee=0.001)
    # A 赚 10、B 赚 20；换月卖 A@110 买 B@200，双边收费 0.31。
    assert result.points[-1].portfolio_return == pytest.approx(0.2969)


def test_missing_price_holds_old_quantity_until_recovery():
    first, missing, last = snapshot(0, 100), snapshot(1, 110, 2), snapshot(2, 120, 0)
    missing.prices = {}
    result = calculate([first, missing, last])
    assert result.skips.missing_ticks == 1
    assert result.points[-1].portfolio_return == pytest.approx(0.2)


def test_missing_conversion_falls_back_to_disclosed_original_weights():
    first = snapshot(0, 100, weight=0.5, reference_price=None)
    last = snapshot(1, 120, weight=0.5, reference_price=None)
    result = calculate([first, last])
    assert result.sizing_fallback_count == 2
    assert result.points[-1].sizing_fallback
    assert result.points[-1].portfolio_return == pytest.approx(0.1)


def test_account_currency_multiplier_uses_frozen_converted_notional():
    items = [snapshot(0, 100, multiplier=7), snapshot(1, 102, multiplier=7)]
    for item in items:
        assert item.target_units == {"A": 7}
    assert calculate(items).points[-1].portfolio_return == pytest.approx(0.14)


def test_currency_conversion_changes_do_not_create_quantity_turnover():
    items = [snapshot(0, 100, multiplier=7), snapshot(1, 100, multiplier=8)]
    assert calculate(items, fee=0.01).points[-1].portfolio_return == 0


def test_multiplier_and_conversion_rate_evidence_without_reference_price():
    items = [
        snapshot(i, price, reference_price=None, unit_multiplier=10, value_conversion={"rate": 7})
        for i, price in enumerate([100, 101])
    ]
    assert items[0].target_units == {"A": 70}
    assert calculate(items).points[-1].portfolio_return == pytest.approx(0.7)


def test_invalid_conversion_evidence_falls_back_whole_observation():
    item = snapshot(0, 100, reference_price=None, unit_multiplier=10, value_conversion={"rate": -1})
    assert item.target_units is None
    assert item.sizing_fallback


def test_partial_execution_keeps_theoretical_target_separate_from_real_holdings():
    first, last = snapshot(0, 100, 2), snapshot(1, 110, 2, asset=110)
    result = calculate([first, last])
    assert result.points[-1].account_return == pytest.approx(0.1)
    assert result.points[-1].portfolio_return == pytest.approx(0.2)


def test_original_weights_rebalance_using_their_own_equity():
    items = [snapshot(i, price, quantity=0, weight=0.5) for i, price in enumerate([100, 110, 121])]
    result = calculate(items)
    assert result.points[-1].portfolio_return == 0
    assert result.points[-1].target_portfolio_return == pytest.approx(1.05**2 - 1)


def test_quantity_clear_uses_exit_price_and_never_resumes_exposure():
    first = snapshot(0, 100, multiplier=10)
    clear = snapshot(1, 102, multiplier=10)
    clear.target = clear.target_weight = clear.target_units = {}
    last = snapshot(2, 120, quantity=0, multiplier=10, weight=0)
    assert calculate([first, clear, last], fee=0.001).points[-1].portfolio_return == pytest.approx(0.1898)


def test_daily_return_can_be_unknown_after_zero_equity_without_losing_cumulative():
    items = [snapshot(i * 24, price, 2, weight=0) for i, price in enumerate([100, 50, 60])]
    result = calculate(items)
    assert result.points[-2].portfolio_return == -1
    assert result.points[-1].portfolio_return == pytest.approx(-0.8)
    assert result.points[-1].portfolio_daily_return is None


def test_missing_initial_equity_does_not_invent_simulation_capital():
    first = snapshot(0, 100)
    first.asset = None
    assert calculate([first]).points == []
