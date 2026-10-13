"""定义交易渠道插件的公开契约与描述模型."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from axile.common.order_param_model import OrderParamModel
from axile.executor.models.unified_input_accounts import BaseAccountConfig

if TYPE_CHECKING:
    from axile.executor.abstract_executor.base import AbstractExecutor

type ChannelId = str
type ExecutionBackend = Literal["thread", "process"]
type ScheduleKind = Literal["continuous", "cn_stock", "cn_futures"]


class ConnectionProgress(Protocol):
    """上报阶段状态及可选的结构化详情。"""

    def __call__(
        self, step: str, status: Literal["running", "passed"], details: dict[str, object] | None = None
    ) -> None:
        """接收阶段状态和可选详情。"""
        ...


type ConnectionValidator = Callable[[BaseAccountConfig, ConnectionProgress], None]
type ExecutorFactory = Callable[[BaseAccountConfig], "AbstractExecutor"]
type TargetTransform = Callable[[dict[str, float], pd.DataFrame], pd.DataFrame]
type CanonicalizeSymbol = Callable[[str], str]
type QuantizeTargetQuantity = Callable[[float, float, float], float]


def _identity_symbol(symbol: str) -> str:
    """默认不做合约别名合并."""
    return symbol


class _FrozenDescriptorModel(BaseModel):
    """为渠道描述模型提供不可变且禁止额外字段的共同配置."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AlgorithmReference(_FrozenDescriptorModel):
    """
    描述渠道默认使用的算法及其参数.

    Attributes
    ----------
    method : str
        算法注册名称。
    params : dict[str, object]
        算法默认参数。
    """

    method: str = Field(min_length=1)
    params: dict[str, object] = Field(default_factory=dict)


class ChannelDefaults(_FrozenDescriptorModel):
    """
    描述新建账户时采用的渠道默认值.

    Attributes
    ----------
    account_control_preset : str
        新建账户默认使用的账户控制预设键。
    long_leverage : float
        默认多头杠杆。
    short_leverage : float
        默认空头杠杆。
    execution_timeout : int
        默认执行总超时秒数。
    trade_algorithm : AlgorithmReference
        默认调仓算法。
    empty_positions_algorithm : AlgorithmReference | None
        默认清仓算法；为空时沿用执行器默认行为。
    """

    account_control_preset: str = Field(default="default", min_length=1)
    long_leverage: float = Field(ge=0)
    short_leverage: float = Field(ge=0)
    execution_timeout: int = Field(ge=1)
    trade_algorithm: AlgorithmReference
    empty_positions_algorithm: AlgorithmReference | None = None


class ChannelLeverage(_FrozenDescriptorModel):
    """
    描述渠道可配置的杠杆范围.

    Attributes
    ----------
    min : float
        最小值。
    max : float
        最大值。
    step : float
        最小调整步长。
    """

    min: float = Field(ge=0)
    max: float = Field(gt=0)
    step: float = Field(gt=0)


class ChannelUnits(_FrozenDescriptorModel):
    """
    描述渠道在界面中使用的数量与价格单位.

    Attributes
    ----------
    quantity_kind : Literal["share", "contract", "base_asset", "custom"]
        数量的业务语义；``base_asset`` 表示单位随品种变化。
    quantity_label : str
        数量后展示的短标签，例如“股”“手”或“基础资产”。
    quantity_max_decimals : int
        前端展示数量时允许保留的最大小数位数。
    price_label : str
        价格后展示的短标签；为空时使用渠道计价币种。
    notional_label : str
        成交金额后展示的短标签；为空时使用渠道计价币种。
    """

    quantity_kind: Literal["share", "contract", "base_asset", "custom"] = "custom"
    quantity_label: str = ""
    quantity_max_decimals: int = Field(default=6, ge=0, le=12)
    price_label: str = ""
    notional_label: str = ""


