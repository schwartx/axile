"""每日分时段算法配置；与渠道市场钟共用左闭右开的隔夜语义。"""

from dataclasses import dataclass, field
from typing import Annotated, Any

from msgspec import Meta

from axile.common.serialization import provided_fields
from axile.server.db.models.account_validation import _validate_algorithm_config

ClockTime = Annotated[str, Meta(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]


@dataclass(kw_only=True)
class ScheduledAlgorithm:
    """北京时间的每日时段及独立、完整的算法配置。"""

    start: ClockTime
    end: ClockTime
    algorithm: dict[str, Any]

    @classmethod
    def validate_algorithm(cls, value: dict[str, Any]) -> dict[str, Any]:
        """复用账户主算法的参数约束。"""
        _validate_algorithm_config(value, "时段算法")
        return value

    def validate_duration(self) -> "ScheduledAlgorithm":
        """相同时刻不能隐式解释为全天，全天配置由默认算法承担。"""
        if self.start == self.end:
            raise ValueError("时段开始和结束不能相同")
        return self

    __forbid_unknown__ = True

    def validate(self) -> None:
        """在输入入口检查本对象的业务约束。"""
        if "algorithm" in provided_fields(self):
            object.__setattr__(self, "algorithm", self.validate_algorithm(self.algorithm))
        self.validate_duration()


def validate_algorithm_schedule(value: list[ScheduledAlgorithm]) -> list[ScheduledAlgorithm]:
    """拆分隔夜时段后检查重叠，允许首尾相接。"""
    segments: list[tuple[str, str, str]] = []
    for rule in value:
        label = f"{rule.start}–{rule.end}"
        if rule.start < rule.end:
            segments.append((rule.start, rule.end, label))
        else:
            segments.extend(((rule.start, "24:00", label), ("00:00", rule.end, label)))
    segments = sorted(segment for segment in segments if segment[0] < segment[1])
    for previous, current in zip(segments, segments[1:]):
        if current[0] < previous[1]:
            raise ValueError(f"时段 {current[2]} 与 {previous[2]} 重叠")
    return value


AlgorithmSchedule = list[ScheduledAlgorithm]


@dataclass(kw_only=True)
class ProductAlgorithm:
    """单个品种独立的默认算法与每日时段，不继承账户时段。"""

    algorithm: dict[str, Any]
    algorithm_schedule: AlgorithmSchedule = field(default_factory=list)

    @classmethod
    def validate_algorithm(cls, value: dict[str, Any]) -> dict[str, Any]:
        """复用完整算法的参数校验。"""
        _validate_algorithm_config(value, "品种默认算法")
        return {**value, "params": value.get("params") or {}}

    __forbid_unknown__ = True

    def validate(self) -> None:
        """在输入入口检查本对象的业务约束。"""
        validate_algorithm_schedule(self.algorithm_schedule)
        if "algorithm" in provided_fields(self):
            object.__setattr__(self, "algorithm", self.validate_algorithm(self.algorithm))
