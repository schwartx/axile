"""
定义账户控制配置与事件写入使用的数据模型.

Notes
-----
该模块负责描述 preset、override、有效策略以及执行期刷盘数据的统一结构。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator

from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.registry import ensure_default_account_control_registry_bootstrapped


class _AccountControlBaseModel(BaseModel):
    """
    约束账户控制模型通用行为的基础模型.

    Notes
    -----
    所有派生模型默认禁止额外字段。
    """

    model_config = ConfigDict(extra="forbid")


class AccountControlContextKind(str, Enum):
    """区分交易执行与独立账户操作，未知历史上下文不推测归属。"""

    EXECUTION = "execution"
    ACCOUNT_OPERATION = "account_operation"
    UNKNOWN = "unknown"


class AccountControlSource(str, Enum):
    """记录受控调用的发起来源，独立于共享额度的运行时。"""

    ALGORITHM = "algorithm"
    ORDER_QUERY = "order_query"
    MANUAL_CANCEL = "manual_cancel"
    CURRENT_ORDERS = "current_orders"
    INSTRUMENT_CATALOG = "instrument_catalog"
    ACCOUNT_ASSETS = "account_assets"
    CONNECTION_PREPARE = "connection_prepare"
    UNKNOWN = "unknown"


class AccountControlBucketType(str, Enum):
    """
    定义计数器使用的时间桶类型.

    Attributes
    ----------
    MINUTE : str
        分钟级时间桶。
    DAY : str
        自然日级时间桶。
    """

    MINUTE = "minute"
    DAY = "day"


class AccountControlScopeType(str, Enum):
    """
    定义计数器生效的作用域类型.

    Attributes
    ----------
    ACCOUNT : str
        账户级作用域。
    SYMBOL : str
        标的级作用域。
    """

    ACCOUNT = "account"
    SYMBOL = "symbol"


class AccountControlDecision(str, Enum):
    """
    定义账户控制对一次尝试的决策结果.

    Attributes
    ----------
    ALLOWED : str
        本次尝试被允许执行。
    BLOCKED : str
        本次尝试被直接阻断。
    """

    ALLOWED = "allowed"
    BLOCKED = "blocked"


class AccountControlTriggerBehavior(str, Enum):
    """
    定义配额命中时的处理行为.

    Attributes
    ----------
    WAIT : str
        等待到配额恢复后再继续。
    BLOCK : str
        直接阻断本次调用。
    """

    WAIT = "wait"
    BLOCK = "block"


def _now_str() -> str:
    from axile.executor.algorithms.utils.clock import get_default_clock

    return datetime.fromtimestamp(get_default_clock().time()).replace(microsecond=0).isoformat()


def _now_ms() -> int:
    from axile.executor.algorithms.utils.clock import get_default_clock

    return int(get_default_clock().time() * 1_000)


def _ensure_timezone(value: str | None) -> str | None:
    if value is None:
        return value
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"无效的 timezone: {value}") from exc
    return value


def _validate_registered_operation_key(operation_key: str) -> None:
    registry = ensure_default_account_control_registry_bootstrapped()
    registry.require_operation(operation_key)


def _validate_registered_group_key(group_key: str) -> None:
    registry = ensure_default_account_control_registry_bootstrapped()
    registry.require_group(group_key)


def _validate_operation_metadata(
    operations: Mapping[str, "AccountControlOperationOverride | AccountControlOperationPolicy"],
) -> None:
    for operation_key, operation_policy in operations.items():
        _validate_registered_operation_key(operation_key)
        if operation_policy.daily_warning_percent is not None and operation_key not in {"place_order", "cancel_order"}:
            raise ValueError("每日额度预警仅支持账户报单和撤单")


class AccountControlRuleOverride(_AccountControlBaseModel):
    """
    定义局部覆盖场景使用的规则模型.

    Attributes
    ----------
    limit : StrictInt | None
        覆盖后的限制值；为空时沿用基线配置。
    on_trigger : AccountControlTriggerBehavior | None
        覆盖后的触发行为；为空时沿用基线配置。
    unlimited : StrictBool
        为真时显式解除该规则（解析为无限制），与 ``limit`` / ``on_trigger`` 互斥。
        仅额度类规则（per_minute / per_day）允许；``min_interval_ms`` 是渠道硬保护，不可解除。
    """

    limit: StrictInt | None = Field(default=None, ge=0)
    on_trigger: AccountControlTriggerBehavior | None = None
    unlimited: StrictBool = False

    @model_validator(mode="after")
    def validate_unlimited_exclusive(self) -> AccountControlRuleOverride:
        """校验 Unlimited 与 limit/on_trigger 互斥."""
        if self.unlimited and (self.limit is not None or self.on_trigger is not None):
            raise ValueError("unlimited 与 limit/on_trigger 互斥，不能同时设置")
        return self


class AccountControlRule(_AccountControlBaseModel):
    """
    定义归一化后的完整规则模型.

    Attributes
    ----------
    limit : StrictInt
        当前规则允许的最大次数或最小间隔值。
    on_trigger : AccountControlTriggerBehavior
        命中规则时采用的处理行为。
    """

    limit: StrictInt = Field(ge=0)
    on_trigger: AccountControlTriggerBehavior


class AccountControlScopeOverride(_AccountControlBaseModel):
    """
    定义单个作用域上的局部覆盖配置.

    Attributes
    ----------
    per_minute : AccountControlRuleOverride | None
        分钟级额度覆盖。
    per_day : AccountControlRuleOverride | None
        日级额度覆盖。
    min_interval_ms : AccountControlRuleOverride | None
        最小调用间隔覆盖。
    """

    per_minute: AccountControlRuleOverride | None = None
    per_day: AccountControlRuleOverride | None = None
    min_interval_ms: AccountControlRuleOverride | None = None

    @field_validator("min_interval_ms")
    @classmethod
    def validate_min_interval_ms(cls, value: AccountControlRuleOverride | None) -> AccountControlRuleOverride | None:
        """校验最小调用间隔覆盖规则的阈值合法性."""
        if value is not None and value.unlimited:
            raise ValueError("min_interval_ms 不允许设置为不限制（渠道硬保护不可解除）")
        if value is not None and value.limit is not None and value.limit <= 0:
            raise ValueError("min_interval_ms.limit 必须大于 0")
        return value


class AccountControlScopePolicy(_AccountControlBaseModel):
    """
    定义单个作用域上的完整策略配置.

    Attributes
    ----------
    per_minute : AccountControlRule | None
        分钟级额度规则。
    per_day : AccountControlRule | None
        日级额度规则。
    min_interval_ms : AccountControlRule | None
        最小调用间隔规则。
    """

    per_minute: AccountControlRule | None = None
    per_day: AccountControlRule | None = None
    min_interval_ms: AccountControlRule | None = None

    @field_validator("min_interval_ms")
    @classmethod
    def validate_min_interval_ms(cls, value: AccountControlRule | None) -> AccountControlRule | None:
        """校验最小调用间隔规则的阈值合法性."""
        if value is not None and value.limit <= 0:
            raise ValueError("min_interval_ms.limit 必须大于 0")
        return value


class AccountControlOperationOverride(_AccountControlBaseModel):
    """
    定义单个操作的局部覆盖配置.

    Attributes
    ----------
    daily_warning_percent : int | None
        账户报单或撤单的每日额度预警比例，范围 1–99；为空时关闭。
        暂停日额度限制时保留该设置，恢复限额后继续使用。
    account : AccountControlScopeOverride | None
        账户级覆盖配置。
    symbol : AccountControlScopeOverride | None
        标的级覆盖配置。
    """

    priority: StrictInt | None = None
    daily_warning_percent: StrictInt | None = Field(default=None, ge=1, le=99)
    account: AccountControlScopeOverride | None = None
    symbol: AccountControlScopeOverride | None = None


class AccountControlOperationPolicy(_AccountControlBaseModel):
    """
    定义单个操作的完整策略配置.

    Attributes
    ----------
    daily_warning_percent : int | None
        账户级每日额度预警比例，独立于排队和阻断行为。
        无限额或限额为 0 时预警不生效。
    account : AccountControlScopePolicy
        账户级规则。
    symbol : AccountControlScopePolicy | None
        标的级规则；为空时表示不启用标的级限制。
    """

    priority: StrictInt = 100
    daily_warning_percent: StrictInt | None = Field(default=None, ge=1, le=99)
    account: AccountControlScopePolicy = Field(default_factory=AccountControlScopePolicy)
    symbol: AccountControlScopePolicy | None = None


class AccountControlOverride(_AccountControlBaseModel):
    """
    定义账户绑定的局部覆盖配置.

    Attributes
    ----------
    timezone : StrictStr | None
        覆盖后的时区标识。
    operations : dict[StrictStr, AccountControlOperationOverride]
        按操作键索引的局部覆盖配置。
    groups : dict[StrictStr, AccountControlScopeOverride]
        按共享节流组键索引的局部覆盖配置。
    """

    timezone: StrictStr | None = None
    operations: dict[StrictStr, AccountControlOperationOverride] = Field(default_factory=dict)
    groups: dict[StrictStr, AccountControlScopeOverride] = Field(default_factory=dict)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        """校验并规范化覆盖配置中的时区字段."""
        return _ensure_timezone(value)

    @field_validator("operations")
    @classmethod
    def validate_operations(
        cls,
        value: dict[str, AccountControlOperationOverride],
    ) -> dict[str, AccountControlOperationOverride]:
        """校验覆盖配置中的操作元数据键."""
        _validate_operation_metadata(value)
        return value

    @field_validator("groups")
    @classmethod
    def validate_groups(
        cls,
        value: dict[str, AccountControlScopeOverride],
    ) -> dict[str, AccountControlScopeOverride]:
        """校验覆盖配置中的共享节流组键."""
        for group_key in value:
            _validate_registered_group_key(group_key)
        return value


class AccountControlPolicy(_AccountControlBaseModel):
    """
    定义执行开始时冻结的完整账户控制策略.

    Attributes
    ----------
    timezone : StrictStr
        计算控制窗口时使用的时区。
    operations : dict[StrictStr, AccountControlOperationPolicy]
        按操作键索引的完整策略。
    groups : dict[StrictStr, AccountControlScopePolicy]
        按共享节流组键索引的完整策略。
    """

    timezone: StrictStr
    operations: dict[StrictStr, AccountControlOperationPolicy] = Field(default_factory=dict)
    groups: dict[StrictStr, AccountControlScopePolicy] = Field(default_factory=dict)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        """校验并规范化完整策略中的时区字段."""
        return _ensure_timezone(value) or value

    @field_validator("operations")
    @classmethod
    def validate_operations(
        cls,
        value: dict[str, AccountControlOperationPolicy],
    ) -> dict[str, AccountControlOperationPolicy]:
        """校验完整策略中的操作元数据键."""
        _validate_operation_metadata(value)
        return value

    @field_validator("groups")
    @classmethod
    def validate_groups(
        cls,
        value: dict[str, AccountControlScopePolicy],
    ) -> dict[str, AccountControlScopePolicy]:
        """校验完整策略中的共享节流组键."""
        for group_key in value:
            _validate_registered_group_key(group_key)
        return value


class EffectiveAccountControlPolicy(AccountControlPolicy):
    """
    定义叠加 preset 与 override 后的有效策略.

    Attributes
    ----------
    preset_key : StrictStr
        当前生效 preset 的键。
    operation_groups : dict[StrictStr, frozenset[StrictStr]]
        当前 preset 为 operation 追加的共享节流组；该映射不接受账户覆盖。
    """

    preset_key: StrictStr
    operation_groups: dict[StrictStr, frozenset[StrictStr]] = Field(default_factory=dict)

    @field_validator("operation_groups")
    @classmethod
    def validate_operation_groups(
        cls,
        value: dict[str, frozenset[str]],
    ) -> dict[str, frozenset[str]]:
        """校验 preset 专属 operation 与共享节流组键。"""
        for operation_key, group_keys in value.items():
            _validate_registered_operation_key(operation_key)
            for group_key in group_keys:
                _validate_registered_group_key(group_key)
        return value


class AccountControlPresetDefinition(_AccountControlBaseModel):
    """
    定义系统内置 preset 的元信息.

    Attributes
    ----------
    preset_key : StrictStr
        preset 唯一键。
    compatible_trade_channels : frozenset[TradeChannel] | None
        该 preset 兼容的交易渠道集合；``None`` 表示兼容所有已注册及未来渠道。
    policy : AccountControlOverride
        该 preset 自带的局部覆盖策略。
    operation_groups : dict[StrictStr, frozenset[StrictStr]]
        仅由 preset 声明的 operation 与共享节流组关系。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    preset_key: StrictStr
    compatible_trade_channels: frozenset[TradeChannel] | None
    policy: AccountControlOverride
    operation_groups: dict[StrictStr, frozenset[StrictStr]] = Field(default_factory=dict)

    @field_validator("operation_groups")
    @classmethod
    def validate_operation_groups(
        cls,
        value: dict[str, frozenset[str]],
    ) -> dict[str, frozenset[str]]:
        """校验 preset 专属 operation 与共享节流组键。"""
        for operation_key, group_keys in value.items():
            _validate_registered_operation_key(operation_key)
            for group_key in group_keys:
                _validate_registered_group_key(group_key)
        return value


