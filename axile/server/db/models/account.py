"""账户与组合绑定数据库模型."""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from axile.common.default_account_notification import DEFAULT_ACCOUNT_NOTIFICATION_CODE
from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.models import AccountControlOverride
from axile.executor.futures_close_policy import FuturesClosePolicy
from axile.executor.models.unified_input import DEFAULT_EXECUTION_TIMEOUT_SECONDS
from axile.server.db.models.account_connection import AccountConnectionPublic, AccountConnectionState
from axile.server.db.models.account_notification import AccountNotificationState, AccountNotificationStatePublic
from axile.server.db.models.account_runtime_sync import AccountRuntimeSyncPublic
from axile.server.db.models.account_settings import SETTINGS_FIELDS, AccountSettings, SupplementSettings
from axile.server.db.models.account_validation import (
    _MAX_EXECUTION_TIMEOUT,
    _validate_algorithm_config,
    _validate_cron_expr,
    _validate_leverage,
    _validate_weight_precision,
)
from axile.server.db.models.base import Model, now_str
from axile.server.db.models.performance import FeeRate, PerformanceSummary, WeightType


class AccountBase(BaseModel):
    """账户模型共用字段."""

    backtest_weight_type: WeightType = Field(default="ts")
    backtest_fee_rate: FeeRate = Field(default=0.0)
    name: str = Field(description="账户名称, 必填")
    market: str = Field(description="交易市场标识, 例如: A股、期货等, 必填")
    trade_channel: TradeChannel = Field(description="实盘渠道, 必填")
    account_control_preset: str = Field(description="账户控制 preset 标识, 必填")
    account_control_override: AccountControlOverride | None = Field(
        default=None,
        description="账户控制的局部 override, 可选；支持 account/group 级规则与 operations.<op>.symbol 级规则",
    )
    account_config: Dict[str, Any] = Field(description="账户配置, JSON格式, 包含登录信息、API密钥等, 必填")
    is_started: bool = Field(description="账户是否已启动, 布尔值, 必填")
    supplement: SupplementSettings | None = None
    cron_expr: str = Field(description="定时任务表达式, 符合crontab语法, 必填")
    remark: Optional[str] = Field(description="账户备注信息, 可选")
    brokerage: str = Field(description="券商名称, 例如华泰、银河等, 必填")
    weight_precision: float = Field(description="权重值的精度, 必须是10的负整数次幂, 默认: 0.01")
    long_leverage: Optional[float] = Field(default=None, description="做多杠杆")
    short_leverage: Optional[float] = Field(default=None, description="做空杠杆")
    algorithm: Dict[str, Any] = Field(description="下单算法,必填")
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
    execution_timeout: int = Field(
        default=DEFAULT_EXECUTION_TIMEOUT_SECONDS,
        ge=1,
        le=_MAX_EXECUTION_TIMEOUT,
        description=f"执行层总超时（秒）, 必填, 取值 1..{_MAX_EXECUTION_TIMEOUT}, 默认 {DEFAULT_EXECUTION_TIMEOUT_SECONDS}; 到点直接中断本次执行, 不等撤单",
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


class AccountCreate(AccountBase):
    """创建账户时使用的载荷."""

    model_config = ConfigDict(extra="forbid")


class AccountSnapshot(AccountBase):
    """执行时使用的普通 Python 数据快照，不携带 session 或 ORM 关系。"""

    id: int | None = None
    copied_from_account_id: int | None = None
    copied_from_account_name: str | None = None
    updated_at: str = Field(default_factory=now_str)
    created_at: str = Field(default_factory=now_str)


class Account(Model):
    """账户身份、配置及当前状态的普通数据对象。"""

    id: int | None = None
    name: str
    market: str
    trade_channel: TradeChannel
    brokerage: str
    remark: str | None = None
    portfolio_id: int | None = None
    copied_from_account_id: int | None = None
    copied_from_account_name: str | None = None
    updated_at: str = Field(default_factory=now_str)
    created_at: str = Field(default_factory=now_str)
    settings: AccountSettings = Field(default_factory=AccountSettings, exclude=True)
    has_pending_supplement: bool = Field(default=False, exclude=True)
    notification_state: AccountNotificationState | None = Field(default=None, exclude=True)
    connection_state: AccountConnectionState | None = Field(default=None, exclude=True)
    runtime_sync: AccountRuntimeSyncPublic | None = Field(default=None, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def _collect_settings(cls, values: Any) -> Any:
        """将扁平配置合并为同一份有类型设置。"""
        if isinstance(values, BaseModel):
            values = values.model_dump()
        if not isinstance(values, dict):
            return values
        data = dict(values)
        patch = {key: data.pop(key) for key in list(data) if key in SETTINGS_FIELDS}
        settings = AccountSettings.model_validate(data.get("settings") or {}).model_copy(deep=True)
        settings.apply_patch(patch)
        data["settings"] = settings
        return data

    @model_validator(mode="after")
    def _link_settings(self) -> "Account":
        """将设置绑定到已校验的账户 ID。"""
        self.settings.account_id = self.id
        return self

    def __getattr__(self, name: str) -> Any:
        """通过账户属性读取所属配置组。"""
        group = SETTINGS_FIELDS.get(name)
        if group:
            return self.settings.connection if group == "connection" else getattr(getattr(self.settings, group), name)
        return getattr(super(), "__getattr__")(name)

    def __setattr__(self, name: str, value: Any) -> None:
        """校验配置后替换所属设置，并刷新修改时间。"""
        if name in SETTINGS_FIELDS:
            self.settings.apply_patch({name: value})
            super().__setattr__("updated_at", now_str())
        else:
            super().__setattr__(name, value)

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        """按账户快照字段输出扁平配置。"""
        values = super().model_dump() | self.settings.flat_values()
        return AccountSnapshot.model_construct(**values).model_dump(**kwargs)

    def snapshot(self) -> AccountSnapshot:
        """复制为可独立修改的执行账户快照。"""
        return AccountSnapshot.model_validate(self.model_dump()).model_copy(deep=True)


type AccountContext = Account | AccountSnapshot


class AccountPublic(Model):
    """账户读取响应。

    与持久化模型分离。渠道声明的连接字段包含密码与密钥，供编辑回显；
    webhook 仅返回是否已配置。
    """

    backtest_weight_type: WeightType = "ts"
    backtest_fee_rate: FeeRate = 0.0
    id: Optional[int]
    copied_from_account_id: Optional[int] = None
    copied_from_account_name: Optional[str] = None
    name: str
    market: str
    trade_channel: TradeChannel
    account_control_preset: str
    account_control_override: AccountControlOverride | None = None
    account_configured: bool = False
    connection_values: dict[str, Any] = Field(default_factory=dict)
    is_started: bool
    cron_expr: str
    supplement: SupplementSettings | None = None
    has_pending_supplement: bool = False
    remark: Optional[str] = None
    brokerage: str
    weight_precision: float
    long_leverage: Optional[float] = None
    short_leverage: Optional[float] = None
    algorithm: Dict[str, Any]
    empty_positions_algorithm: Optional[Dict[str, Any]] = None
    trade_rules: Optional[Dict[str, Any]] = None
    futures_close_policy: FuturesClosePolicy = Field(default_factory=FuturesClosePolicy)
    forbidden_symbols: Optional[List[str]] = None
    risk_symbols: Optional[List[str]] = None
    feishu_configured: bool = False
    execution_notification_code: str | None = None
    portfolio_id: Optional[int] = None
    write_empty_record: Optional[int] = None
    execution_timeout: int
    updated_at: str
    created_at: str
    runtime_sync: AccountRuntimeSyncPublic | None = None
    notification_state: AccountNotificationStatePublic | None = None
    connection: AccountConnectionPublic = Field(default_factory=AccountConnectionPublic)

    @computed_field
    @property
    def execution_notification_status(self) -> Literal["none", "default", "function"]:
        """按已保存源码区分通知方式；Webhook 配置不改变通知状态。"""
        code = (self.execution_notification_code or "").strip()
        if not code:
            return "none"
        if code == DEFAULT_ACCOUNT_NOTIFICATION_CODE.strip():
            return "default"
        return "function"


class AccountListPublic(Model):
    """账户载荷的列表响应封装."""

    data: List[AccountPublic]


class AccountNextRunPublic(Model):
    """账户未来调度执行时间的响应载荷.

    Attributes
    ----------
    account_id : int
        账户 ID。
    is_scheduled : bool
        当前是否存在对应的调度任务（账户已启动且已绑定组合时才有）。
    next_run_time : Optional[str]
        下一次执行时间的 ISO8601 字符串；无调度任务或任务无下次触发时为 ``None``。
    next_run_times : List[str]
        未来最多三次执行时间的 ISO8601 字符串，按时间升序排列。
    next_execution_times : List[str]
        按交易日历过滤后，未来最多三次实际执行时间。
    """

    account_id: int
    is_scheduled: bool
    next_run_time: Optional[str] = None
    next_run_times: List[str] = Field(default_factory=list)
    next_execution_times: List[str] = Field(default_factory=list)


class AccountDashboardItemPublic(Model):
    """仪表盘聚合中的单账户项.

    一次性把舰队卡所需数据拼齐，避免前端对每个账户分别请求下次执行、执行记录等。
    权益与持仓取自该账户最近一次持久化的资产快照。

    Attributes
    ----------
    account_id : int
        账户 ID。
    name : str
        账户名称。
    remark : Optional[str]
        账户备注；未填写时为 ``None``。
    market : str
        市场。
    trade_channel : TradeChannel
        交易渠道。
    is_started : bool
        是否已启动自动执行。
    portfolio_id : Optional[int]
        当前绑定的组合 ID；未绑定为 ``None``。
    is_scheduled : bool
        是否存在对应的调度任务。
    next_run_time : Optional[str]
        下一次执行时间的 ISO8601 字符串；无调度或无下次触发时为 ``None``。
    total_asset : float
        最近一次快照的账户总权益。
    currency : str
        权益计价币种。
    holdings_count : int
        最近一次快照的持仓品种数。
    position_weights : List[float]
        最近一次快照各持仓的市值（降序，最多 12 项），用于持仓分布条。
    performance : PerformanceSummary
        已发布的全区间绩效，金额、日收益与曲线同版。
    asset_observed_at : Optional[str]
        最近一次账户资产观测时间；无快照时为 ``None``。
    previous_close : Optional[float]
        该观测日之前的绩效日末权益；无观测或无更早绩效时为 ``None``。
    last_is_success : Optional[int]
        最近一次执行是否成功（1/0）；无记录时为 ``None``。
    last_exec_at : Optional[str]
        最近一次执行的时间戳；无记录时为 ``None``。
    last_output_status : Optional[str]
        最近一次执行器输出状态（``SUCCEEDED`` / ``BLOCKED`` / ``FAILED`` 等）；无记录时为 ``None``。
    off_symbol_count : Optional[int]
        持仓相对目标待调整的品种数；缺少快照或目标时为 ``None``，不无证推定 0。
    running_execution_id : Optional[str]
        当前正在运行的执行链路标识；账户此刻无在途执行时为 ``None``。
        取自执行并发锁（唯一真源），使前端能看见调度器/他端发起的执行。
    running_kind : Optional[str]
        当前在途执行的种类（如 ``rebalance``/``clear_positions``）；无在途执行时为 ``None``。
    running_phase : Optional[str]
        当前在途执行的阶段标签（``triggered``/``snapshot``/``planning``/``executing``/
        ``settling`` 之一，见 :data:`axile.server.execution.live.PHASE_ORDER`）；无在途执行时为 ``None``。
    """

    account_id: int
    connection: AccountConnectionPublic = Field(default_factory=AccountConnectionPublic)
    name: str
    remark: Optional[str] = None
    market: str
    trade_channel: TradeChannel
    is_started: bool
    portfolio_id: Optional[int] = None
    is_scheduled: bool
    next_run_time: Optional[str] = None
    total_asset: float
    currency: str
    holdings_count: int
    position_weights: List[float]
    performance: PerformanceSummary = Field(default_factory=PerformanceSummary)
    asset_observed_at: Optional[str] = None
    previous_close: Optional[float] = None
    last_is_success: Optional[int] = None
    last_exec_at: Optional[str] = None
    last_output_status: Optional[str] = None
    off_symbol_count: Optional[int] = None
    running_execution_id: Optional[str] = None
    running_kind: Optional[str] = None
    running_phase: Optional[str] = None
    running_status: Optional[str] = None
    pending_execution_id: Optional[str] = None
    pending_kind: Optional[str] = None


class AccountDashboardPublic(Model):
    """仪表盘聚合响应.

    Attributes
    ----------
    data : List[AccountDashboardItemPublic]
        每个账户一项的聚合列表。
    """

    data: List[AccountDashboardItemPublic]


class AccountRebalancePlanRowPublic(Model):
    """账户可执行持仓对照中的一个规范化品种."""

    symbol: str
    current_weight: float
    target_weight: float
    current_quantity: Optional[float] = None
    target_quantity: Optional[float] = None
    action: str
    side: str
    aligned: bool


class AccountRebalancePlanPublic(Model):
    """同一资产与目标快照下的可执行持仓对照."""

    rows: List[AccountRebalancePlanRowPublic] = Field(default_factory=list)
    off_symbol_count: Optional[int] = None
    observed_at: Optional[str] = None
    target_calculated_at: Optional[str] = None


class AccountUpdate(Model):
    """账户变更时使用的局部更新载荷."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    market: Optional[str] = None
    trade_channel: Optional[TradeChannel] = None
    account_control_preset: Optional[str] = None
    account_control_override: AccountControlOverride | None = None
    account_config: Optional[Dict[str, Any]] = None
    is_started: Optional[bool] = None
    cron_expr: Optional[str] = None
    supplement: SupplementSettings | None = None
    remark: Optional[str] = None
    brokerage: Optional[str] = None
    login_secret: Optional[str] = None
    weight_precision: Optional[float] = None
    long_leverage: Optional[float] = None
    short_leverage: Optional[float] = None
    algorithm: Optional[Dict[str, Any]] = None
    empty_positions_algorithm: Optional[Dict[str, Any]] = None
    trade_rules: Optional[Dict[str, Any]] = None
    futures_close_policy: FuturesClosePolicy | None = None
    forbidden_symbols: Optional[List[str]] = None
    risk_symbols: Optional[List[str]] = None
    feishu_key: Optional[str] = None
    execution_notification_code: str | None = None
    portfolio_id: Optional[int] = None
    write_empty_record: Optional[int] = None
    execution_timeout: Optional[int] = Field(default=None, ge=1, le=_MAX_EXECUTION_TIMEOUT)

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


class PortfolioAccount(PortfolioAccountBase):
    """账户和组合的记录, 只加不修改."""

    id: Optional[int] = Field(default=None)


class PortfolioAccountPublic(PortfolioAccountBase):
    """账户与组合绑定记录的公开表示."""


class PortfolioAccountListPublic(Model):
    """账户与组合绑定记录的列表响应封装."""

    data: List[PortfolioAccountPublic]
    count: int
