"""账户与组合绑定数据库模型."""

from dataclasses import dataclass, field
from typing import Annotated, Any, Dict, List, Optional

from msgspec import UNSET, Meta, UnsetType

from axile.channels.rebalance_signal import SIGNAL_CHANNEL
from axile.channels.schedule_settings import validate_schedule_options
from axile.common.serialization import provided_fields
from axile.common.trade_channel import TradeChannel
from axile.executor.futures_close_policy import FuturesClosePolicy
from axile.executor.models.cash_flow import CashFlowInput
from axile.executor.models.unified_input import DEFAULT_EXECUTION_TIMEOUT_SECONDS
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
from axile.server.db.models.algorithm_schedule import AlgorithmSchedule, ProductAlgorithm, validate_algorithm_schedule
from axile.server.db.models.base import now_str
from axile.server.db.models.performance import FeeRate, WeightType

# HTTP 外层保留 JSON；嵌套约束和公开形状均来自 Rust。
AccountControlOverrideData = Any


@dataclass(kw_only=True)
class AccountBase:
    """账户模型共用字段."""

    backtest_weight_type: WeightType = field(default="cs")
    backtest_fee_rate: FeeRate = field(default=0.0)
    name: Annotated[str, Meta(description="账户名称, 必填")]
    market: Annotated[str, Meta(description="交易市场标识, 例如: A股、期货等, 必填")]
    trade_channel: Annotated[TradeChannel, Meta(description="实盘渠道, 必填")]
    account_control_preset: Annotated[str, Meta(description="账户控制 preset 标识, 必填")]
    account_control_override: Annotated[
        AccountControlOverrideData | None,
        Meta(description="账户控制的局部 override, 可选；支持 account/group 级规则与 operations.<op>.symbol 级规则"),
    ] = field(default=None)
    account_config: Annotated[Dict[str, Any], Meta(description="账户配置, JSON格式, 包含登录信息、API密钥等, 必填")]
    is_started: Annotated[bool, Meta(description="账户是否已启动, 布尔值, 必填")]
    channel_options: dict[str, dict[str, Any]] = field(default_factory=dict)
    supplement: SupplementSettings | None = None
    cron_expr: Annotated[str, Meta(description="定时任务表达式, 符合crontab语法, 必填")]
    remark: Annotated[Optional[str], Meta(description="账户备注信息, 可选")]
    brokerage: Annotated[str, Meta(description="券商名称, 例如华泰、银河等, 必填")]
    weight_precision: Annotated[float, Meta(description="权重值的精度, 必须是10的负整数次幂, 默认: 0.01")]
    long_leverage: Annotated[Optional[float], Meta(description="做多杠杆")] = field(default=None)
    short_leverage: Annotated[Optional[float], Meta(description="做空杠杆")] = field(default=None)
    algorithm: Annotated[Dict[str, Any], Meta(description="下单算法,必填")]
    algorithm_schedule: AlgorithmSchedule = field(default_factory=list)
    product_algorithms: dict[str, ProductAlgorithm] = field(default_factory=dict)
    empty_positions_algorithm: Annotated[
        Optional[Dict[str, Any]], Meta(description="清仓算法, 非必填; null 时由执行逻辑回退到默认值")
    ] = field(default=None)
    futures_close_policy: FuturesClosePolicy = field(default_factory=FuturesClosePolicy)
    trade_rules: Annotated[Optional[Dict[str, Any]], Meta(description="交易规则,非必填")]
    forbidden_symbols: Annotated[Optional[List[str]], Meta(description="禁用品种,非必填")]
    risk_symbols: Annotated[Optional[List[str]], Meta(description="风险品种,自动平仓,非必填")]
    feishu_key: Annotated[Optional[str], Meta(description="飞书KEY, 可选")]
    execution_notification_code: str | None = field(default=None)
    portfolio_id: Annotated[Optional[int], Meta(description="当前绑定组合 ID（可空，删除组合时 DB 自动设为 NULL）")] = (
        field(default=None)
    )
    write_empty_record: Annotated[
        Optional[int], Meta(description="是否写入空订单的执行记录, 0/1 或 null 表示不写入,只有设置1才写入, 默认不写入")
    ] = field(default=None)
    replay_recording_enabled: Annotated[bool, Meta(description="是否录制已有连接上的执行回放")] = field(default=False)
    execution_timeout: Annotated[
        int,
        Meta(
            ge=1,
            description=f"执行层总超时（秒）, 必须为正整数, 默认 {DEFAULT_EXECUTION_TIMEOUT_SECONDS}; 到点直接中断本次执行, 不等撤单",
        ),
    ] = field(default=DEFAULT_EXECUTION_TIMEOUT_SECONDS)

    @classmethod
    def _check_leverage_fields(cls, value: Optional[float]) -> Optional[float]:
        """校验多空杠杆取值范围."""
        return _validate_leverage(value)

    @classmethod
    def _check_cron_expr(cls, value: Optional[str]) -> Optional[str]:
        """校验定时表达式可解析；空串（仅手动触发）放行."""
        return _validate_cron_expr(value)

    @classmethod
    def _check_algorithm_field(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        """按注册表校验下单算法参数是否越界."""
        validated = _validate_algorithm_config(value, "下单算法")
        if validated is None:
            raise ValueError("下单算法不能为空")
        return validated

    @classmethod
    def _check_empty_positions_algorithm_field(cls, value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """按注册表校验清仓算法参数是否越界."""
        return _validate_algorithm_config(value, "清仓算法")

    def validate_scheduled_algorithm_channels(self) -> "AccountBase":
        """创建、复制和局部更新后的完整账户统一校验时段算法渠道。"""
        from axile.server.db.models.account_validation import _check_algorithm_channel_compat

        for rule in self.algorithm_schedule:
            _check_algorithm_channel_compat(rule.algorithm, str(self.trade_channel), "时段算法")
        if self.product_algorithms:
            from axile.channels import get_channel
            from axile.channels.algorithm_scopes import normalize_algorithm_scopes

            normalized = normalize_algorithm_scopes(get_channel(self.trade_channel), self.product_algorithms)
            for product, profile in normalized.items():
                _check_algorithm_channel_compat(profile.algorithm, str(self.trade_channel), f"{product} 默认算法")
                for rule in profile.algorithm_schedule:
                    _check_algorithm_channel_compat(rule.algorithm, str(self.trade_channel), f"{product} 时段算法")
            object.__setattr__(self, "product_algorithms", normalized)
        return self

    def validate(self) -> None:
        """在输入入口检查本对象的业务约束。"""
        if "channel_options" in provided_fields(self):
            object.__setattr__(self, "channel_options", validate_schedule_options(self.channel_options))
        if "weight_precision" in provided_fields(self):
            object.__setattr__(self, "weight_precision", _validate_weight_precision(self.weight_precision))
        if "long_leverage" in provided_fields(self):
            object.__setattr__(self, "long_leverage", self._check_leverage_fields(self.long_leverage))
        if "short_leverage" in provided_fields(self):
            object.__setattr__(self, "short_leverage", self._check_leverage_fields(self.short_leverage))
        if "cron_expr" in provided_fields(self):
            object.__setattr__(self, "cron_expr", self._check_cron_expr(self.cron_expr))
        if self.trade_channel == SIGNAL_CHANNEL:
            if self.algorithm or self.empty_positions_algorithm or self.algorithm_schedule or self.product_algorithms:
                raise ValueError("调仓信号账户不支持配置交易算法")
            if self.supplement is not None:
                raise ValueError("调仓信号账户不支持交易补发")
        elif "algorithm" in provided_fields(self):
            object.__setattr__(self, "algorithm", self._check_algorithm_field(self.algorithm))
        if "empty_positions_algorithm" in provided_fields(self):
            object.__setattr__(
                self,
                "empty_positions_algorithm",
                self._check_empty_positions_algorithm_field(self.empty_positions_algorithm),
            )
        validate_algorithm_schedule(self.algorithm_schedule)
        self.validate_scheduled_algorithm_channels()


@dataclass(kw_only=True)
class AccountCreate(AccountBase):
    """创建账户时使用的载荷."""

    initial_deposit: CashFlowInput | None = None

    __forbid_unknown__ = True

    def validate(self) -> None:
        """在输入入口检查本对象的业务约束。"""
        super().validate()
        if self.initial_deposit is not None and (
            self.trade_channel != SIGNAL_CHANNEL or self.initial_deposit.direction != "deposit"
        ):
            raise ValueError("首次入金只用于创建调仓信号账户")


@dataclass(kw_only=True)
class AccountSnapshot(AccountBase):
    """执行时使用的普通 Python 数据快照，不携带 session 或 ORM 关系。"""

    id: int | None = None
    copied_from_account_id: int | None = None
    copied_from_account_name: str | None = None
    archived_at: str | None = None
    updated_at: str = field(default_factory=now_str)
    created_at: str = field(default_factory=now_str)
    replay_config_version: Annotated[int, Meta(ge=0)] = field(default=0)

    @classmethod
    def _replay_configuration_version(cls, value: Any) -> Any:
        """从同步状态提取录制配置版本，供 worker 拒绝旧更新。"""
        if isinstance(value, dict) and "replay_config_version" not in value:
            sync = value.get("runtime_sync")
            revision = sync.get("revision", 0) if isinstance(sync, dict) else getattr(sync, "revision", 0)
            return {**value, "replay_config_version": revision}
        return value

    def validate(self) -> None:
        """在输入入口检查本对象的业务约束。"""
        super().validate()

    @classmethod
    def prepare(cls, value):
        """在输入入口归一化历史字段和渠道标识。"""
        value = cls._replay_configuration_version(value)
        return value


@dataclass(kw_only=True)
class Account(AccountSnapshot):
    """账户身份、配置及当前状态的普通数据对象。"""

    account_control_preset: str = "default"
    account_config: dict[str, Any] = field(default_factory=dict)
    is_started: bool = False
    cron_expr: str = ""
    remark: str | None = None
    weight_precision: float = 0.01
    algorithm: dict[str, Any] = field(default_factory=dict)
    trade_rules: dict[str, Any] | None = None
    forbidden_symbols: list[str] | None = None
    risk_symbols: list[str] | None = None
    feishu_key: str | None = None
    has_pending_supplement: bool = field(default=False, metadata={"exclude": True})
    notification_state: AccountNotificationState | None = field(default=None, metadata={"exclude": True})
    connection_state: AccountConnectionState | None = field(default=None, metadata={"exclude": True})
    runtime_sync: AccountRuntimeSyncState | None = field(default=None, metadata={"exclude": True})

    @classmethod
    def _check_algorithm_field(cls, value: dict[str, Any]) -> dict[str, Any]:
        """允许历史未配置账户的空算法，已配置算法保留参数校验。"""
        return super()._check_algorithm_field(value) if value else value

    def validate(self) -> None:
        """在输入入口检查本对象的业务约束。"""
        super().validate()
        if "algorithm" in provided_fields(self):
            object.__setattr__(self, "algorithm", self._check_algorithm_field(self.algorithm))


type AccountContext = AccountSnapshot


@dataclass(kw_only=True)
class AccountUpdate:
    """账户变更时使用的局部更新载荷."""

    name: Optional[str] | UnsetType = UNSET
    market: Optional[str] | UnsetType = UNSET
    trade_channel: Optional[TradeChannel] | UnsetType = UNSET
    account_control_preset: Optional[str] | UnsetType = UNSET
    account_control_override: AccountControlOverrideData | None | UnsetType = UNSET
    account_config: Optional[Dict[str, Any]] | UnsetType = UNSET
    is_started: Optional[bool] | UnsetType = UNSET
    cron_expr: Optional[str] | UnsetType = UNSET
    channel_options: dict[str, dict[str, Any]] | UnsetType = UNSET
    supplement: SupplementSettings | None | UnsetType = UNSET
    remark: Optional[str] | UnsetType = UNSET
    brokerage: Optional[str] | UnsetType = UNSET
    login_secret: Optional[str] | UnsetType = UNSET
    weight_precision: Optional[float] | UnsetType = UNSET
    long_leverage: Optional[float] | UnsetType = UNSET
    short_leverage: Optional[float] | UnsetType = UNSET
    algorithm: Optional[Dict[str, Any]] | UnsetType = UNSET
    algorithm_schedule: AlgorithmSchedule | UnsetType = UNSET
    product_algorithms: dict[str, ProductAlgorithm] | UnsetType = UNSET
    empty_positions_algorithm: Optional[Dict[str, Any]] | UnsetType = UNSET
    trade_rules: Optional[Dict[str, Any]] | UnsetType = UNSET
    futures_close_policy: FuturesClosePolicy | None | UnsetType = UNSET
    forbidden_symbols: Optional[List[str]] | UnsetType = UNSET
    risk_symbols: Optional[List[str]] | UnsetType = UNSET
    feishu_key: Optional[str] | UnsetType = UNSET
    execution_notification_code: str | None | UnsetType = UNSET
    portfolio_id: Optional[int] | UnsetType = UNSET
    write_empty_record: Optional[int] | UnsetType = UNSET
    replay_recording_enabled: bool | UnsetType = UNSET
    execution_timeout: Annotated[int, Meta(ge=1)] | None | UnsetType = UNSET

    @classmethod
    def _check_leverage_fields(cls, value: Optional[float]) -> Optional[float]:
        """校验多空杠杆取值范围."""
        return _validate_leverage(value)

    @classmethod
    def _check_cron_expr(cls, value: Optional[str]) -> Optional[str]:
        """校验定时表达式可解析；空串（仅手动触发）放行."""
        return _validate_cron_expr(value)

    @classmethod
    def _check_algorithm_field(cls, value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """按注册表校验下单算法参数是否越界."""
        return _validate_algorithm_config(value, "下单算法")

    @classmethod
    def _check_empty_positions_algorithm_field(cls, value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """按注册表校验清仓算法参数是否越界."""
        return _validate_algorithm_config(value, "清仓算法")

    __forbid_unknown__ = True

    def validate(self) -> None:
        """在输入入口检查本对象的业务约束。"""
        if "algorithm_schedule" in provided_fields(self):
            validate_algorithm_schedule(self.algorithm_schedule)
        if "channel_options" in provided_fields(self):
            object.__setattr__(self, "channel_options", validate_schedule_options(self.channel_options))
        if "weight_precision" in provided_fields(self):
            object.__setattr__(self, "weight_precision", _validate_weight_precision(self.weight_precision))
        if "long_leverage" in provided_fields(self):
            object.__setattr__(self, "long_leverage", self._check_leverage_fields(self.long_leverage))
        if "short_leverage" in provided_fields(self):
            object.__setattr__(self, "short_leverage", self._check_leverage_fields(self.short_leverage))
        if "cron_expr" in provided_fields(self):
            object.__setattr__(self, "cron_expr", self._check_cron_expr(self.cron_expr))
        if "algorithm" in provided_fields(self):
            object.__setattr__(self, "algorithm", self._check_algorithm_field(self.algorithm))
        if "empty_positions_algorithm" in provided_fields(self):
            object.__setattr__(
                self,
                "empty_positions_algorithm",
                self._check_empty_positions_algorithm_field(self.empty_positions_algorithm),
            )


@dataclass(kw_only=True)
class PortfolioAccountBase:
    """账户与组合绑定历史记录的共用字段."""

    account_id: int
    portfolio_id: Optional[int]
    created_at: str = field(default_factory=now_str)
