"""账户绩效设置与查询参数。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

WeightType = Literal["cs"]
FeeRate = Annotated[float, Field(strict=True, ge=0, lt=1, allow_inf_nan=False)]
RangeKey = Literal["30", "90", "all"]


class PerformanceSettings(BaseModel):
    """账户级回测设置，费率使用小数表示."""

    model_config = ConfigDict(extra="forbid")
    backtest_weight_type: WeightType
    backtest_fee_rate: FeeRate
