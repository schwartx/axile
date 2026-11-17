"""账户期货平仓规则、品种预设与可审计的手续费比较。"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from importlib.resources import files
from math import isfinite
from typing import Annotated, Literal

from msgspec import Meta

from axile.common.serialization import copy_data, provided_fields
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


@dataclass(kw_only=True, frozen=True)
class FuturesClosePolicy:
    """账户只保存模式、兜底及人工覆盖；预设由程序维护。"""

    mode: CloseMode = "rules"
    fallback: ClosePriority = "yesterday_first"
    overrides: Annotated[dict[str, ClosePriority], Meta(max_length=200)] = field(default_factory=dict)

    avoid_close_today: Annotated[list[Literal["IF", "IH", "IC", "IM"]], Meta(max_length=4)] = field(
        default_factory=lambda: ["IC", "IF", "IH", "IM"]
    )

    @classmethod
    def prepare(cls, value: dict) -> dict:
        """锁今品种统一大写去重，空列表保留为显式关闭。"""
        products = value.get("avoid_close_today")
        if isinstance(products, list) and all(isinstance(product, str) for product in products):
            return {**value, "avoid_close_today": sorted({product.upper() for product in products})}
        return value

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
        if self.mode == "fee" and channel != TradeChannel("ctp"):
            raise ValueError("当前渠道不支持低手续费优先")

    __forbid_unknown__ = True

    def validate(self) -> None:
        """在输入入口检查本对象的业务约束。"""
        if "overrides" in provided_fields(self):
            object.__setattr__(self, "overrides", self.normalize_products(self.overrides))


@dataclass(kw_only=True, frozen=True)
class FuturesClosePreset:
    """预设手续费为展示说明，空值不代表免手续费。"""

    product: str
    exchange: str
    priority: ClosePriority = "yesterday_first"
    yesterday_fee: str | None = None
    today_fee: str | None = None
    can_choose: bool

    __forbid_unknown__ = True


_PRESETS = {
    row["product"]: FuturesClosePreset(**row, can_choose=row["exchange"] in {"SHFE", "INE"})
    for row in _PRESET_DATA["products"]
}


@dataclass(kw_only=True)
class FuturesCloseCapability:
    """供公共前端渲染模式与预设，不要求前端判断渠道名称。"""

    modes: list[CloseMode]
    preset_version: str = PRESET_VERSION
    notice: str = _PRESET_DATA["notice"]
    presets: list[FuturesClosePreset]
    avoid_close_today_products: list[str] = field(default_factory=list)


def close_capability(*, automatic: bool) -> FuturesCloseCapability:
    """生成渠道独立的预设展示副本。"""
    return FuturesCloseCapability(
        modes=["rules", "fee"] if automatic else ["rules"],
        presets=[copy_data(preset, deep=True) for preset in _PRESETS.values()],
        avoid_close_today_products=["IF", "IH", "IC", "IM"],
    )


def rule_priority(policy: FuturesClosePolicy, product: str | None) -> tuple[ClosePriority, str]:
    """人工覆盖优先于程序预设，未知品种使用账户兜底。"""
    if product in policy.overrides:
        return policy.overrides[product], "override"
    if product in _PRESETS:
        return _PRESETS[product].priority, "preset"
    return policy.fallback, "default"


@dataclass(kw_only=True)
class CommissionRates:
    """实际账户合约费率；缺失字段和非有限值不能默认为零。"""

    yesterday_money: Annotated[float, Meta(ge=0)]
    yesterday_volume: Annotated[float, Meta(ge=0)]
    today_money: Annotated[float, Meta(ge=0)]
    today_volume: Annotated[float, Meta(ge=0)]
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

    def validate(self) -> None:
        """在输入入口检查本对象的业务约束。"""
        if (
            "yesterday_money" in provided_fields(self)
            and self.yesterday_money is not None
            and not isfinite(self.yesterday_money)
        ):
            raise ValueError("yesterday_money 必须是有限数值")
        if (
            "yesterday_volume" in provided_fields(self)
            and self.yesterday_volume is not None
            and not isfinite(self.yesterday_volume)
        ):
            raise ValueError("yesterday_volume 必须是有限数值")
        if "today_money" in provided_fields(self) and self.today_money is not None and not isfinite(self.today_money):
            raise ValueError("today_money 必须是有限数值")
        if (
            "today_volume" in provided_fields(self)
            and self.today_volume is not None
            and not isfinite(self.today_volume)
        ):
            raise ValueError("today_volume 必须是有限数值")
