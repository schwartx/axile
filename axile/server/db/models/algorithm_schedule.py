"""每日分时段算法配置；与渠道市场钟共用左闭右开的隔夜语义。"""

from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from axile.server.db.models.account_validation import _validate_algorithm_config

ClockTime = Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]


class ScheduledAlgorithm(BaseModel):
    """北京时间的每日时段及独立、完整的算法配置。"""

    model_config = ConfigDict(extra="forbid")

    start: ClockTime
    end: ClockTime
    algorithm: dict[str, Any]

    @field_validator("algorithm")
    @classmethod
    def validate_algorithm(cls, value: dict[str, Any]) -> dict[str, Any]:
        """复用账户主算法的参数约束。"""
        _validate_algorithm_config(value, "时段算法")
        return value

    @model_validator(mode="after")
    def validate_duration(self) -> "ScheduledAlgorithm":
        """相同时刻不能隐式解释为全天，全天配置由默认算法承担。"""
        if self.start == self.end:
            raise ValueError("时段开始和结束不能相同")
        return self


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


AlgorithmSchedule = Annotated[list[ScheduledAlgorithm], AfterValidator(validate_algorithm_schedule)]
