"""组合函数的目标口径与严格数值契约。"""

from __future__ import annotations

import math
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, field_validator

TargetMode = Literal["weight", "quantity"]


class PortfolioTarget(BaseModel):
    """一份完整的带符号目标持仓；数量使用渠道原生交易单位。"""

    model_config = ConfigDict(extra="forbid")

    mode: TargetMode = "weight"
    targets: dict[str, float]

    @field_validator("targets", mode="before")
    @classmethod
    def validate_targets(cls, value: object) -> dict[str, float]:
        """拒绝隐式字符串转换、布尔值和非有限数值。"""
        if not isinstance(value, dict):
            raise TypeError("组合目标必须是 dict[str, float]")
        normalized: dict[str, float] = {}
        for key, number in cast("dict[object, object]", value).items():
            if not isinstance(key, str):
                raise TypeError(f"目标字典的键必须是品种字符串，实际为 {type(key).__name__}: {key!r}")
            if isinstance(number, bool) or not isinstance(number, (int, float)):
                raise TypeError(f"品种 {key} 的目标必须是数字，实际为 {type(number).__name__}: {number!r}")
            target = float(number)
            if not math.isfinite(target):
                raise ValueError(f"品种 {key} 的目标不是有限数值: {number!r}")
            normalized[key] = target
        return normalized

    @classmethod
    def from_result(cls, result: object) -> PortfolioTarget:
        """旧字典按权重解释，显式返回值必须完整声明口径与目标。"""
        if isinstance(result, cls):
            return cls.model_validate(result.model_dump())
        if not isinstance(result, dict):
            raise TypeError(f"calculate_portfolio 必须返回目标字典，实际返回 {type(result).__name__}")
        if isinstance(result.get("mode"), str) or isinstance(result.get("targets"), dict):
            if set(result) != {"mode", "targets"}:
                raise ValueError("显式组合目标必须且只能包含 mode 和 targets")
            return cls.model_validate(result)
        return cls(targets=cls.validate_targets(result))
