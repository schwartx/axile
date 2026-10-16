"""
交易算法模块.

提供函数式的交易算法实现，支持通过装饰器注册新算法。
所有执行器均实现统一的 ExecutorProtocol 协议。
"""

from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol, Type, overload, runtime_checkable

from pydantic import BaseModel

from axile.common.order_param_model import OrderParamModel
from axile.common.trade_channel import TradeChannel
from axile.executor.models.execution_result import AlgorithmResult
from axile.executor.models.unified_account_assets import PositionDirection, UnifiedAccountAssets
from axile.executor.models.unified_callback import (
    OrderUpdateCallback,
    PriceDataCallback,
    TradeRecordCallback,
)
from axile.executor.models.unified_order import OrderDirection, OrderType, TradeRecord, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData


@runtime_checkable
class LoggerProtocol(Protocol):
    """执行器和算法共用的 Loguru 兼容日志协议."""

    @overload
    def debug(self, __message: str, *args: object, **kwargs: object) -> None:
        pass

    @overload
    def debug(self, __message: object) -> None:
        pass

    @overload
    def info(self, __message: str, *args: object, **kwargs: object) -> None:
        pass

    @overload
    def info(self, __message: object) -> None:
        pass

    @overload
    def warning(self, __message: str, *args: object, **kwargs: object) -> None:
        pass

    @overload
    def warning(self, __message: object) -> None:
        pass

    @overload
    def error(self, __message: str, *args: object, **kwargs: object) -> None:
        pass

    @overload
    def error(self, __message: object) -> None:
        pass

    @overload
    def exception(self, __message: str, *args: object, **kwargs: object) -> None:
        pass

    @overload
    def exception(self, __message: object) -> None:
        pass


@dataclass(slots=True, frozen=True)
class AlgorithmInput:
    """算法层可见的单品种输入.

    该对象由通用执行层在 planning 阶段生成。
    算法只消费已经规划好的目标数量与当前品种配置，
    不再直接依赖 `UnifiedStandardInput`。
    """

    symbol: str
    target_volume: int | float
    trade_rule: dict[str, object]
    params: object | None = None


