"""账户绩效设置与查询参数。"""

from dataclasses import dataclass
from typing import Annotated, Literal

from msgspec import Meta

WeightType = Literal["cs"]
FeeRate = Annotated[float, Meta(ge=0, lt=1)]
RangeKey = Literal["30", "90", "all"]


@dataclass(kw_only=True)
class PerformanceSettings:
    """账户级回测设置，费率使用小数表示."""

    backtest_weight_type: WeightType
    backtest_fee_rate: FeeRate

    __forbid_unknown__ = True