class ChannelUi(_FrozenDescriptorModel):
    """
    描述公共前端可消费的渠道界面差异.

    Attributes
    ----------
    account_connect_lead : str
        账户连接页的渠道专属说明；为空时使用公共说明。
    leverage_title : str
        杠杆配置区标题。
    leverage_note : str
        杠杆配置区的补充说明。
    long_leverage_label : str
        多头杠杆输入项标签。
    short_leverage_label : str
        空头杠杆输入项标签。
    show_short_leverage : bool
        是否展示并提交空头杠杆配置。
    position_value_label : str
        持仓金额在界面中的业务名称。
    """

    account_connect_lead: str = ""
    leverage_title: str = "杠杆"
    leverage_note: str = "多空可分设"
    long_leverage_label: str = "做多杠杆"
    short_leverage_label: str = "做空杠杆"
    show_short_leverage: bool = True
    position_value_label: str = "市值"


class ChannelAccountOption(_FrozenDescriptorModel):
    """描述账户表单选择字段中的一个选项."""

    value: str
    label: str
    description: str = ""


class ChannelAccountFieldCondition(_FrozenDescriptorModel):
    """描述账户字段的声明式显示条件."""

    field: str = Field(min_length=1)
    equals: object


class ChannelAccountFieldClipboard(_FrozenDescriptorModel):
    """描述地址字段在智能剪贴板中的语义角色与批量匹配分组."""

    role: Literal["trading", "market-data", "rpc", "proxy"]
    group: str | None = Field(default=None, min_length=1)


class ChannelEndpointConstraints(_FrozenDescriptorModel):
    """描述地址字段可由客户端确定的结构约束."""

    scheme: Literal["required", "optional", "forbidden"] = "optional"
    allowed_schemes: tuple[Literal["tcp", "http", "https", "ftp", "ws", "wss"], ...] = ()
    port: Literal["required", "optional"] = "required"
    allow_path: bool = False

    @model_validator(mode="after")
    def validate_scheme_configuration(self) -> "ChannelEndpointConstraints":
        """拒绝互相矛盾的协议配置."""
        if self.scheme == "required" and not self.allowed_schemes:
            raise ValueError("scheme=required 时必须声明 allowed_schemes")
        if self.scheme == "forbidden" and self.allowed_schemes:
            raise ValueError("scheme=forbidden 时不得声明 allowed_schemes")
        return self


class ChannelNumberConstraints(_FrozenDescriptorModel):
    """描述金额字段的确定性数值下界."""

    gt: float | None = None
    gte: float | None = None

    @model_validator(mode="after")
    def validate_bounds(self) -> "ChannelNumberConstraints":
        """同一字段只允许一种下界语义."""
        if self.gt is not None and self.gte is not None:
            raise ValueError("gt 与 gte 不能同时声明")
        return self


class ChannelAccountFieldConstraints(_FrozenDescriptorModel):
    """描述账户字段的可选结构化校验规则."""

    endpoint: ChannelEndpointConstraints | None = None
    number: ChannelNumberConstraints | None = None