class AccountControlCounterDeltaWrite(_AccountControlBaseModel):
    """
    定义执行结束时待刷盘的计数器增量记录.

    Attributes
    ----------
    account_id : StrictInt
        账户 ID。
    execution_id : StrictStr
        控制上下文 ID，沿用历史字段名。
    context_kind : AccountControlContextKind
        所属上下文类型。
    source : AccountControlSource
        本条计数增量的调用来源。
    control_date : StrictStr
        控制日期。
    bucket_type : AccountControlBucketType
        时间桶类型。
    bucket_start : StrictStr
        时间桶起始时间。
    scope_type : AccountControlScopeType
        计数作用域。
    symbol : StrictStr | None
        标的代码；账户级记录时为空。
    operation : StrictStr
        操作键。
    delta_count : StrictInt
        本次新增的计数值。
    delta_uid : StrictStr
        保证幂等写入的唯一键。
    """

    account_id: StrictInt = Field(ge=1)
    execution_id: StrictStr
    context_kind: AccountControlContextKind = AccountControlContextKind.UNKNOWN
    source: AccountControlSource = AccountControlSource.UNKNOWN
    control_date: StrictStr
    bucket_type: AccountControlBucketType
    bucket_start: StrictStr
    scope_type: AccountControlScopeType
    symbol: StrictStr | None = None
    operation: StrictStr
    delta_count: StrictInt = Field(ge=1)
    delta_uid: StrictStr

    @field_validator("operation")
    @classmethod
    def validate_operation(cls, value: str) -> str:
        """校验计数增量记录中的操作键不为空."""
        if not value.strip():
            raise ValueError("operation 不能为空")
        return value


