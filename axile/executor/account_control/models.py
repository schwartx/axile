"""
定义账户控制配置与事件写入使用的数据模型.

Notes
-----
该模块负责描述 preset、override、有效策略以及执行期刷盘数据的统一结构。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

from axile.common.trade_channel import TradeChannel

if TYPE_CHECKING:
    from axile.server._native._core import (
        AccountControlOperationOverride as AccountControlOperationOverride,
    )
    from axile.server._native._core import (
        AccountControlOperationPolicy as AccountControlOperationPolicy,
    )
    from axile.server._native._core import (
        AccountControlOverride as AccountControlOverride,
    )
    from axile.server._native._core import (
        AccountControlPolicy as AccountControlPolicy,
    )
    from axile.server._native._core import (
        AccountControlRule as AccountControlRule,
    )
    from axile.server._native._core import (
        AccountControlRuleOverride as AccountControlRuleOverride,
    )
    from axile.server._native._core import (
        AccountControlScopeOverride as AccountControlScopeOverride,
    )
    from axile.server._native._core import (
        AccountControlScopePolicy as AccountControlScopePolicy,
    )
    from axile.server._native._core import (
        AccountControlTriggerBehavior as AccountControlTriggerBehavior,
    )
    from axile.server._native._core import (
        EffectiveAccountControlPolicy as EffectiveAccountControlPolicy,
    )

_NATIVE_MODELS = frozenset(
    {
        "AccountControlOperationOverride",
        "AccountControlOperationPolicy",
        "AccountControlOverride",
        "AccountControlPolicy",
        "AccountControlRule",
        "AccountControlRuleOverride",
        "AccountControlScopeOverride",
        "AccountControlScopePolicy",
        "AccountControlTriggerBehavior",
        "EffectiveAccountControlPolicy",
    }
)


def __getattr__(name: str) -> Any:
    """使用配置时才加载原生模块，CLI 帮助和装饰器声明无需二进制。"""
    if name not in _NATIVE_MODELS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from axile.executor.account_control.registry import ensure_default_account_control_registry_bootstrapped
    from axile.server._native import _core

    ensure_default_account_control_registry_bootstrapped()._sync_native()
    return getattr(_core, name)


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


def _now_str() -> str:
    from axile.executor.algorithms.utils.clock import get_default_clock

    return datetime.fromtimestamp(get_default_clock().time()).replace(microsecond=0).isoformat()


def _now_ms() -> int:
    from axile.executor.algorithms.utils.clock import get_default_clock

    return int(get_default_clock().time() * 1_000)


@dataclass(frozen=True)
class AccountControlPresetDefinition:
    """保存 Python 渠道声明的预设数据；策略语义由原生模块校验。"""

    preset_key: str
    compatible_trade_channels: frozenset[TradeChannel] | None
    policy: AccountControlOverride
    operation_groups: dict[str, frozenset[str]] = field(default_factory=dict)


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
