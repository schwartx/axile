"""自定义函数组合路由契约测试。"""

import pytest

from axile.server.portfolio_function import _normalize_target

CODE = "def calculate_portfolio(context):\n    return {'rb2610': 1.0}\n"


@pytest.mark.parametrize(
    "target",
    [[], {1: 0.5}, {"rb2610": True}, {"rb2610": "0.5"}, {"rb2610": float("inf")}],
)
def test_target_must_be_finite_symbol_weight_mapping(target: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        _normalize_target(target)
