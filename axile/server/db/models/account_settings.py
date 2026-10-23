"""账户设置的有类型配置与一对一持久化。"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from axile.executor.account_control.models import AccountControlOverride
from axile.executor.futures_close_policy import FuturesClosePolicy
from axile.executor.models.unified_input import DEFAULT_EXECUTION_TIMEOUT_SECONDS
from axile.server.db.models.account_validation import (
    _MAX_EXECUTION_TIMEOUT,
    _validate_algorithm_config,
    _validate_cron_expr,
    _validate_leverage,
    _validate_weight_precision,
)
from axile.server.db.models.base import Model
from axile.server.db.models.performance import FeeRate, WeightType


class SettingsModel(BaseModel):
    """设置值以替换方式更新，拒绝未知字段与直接标量修改。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ExecutionSettings(SettingsModel):
    """交易执行参数；整份模型校验后替换 JSON 列。"""

    account_control_preset: str = "default"
    account_control_override: AccountControlOverride | None = None
    weight_precision: float = 0.01
    long_leverage: float | None = None
    short_leverage: float | None = None
    algorithm: dict[str, Any] = Field(default_factory=dict)
    empty_positions_algorithm: dict[str, Any] | None = None
    trade_rules: dict[str, Any] | None = None
    futures_close_policy: FuturesClosePolicy = Field(default_factory=FuturesClosePolicy)
    forbidden_symbols: list[str] | None = None
    risk_symbols: list[str] | None = None
    write_empty_record: int | None = None
    execution_timeout: int = Field(default=DEFAULT_EXECUTION_TIMEOUT_SECONDS, ge=1, le=_MAX_EXECUTION_TIMEOUT)

    @field_validator("algorithm", "empty_positions_algorithm")
    @classmethod
    def validate_algorithm(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """空对象兼容尚未配置的旧账户；已配置算法按注册表校验。"""
        return _validate_algorithm_config(value, "执行算法") if value else value

    _precision = field_validator("weight_precision")(_validate_weight_precision)
    _leverage = field_validator("long_leverage", "short_leverage")(_validate_leverage)


class SupplementSettings(SettingsModel):
    """基础触发后的独立补发配置。"""

    count: int = Field(ge=1, le=10)
    interval_minutes: int = Field(ge=1, le=60)


class ScheduleSettings(SettingsModel):
    """账户期望的调度配置，不表示当前执行状态。"""

    is_started: bool = False
    cron_expr: str = ""
    supplement: SupplementSettings | None = None

    _cron = field_validator("cron_expr")(_validate_cron_expr)


class NotificationSettings(SettingsModel):
    """通知源码与发送凭据；公开响应必须显式排除凭据。"""

    feishu_key: str | None = None
    execution_notification_code: str | None = None


class BacktestSettings(SettingsModel):
    """账户业绩计算使用的回测口径。"""

    backtest_weight_type: WeightType = "ts"
    backtest_fee_rate: FeeRate = 0.0


SETTINGS_GROUPS = {
    "execution": ExecutionSettings,
    "schedule": ScheduleSettings,
    "notification": NotificationSettings,
    "backtest": BacktestSettings,
}
SETTINGS_FIELDS = {field: group for group, model in SETTINGS_GROUPS.items() for field in model.model_fields} | {
    "account_config": "connection"
}


class AccountSettings(Model):
    """与账户一对一的配置；运行结果不写入本表。"""

    account_id: int | None = Field(default=None)
    connection: dict[str, Any] = Field(default_factory=dict)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    notification: NotificationSettings = Field(default_factory=NotificationSettings)
    backtest: BacktestSettings = Field(default_factory=BacktestSettings)

    def flat_values(self) -> dict[str, Any]:
        """提供旧 API 和执行快照的兼容字段。"""
        values = {"account_config": dict(self.connection)}
        for group in SETTINGS_GROUPS:
            model = getattr(self, group)
            values.update({name: getattr(model, name) for name in type(model).model_fields})
        return values

    def apply_patch(self, patch: dict[str, Any]) -> None:
        """先合并校验全部配置，再整体赋值，保证失败时不部分更新。"""
        if "account_config" in patch and not isinstance(patch["account_config"], dict):
            raise ValueError("account_config 必须是对象")
        replacements = {}
        for group, model in SETTINGS_GROUPS.items():
            changes = {key: value for key, value in patch.items() if SETTINGS_FIELDS.get(key) == group}
            if changes:
                replacements[group] = model.model_validate(getattr(self, group).model_dump() | changes)
        for group, value in replacements.items():
            setattr(self, group, value)
        if "account_config" in patch:
            self.connection = dict(patch["account_config"])
