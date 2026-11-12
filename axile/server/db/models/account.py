"""账户与组合绑定数据库模型."""

from typing import Annotated, Any, Dict, List, Optional

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

from axile.channels.gm_sessions import GMSessionSettings
from axile.channels.schedule_settings import (
    legacy_schedule_values,
    normalize_schedule_options,
    validate_schedule_options,
)
from axile.common.trade_channel import TradeChannel
from axile.executor.futures_close_policy import FuturesClosePolicy
from axile.executor.models.unified_input import DEFAULT_EXECUTION_TIMEOUT_SECONDS
from axile.server._native import _core
from axile.server.db.models.account_connection import AccountConnectionState
from axile.server.db.models.account_notification import AccountNotificationState
from axile.server.db.models.account_runtime_sync import AccountRuntimeSyncState
from axile.server.db.models.account_settings import SupplementSettings
from axile.server.db.models.account_validation import (
    _validate_algorithm_config,
    _validate_cron_expr,
    _validate_leverage,
    _validate_weight_precision,
)
from axile.server.db.models.algorithm_schedule import AlgorithmSchedule
from axile.server.db.models.base import Model, now_str
from axile.server.db.models.performance import FeeRate, WeightType

# HTTP 外层保留 JSON；嵌套约束和公开形状均来自 Rust。
AccountControlOverrideData = Annotated[
    Any,
    BeforeValidator(
        lambda value: value.model_dump(mode="json") if isinstance(value, _core.AccountControlOverride) else value
    ),
]