class ChannelAccountField(_FrozenDescriptorModel):
    """
    描述渠道账户表单中的一个字段.

    Attributes
    ----------
    name : str
        提交到账户配置中的字段名。
    label : str
        面向用户的字段标签。
    kind : Literal["text", "identifier", "secret", "endpoint", "directory", "money", "boolean", "select"]
        字段的业务语义与前端控件类型。
    width : Literal["half", "full"]
        桌面表单中的建议宽度；移动端统一单列。
    required : bool
        是否要求用户填写。
    help : str | None
        输入项下方展示的渠道专属说明。
    """

    name: str = Field(min_length=1)
    label: str = Field(min_length=1)
    kind: Literal["text", "identifier", "secret", "endpoint", "directory", "money", "boolean", "select"]
    width: Literal["half", "full"]
    required: bool = True
    placeholder: str | None = None
    help: str | None = None
    default: object | None = None
    options: tuple[ChannelAccountOption, ...] = ()
    presentation: Literal["segmented", "conditional_reveal"] = "segmented"
    visible_when: ChannelAccountFieldCondition | None = None
    constraints: ChannelAccountFieldConstraints | None = None
    clipboard: ChannelAccountFieldClipboard | None = None

    @model_validator(mode="after")
    def validate_constraints_match_kind(self) -> "ChannelAccountField":
        """确保约束类型与字段语义一致."""
        if self.presentation != "segmented" and self.kind != "select":
            raise ValueError("非默认展示模式只能用于 select 字段")
        if self.clipboard is not None and self.kind != "endpoint":
            raise ValueError("clipboard 元数据只能用于 endpoint 字段")
        if self.constraints is None:
            return self
        if self.constraints.endpoint is not None and self.kind != "endpoint":
            raise ValueError("endpoint 约束只能用于 endpoint 字段")
        if self.constraints.number is not None and self.kind != "money":
            raise ValueError("number 约束只能用于 money 字段")
        return self


class ChannelAccountNotice(_FrozenDescriptorModel):
    """描述账户连接表单旁展示的一条提示."""

    tone: Literal["info", "warning"] = "info"
    text: str = Field(min_length=1)


class ChannelAccountForm(_FrozenDescriptorModel):
    """描述渠道账户连接表单与提示信息."""

    fields: tuple[ChannelAccountField, ...] = ()
    notices: tuple[ChannelAccountNotice, ...] = ()


class ChannelCalendar(_FrozenDescriptorModel):
    """描述渠道使用的共享交易日历。"""

    calendar_id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    label: str = Field(min_length=1)


class ChannelNightSchedule(_FrozenDescriptorModel):
    """描述渠道快捷排程可合并的夜盘触发时点。"""

    label: str = Field(min_length=1)
    range_label: str = Field(min_length=1)
    close: tuple[str, ...] = Field(min_length=1)
    m15: tuple[str, ...] = Field(min_length=1)
    m60: tuple[str, ...] = Field(min_length=1)


class ChannelSessionWindow(_FrozenDescriptorModel):
    """一条左闭右开的渠道可报单窗，时刻为 ``HH:MM``."""

    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    end: str = Field(pattern=r"^\d{2}:\d{2}$")


class ChannelSchedule(_FrozenDescriptorModel):
    """描述渠道采用的公共定时规则类型与可报单窗.

    ``windows`` 是调度市场钟：空元组表示连续交易；隔夜窗 ``start >= end``。
    节奏 crontab 由频率在这些窗内对齐，真正能否下单还要看交易日历。
    """

    kind: ScheduleKind
    night: ChannelNightSchedule | None = None
    windows: tuple[ChannelSessionWindow, ...] = ()


class ChannelPortfolioPreset(_FrozenDescriptorModel):
    """描述组合向导中某市场的名称与示例标的."""

    market_label: str = Field(min_length=1)
    example_symbols: tuple[str, ...] = Field(min_length=1)


class ConnectionValidationStep(_FrozenDescriptorModel):
    """一个渠道声明的连接检查项；标识只在该渠道内有意义。"""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1)


class ChannelConnectionValidation(_FrozenDescriptorModel):
    """渠道的连接验证能力，具体流程由渠道自己的验证器实现。"""

    steps: tuple[ConnectionValidationStep, ...] = Field(min_length=1)
    notice: str = Field(min_length=1)


