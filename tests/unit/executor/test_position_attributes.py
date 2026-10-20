"""原生持仓展示字段不能补零、反推或把部分分项当作完整合计。"""

from datetime import datetime
from types import SimpleNamespace

import pytest

from axile.executor.position_attributes import native_position_attributes, sum_native_position_attributes


@pytest.mark.parametrize("invalid", [None, "", "bad", True, float("nan"), float("inf"), 1.7976931348623157e308])
def test_missing_and_invalid_native_fields_are_omitted(invalid):
    assert native_position_attributes({"pnl": invalid}, {"pnl": "pnl", "price": "price"}) == {}


def test_native_zero_and_negative_profit_survive_without_deriving_price():
    row = SimpleNamespace(pnl="-12.5", margin="0", notional="1000", volume="2")
    assert native_position_attributes(row, {"pnl": "pnl", "margin": "margin", "price": "entry_price"}) == {
        "pnl": -12.5,
        "margin": 0.0,
    }


def test_incomplete_native_totals_are_omitted():
    assert sum_native_position_attributes([{"pnl": 0.0, "margin": 2.0}, {"margin": 3.0}], ("pnl", "margin")) == {
        "margin": 5.0
    }


def test_native_dates_are_serialized_without_inventing_timestamps():
    assert native_position_attributes(
        {"time": datetime(2026, 10, 19, 10)}, {}, {"time": "time", "missing": "missing"}
    ) == {"time": "2026-10-19T10:00:00"}
