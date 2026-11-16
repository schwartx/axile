"""
算法参数验证单元测试.

测试 BaseAlgorithmParams 和 ChaseParamsMixin 的参数验证规则。
"""

from dataclasses import dataclass

import pytest

from axile.common.serialization import parse_input
from axile.executor.algorithms.common.params import (
    BaseAlgorithmParams,
    ChaseParamsMixin,
)


class TestChaseParamsMixin:
    """测试追单参数验证."""

    def test_total_chase_time_exceeds_limit(self):
        """测试总追单时间超过限制."""
        with pytest.raises(ValueError) as exc_info:
            parse_input(ChaseParamsMixin, {"chase_enabled": True, "max_chase_count": 50, "chase_interval": 15.0})

        assert "追单总时间" in str(exc_info.value)
        assert "600" in str(exc_info.value)

    def test_short_interval_with_many_chases(self):
        """测试短间隔配合多次追单."""
        with pytest.raises(ValueError) as exc_info:
            parse_input(ChaseParamsMixin, {"chase_enabled": True, "max_chase_count": 20, "chase_interval": 0.5})

        assert "速率限制" in str(exc_info.value)


class TestCombinedParams:
    """测试组合参数."""

    def test_combined_validation_invalid_base(self):
        """测试组合参数中基础参数无效."""

        @dataclass(kw_only=True)
        class TestParams(BaseAlgorithmParams, ChaseParamsMixin):
            pass

        with pytest.raises(ValueError):
            parse_input(TestParams, {"max_wait_seconds": 4000, "chase_enabled": True})

    def test_combined_validation_invalid_chase(self):
        """测试组合参数中追单参数无效."""

        @dataclass(kw_only=True)
        class TestParams(BaseAlgorithmParams, ChaseParamsMixin):
            pass

        with pytest.raises(ValueError):
            parse_input(
                TestParams,
                {"max_wait_seconds": 120, "chase_enabled": True, "max_chase_count": 100, "chase_interval": 10.0},
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