class ChannelDescriptor(_FrozenDescriptorModel):
    """
    描述前端与 API 可见的渠道静态能力.

    Attributes
    ----------
    channel : str
        全局唯一的渠道标识。
    label : str
        面向用户的渠道名称。
    description : str
        渠道用途简述。
    icon : str
        前端图标目录中的图标标识。
    market : str
        渠道所属市场类别。
    currency : str
        账户权益默认计价币种。
    units : ChannelUnits
        数量、价格与成交金额的展示单位。
    ui : ChannelUi
        公共前端可消费的渠道界面差异。
    defaults : ChannelDefaults
        新建账户默认值。
    leverage : ChannelLeverage
        可配置杠杆范围。
    account_form : ChannelAccountForm
        账户连接表单定义。
    schedule : ChannelSchedule
        渠道在公共前端采用的定时规则类型。
    calendar : ChannelCalendar | None
        渠道使用的共享交易日历；为空表示无需交易日历。
    portfolio : ChannelPortfolioPreset
        组合向导使用的市场名称与示例标的。
    """

    channel: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    label: str = Field(min_length=1)
    description: str = Field(min_length=1)
    icon: str = Field(min_length=1)
    market: str = Field(min_length=1)
    currency: str = Field(min_length=1)
    units: ChannelUnits = Field(default_factory=ChannelUnits)
    ui: ChannelUi = Field(default_factory=ChannelUi)
    defaults: ChannelDefaults
    leverage: ChannelLeverage
    account_form: ChannelAccountForm
    schedule: ChannelSchedule
    calendar: ChannelCalendar | None = None
    connection_validation: ChannelConnectionValidation | None = None
    portfolio: ChannelPortfolioPreset


@dataclass(frozen=True, slots=True)
class ChannelPlugin:
    """
    聚合一个交易渠道的静态描述与运行时能力.

    Attributes
    ----------
    descriptor : ChannelDescriptor
        API 与前端可见的静态描述。
    account_config_model : type[BaseAccountConfig]
        用于验证渠道账户配置的 Pydantic 模型。
    create_executor : ExecutorFactory
        根据已验证配置创建执行器的工厂。
    target_transform : TargetTransform
        将策略权重表转换为渠道目标贡献度的函数。
    execution_backend : ExecutionBackend
        执行阻塞渠道逻辑所用的隔离后端。
    required_modules : tuple[str, ...]
        判断运行时可用性所需的顶层 Python 模块。
    install_extra : str | None
        公开包中对应的可选依赖 extra；外部插件通常为空。
    max_parallel_symbols : int | None
        单次执行允许并行处理的最大品种数；``None`` 表示渠道不附加上限，
        仅使用执行环境推导出的系统保护上限。
    order_param_model : OrderParamModel
        渠道表达下单意图的参数模型；未声明时为 ``UNKNOWN``，
        算法在未知模型上按"歧义即失败"拒绝猜测下单语义。
    canonicalize_symbol : CanonicalizeSymbol
        把策略代码收到与持仓同一套标识；默认恒等。
    quantize_target_quantity : QuantizeTargetQuantity | None
        把目标权重量化成可下单数量 ``(weight, equity, notional_per_unit) -> qty``；
        缺省表示该渠道尚未提供量化，在位性回退权重阈值。
    """

    descriptor: ChannelDescriptor
    account_config_model: type[BaseAccountConfig]
    create_executor: ExecutorFactory
    target_transform: TargetTransform
    execution_backend: ExecutionBackend = "thread"
    required_modules: tuple[str, ...] = ()
    install_extra: str | None = None
    max_parallel_symbols: int | None = 1
    order_param_model: OrderParamModel = OrderParamModel.UNKNOWN
    canonicalize_symbol: CanonicalizeSymbol = _identity_symbol
    quantize_target_quantity: QuantizeTargetQuantity | None = None
    requires_pre_connect_guard: bool = False
    validate_connection: ConnectionValidator | None = None

    def __post_init__(self) -> None:
        """校验插件运行时契约中的基本不变量."""
        if (self.validate_connection is None) != (self.descriptor.connection_validation is None):
            raise ValueError("连接验证描述与验证器必须同时声明")
        if not issubclass(self.account_config_model, BaseAccountConfig):
            raise TypeError("account_config_model 必须继承 BaseAccountConfig")
        if self.max_parallel_symbols is not None and self.max_parallel_symbols < 1:
            raise ValueError("max_parallel_symbols 必须大于等于 1")