class AccountBase(BaseModel):
    """账户模型共用字段."""

    backtest_weight_type: WeightType = Field(default="ts")
    backtest_fee_rate: FeeRate = Field(default=0.0)
    name: str = Field(description="账户名称, 必填")
    market: str = Field(description="交易市场标识, 例如: A股、期货等, 必填")
    trade_channel: TradeChannel = Field(description="实盘渠道, 必填")
    account_control_preset: str = Field(description="账户控制 preset 标识, 必填")
    account_control_override: AccountControlOverrideData | None = Field(
        default=None,
        description="账户控制的局部 override, 可选；支持 account/group 级规则与 operations.<op>.symbol 级规则",
    )
    account_config: Dict[str, Any] = Field(description="账户配置, JSON格式, 包含登录信息、API密钥等, 必填")
    is_started: bool = Field(description="账户是否已启动, 布尔值, 必填")
    gm_sessions: GMSessionSettings | None = None
    supplement: SupplementSettings | None = None
    cron_expr: str = Field(description="定时任务表达式, 符合crontab语法, 必填")
    remark: Optional[str] = Field(description="账户备注信息, 可选")
    brokerage: str = Field(description="券商名称, 例如华泰、银河等, 必填")
    weight_precision: float = Field(description="权重值的精度, 必须是10的负整数次幂, 默认: 0.01")
    long_leverage: Optional[float] = Field(default=None, description="做多杠杆")
    short_leverage: Optional[float] = Field(default=None, description="做空杠杆")
    algorithm: Dict[str, Any] = Field(description="下单算法,必填")
    algorithm_schedule: AlgorithmSchedule = Field(default_factory=list)
    empty_positions_algorithm: Optional[Dict[str, Any]] = Field(
        default=None, description="清仓算法, 非必填; null 时由执行逻辑回退到默认值"
    )
    futures_close_policy: FuturesClosePolicy = Field(default_factory=FuturesClosePolicy)
    trade_rules: Optional[Dict[str, Any]] = Field(description="交易规则,非必填")
    forbidden_symbols: Optional[List[str]] = Field(description="禁用品种,非必填")
    risk_symbols: Optional[List[str]] = Field(description="风险品种,自动平仓,非必填")
    feishu_key: Optional[str] = Field(description="飞书KEY, 可选")
    execution_notification_code: str | None = Field(default=None)
    portfolio_id: Optional[int] = Field(
        default=None, description="当前绑定组合 ID（可空，删除组合时 DB 自动设为 NULL）"
    )
    write_empty_record: Optional[int] = Field(
        default=None, description="是否写入空订单的执行记录, 0/1 或 null 表示不写入,只有设置1才写入, 默认不写入"
    )
    replay_recording_enabled: StrictBool = Field(default=False, description="是否录制已有连接上的执行回放")
    execution_timeout: int = Field(
        default=DEFAULT_EXECUTION_TIMEOUT_SECONDS,
        ge=1,
        description=f"执行层总超时（秒）, 必须为正整数, 默认 {DEFAULT_EXECUTION_TIMEOUT_SECONDS}; 到点直接中断本次执行, 不等撤单",
    )

    _check_weight_precision = field_validator("weight_precision")(_validate_weight_precision)

    @field_validator("long_leverage", "short_leverage")
    def _check_leverage_fields(cls, value: Optional[float]) -> Optional[float]:
        """校验多空杠杆取值范围."""
        return _validate_leverage(value)

    @field_validator("cron_expr")
    def _check_cron_expr(cls, value: Optional[str]) -> Optional[str]:
        """校验定时表达式可解析；空串（仅手动触发）放行."""
        return _validate_cron_expr(value)

    @field_validator("algorithm")
    def _check_algorithm_field(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        """按注册表校验下单算法参数是否越界."""
        validated = _validate_algorithm_config(value, "下单算法")
        if validated is None:
            raise ValueError("下单算法不能为空")
        return validated

    @field_validator("empty_positions_algorithm")
    def _check_empty_positions_algorithm_field(cls, value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """按注册表校验清仓算法参数是否越界."""
        return _validate_algorithm_config(value, "清仓算法")

    @model_validator(mode="after")
    def validate_scheduled_algorithm_channels(self) -> "AccountBase":
        """创建、复制和局部更新后的完整账户统一校验时段算法渠道。"""
        from axile.server.db.models.account_validation import _check_algorithm_channel_compat

        for rule in self.algorithm_schedule:
            _check_algorithm_channel_compat(rule.algorithm, str(self.trade_channel), "时段算法")
        return self


class AccountCreate(AccountBase):
    """创建账户时使用的载荷."""

    model_config = ConfigDict(extra="forbid")


class AccountSnapshot(AccountBase):
    """执行时使用的普通 Python 数据快照，不携带 session 或 ORM 关系。"""

    channel_options: dict[str, dict[str, Any]] = Field(default_factory=dict)
    _options = field_validator("channel_options")(validate_schedule_options)

    @model_validator(mode="before")
    @classmethod
    def normalize_channel_options(cls, value: Any) -> Any:
        """进程快照兼容旧字段，并通过插件恢复对应配置模型。"""
        if not isinstance(value, dict):
            return value
        normalized = normalize_schedule_options(value)
        return normalized | legacy_schedule_values(normalized["channel_options"])

    id: int | None = None
    copied_from_account_id: int | None = None
    copied_from_account_name: str | None = None
    archived_at: str | None = None
    updated_at: str = Field(default_factory=now_str)
    created_at: str = Field(default_factory=now_str)
    replay_config_version: int = Field(default=0, ge=0, strict=True)

    @model_validator(mode="before")
    @classmethod
    def _replay_configuration_version(cls, value: Any) -> Any:
        """从同步状态提取录制配置版本，供 worker 拒绝旧更新。"""
        if isinstance(value, dict) and "replay_config_version" not in value:
            sync = value.get("runtime_sync")
            revision = sync.get("revision", 0) if isinstance(sync, dict) else getattr(sync, "revision", 0)
            return {**value, "replay_config_version": revision}
        return value


class Account(AccountSnapshot):
    """账户身份、配置及当前状态的普通数据对象。"""

    model_config = ConfigDict(from_attributes=True, validate_assignment=True)

    account_control_preset: str = "default"
    account_config: dict[str, Any] = Field(default_factory=dict)
    is_started: bool = False
    cron_expr: str = ""
    remark: str | None = None
    weight_precision: float = 0.01
    algorithm: dict[str, Any] = Field(default_factory=dict)
    trade_rules: dict[str, Any] | None = None
    forbidden_symbols: list[str] | None = None
    risk_symbols: list[str] | None = None
    feishu_key: str | None = None
    has_pending_supplement: bool = Field(default=False, exclude=True)
    notification_state: AccountNotificationState | None = Field(default=None, exclude=True)
    connection_state: AccountConnectionState | None = Field(default=None, exclude=True)
    runtime_sync: AccountRuntimeSyncState | None = Field(default=None, exclude=True)

    @field_validator("algorithm")
    def _check_algorithm_field(cls, value: dict[str, Any]) -> dict[str, Any]:
        """允许历史未配置账户的空算法，已配置算法保留参数校验。"""
        return super()._check_algorithm_field(value) if value else value


type AccountContext = AccountSnapshot


class AccountUpdate(Model):
    """账户变更时使用的局部更新载荷."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    market: Optional[str] = None
    trade_channel: Optional[TradeChannel] = None
    account_control_preset: Optional[str] = None
    account_control_override: AccountControlOverrideData | None = None
    account_config: Optional[Dict[str, Any]] = None
    is_started: Optional[bool] = None
    cron_expr: Optional[str] = None
    gm_sessions: GMSessionSettings | None = None
    supplement: SupplementSettings | None = None
    remark: Optional[str] = None
    brokerage: Optional[str] = None
    login_secret: Optional[str] = None
    weight_precision: Optional[float] = None
    long_leverage: Optional[float] = None
    short_leverage: Optional[float] = None
    algorithm: Optional[Dict[str, Any]] = None
    algorithm_schedule: AlgorithmSchedule = Field(default_factory=list)
    empty_positions_algorithm: Optional[Dict[str, Any]] = None
    trade_rules: Optional[Dict[str, Any]] = None
    futures_close_policy: FuturesClosePolicy | None = None
    forbidden_symbols: Optional[List[str]] = None
    risk_symbols: Optional[List[str]] = None
    feishu_key: Optional[str] = None
    execution_notification_code: str | None = None
    portfolio_id: Optional[int] = None
    write_empty_record: Optional[int] = None
    replay_recording_enabled: StrictBool = False
    execution_timeout: Optional[int] = Field(default=None, ge=1)

    _check_weight_precision = field_validator("weight_precision")(_validate_weight_precision)

    @field_validator("long_leverage", "short_leverage")
    def _check_leverage_fields(cls, value: Optional[float]) -> Optional[float]:
        """校验多空杠杆取值范围."""
        return _validate_leverage(value)

    @field_validator("cron_expr")
    def _check_cron_expr(cls, value: Optional[str]) -> Optional[str]:
        """校验定时表达式可解析；空串（仅手动触发）放行."""
        return _validate_cron_expr(value)

    @field_validator("algorithm")
    def _check_algorithm_field(cls, value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """按注册表校验下单算法参数是否越界."""
        return _validate_algorithm_config(value, "下单算法")

    @field_validator("empty_positions_algorithm")
    def _check_empty_positions_algorithm_field(cls, value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """按注册表校验清仓算法参数是否越界."""
        return _validate_algorithm_config(value, "清仓算法")


class PortfolioAccountBase(Model):
    """账户与组合绑定历史记录的共用字段."""

    account_id: int
    portfolio_id: Optional[int]
    created_at: str = Field(default_factory=now_str)
