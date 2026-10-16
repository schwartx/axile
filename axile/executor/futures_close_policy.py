"""账户期货平仓规则、品种预设与可审计的手续费比较。"""

from __future__ import annotations

import json
import math
import re
from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from axile.common.trade_channel import TradeChannel

type ClosePriority = Literal["yesterday_first", "today_first"]
type CloseMode = Literal["rules", "fee"]
_PRESET_DATA = json.loads(files("axile.executor").joinpath("futures_close_presets.json").read_text(encoding="utf-8"))
PRESET_VERSION = _PRESET_DATA["version"]


def futures_product(symbol: str) -> str | None:
    """只解析普通期货合约，移除年月数字；期权和组合不猜测。"""
    local = symbol.rsplit(".", 1)[-1]
    match = re.fullmatch(r"([A-Za-z]+)\d{3,4}", local)
    return match[1].upper() if match else None


class FuturesClosePolicy(BaseModel):
    """账户只保存模式、兜底及人工覆盖；预设由程序维护。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: CloseMode = "rules"
    fallback: ClosePriority = "yesterday_first"
    overrides: dict[str, ClosePriority] = Field(default_factory=dict, max_length=200)

    @field_validator("overrides", mode="before")
    @classmethod
    def normalize_products(cls, value: object) -> object:
        """拒绝月份合约键，并拒绝大小写别名产生的冲突。"""
        if not isinstance(value, dict):
            return value
        result = {}
        for key, priority in value.items():
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z]+", key):
                raise ValueError("平仓规则仅接受字母品种代码，例如 AG，不能填写 AG2611")
            product = key.upper()
            if product in result and result[product] != priority:
                raise ValueError(f"{product}: 品种规则重复且冲突")
            result[product] = priority
        return result

    def validate_channel(self, channel: object) -> None:
        """自动费用比较仅由 CTP 提供，其他渠道不能伪装支持。"""
        if self.mode == "fee" and channel != TradeChannel.CTP:
            raise ValueError("当前渠道不支持低手续费优先")


class FuturesClosePreset(BaseModel):
    """预设手续费为展示说明，空值不代表免手续费。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    product: str
    exchange: str
    priority: ClosePriority = "yesterday_first"
    yesterday_fee: str | None = None
    today_fee: str | None = None
    can_choose: bool


_PRESETS = {
    row["product"]: FuturesClosePreset(**row, can_choose=row["exchange"] in {"SHFE", "INE"})
    for row in _PRESET_DATA["products"]
}


class FuturesCloseCapability(BaseModel):
    """供公共前端渲染模式与预设，不要求前端判断渠道名称。"""

    modes: list[CloseMode]
    preset_version: str = PRESET_VERSION
    notice: str = _PRESET_DATA["notice"]
    presets: list[FuturesClosePreset]


def close_capability(*, automatic: bool) -> FuturesCloseCapability:
    """生成渠道独立的预设展示副本。"""
    return FuturesCloseCapability(
        modes=["rules", "fee"] if automatic else ["rules"],
        presets=[preset.model_copy(deep=True) for preset in _PRESETS.values()],
    )


def rule_priority(policy: FuturesClosePolicy, product: str | None) -> tuple[ClosePriority, str]:
    """人工覆盖优先于程序预设，未知品种使用账户兜底。"""
    if product in policy.overrides:
        return policy.overrides[product], "override"
    if product in _PRESETS:
        return _PRESETS[product].priority, "preset"
    return policy.fallback, "default"


class CommissionRates(BaseModel):
    """实际账户合约费率；缺失字段和非有限值不能默认为零。"""

    yesterday_money: float = Field(ge=0, allow_inf_nan=False)
    yesterday_volume: float = Field(ge=0, allow_inf_nan=False)
    today_money: float = Field(ge=0, allow_inf_nan=False)
    today_volume: float = Field(ge=0, allow_inf_nan=False)
    queried_at: str

    def compare_priority(self) -> ClosePriority | Literal["equal"] | None:
        """分别比较同单位费率；两项高低相反时不猜测成交额。"""
        yesterday = (self.yesterday_money, self.yesterday_volume)
        today = (self.today_money, self.today_volume)
        if yesterday == today:
            return "equal"
        if all(y <= t for y, t in zip(yesterday, today, strict=True)):
            return "yesterday_first"
        if all(t <= y for y, t in zip(yesterday, today, strict=True)):
            return "today_first"
        return None

    def costs(self, price: float, multiplier: float) -> tuple[float, float]:
        """按同一参考价换算每手费用；纯按手计费不依赖行情。"""
        turnover = 0.0
        if self.yesterday_money or self.today_money:
            if not all(math.isfinite(v) and v > 0 for v in (price, multiplier)):
                raise ValueError("按金额计费缺少有效参考价或合约乘数")
            turnover = price * multiplier
        yesterday = turnover * self.yesterday_money + self.yesterday_volume
        today = turnover * self.today_money + self.today_volume
        if not all(math.isfinite(v) for v in (yesterday, today)):
            raise ValueError("手续费估算溢出")
        return yesterday, today
