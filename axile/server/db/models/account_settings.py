"""补发与回测配置的独立输入模型。"""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from axile.channels.gm_sessions import GM_SPECIAL_WINDOWS
from axile.server.db.models.performance import FeeRate, WeightType


class SettingsModel(BaseModel):
    """设置值以替换方式更新，拒绝未知字段与直接标量修改。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SupplementSettings(SettingsModel):
    """基础触发后的独立补发配置。"""

    count: int = Field(ge=1, le=10)
    interval_minutes: int = Field(ge=1, le=60)


class GMSessionTime(SettingsModel):
    """一次独立阶段触发。"""

    enabled: bool = False
    time: str


class GMSessionSettings(SettingsModel):
    """GM 自动调仓阶段；盘后专用委托尚未验证，禁止启用。"""

    opening_cancelable: GMSessionTime = GMSessionTime(time="09:15")
    opening_locked: GMSessionTime = GMSessionTime(time="09:20")
    closing_auction: GMSessionTime = GMSessionTime(time="14:57")
    after_hours: GMSessionTime = GMSessionTime(time="15:05")

    @model_validator(mode="after")
    def validate_times(self) -> "GMSessionSettings":
        """固定阶段触发时间，拒绝尚未验证的盘后能力。"""
        for name, (start, _end, label) in GM_SPECIAL_WINDOWS.items():
            value = getattr(self, name)
            if value.time != start:
                raise ValueError(f"{label}触发时间固定为 {start}")
        if self.after_hours.enabled:
            raise ValueError("当前渠道尚未验证盘后定价交易能力，暂不能启用")
        return self

    def validate_channel(self, channel: str) -> None:
        """特殊阶段配置仅允许用于 GM。"""
        if str(channel) != "gm":
            raise ValueError("集合竞价和盘后定价交易配置仅适用于股票（GM）")


class BacktestSettings(SettingsModel):
    """账户业绩计算使用的回测口径。"""

    backtest_weight_type: WeightType = "ts"
    backtest_fee_rate: FeeRate = 0.0