@runtime_checkable
class ExecutorProtocol(Protocol):
    """执行器协议.

    定义算法可见的 symbol 级执行会话接口。
    算法通过此协议与当前品种的执行上下文交互。
    """

    @property
    def channel_type(self) -> TradeChannel:
        """当前执行会话所属交易渠道."""
        ...

    @property
    def order_param_model(self) -> OrderParamModel:
        """当前渠道的订单参数模型(未声明时为 UNKNOWN)."""
        ...

    @property
    def symbol(self) -> str:
        """当前执行会话绑定的品种代码."""
        ...

    def get_current_volume(self, account_assets: UnifiedAccountAssets) -> float:
        """获取当前品种的净持仓数量（支持小数精度）."""
        ...

    def plan_close_orders(
        self, direction: OrderDirection, volume: float, account_assets: UnifiedAccountAssets
    ) -> list[tuple[float, dict[str, object]]]:
        """按账户规则和渠道能力生成当前合约的平仓计划。"""
        ...

    def get_positions(self, account_assets: UnifiedAccountAssets) -> list[tuple[float, PositionDirection]]:
        """获取当前品种的所有持仓（含方向）."""
        ...

    def place_order(
        self,
        direction: OrderDirection,
        order_type: OrderType,
        volume: float,
        price: float = 0,
        **kwargs: object,
    ) -> UnifiedOrder:
        """下单."""
        ...

    def get_pending_orders(self) -> list[UnifiedOrder]:
        """获取未完成订单."""
        ...

    def query_trades(self, order_id: str) -> list[TradeRecord]:
        """获取指定订单的成交明细."""
        ...

    def is_termination_requested(self) -> bool:
        """当前 execution 是否已收到 terminate 请求."""
        ...

    def get_termination_mode(self) -> str | None:
        """读取当前 terminate 模式."""
        ...

    def handle_termination_checkpoint(self) -> None:
        """在算法检查点响应 terminate 请求."""
        ...

    def sleep_or_terminate(self, seconds: float) -> None:
        """协作式片间等待：睡满 ``seconds``，或收到 terminate 请求时立即中断并抛出."""
        ...

    @property
    def logger(self) -> LoggerProtocol:
        """日志记录器."""
        ...

    @property
    def audit_context(self) -> dict[str, Any]:
        """当前执行的审计上下文."""
        ...

    def set_audit_context(self, context: dict[str, Any]) -> None:
        """设置当前执行的审计上下文."""
        ...

    def next_audit_seq(self) -> int:
        """获取下一个单调递增的审计序号."""
        ...

    def register_order_audit_metadata(self, order_id: str, metadata: dict[str, Any]) -> None:
        """为订单附加审计元数据."""
        ...

    def get_order_audit_metadata(self, order_id: str) -> dict[str, Any]:
        """读取订单关联的审计元数据."""
        ...

    def emit_audit_event(
        self,
        *,
        event_type: object,
        status: object,
        reason_family: object,
        reason_code: str,
        symbol: str | None = None,
        intent_id: str | None = None,
        order_id: str | None = None,
        client_order_id: str | None = None,
        ts_exchange: str | None = None,
        seq: int | None = None,
        details: dict[str, object] | None = None,
        audit_context: dict[str, Any] | None = None,
    ) -> bool:
        """为当前执行持久化一条审计事件."""
        ...

    def get_account_assets(self) -> UnifiedAccountAssets:
        """获取账户资产."""
        ...

    def get_market_data(self) -> UnifiedPriceData | None:
        """获取当前品种的市场数据快照."""
        ...

    def get_tick_size(self) -> float | None:
        """获取当前品种的最小价格变动单位."""
        ...

    def get_min_notional(self) -> float | None:
        """获取当前品种下单的最小名义价值（单位为计价货币）；渠道未知时返回 ``None``."""
        ...

    def register_order_callback(self, callback: OrderUpdateCallback) -> None:
        """注册订单更新回调函数."""
        ...

    def register_price_callback(self, callback: PriceDataCallback) -> None:
        """注册价格数据回调函数."""
        ...

    def register_trade_callback(self, callback: TradeRecordCallback) -> None:
        """注册成交记录回调函数."""
        ...

    def unregister_order_callback(self, callback: OrderUpdateCallback) -> None:
        """注销订单更新回调函数."""
        ...

    def unregister_price_callback(self, callback: PriceDataCallback) -> None:
        """注销价格数据回调函数."""
        ...

    def unregister_trade_callback(self, callback: TradeRecordCallback) -> None:
        """注销成交记录回调函数."""
        ...

    def is_monitoring(self) -> bool:
        """检查 WebSocket 是否正在监控."""
        ...

    def cancel_order(self, order_id: str) -> bool:
        """撤销当前品种的指定订单."""
        ...


AlgorithmFunc = Callable[[ExecutorProtocol, AlgorithmInput], AlgorithmResult]

AlgorithmSlot = Literal["trade", "empty"]
"""算法可用槽位：``trade`` 为主交易（调仓到目标），``empty`` 为清仓（目标清空时平仓）."""


