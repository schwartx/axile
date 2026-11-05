"""补发与回测配置的独立输入模型。"""

from pydantic import BaseModel, ConfigDict, Field

from axile.server.db.models.performance import FeeRate, WeightType


class SettingsModel(BaseModel):
    """设置值以替换方式更新，拒绝未知字段与直接标量修改。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SupplementSettings(SettingsModel):
    """基础触发后的独立补发配置。"""

    count: int = Field(ge=1, le=10)
    interval_minutes: int = Field(ge=1, le=60)


class BacktestSettings(SettingsModel):
    """账户业绩计算使用的回测口径。"""

    backtest_weight_type: WeightType = "ts"
    backtest_fee_rate: FeeRate = 0.0