class AccountControlEventWrite(_AccountControlBaseModel):
    """
    定义执行结束时待刷盘的事件记录.

    Attributes
    ----------
    account_id : StrictInt
        账户 ID。
    control_date : StrictStr
        控制日期。
    execution_id : StrictStr
        控制上下文 ID，沿用历史字段名。
    context_kind : AccountControlContextKind
        所属上下文类型。
    source : AccountControlSource
        本次调用的发起来源。
    seq : StrictInt
        当前执行内的事件顺序号。
    channel : TradeChannel
        触发事件的交易渠道。
    operation : StrictStr
        操作键。
    symbol : StrictStr | None
        关联的交易标的代码。
    metadata : dict[str, object]
        事件附加元数据。
    decision : AccountControlDecision
        账户控制对本次尝试的决策。
    counted : bool
        当前事件是否计入额度统计。
    outcome : StrictStr
        事件最终结果。
    event_uid : StrictStr
        保证幂等写入的唯一键。
    created_at : StrictStr
        事件创建时间字符串。
    occurred_at_ms : StrictInt
        事件发生的毫秒时间戳。
    """

    account_id: StrictInt = Field(ge=1)
    control_date: StrictStr
    execution_id: StrictStr
    context_kind: AccountControlContextKind = AccountControlContextKind.UNKNOWN
    source: AccountControlSource = AccountControlSource.UNKNOWN
    seq: StrictInt = Field(ge=0)
    channel: TradeChannel
    operation: StrictStr
    symbol: StrictStr | None = None
    metadata: dict[str, object] = Field(default_factory=dict)
    decision: AccountControlDecision
    counted: bool
    outcome: StrictStr
    event_uid: StrictStr
    created_at: StrictStr = Field(default_factory=_now_str, description="事件创建时间，秒级可读时间戳")
    occurred_at_ms: StrictInt = Field(
        default_factory=_now_ms,
        ge=0,
        description="事件发生时间的毫秒时间戳，用于 min_interval_ms 判断与排障",
    )

    @field_validator("operation")
    @classmethod
    def validate_operation(cls, value: str) -> str:
        """校验事件记录中的操作键不为空."""
        if not value.strip():
            raise ValueError("operation 不能为空")
        return value