@dataclass(slots=True, frozen=True)
class AlgorithmMetadata:
    """
    描述已注册算法的静态元数据。

    Attributes
    ----------
    name : str
        算法名称。
    func : AlgorithmFunc
        算法实现函数。
    label : str
        面向用户的算法名称。
    description : str
        面向用户的算法说明。
    default_params : dict[str, object]
        新建算法配置时使用的默认参数。
    params_schema : dict[str, object]
        参数模型对应的 JSON Schema。
    channels : frozenset[str] | None
        支持的交易渠道集合；为 ``None`` 时表示全渠道通用。
    order_param_models : frozenset[str] | None
        算法适配的订单参数模型集合；为 ``None`` 时表示全模型通用。
        与 ``channels`` 正交:channels 约束"能不能跑",本字段约束"跑的时候
        下单参数语义能不能被渠道满足",供配置期配对校验。
    params_class : Type[BaseModel] | None
        参数模型类型；为 ``None`` 时表示算法自行处理原始参数对象。
    slots : frozenset[AlgorithmSlot] | None
        算法可用的槽位集合；为 ``None`` 时表示主交易与清仓两槽通用，
        为空集合时表示两槽都不适用（如期权行权等特种任务）。
    """

    name: str
    func: AlgorithmFunc
    label: str
    description: str
    default_params: dict[str, object]
    params_schema: dict[str, object]
    channels: frozenset[str] | None
    order_param_models: frozenset[str] | None = None
    params_class: Type[BaseModel] | None = None
    slots: frozenset[AlgorithmSlot] | None = None

    def __str__(self) -> str:
        """返回紧凑的算法元数据字符串表示."""
        channels_str = "all" if self.channels is None else sorted(self.channels)
        slots_str = "all" if self.slots is None else sorted(self.slots)
        return (
            f"AlgorithmMetadata(name={self.name}, "
            f"channels={channels_str}, "
            f"slots={slots_str}, "
            f"params_class={self.params_class.__name__ if self.params_class else None})"
        )


_algorithm_registry: dict[str, AlgorithmMetadata] = {}
_algorithms_loaded: bool = False

ALL_ORDER_PARAM_MODELS: list[OrderParamModel] = [
    OrderParamModel.POSITION_SIDE,
    OrderParamModel.DIRECTIONAL,
    OrderParamModel.OFFSET,
]
"""通用算法声明全模型适配的便捷常量;新模型出现时需重新评估算法兼容性."""


def register_algorithm(
    name: str,
    *,
    channels: list[str] | None = None,
    order_param_models: list[OrderParamModel] | None = None,
    params_class: Type[BaseModel] | None = None,
    slots: list[AlgorithmSlot] | None = None,
    label: str | None = None,
    description: str | None = None,
    default_params: dict[str, object] | None = None,
) -> Callable[[AlgorithmFunc], AlgorithmFunc]:
    """
    注册算法函数并写入全局注册表。

    Parameters
    ----------
    name : str
        算法名称。
    channels : list[str] | None, default=None
        支持的交易渠道列表；为空时表示所有渠道都可使用。
    order_param_models : list[OrderParamModel] | None, default=None
        算法适配的订单参数模型列表；为空时表示全模型通用。
        供账户配置期与渠道的 ``ChannelPlugin.order_param_model`` 配对校验。
    params_class : Type[BaseModel] | None, default=None
        算法参数模型类型；为空时表示算法自行解释参数对象。
    slots : list[AlgorithmSlot] | None, default=None
        算法适用的槽位列表；``None`` 表示主交易与清仓两槽通用，
        空列表 ``[]`` 表示两槽都不适用。与 ``channels`` 不同，此处空列表
        不等价于 ``None``，用于把特种任务（如期权行权）排除在两槽之外。
    label : str | None, default=None
        面向用户的算法名称；为空时使用注册名称。
    description : str | None, default=None
        面向用户的算法说明；为空时取函数 docstring 首行。
    default_params : dict[str, object] | None, default=None
        默认参数；为空且参数模型可无参构造时从参数模型生成。

    Returns
    -------
    Callable[[AlgorithmFunc], AlgorithmFunc]
        用于包裹算法函数的装饰器。

    Notes
    -----
    装饰器执行时只登记元数据，不做兼容性校验；真正的渠道校验延后到
    ``resolve_algorithm``，这样模块导入阶段不会因为当前上下文缺失而失败。
    槽位不参与执行期校验，仅供上层（如 UI）过滤候选算法。
    """

    def decorator(func: AlgorithmFunc) -> AlgorithmFunc:
        resolved_description = description
        if resolved_description is None:
            docstring = (func.__doc__ or "").strip()
            resolved_description = docstring.splitlines()[0].strip() if docstring else ""

        resolved_defaults = dict(default_params or {})
        params_schema: dict[str, object] = {}
        if params_class is not None:
            params_schema = params_class.model_json_schema()
            if default_params is None:
                try:
                    resolved_defaults = dict(params_class().model_dump(mode="json"))
                except Exception:
                    resolved_defaults = {}

        metadata = AlgorithmMetadata(
            name=name,
            func=func,
            label=label or name,
            description=resolved_description,
            default_params=resolved_defaults,
            params_schema=params_schema,
            channels=frozenset(str(channel) for channel in channels) if channels else None,
            order_param_models=frozenset(str(model) for model in order_param_models) if order_param_models else None,
            params_class=params_class,
            slots=frozenset(slots) if slots is not None else None,
        )
        _algorithm_registry[name] = metadata
        return func

    return decorator


