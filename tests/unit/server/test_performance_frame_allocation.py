"""Skipping unused frames preserves complete ledger results and retired oracle defaults."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from axile.server.db.models.performance import PerformanceSettings
from tests.unit.server import _performance_oracle as performance


def observations():
    """Include explicit zero, absent targets, exit-price gaps and recovery."""
    result = []
    for index in range(120):
        prices = {"A": 100.0 + index * 0.125, "B": 55.123456789012345}
        target = {"A": 0.1234567890123456, "B": -0.25}
        if index % 13 == 0:
            target = {}
        if index % 17 == 0:
            prices = {"B": prices["B"]}
        if index % 19 == 0:
            target = None
        result.append(
            performance.Observation(
                id=index,
                execution_id=str(index),
                time=datetime(2026, 1, 1) + timedelta(days=index),
                asset=1000.1234567890123 + index,
                target=target,
                prices=prices,
                target_weight=None if target is None else dict(target),
            )
        )
    return result


@pytest.mark.parametrize("range_key", ["all", "30", "90"])
@pytest.mark.parametrize("fee", [0.0, 0.000123456789012345])
@pytest.mark.parametrize("empty", [False, True])
def test_complete_ledger_matches_materialized_reference_without_dataframe(range_key, fee, empty):
    items = [] if empty else observations()
    settings = PerformanceSettings(backtest_weight_type="cs", backtest_fee_rate=fee)
    original = performance.build_wbt_input

    def materialized(items, target_weight=False, **_kwargs):
        return original(items, target_weight, materialize_frame=True)

    with patch.object(performance, "build_wbt_input", materialized):
        expected = performance.calculate_performance(items, settings, range_key)
    # Fails immediately if the oracle starts constructing either frame again.
    with patch("pandas.DataFrame", side_effect=AssertionError("Unused frame allocated")):
        actual = performance.calculate_performance(items, settings, range_key)
    assert actual.model_dump_json() == expected.model_dump_json()


def test_retired_oracle_preserves_the_original_typed_frame():
    result = performance.build_wbt_input(observations())
    assert result.frame is not None
    assert list(result.frame.columns) == ["dt", "symbol", "weight", "price"]
    assert str(result.frame.dtypes["weight"]) == "float64"
    assert str(result.frame.dtypes["price"]) == "float64"
    assert not result.frame.empty
