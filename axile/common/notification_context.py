"""自定义通知函数接收的公开数据结构。"""

from typing import Literal, NotRequired, TypedDict

ACCOUNT_CONTROL_WARNING_EVENT = "account_control.warning"


class SupplementNotificationItem(TypedDict):
    """一次基础或补发触发在通知中的身份与结束状态。"""

    group_id: str
    base_scheduled_at: str
    scheduled_at: str
    index: int
    configured_count: int
    effective_count: int
    is_last: bool
    group_status: str
    end_reason: str | None


class AccountNotificationExecution(TypedDict):
    """账户执行通知中的执行摘要。"""

    supplements: NotRequired[list[SupplementNotificationItem]]
    id: str | None
    kind: str | None
    trigger_source: str | None
    notified_at: str
    execution_time: float
    outcome_reason: str | None
    channel_type: str
    status: str
    success: bool
    outcome: str | None
    reason_code: str | None
    error: str | None
    is_test: bool


class AccountNotificationSummary(TypedDict):
    """账户执行通知中的计数摘要。"""

    symbol_count: int
    position_count: int | None
    order_count: int
    filled_order_count: int
    active_order_count: int
    trade_count: int
    trade_value: float
    succeeded_symbol_count: int
    failed_symbol_count: int


class AccountNotificationContext(TypedDict):
    """账户执行结束时交给 ``notify`` 的脱敏快照。"""

    is_test: NotRequired[bool]
    event_id: str | None
    account: dict[str, object]
    event_type: NotRequired[Literal["execution.finished", "supplement.cancelled"]]
    supplement: NotRequired[dict[str, object]]
    last_execution: NotRequired[dict[str, object] | None]
    last_execution_at: NotRequired[str | None]
    execution: AccountNotificationExecution | None
    strategy: dict[str, object]
    assets: dict[str, object]
    targets: dict[str, object]
    positions: list[dict[str, object]] | None
    orders: list[dict[str, object]]
    trades: list[dict[str, object]]
    symbols: list[dict[str, object]]
    summary: AccountNotificationSummary
    default_feishu_variables: dict[str, object]


class SystemNotificationError(TypedDict):
    """系统级执行错误内容。"""

    type: str
    message: str
    traceback: str


class SystemNotificationAccount(TypedDict):
    """系统级通知中的账户标识。"""

    id: int | None
    name: str


class SystemNotificationQuota(TypedDict):
    """账户每日额度的本地放行用量，不能作为柜台受理或成交次数。"""

    operation: Literal["place_order", "cancel_order"]
    control_date: str
    timezone: str
    used: int
    limit: int
    warning_percent: int
    threshold_count: int
    remaining: int
    source: Literal["axile_local_allowed"]


class SystemNotificationContext(TypedDict):
    """系统执行错误或额度预警交给 ``notify`` 的上下文。"""

    event_id: str
    event_type: str
    execution_id: str | None
    occurred_at: str
    account: SystemNotificationAccount | None
    error: SystemNotificationError | None
    quota: NotRequired[SystemNotificationQuota]
    is_test: bool