def _ensure_algorithms_loaded() -> None:
    """
    确保默认算法与插件算法都已完成注册。

    Notes
    -----
    这里采用惰性加载，是为了避免单纯导入算法注册表模块就触发整棵默认算法包和
    entry point 插件的副作用。
    """
    global _algorithms_loaded
    if _algorithms_loaded:
        return

    from .loader import load_default_algorithms, load_entrypoint_algorithms

    load_default_algorithms()
    load_entrypoint_algorithms()
    _algorithms_loaded = True


def get_algorithm(name: str) -> AlgorithmFunc:
    """
    读取已注册的算法函数，不做渠道兼容性验证。

    Parameters
    ----------
    name : str
        算法名称。

    Returns
    -------
    AlgorithmFunc
        对应的算法函数。

    Raises
    ------
    ValueError
        指定算法不存在时抛出。
    """
    _ensure_algorithms_loaded()
    if name not in _algorithm_registry:
        raise ValueError(f"未找到算法: {name}")
    return _algorithm_registry[name].func


def get_algorithm_metadata(name: str) -> AlgorithmMetadata:
    """
    读取已注册算法的元数据。

    Parameters
    ----------
    name : str
        算法名称。

    Returns
    -------
    AlgorithmMetadata
        对应的算法元数据。

    Raises
    ------
    ValueError
        指定算法不存在时抛出。
    """
    _ensure_algorithms_loaded()
    if name not in _algorithm_registry:
        raise ValueError(f"未找到算法: {name}")
    return _algorithm_registry[name]


def resolve_algorithm(
    name: str,
    executor: ExecutorProtocol,
) -> AlgorithmFunc:
    """
    根据算法名称解析算法函数，并校验渠道兼容性。

    Parameters
    ----------
    name : str
        算法名称。
    executor : ExecutorProtocol
        当前执行器会话。

    Returns
    -------
    AlgorithmFunc
        通过兼容性校验后的算法函数。

    Raises
    ------
    ValueError
        算法不存在，或当前渠道不在算法支持范围内时抛出。
    """
    _ensure_algorithms_loaded()
    if name not in _algorithm_registry:
        raise ValueError(f"未找到算法: {name}")

    meta = _algorithm_registry[name]

    if meta.channels is not None:
        channel = getattr(executor, "channel_type", None)
        if channel is not None and channel not in meta.channels:
            supported = sorted(meta.channels)
            raise ValueError(f"算法 {name} 不支持渠道 {channel}，仅支持: {supported}")

    return meta.func


def list_algorithms() -> list[str]:
    """
    列出所有已注册算法名称。

    Returns
    -------
    list[str]
        当前注册表中的算法名称列表。
    """
    _ensure_algorithms_loaded()
    return list(_algorithm_registry.keys())


def list_algorithms_metadata() -> list[AlgorithmMetadata]:
    """
    列出所有已注册算法的元数据。

    Returns
    -------
    list[AlgorithmMetadata]
        当前注册表中的算法元数据列表。
    """
    _ensure_algorithms_loaded()
    return list(_algorithm_registry.values())
