"""补发与回测配置的独立输入模型。"""

from dataclasses import dataclass
from typing import Annotated

from msgspec import Meta

from axile.server.db.models.performance import FeeRate, WeightType


@dataclass(kw_only=True, frozen=True)
class SupplementSettings:
    """基础触发后的独立补发配置。"""

    __forbid_unknown__ = True

    count: Annotated[int, Meta(ge=1, le=10)]
    interval_minutes: Annotated[int, Meta(ge=1, le=60)]


@dataclass(kw_only=True, frozen=True)
class BacktestSettings:
    """账户业绩计算使用的回测口径。"""

    __forbid_unknown__ = True

    backtest_weight_type: WeightType = "cs"
    backtest_fee_rate: FeeRate = 0.0
