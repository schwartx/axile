"""
GM（掘金）交易执行器，全面使用回调模式.

基于 ``AbstractExecutor`` 架构实现统一下单、查询和回调注册能力，并接入单账户 bridge 运行时。

Examples
--------
>>> from axile.executor.gm import GMExecutor
>>> from axile.executor.models.unified_input import GMAccountConfig
>>> config = GMAccountConfig(
...     connection_mode="terminal",
...     account_id="your_account_id",
...     token="your_token",
...     terminal_path="C:/goldminer3",
... )
>>> executor = GMExecutor(config)
>>> executor.start_callback_monitoring()
>>> executor.stop()
"""

import json
import math
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast, override

import psutil
from loguru import logger

from axile.channels.schedule_clock import CN_STOCK_WINDOWS, is_within_schedule_windows
from axile.common.gm_symbols import GM_SYMBOL_RESOLVER, normalize_gm_standard_input
from axile.common.trade_channel import TradeChannel
from axile.executor.abstract_executor.base import AbstractExecutor
from axile.executor.connection_state import connection_monitor, preserve_connection_failure
from axile.executor.constants.order_status import OrderStatus
from axile.executor.gm.common import (
    convert_gm_order_to_unified,
    convert_gm_trade_to_trade_record,
    from_gm_price,
)
from axile.executor.gm.connection import report_gm_failure
from axile.executor.gm.core.api_bridge import (
    CurrentRequest,
    GetCashRequest,
    GetExecutionReportsRequest,
    GetOrdersRequest,
    GetPositionRequest,
    GetUnfinishedOrdersRequest,
    GMCancelOrderTarget,
    GMOrderKind,
    GMOrderSide,
    GMPositionEffect,
    GMSdkRequest,
    OrderCancelRequest,
    OrderVolumeRequest,
)
from axile.executor.gm.core.callback_dispatcher import GMCallbackDispatcher
from axile.executor.gm.core.strategy_bridge import GMStrategyBridge
from axile.executor.models.execution_result import TargetSizingDecision
from axile.executor.models.unified_account_assets import Position, PositionDirection, UnifiedAccountAssets
from axile.executor.models.unified_callback import (
    OrderUpdateCallback,
    PriceDataCallback,
    TradeRecordCallback,
    UnifiedCallbackClient,
)
from axile.executor.models.unified_input import AccountConfig, GMAccountConfig, UnifiedStandardInput
from axile.executor.models.unified_order import OrderDirection, OrderType, TradeRecord, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData

__all__ = [
    "GMExecutor",
    "convert_gm_order_to_unified",
    "convert_gm_trade_to_trade_record",
    "from_gm_price",
]


def _is_gm_ashare_symbol(symbol: str) -> bool:
    """判断是否为 Axile 统一格式的沪深北 A 股标的."""
    return symbol.endswith((".SH", ".SZ", ".BJ"))


def _gm_available_volume(position: dict[str, Any]) -> float:
    """读取 GM 当前真实可平数量，并兼容不含 ``available_now`` 的旧 SDK."""
    raw_available = position["available_now"] if "available_now" in position else position.get("available", 0)
    return max(float(raw_available or 0), 0.0)


# ==================== GM 执行器 ====================


