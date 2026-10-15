"""
算法参数验证单元测试.

测试 BaseAlgorithmParams 和 ChaseParamsMixin 的参数验证规则。
"""

import pytest
from pydantic import ValidationError

from axile.executor.algorithms.common.params import (
    BaseAlgorithmParams,
    ChaseParamsMixin,
)


class TestChaseParamsMixin:
    """测试追单参数验证."""

    def test_total_chase_time_exceeds_limit(self):
        """测试总追单时间超过限制."""
        with pytest.raises(ValidationError) as exc_info:
            ChaseParamsMixin(
                chase_enabled=True,
                max_chase_count=50,  # 50次（不超过上限）
                chase_interval=15.0,  # 15秒间隔
                # 总时间 = 50 * 15 = 750秒 > 600秒
            )

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert "追单总时间" in errors[0]["msg"]
        assert "600" in errors[0]["msg"]

    def test_short_interval_with_many_chases(self):
        """测试短间隔配合多次追单."""
        with pytest.raises(ValidationError) as exc_info:
            ChaseParamsMixin(
                chase_enabled=True,
                max_chase_count=20,  # 20次
                chase_interval=0.5,  # 0.5秒间隔
                # 可能触发速率限制
            )

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert "速率限制" in errors[0]["msg"]


class TestCombinedParams:
    """测试组合参数."""

    def test_combined_validation_invalid_base(self):
        """测试组合参数中基础参数无效."""

        class TestParams(BaseAlgorithmParams, ChaseParamsMixin):
            pass

        with pytest.raises(ValidationError):
            TestParams(
                max_wait_seconds=4000,  # 超过上限
                chase_enabled=True,
            )

    def test_combined_validation_invalid_chase(self):
        """测试组合参数中追单参数无效."""

        class TestParams(BaseAlgorithmParams, ChaseParamsMixin):
            pass

        with pytest.raises(ValidationError):
            TestParams(
                max_wait_seconds=120,
                chase_enabled=True,
                max_chase_count=100,  # 过大
                chase_interval=10.0,
            )

        # 注意：chase_ticks=0 会触发 Field 的 ge=1 验证错误
        # 所以这个测试会失败，这是预期行为

        # 实际的警告应该在算法执行时输出


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
