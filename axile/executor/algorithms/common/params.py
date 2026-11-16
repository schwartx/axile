"""
算法参数模型公共基础类.

提供所有算法参数的基础类和混入类，包含完整的参数验证规则。
"""

from dataclasses import dataclass, field
from typing import Annotated

from msgspec import Meta


@dataclass(kw_only=True)
class BaseAlgorithmParams:
    """
    定义所有算法共享的基础参数。

    Notes
    -----
    该基类只放跨算法都适用的运行时约束，避免渠道或策略特有参数污染所有
    参数模型的公共接口。
    """

    max_wait_seconds: Annotated[
        int,
        Meta(
            ge=1,
            le=3600,
            title="最大等待",
            description="本轮等待成交的上限，不保证到时完成。",
            extra_json_schema={"x-order": 20, "x-unit": "秒", "x-control": "stepper"},
        ),
    ] = field(default=60)

    def __str__(self) -> str:
        """便于记录日志的字符串表示."""
        return f"BaseAlgorithmParams(max_wait_seconds={self.max_wait_seconds})"


@dataclass(kw_only=True)
class ChaseParamsMixin:
    """
    为算法参数补充追单相关配置。

    Notes
    -----
    该 mixin 同时承担两类约束：字段级的基础范围校验，以及多字段联动的
    一致性校验。后者主要用于拦截会显著放大成交滑点、运行时长或交易所限频
    风险的配置组合。
    """

    chase_enabled: Annotated[
        bool,
        Meta(
            title="追单",
            description="开启后按条件撤单追价；关闭时保留下面的追单配置。",
            extra_json_schema={"x-order": 50},
        ),
    ] = field(default=False)
    chase_ticks: Annotated[
        int,
        Meta(
            ge=0,
            title="偏离跳数",
            description="触发追价的价格偏离，不是每次加价幅度。开启追单后生效。",
            extra_json_schema={"x-order": 60, "x-unit": "跳", "x-control": "stepper"},
        ),
    ] = field(default=1)
    max_chase_count: Annotated[
        int,
        Meta(
            ge=0,
            title="最大追单次数",
            description="单笔委托最多追价次数。开启追单后生效。",
            extra_json_schema={"x-order": 70, "x-unit": "次", "x-control": "stepper"},
        ),
    ] = field(default=5)
    chase_interval: Annotated[
        float,
        Meta(
            ge=0.0,
            title="追单间隔",
            description="两次追价的最小间隔。开启追单后生效。",
            extra_json_schema={"x-order": 80, "x-unit": "秒", "x-control": "stepper"},
        ),
    ] = field(default=5.0)

    def validate_chase_consistency(self) -> "ChaseParamsMixin":
        """
        校验追单参数的组合约束。

        Returns
        -------
        ChaseParamsMixin
            当前通过一致性校验的参数对象。

        Raises
        ------
        ValueError
            启用追单后，任一参数组合超出算法允许的风险边界时抛出。

        Notes
        -----
        这里的限制不是随意的样式偏好，而是为了在参数层就阻断明显危险的组合：
        例如运行时长过久、追单过于频繁，或单次价格追逐过激进。
        """
        if not self.chase_enabled:
            return self

        if self.chase_ticks < 1:
            raise ValueError(f"启用追单时，追单位数必须 >= 1，当前为 {self.chase_ticks}")

        if self.max_chase_count < 1:
            raise ValueError(f"启用追单时，追单次数必须 >= 1，当前为 {self.max_chase_count}")

        if self.chase_interval < 0.1:
            raise ValueError(f"启用追单时，追单间隔必须 >= 0.1秒，当前为 {self.chase_interval}")

        # 这些硬上限主要防止把一次追单流程配置成“极慢”或“极密”，从而在
        # 真实交易里放大运行时长、滑点和限频风险。
        if self.chase_ticks > 100:
            raise ValueError(f"追单位数（{self.chase_ticks}）过大，不应该超过100跳。")

        if self.max_chase_count > 50:
            raise ValueError(f"追单次数（{self.max_chase_count}）过多，不应该超过50次。")

        if self.chase_interval > 300:
            raise ValueError(f"追单间隔（{self.chase_interval}秒）过长，不应该超过300秒（5分钟）。")

        total_chase_time = self.max_chase_count * self.chase_interval

        if total_chase_time > 600:
            raise ValueError(
                f"追单总时间（max_chase_count * chase_interval = {total_chase_time}秒）"
                f"过长，可能导致算法运行时间过长。"
                f"请减少 max_chase_count 或 chase_interval，使总时间不超过600秒。"
            )

        if self.chase_interval < 1.0 and self.max_chase_count > 10:
            raise ValueError(
                f"追单间隔（{self.chase_interval}秒）过短且追单次数过多，"
                f"可能触发交易所速率限制。请增加追单间隔（>=1.0秒）或减少追单次数（<=10）。"
            )

        return self

    def __str__(self) -> str:
        """便于记录日志的字符串表示."""
        if not self.chase_enabled:
            return "ChaseParamsMixin(chase_enabled=False)"
        return (
            f"ChaseParamsMixin("
            f"enabled={self.chase_enabled}, "
            f"ticks={self.chase_ticks}, "
            f"max_count={self.max_chase_count}, "
            f"interval={self.chase_interval})"
        )

    def validate(self) -> None:
        """在输入入口检查本对象的业务约束。"""
        self.validate_chase_consistency()