class GMExecutor(AbstractExecutor, UnifiedCallbackClient):
    """
    GM（掘金）交易执行器，全面使用回调模式.

    继承自 ``AbstractExecutor`` 并实现 ``UnifiedCallbackClient``，通过单账户 bridge
    接收订单状态、成交回报和可用的行情回调。

    Notes
    -----
    创建实例时会尝试启动单账户 bridge 回调。执行期订单跟踪不再回退到挂单轮询，
    GM 下单链路统一依赖回调事件收敛状态。
    """

    def __init__(self, account_config: GMAccountConfig) -> None:
        """初始化GM执行器."""
        self.account_id: str | None = None
        self.just_started = False

        # 回调组件
        self._callback_dispatcher = GMCallbackDispatcher()
        self._strategy_bridge: GMStrategyBridge | None = None
        self._callback_monitoring = False

        # 保存配置用于启动回调
        self._gm_config: GMAccountConfig | None = None

        # 订阅的行情标的列表
        self._subscribe_symbols: list[str] = []

        super().__init__(TradeChannel.GM, account_config)

        # GM 下单依赖回调收敛状态，创建阶段必须确认 bridge 可用；失败直接交给
        # worker/API 生命周期落成 FAILED，不能吞掉后在执行阶段重复等待。
        self._ensure_strategy_bridge()
        self._register_execution_query_runtime_callback_observers()

    def _normalize_symbol(self, symbol: str) -> str:
        """将 GM 或 Tushare A 股代码统一为 Tushare 格式."""
        return GM_SYMBOL_RESOLVER.to_axile(GM_SYMBOL_RESOLVER.to_gm(symbol))

    def _normalize_standard_input(self, standard_input: UnifiedStandardInput) -> UnifiedStandardInput:
        """在 planning 前统一标准输入中的 A 股代码."""
        return normalize_gm_standard_input(standard_input)

    # ==================== 连接管理方法 ====================

    def _initialize_connection(self, account_config: AccountConfig) -> None:
        """初始化并建立GM连接."""
        if not isinstance(account_config, GMAccountConfig):
            raise TypeError("GMExecutor 需要 GMAccountConfig 类型的账户配置")

        self._gm_config = account_config
        self.account_id = account_config.account_id

        if account_config.connection_mode == "service":
            self.just_started = False
        else:
            terminal_path = account_config.terminal_path
            if terminal_path is None:
                raise RuntimeError("GM terminal 模式缺少终端目录")
            with connection_monitor(self).connecting():
                self.just_started = self._start_gm_desktop_if_not(terminal_path)

        logger.success(f"账户 {self.account_id} 初始化完成，GM runtime 将通过 bridge 懒启动")

    def _verify_connection(self) -> bool:
        """验证连接是否有效."""
        try:
            self._ensure_strategy_bridge(timeout=30.0)
            return True
        except Exception as e:
            logger.opt(exception=e).error("验证连接失败: {}", e)
            return False

    def _check_trading_time(self) -> bool:
        """检查当前是否处于 A 股可报单窗且自然日开市。"""
        return is_within_schedule_windows(datetime.now(), CN_STOCK_WINDOWS) and self._is_channel_calendar_open()

    # ==================== 回调模式方法 ====================

    def start_callback_monitoring(self, timeout: float = 30.0, subscribe_symbols: list[str] | None = None) -> bool:
        """
        启动回调监控.

        启动后台策略框架，开始接收订单和成交的实时推送。

        Parameters
        ----------
        timeout : float, default=30.0
            启动当前执行器 bridge 的超时时间，单位为秒。
        subscribe_symbols : list[str] | None, optional
            启动后需要订阅的标的列表。

        Returns
        -------
        bool
            启动成功时返回 ``True``，否则返回 ``False``。

        Notes
        -----
        由于 GM SDK 的限制，后台线程中的 ``subscribe tick`` 无法稳定工作。
        订单状态和成交回报回调可以正常使用；如需实时行情，请改用 ``get_market_data()`` 获取快照。
        """
        try:
            self._ensure_strategy_bridge(timeout=timeout, subscribe_symbols=subscribe_symbols)
            logger.success("回调监控启动成功")
            return True
        except Exception as e:
            logger.opt(exception=e).error("回调监控启动失败: {}", e)
            return False

    def subscribe_price(self, symbols: list[str]) -> None:
        """
        订阅行情数据.

        在调用 start_callback_monitoring() 之前调用此方法，
        或者在 start_callback_monitoring() 时传入 subscribe_symbols 参数。

        Parameters
        ----------
        symbols : list[str]
            要订阅的标的列表，例如 ``["600000.SH", "000001.SZ"]``；
            同时兼容 GM 原生格式。

        Notes
        -----
        如果回调监控已经启动，会尝试通过当前 bridge 动态补订阅新增标的。
        """
        self._subscribe_symbols = [self._normalize_symbol(symbol) for symbol in self._subscribe_symbols]
        normalized_symbols = [self._normalize_symbol(symbol) for symbol in symbols if symbol]
        new_symbols = [symbol for symbol in normalized_symbols if symbol not in self._subscribe_symbols]
        self._subscribe_symbols = list(dict.fromkeys(self._subscribe_symbols + normalized_symbols))

        if new_symbols and self._strategy_bridge is not None and self._strategy_bridge.is_running():
            self._strategy_bridge.request_symbols([GM_SYMBOL_RESOLVER.to_gm(symbol) for symbol in new_symbols])
            logger.info(f"已动态更新行情订阅: {new_symbols}")
            return

        logger.info(f"已添加行情订阅: {normalized_symbols}")

    def stop_callback_monitoring(self) -> None:
        """停止回调监控."""
        if self._strategy_bridge:
            self._strategy_bridge.stop()
            self._strategy_bridge = None
        self._callback_monitoring = False
        logger.info("回调监控已停止")

    def initialize_websocket(self, symbols: list[str] | None = None) -> None:
        """
        初始化 WebSocket 连接.

        委托给 start_callback_monitoring 方法启动回调监控。

        Parameters
        ----------
        symbols : list[str] | None, optional
            需要订阅行情的标的列表。
        """
        self.start_callback_monitoring(subscribe_symbols=symbols)

    def _ensure_strategy_bridge(
        self,
        *,
        timeout: float = 30.0,
        subscribe_symbols: list[str] | None = None,
    ) -> GMStrategyBridge:
        """确保当前执行器的 bridge runtime 已启动."""
        self._subscribe_symbols = [self._normalize_symbol(symbol) for symbol in self._subscribe_symbols]
        all_symbols = list(
            dict.fromkeys(
                self._subscribe_symbols
                + [self._normalize_symbol(symbol) for symbol in (subscribe_symbols or []) if symbol]
            )
        )
        gm_symbols = [GM_SYMBOL_RESOLVER.to_gm(symbol) for symbol in all_symbols]

        if self._strategy_bridge and self._strategy_bridge.is_running():
            if all_symbols:
                self._strategy_bridge.request_symbols(gm_symbols)
            self._callback_monitoring = True
            return self._strategy_bridge

        if not self._gm_config:
            raise ValueError("账户配置未初始化")

        if all_symbols:
            logger.info(f"将订阅以下标的的行情: {all_symbols}")

        bridge = GMStrategyBridge(
            token=self._gm_config.token,
            account_id=self._gm_config.account_id,
            callback_dispatcher=self._callback_dispatcher,
            serv_addr=self._gm_config.serv_addr,
            subscribe_symbols=gm_symbols,
        )
        bridge.connection_monitor = connection_monitor(self)
        if not bridge.start(timeout=timeout):
            error = RuntimeError("GM bridge 启动失败")
            startup_error = getattr(bridge, "_startup_error", None)
            if isinstance(startup_error, BaseException):
                preserve_connection_failure(error, startup_error)
            connection_monitor(self).failed(error)
            raise error
        self._strategy_bridge = bridge
        self._callback_monitoring = True
        return self._strategy_bridge

    def _call_bridge(self, request: GMSdkRequest, *, timeout: float = 30.0) -> Any:
        """统一通过当前执行器持有的 bridge 调用 GM SDK."""
        if getattr(self, "_gm_config", None) is None:
            raise RuntimeError("GMExecutor 尚未初始化连接配置，无法调用 GM bridge")
        bridge = self._ensure_strategy_bridge(timeout=timeout)
        # GM 对无效账户也可能返回空列表；空持仓/订单不能证明账户有效。
        if request.operation == "get_cash" and getattr(request, "account_id", None) in {None, self.account_id}:
            with connection_monitor(self).accessing():
                cash = bridge.call(request, timeout=timeout)
                returned_account = cash.get("account_id") if isinstance(cash, dict) else None
                if not cash or returned_account not in {None, "", self.account_id}:
                    error = RuntimeError(
                        f"未获取到指定账户的有效资金信息，请检查账户 ID、登录状态和访问权限（账户 ID：{self.account_id}）"
                    )
                    report_gm_failure(
                        connection_monitor(self),
                        error,
                        "账户访问异常",
                        "当前资金查询未取得目标账户的有效资金信息。",
                        facts={"目标账户": self.account_id, "返回账户": returned_account, "有效资金信息": "未取得"},
                        steps=("核对账户 ID、登录状态和当前服务的访问权限。",),
                    )
                    logger.error("{}", error)
                    raise error
                return cash
        return bridge.call(request, timeout=timeout)

    # ==================== 实现 UnifiedCallbackClient 接口 ====================

    def register_order_callback(self, callback: OrderUpdateCallback) -> None:
        """注册订单更新回调函数."""
        self._callback_dispatcher.register_order_callback(callback)

    def register_trade_callback(self, callback: TradeRecordCallback) -> None:
        """注册成交记录回调函数."""
        self._callback_dispatcher.register_trade_callback(callback)

    def register_price_callback(self, callback: PriceDataCallback) -> None:
        """注册价格数据回调函数."""
        self._callback_dispatcher.register_price_callback(callback)

    def unregister_order_callback(self, callback: OrderUpdateCallback) -> None:
        """注销订单更新回调函数."""
        self._callback_dispatcher.unregister_order_callback(callback)

    def unregister_trade_callback(self, callback: TradeRecordCallback) -> None:
        """注销成交记录回调函数."""
        self._callback_dispatcher.unregister_trade_callback(callback)

    def unregister_price_callback(self, callback: PriceDataCallback) -> None:
        """注销价格数据回调函数."""
        self._callback_dispatcher.unregister_price_callback(callback)

    def is_monitoring(self) -> bool:
        """检查是否正在监控."""
        return self._callback_monitoring

    def stop(self) -> None:
        """停止回调客户端并清理资源."""
        connection_monitor(self).close()
        self.stop_callback_monitoring()
        self._callback_dispatcher.clear_all_callbacks()
        logger.info("执行器已停止")

    def get_callback_count(self) -> dict[str, int]:
        """获取当前注册的回调函数数量."""
        return self._callback_dispatcher.get_callback_count()

    def get_callback_stats(self) -> dict[str, int]:
        """获取回调统计信息."""
        stats = self._callback_dispatcher.get_stats()
        if self._strategy_bridge:
            bridge_stats = self._strategy_bridge.get_stats()
            stats.update(bridge_stats)
        return stats

    # ==================== 核心交易方法 ====================

    def get_account_assets(self) -> UnifiedAccountAssets:
        """获取账户资产."""
        if not self.account_id:
            raise ValueError("账户ID未初始化")

        logger.info(f"开始获取账户资产: account_id={self.account_id}")
        logger.info(f"准备查询持仓: account_id={self.account_id}")
        positions = cast(
            "list[Any]",
            self._call_bridge(GetPositionRequest(account_id=self.account_id), timeout=10.0),
        )
        logger.info(f"持仓查询完成: account_id={self.account_id}, positions={len(positions)}")
        logger.info(f"准备查询资金: account_id={self.account_id}")
        raw_cash = self._call_bridge(GetCashRequest(account_id=self.account_id), timeout=10.0)
        logger.info(f"资金查询完成: account_id={self.account_id}, has_cash={bool(raw_cash)}")
        cash: dict[str, Any] = (
            cast("dict[str, Any]", dict(raw_cash)) if raw_cash else {}  # pyright: ignore[reportUnknownArgumentType]
        )

        unified_positions: list[Position] = []
        for pos in positions:  # pyright: ignore[reportUnknownVariableType]
            pos_dict = cast("dict[str, Any]", pos)
            direction = PositionDirection.LONG if pos_dict.get("side", 1) == 1 else PositionDirection.SHORT

            position = Position.model_construct(
                symbol=GM_SYMBOL_RESOLVER.to_axile(str(pos_dict.get("symbol", ""))),
                volume=float(pos_dict.get("volume", 0)),
                available_volume=_gm_available_volume(pos_dict),
                market_value=float(pos_dict.get("market_value", 0)),
                direction=direction,
                avg_price=0.0,
                extra={
                    "channel_type": TradeChannel.GM,
                    "gm_symbol": str(pos_dict.get("symbol", "")),
                    "raw_position_data": pos_dict,
                },
            )
            unified_positions.append(position)

        market_value = sum(p.market_value for p in unified_positions)
        available_cash = float(cash.get("available", 0.0))  # pyright: ignore
        total_asset = available_cash + market_value

        logger.info(
            f"账户资产构建完成: account_id={self.account_id}, "
            f"available_cash={available_cash}, positions={len(unified_positions)}, market_value={market_value}"
        )

        return UnifiedAccountAssets.model_construct(
            available_cash=available_cash,
            total_asset=total_asset,
            market_value=market_value,
            positions=unified_positions,
            currency="CNY",
            extra={"channel_type": TradeChannel.GM},
        )

    def get_market_data(self, symbols: list[str]) -> dict[str, UnifiedPriceData]:
        """获取市场数据."""
        normalized_symbols = [self._normalize_symbol(symbol) for symbol in symbols]
        gm_symbols = [GM_SYMBOL_RESOLVER.to_gm(symbol) for symbol in normalized_symbols]
        logger.info(f"开始获取以下标的的实时行情快照: {normalized_symbols}")

        market_data: dict[str, UnifiedPriceData] = {}
        try:
            logger.info(f"准备调用 current 获取行情快照, symbol_count={len(gm_symbols)}")
            current_ticks = self._call_bridge(CurrentRequest(symbols=gm_symbols), timeout=10.0)
            logger.info(
                f"current 调用完成, symbol_count={len(gm_symbols)}, tick_count={len(current_ticks) if current_ticks else 0}"
            )

            if not current_ticks:
                logger.warning("实时行情数据为空或格式异常")
                return market_data

            for tick in current_ticks:
                tick_dict: dict[str, Any] = dict(tick)  # pyright: ignore
                try:
                    dt_str = tick_dict["created_at"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                    timestamp_ms = int(tick_dict["created_at"].timestamp() * 1000)

                    quotes: list[dict[str, Any]] = tick_dict.get("quotes", [])
                    bid_price = [q.get("bid_p", 0.0) for q in quotes if q.get("bid_p", 0.0) > 0]
                    ask_price = [q.get("ask_p", 0.0) for q in quotes if q.get("ask_p", 0.0) > 0]
                    bid_volume = [q.get("bid_v", 0) for q in quotes if q.get("bid_p", 0.0) > 0]
                    ask_volume = [q.get("ask_v", 0) for q in quotes if q.get("ask_p", 0.0) > 0]

                    gm_tick = {
                        "symbol": tick_dict.get("symbol", "UNKNOWN"),
                        "dt": dt_str,
                        "timestamp": timestamp_ms,
                        "price": tick_dict["price"],
                        "volume": tick_dict["last_volume"],
                        "bid_price": bid_price,
                        "ask_price": ask_price,
                        "bid_volume": bid_volume,
                        "ask_volume": ask_volume,
                    }

                    unified_price = from_gm_price(gm_tick, TradeChannel.GM)
                    market_data[unified_price.symbol] = unified_price
                    logger.success(f"{unified_price.symbol} 的快照数据获取成功")

                except Exception as e_inner:
                    logger.exception(f"处理 {tick_dict.get('symbol', 'UNKNOWN')} 的快照数据时发生异常: {e_inner}")

        except Exception as e_outer:
            logger.exception(f"获取实时快照数据时发生异常: {e_outer}")

        return market_data

    def _query_unfinished_order_records(self, account_id: str | None = None) -> list[UnifiedOrder]:
        """获取账户级 unfinished-order 统一订单快照，不处理账户控制."""
        query_account_id = account_id or self.account_id
        if not query_account_id:
            return []
        return [
            convert_gm_order_to_unified(order)
            for order in cast(
                "list[dict[str, Any]]",
                self._call_bridge(GetUnfinishedOrdersRequest(account_id=self.account_id), timeout=10.0),
            )
            if order.get("account_id") == query_account_id
        ]

    def _query_trade_records(self, account_id: str | None = None) -> list[TradeRecord]:
        """获取账户级 execution reports 统一成交快照，不处理账户控制."""
        query_account_id = account_id or self.account_id
        if not query_account_id:
            return []
        return [
            convert_gm_trade_to_trade_record(trade)
            for trade in cast(
                "list[dict[str, Any]]",
                self._call_bridge(GetExecutionReportsRequest(account_id=self.account_id), timeout=10.0),
            )
            if trade.get("account_id") == query_account_id
        ]

    def _query_order_records(self, account_id: str | None = None) -> list[UnifiedOrder]:
        """获取账户级 all-orders 统一订单快照，不处理账户控制."""
        query_account_id = account_id or self.account_id
        if not query_account_id:
            return []
        return [
            convert_gm_order_to_unified(order)
            for order in cast(
                "list[dict[str, Any]]",
                self._call_bridge(GetOrdersRequest(account_id=self.account_id), timeout=10.0),
            )
            if order.get("account_id") == query_account_id
        ]

    def _submit_cancel_request(
        self,
        *,
        wait_cancel_orders: list[GMCancelOrderTarget],
    ) -> None:
        """执行纯 GM 撤单请求，不处理账户控制."""
        self._call_bridge(OrderCancelRequest(wait_cancel_orders=wait_cancel_orders), timeout=10.0)

    def _cancel_order_impl(self, symbol: str, order_id: str) -> bool:
        """
        撤销指定订单.

        Parameters
        ----------
        symbol : str
            品种代码。
        order_id : str
            待撤销订单的 ID，对应 ``cl_ord_id``。

        Returns
        -------
        bool
            撤销请求提交成功时返回 ``True``，否则返回 ``False``。
        """
        symbol = self._normalize_symbol(symbol)
        if not self.account_id:
            logger.warning("无法撤销订单：账户ID未设置")
            return False

        try:
            self._submit_cancel_request(
                wait_cancel_orders=[GMCancelOrderTarget(cl_ord_id=order_id, account_id=self.account_id)],
            )
            logger.info(f"撤销订单请求已提交: {symbol} 订单ID {order_id}")
            return True
        except Exception as e:
            logger.error(f"撤销订单失败: {symbol} 订单ID {order_id}, 错误: {e}")
            return False

    @override
    def _query_trades_impl(self, symbol: str, order_id: str) -> list[TradeRecord]:
        """获取指定订单的成交明细."""
        symbol = self._normalize_symbol(symbol)
        return [
            trade
            for trade in self._query_trade_records()
            if trade.symbol == symbol
            and order_id
            in {
                trade.order_id,
                str(trade.extra.get("cl_ord_id") or ""),
                str(trade.extra.get("exchange_order_id") or ""),
            }
        ]

    @override
    def _get_execution_pending_orders_snapshot_fetcher(self):
        """返回 Execution 内部共享的账户级 unfinished-orders snapshot."""
        return lambda: self._run_execution_shared_fetch(
            operation="query_order",
            shared_query_key=("pending_orders_snapshot",),
            query_scope="snapshot",
            fetcher=self._query_unfinished_order_records,
        )

    @override
    def _get_execution_trades_snapshot_fetcher(self):
        """返回 execution 内部共享的账户级成交原始快照 fetcher."""
        return lambda: self._run_execution_shared_fetch(
            operation="query_trades",
            shared_query_key=("trades_snapshot",),
            query_scope="snapshot",
            fetcher=self._fetch_trade_records_snapshot_for_execution,
        )

    def _fetch_trade_records_snapshot_for_execution(self) -> list[TradeRecord]:
        """将账户级 execution reports 转成统一成交快照，供 execution runtime 过滤复用."""
        return self._query_trade_records()

    def _register_execution_query_runtime_callback_observers(self) -> None:
        """注册 execution 共享查询失效观察者."""
        if getattr(self, "_execution_query_runtime_observers_registered", False):
            return
        bridge = self.get_execution_query_runtime_bridge()
        self._callback_dispatcher.register_order_observer(bridge.observe_order_update)
        self._callback_dispatcher.register_trade_observer(bridge.observe_trade_record)
        self._execution_query_runtime_observers_registered = True

    def _place_order_impl(
        self,
        symbol: str,
        direction: OrderDirection,
        order_type: OrderType,
        volume: float,
        price: float = 0,
        **_kwargs: object,
    ) -> UnifiedOrder:
        """下单，返回统一订单模型."""
        symbol = self._normalize_symbol(symbol)
        if not self.account_id:
            raise ValueError("账户ID未初始化")

        side = GMOrderSide.BUY if direction == OrderDirection.BUY else GMOrderSide.SELL
        position_effect = GMPositionEffect.OPEN if direction == OrderDirection.BUY else GMPositionEffect.CLOSE
        gm_order_type = GMOrderKind.LIMIT if order_type == OrderType.LIMIT else GMOrderKind.MARKET
        order_price = price if order_type == OrderType.LIMIT and price > 0 else 0
        gm_volume = int(volume)
        if side is GMOrderSide.SELL and position_effect is GMPositionEffect.CLOSE:
            self._ensure_gm_sell_close_supported(symbol=symbol, requested_volume=gm_volume)

        logger.info(
            f"准备下单: symbol={symbol}, volume={volume}, "
            f"side={'买开' if side is GMOrderSide.BUY else '卖平'}, order_type={order_type}, 价格={order_price}"
        )

        try:
            order_result = self._call_bridge(
                OrderVolumeRequest(
                    symbol=GM_SYMBOL_RESOLVER.to_gm(symbol),
                    volume=gm_volume,
                    side=side,
                    order_type=gm_order_type,
                    position_effect=position_effect,
                    price=order_price,
                    account=self.account_id,
                ),
                timeout=10.0,
            )
        except Exception:
            raise

        provisional_order = self._build_provisional_order_from_submit_result(
            result=order_result,
            symbol=symbol,
            direction=direction,
            order_type=order_type,
            volume=volume,
            price=order_price,
        )
        logger.info(
            f"委托已提交，返回临时订单视图: "
            f"order_id={provisional_order.order_id}, symbol={symbol}, status={provisional_order.status}"
        )
        return provisional_order

    def _ensure_gm_sell_close_supported(self, symbol: str, requested_volume: int) -> None:
        """校验 GM A 股卖平单不会越过当前可平多头数量."""
        if not self.account_id or requested_volume <= 0 or not _is_gm_ashare_symbol(symbol):
            return

        available_long_volume = self._get_available_long_volume(symbol)
        if available_long_volume + 1e-9 >= requested_volume:
            return

        raise ValueError(
            "GM A股账户不支持空仓卖平或超出可平数量: "
            f"symbol={symbol}, requested={requested_volume}, available={available_long_volume}"
        )

    def _get_available_long_volume(self, symbol: str) -> float:
        """获取指定标的当前可平的多头数量."""
        symbol = self._normalize_symbol(symbol)
        if not self.account_id:
            return 0.0

        positions = cast(
            "list[Any]",
            self._call_bridge(GetPositionRequest(account_id=self.account_id), timeout=10.0),
        )
        if not positions:
            return 0.0
        available_long_volume = 0.0
        for pos in positions:  # pyright: ignore[reportUnknownVariableType]
            pos_dict = cast("dict[str, Any]", pos)
            if GM_SYMBOL_RESOLVER.to_axile(str(pos_dict.get("symbol", ""))) != symbol:
                continue
            if int(pos_dict.get("side", 1)) != 1:
                continue
            available_long_volume += _gm_available_volume(pos_dict)
        return available_long_volume

    def reconcile_terminal_order(self, symbol: str, order_id: str) -> UnifiedOrder | None:
        """按完整订单身份核实实际终态，不以挂单列表缺失代替撤单回报。"""
        return next(
            (
                order
                for order in self._query_order_records()
                if order.order_id == order_id and order.symbol == symbol and order.is_completed()
            ),
            None,
        )

    def _get_pending_orders_impl(self, symbol: str | None = None) -> list[UnifiedOrder]:
        """获取未完成订单."""
        if symbol is not None:
            symbol = self._normalize_symbol(symbol)
        return [order for order in self._query_unfinished_order_records() if symbol is None or order.symbol == symbol]

    @override
    def get_tick_size(self, symbol: str) -> float | None:
        """获取品种的最小价格变动单位."""
        try:
            symbol = self._normalize_symbol(symbol)
            code_parts = symbol.split(".")
            code = code_parts[0] if code_parts else symbol

            is_stock_market = any(suffix in symbol.upper() for suffix in [".SH", ".SZ", ".SHSE", ".SZSE"])

            if is_stock_market:
                if code.startswith("5") or code.startswith("51"):
                    return 0.001
                if code.startswith("12"):
                    return 0.001
                return 0.01

            return None

        except Exception as e:
            self.logger.warning(f"获取 {symbol} tick_size 失败: {e}")
            return None

    def _cleanup(self) -> None:
        """清理资源."""
        self.stop_callback_monitoring()

    # ==================== 辅助方法 ====================

    @override
    def _get_account_mark(self) -> str:
        """获取GM账户标识."""
        if self.account_id:
            return self.account_id
        if isinstance(self.account_config, GMAccountConfig):
            return self.account_config.account_id
        raise ValueError("账户配置未初始化")

    def _get_operation_display(self, order: UnifiedOrder) -> str:
        """获取GM的操作类型显示."""
        position_effect = order.extra.get("position_effect", "")
        position_side = order.extra.get("position_side", "")

        if position_effect and position_side:
            if position_effect == "PositionEffect_Open":
                if position_side == "PositionSide_Long":
                    return "开多"
                else:
                    return "开空"
            elif position_effect == "PositionEffect_Close":
                if position_side == "PositionSide_Long":
                    return "平多"
                else:
                    return "平空"

        return "买入" if order.direction == OrderDirection.BUY else "卖出"

    def _start_gm_desktop_if_not(self, base_dir: str) -> bool:
        """如果掘金桌面端未启动，则启动它."""
        phase = "读取终端端口配置"
        gm_port = None
        try:
            base_path = Path(base_dir)
            gm_port = self._get_rpc_port_from_dir(base_path)

            if self._wait_for_port_listen(gm_port, 0.1):
                logger.success("掘金桌面端已经启动!")
                return False

            logger.info("启动掘金桌面端..")
            phase = "启动终端进程"
            startupinfo = None
            creationflags = 0
            if sys.platform == "win32":
                creationflags = subprocess.CREATE_NO_WINDOW
            _ = subprocess.Popen(
                [base_path / "goldminer3.exe"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                startupinfo=startupinfo,
            )

            phase = "等待终端端口监听"
            if self._wait_for_port_listen(gm_port, 120):
                logger.success("掘金桌面端成功启动!")
                return True
            else:
                error = ValueError("掘金终端等待超时")
                report_gm_failure(
                    connection_monitor(self),
                    error,
                    "终端启动超时",
                    "终端启动后，未在等待期限内观察到端口监听。",
                    facts={"运行模式": "terminal", "失败阶段": phase, "端口": gm_port},
                    facts_title="终端启动阶段",
                    steps_title="启动检查",
                    steps=("检查掘金终端是否完成启动。", "检查终端端口配置及端口占用情况。"),
                )
                raise error

        except Exception as e:
            report_gm_failure(
                connection_monitor(self),
                e,
                "终端启动失败",
                "本机终端初始化过程失败，尚未确认交易账户已登录。",
                facts={"运行模式": "terminal", "失败阶段": phase, "终端目录": base_dir, "端口": gm_port},
                facts_title="终端启动阶段",
                steps_title="启动检查",
                steps=("检查终端目录、gmserv.json 端口配置和终端程序。", "确认本机允许启动终端并监听配置端口。"),
            )
            logger.exception(f"启动掘金终端失败: {e}")
            raise

    def _get_rpc_port_from_dir(self, base_path: Path) -> int:
        """从指定目录下的 gmserv.json 文件中提取 rpcPort 端口号."""
        json_path = base_path / "resources" / "app" / "gmserv.json"

        logger.info(f"开始查找 gmserv.json 文件：{json_path}")

        if not json_path.is_file():
            logger.error(f"未找到 gmserv.json 文件，路径无效：{json_path}")
            raise FileNotFoundError(f"gmserv.json 文件不存在: {json_path}")

        try:
            with json_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            logger.debug(f"成功加载 JSON 数据: {data}")
            port_str = data["default"]["rpcPort"]
            port = int(port_str)
            logger.success(f"成功解析 掘金桌面端端口: {port}")
            return port
        except json.JSONDecodeError as e:
            logger.exception(f"JSON 文件格式错误，无法解析: {e}")
            raise
        except KeyError as e:
            logger.exception(f"JSON 中缺少必要的字段: {e}")
            raise
        except ValueError as e:
            logger.exception(f"rpcPort 不是有效的整数: {e}")
            raise

    def _wait_for_port_listen(self, port: int, timeout: int | float, log_interval: int = 5) -> bool:
        """在 Windows 上等待某端口进入监听状态."""
        logger.info(f"正在等待端口 {port} 进入监听状态（最长等待 {timeout} 秒）...")
        deadline = time.time() + timeout
        last_log_time = 0

        while time.time() < deadline:
            for conn in psutil.net_connections(kind="inet"):
                if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port:  # type: ignore
                    logger.success(f"端口 {port} 已进入监听状态。")
                    return True

            now = time.time()
            if now - last_log_time >= log_interval:
                logger.info(f"仍在等待端口 {port} 进入监听状态...")
                last_log_time = now

            time.sleep(0.2)

        logger.error(f"超时未检测到端口 {port} 监听。")
        return False

    def _build_provisional_order_from_submit_result(
        self,
        *,
        result: list[dict[str, Any]],
        symbol: str,
        direction: OrderDirection,
        order_type: OrderType,
        volume: float,
        price: float,
    ) -> UnifiedOrder:
        """根据下单返回的 cl_ord_id 构建可追踪的临时订单视图."""
        if not result or "cl_ord_id" not in result[0]:
            raise ValueError("下单结果缺少 cl_ord_id，无法构建临时订单")

        cl_ord_id = str(result[0]["cl_ord_id"])
        provisional_order = UnifiedOrder.create(
            order_id=cl_ord_id,
            symbol=symbol,
            direction=direction.value,
            order_type=order_type.value,
            volume=float(volume),
            price=float(price),
            channel_type=TradeChannel.GM,
            status=OrderStatus.SUBMITTED,
            raw_order_data={"submit_result": result},
        )
        provisional_order.extra.update(
            {
                "cl_ord_id": cl_ord_id,
                "exchange_order_id": None,
                "gm_symbol": GM_SYMBOL_RESOLVER.to_gm(symbol),
            }
        )
        return provisional_order

    def _cancel_timeout_orders(self, account_id: str, timeout: int | float) -> None:
        """撤销指定账户中超时未完成的委托订单."""
        try:
            logger.info(f"开始检查账户 [{account_id}] 是否存在超时委托，超时时间设定为 {timeout} 秒")
            unfinished_orders = self._query_unfinished_order_records(account_id)
            now = datetime.now(timezone.utc).astimezone()

            to_cancel: list[GMCancelOrderTarget] = []

            for order in unfinished_orders:
                if order.extra.get("account_id") != account_id:
                    continue

                created_at_raw = order.create_time
                if not created_at_raw:
                    logger.warning(f"委托 {order.order_id} 缺少 created_at 字段，跳过处理")
                    continue

                created_at = datetime.fromisoformat(created_at_raw)
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=now.tzinfo)

                elapsed_time = (now - created_at).total_seconds()
                if elapsed_time > timeout:
                    logger.info(
                        f"委托超时，准备撤销：cl_ord_id={order.extra.get('cl_ord_id')}，"
                        f"symbol={order.symbol}，委托时间={created_at}，"
                        f"已等待 {elapsed_time:.2f} 秒"
                    )
                    to_cancel.append(
                        GMCancelOrderTarget(
                            cl_ord_id=str(order.extra["cl_ord_id"]),
                            account_id=str(order.extra["account_id"]),
                        )
                    )

            if to_cancel:
                self._submit_cancel_request(wait_cancel_orders=to_cancel)
                logger.success(f"成功提交撤销请求，共 {len(to_cancel)} 个超时委托已处理")
            else:
                logger.info("没有发现任何超时的委托，无需撤销")

        except Exception as e:
            logger.exception(f"处理超时撤单时发生异常: {e}")

    def _filter_recent_orders(self, within_seconds: int, account_id: str) -> list[UnifiedOrder]:
        """过滤指定 account_id 的订单，并返回在 within_seconds 秒内创建的订单."""
        now = datetime.now(timezone(timedelta(hours=8), "Asia/Shanghai"))
        return [
            order
            for order in self._query_order_records(account_id)
            if order.create_time is not None
            and (
                now
                - (
                    datetime.fromisoformat(order.create_time).replace(tzinfo=now.tzinfo)
                    if datetime.fromisoformat(order.create_time).tzinfo is None
                    else datetime.fromisoformat(order.create_time)
                )
            ).total_seconds()
            <= within_seconds
        ]

    # ==================== 重写父类方法 ====================

    def _calculate_generic_sizing(
        self,
        weight: float,
        price: float,
        account_assets: UnifiedAccountAssets,
        trade_rule: dict[str, Any],
        *,
        symbol: str | None = None,
    ) -> TargetSizingDecision:
        """按股票整手规则生成目标股数及换算证据."""
        total_asset = account_assets.total_asset
        raw_lot_size = trade_rule.get("一手数量", 100)
        lot_size = float(raw_lot_size) if isinstance(raw_lot_size, int | float) else 100.0
        raw_quantity = total_asset * weight / price
        target_quantity = math.floor(raw_quantity / lot_size) * lot_size
        reason_code = "COMMON.SIZING.EXACT"
        if raw_quantity != 0 and target_quantity == 0:
            reason_code = "COMMON.SIZING.BELOW_MIN_QUANTITY"
        elif abs(raw_quantity - target_quantity) > 1e-12:
            reason_code = "COMMON.SIZING.QUANTIZED"
        return TargetSizingDecision(
            symbol=symbol or "",
            account_weight=weight,
            equity=total_asset,
            reference_price=price,
            unit_multiplier=1.0,
            unit_notional=price,
            target_notional=abs(total_asset * weight),
            raw_quantity=raw_quantity,
            target_quantity=float(target_quantity),
            quantity_step=lot_size,
            min_quantity=lot_size,
            reason_code=reason_code,
        )

    def _get_default_trade_rules_for_empty(self, _symbols: list[str]) -> dict[str, Any]:
        """获取清仓时的默认交易规则."""
        return {}
