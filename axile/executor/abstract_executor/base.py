"""统一交易执行器抽象基类定义.

本文件只负责声明 `AbstractExecutor` 的抽象契约与类型别名，
不承载具体执行逻辑；实际默认实现由同目录下的 mixin 提供。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from axile.executor.abstract_executor.capability import AbstractExecutorCapabilityMixin
from axile.executor.abstract_executor.execution import AbstractExecutorExecutionMixin
from axile.executor.abstract_executor.facade import AbstractExecutorFacadeMixin
from axile.executor.account_orders import AccountOrderBook
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_callback import OrderUpdateCallback, PriceDataCallback
from axile.executor.models.unified_input import AccountConfig
from axile.executor.models.unified_order import OrderDirection, OrderType, TradeRecord, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData

if TYPE_CHECKING:
    from loguru import Logger

    from axile.common.trade_channel import TradeChannel
    from axile.executor.execution_query_runtime import ExecutionQueryRuntimeBridge
    from axile.executor.execution_runtime import ExecutionRuntime, ExecutionRuntimeBindings
    from axile.executor.trading_calendar import TradingCalendar

type ObjectDict = dict[str, object]
type AuditContext = ObjectDict
type OrderAuditMetadata = ObjectDict
type TradeRule = ObjectDict
type TradeRules = dict[str, TradeRule]
type TargetVolumeValue = int | float
type TargetVolumes = dict[str, TargetVolumeValue]


class AbstractExecutor(
    AbstractExecutorExecutionMixin,
    AbstractExecutorCapabilityMixin,
    AbstractExecutorFacadeMixin,
    ABC,
):
    """
    交易执行器抽象基类.

    Notes
    -----
    主文件只保留渠道接口声明；执行生命周期、默认能力和 facade 逻辑均由 mixin 承载。

    阅读顺序建议：

    1. 先看 `execution_lifecycle.py` 中的 `execute()` 主流程。
    2. 再看 `execution_engine.py` 理解 symbol 级编排。
    3. 最后回到本类和各渠道执行器，查看哪些抽象接口由渠道实现覆盖。
    """

    # 这里把“执行生命周期”“默认能力”“对外 facade”组合到一个抽象宿主上。
    # 读代码时可以把它理解为：本类定义渠道契约，mixin 提供统一骨架。
    # 这些宿主字段由 `AbstractExecutorExecutionMixin.__init__()` 初始化，保留在主类上仅用于
    # 静态类型表达，避免 mixin 访问它们时退化成“未知属性”。
    account_orders: AccountOrderBook
    channel_type: TradeChannel
    logger: Logger
    account_config: AccountConfig | None
    _runtime_bindings: ExecutionRuntimeBindings
    _active_execution_runtime: ExecutionRuntime | None
    _execution_query_runtime_bridge: ExecutionQueryRuntimeBridge
    _trading_calendar: TradingCalendar | None
    _channel_calendar_id: str | None

    @abstractmethod
    def _initialize_connection(self, account_config: AccountConfig) -> None:
        """
        初始化并建立连接.

        Parameters
        ----------
        account_config : AccountConfig
            账户配置信息，包含认证和连接所需字段。
        """

    @abstractmethod
    def _verify_connection(self) -> bool:
        """
        验证当前连接是否有效.

        Returns
        -------
        bool
            连接有效时返回 ``True``，否则返回 ``False``。
        """

    @abstractmethod
    def _check_trading_time(self) -> bool:
        """
        检查当前是否处于可交易时间.

        Returns
        -------
        bool
            可交易时返回 ``True``，否则返回 ``False``。
        """

    @abstractmethod
    def get_account_assets(self) -> UnifiedAccountAssets:
        """
        获取账户资产快照.

        Returns
        -------
        UnifiedAccountAssets
            统一格式的账户资产信息。
        """

    @abstractmethod
    def get_market_data(self, symbols: list[str]) -> dict[str, UnifiedPriceData]:
        """
        获取指定品种的市场数据.

        Parameters
        ----------
        symbols : list[str]
            需要获取行情的品种代码列表。

        Returns
        -------
        dict[str, UnifiedPriceData]
            品种代码到市场数据的映射字典。
        """

    @abstractmethod
    def _cleanup(self) -> None:
        """清理执行过程中产生的资源."""

    @abstractmethod
    def _get_account_mark(self) -> str:
        """
        获取账户标识.

        Returns
        -------
        str
            用于通知和日志展示的账户标识字符串。
        """

    @abstractmethod
    def _get_default_trade_rules_for_empty(self, symbols: list[str]) -> TradeRules:
        """
        获取清仓场景下的默认交易规则.

        Parameters
        ----------
        symbols : list[str]
            需要清仓的品种代码列表。

        Returns
        -------
        TradeRules
            默认的交易规则字典。
        """

    @abstractmethod
    def register_order_callback(self, callback: OrderUpdateCallback) -> None:
        """
        注册订单更新回调函数.

        Parameters
        ----------
        callback : OrderUpdateCallback
            符合订单回调协议的可调用对象。
        """

    @abstractmethod
    def register_price_callback(self, callback: PriceDataCallback) -> None:
        """
        注册价格数据回调函数.

        Parameters
        ----------
        callback : PriceDataCallback
            符合价格回调协议的可调用对象。
        """

    @abstractmethod
    def unregister_order_callback(self, callback: OrderUpdateCallback) -> None:
        """
        注销订单更新回调函数.

        Parameters
        ----------
        callback : OrderUpdateCallback
            待注销的回调函数。
        """

    @abstractmethod
    def unregister_price_callback(self, callback: PriceDataCallback) -> None:
        """
        注销价格数据回调函数.

        Parameters
        ----------
        callback : PriceDataCallback
            待注销的回调函数。
        """

    @abstractmethod
    def initialize_websocket(self, symbols: list[str] | None = None) -> None:
        """
        初始化 WebSocket 连接.

        Parameters
        ----------
        symbols : list[str] | None, default=None
            需要订阅行情的品种代码列表；为空时由实现类自行决定订阅范围。
        """

    @abstractmethod
    def is_monitoring(self) -> bool:
        """
        返回当前是否处于监控状态.

        Returns
        -------
        bool
            正在监控时返回 ``True``，否则返回 ``False``。
        """

    @abstractmethod
    def _place_order_impl(
        self,
        symbol: str,
        direction: OrderDirection,
        order_type: OrderType,
        volume: float,
        price: float = 0,
        **kwargs: object,
    ) -> UnifiedOrder:
        """
        执行渠道原生下单.

        Parameters
        ----------
        symbol : str
            下单品种代码。
        direction : OrderDirection
            买卖方向。
        order_type : OrderType
            订单类型。
        volume : float
            委托数量。
        price : float, default=0
            委托价格。
        **kwargs : object
            透传给渠道实现的扩展字段。

        Returns
        -------
        UnifiedOrder
            渠道返回并规范化后的订单对象。
        """

    @abstractmethod
    def _get_pending_orders_impl(self, symbol: str | None = None) -> list[UnifiedOrder]:
        """
        执行渠道原生挂单查询.

        Parameters
        ----------
        symbol : str | None, default=None
            需要查询的品种代码；为空时查询全部未完成订单。

        Returns
        -------
        list[UnifiedOrder]
            渠道返回的未完成订单列表。
        """

    @abstractmethod
    def _query_trades_impl(self, symbol: str, order_id: str) -> list[TradeRecord]:
        """
        执行渠道原生的按单成交查询.

        Parameters
        ----------
        symbol : str
            品种代码。
        order_id : str
            订单标识。

        Returns
        -------
        list[TradeRecord]
            该订单对应的成交明细列表。
        """

    @abstractmethod
    def _cancel_order_impl(self, symbol: str, order_id: str) -> bool:
        """
        执行渠道原生撤单.

        Parameters
        ----------
        symbol : str
            品种代码。
        order_id : str
            需要撤销的订单标识。

        Returns
        -------
        bool
            撤单提交成功时返回 ``True``，否则返回 ``False``。
        """
