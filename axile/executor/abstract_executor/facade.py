"""`AbstractExecutor` 的公开 facade 默认实现.

本文件承载公开下单/查单/撤单接口、symbol-scoped callback 包装，
以及 `empty_positions()` 这类面向调用方的薄入口逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from axile.executor.abstract_executor.support import _coerce_object_dict, _coerce_trade_rules
from axile.executor.account_control.decorators import controlled_operation
from axile.executor.models.execution_result import ExecutionOutcome, ExecutionStatus
from axile.executor.models.unified_account_assets import Position, UnifiedAccountAssets
from axile.executor.models.unified_callback import OrderUpdateCallback, PriceDataCallback, TradeRecordCallback
from axile.executor.models.unified_input import DEFAULT_EXECUTION_TIMEOUT_SECONDS, UnifiedStandardInput
from axile.executor.models.unified_input_support import as_timeout_int
from axile.executor.models.unified_order import OrderDirection, OrderType, TradeRecord, UnifiedOrder
from axile.executor.models.unified_output import UnifiedStandardOutput
from axile.executor.order_input_validation import normalize_order_enums

if TYPE_CHECKING:
    from axile.executor.abstract_executor.base import AbstractExecutor

DEFAULT_CLEAR_TIMEOUT = 10 * DEFAULT_EXECUTION_TIMEOUT_SECONDS
"""清仓路径的默认总超时秒数（调用方未显式指定时使用）。.

Notes
-----
刻意比调仓的默认额度宽得多，故按 :data:`DEFAULT_EXECUTION_TIMEOUT_SECONDS` 的倍数表达：
两者是同一个概念的两档取值，写成独立字面量会让这个 10 倍差在各自的定义处都看不见。

清仓是应急去风险动作，**跑久了不如被截断一半糟糕**：
中途终止会把仓位留在账上，而这正是清仓要消除的东西。而串行下单的渠道（CTP / GM
均不支持按品种并行）一次清仓的总时长约为 ``品种数 × max_wait_seconds``，用调仓那档
额度会让稍多品种的清仓稳定半途而废。这里仍保留一个上限，只是把它放在「明显异常」而非
「业务节奏」的量级上。
"""


def _executor(owner: object) -> AbstractExecutor:
    return cast("AbstractExecutor", owner)


@dataclass(frozen=True)
class _EmptyPositionsPlan:
    """清仓 facade 在执行前整理出的标准输入快照."""

    curr_target: dict[str, float]
    last_target: dict[str, float]
    trade_rules: dict[str, dict[str, object]]
    futures_close_policy: dict[str, object]
    algorithm: dict[str, object]
    forbidden_symbols: list[str]
    feishu_key: str | None
    execution_notification_code: str | None
    feishu_account: dict[str, object]
    extra: dict[str, object]
    execution_timeout: int


class AbstractExecutorFacadeMixin:
    """承载 AbstractExecutor 的公开 facade 默认实现."""

    @controlled_operation(
        "place_order",
        symbol_arg="symbol",
        success_outcome="submitted",
        result_metadata_resolver=lambda order: {"order_id": order.order_id},
    )
    def place_order(
        self,
        symbol: str,
        direction: OrderDirection,
        order_type: OrderType,
        volume: float,
        price: float = 0,
        **kwargs: object,
    ) -> UnifiedOrder:
        """
        下单并同步更新 execution 查询快照.

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
            透传给底层下单实现的扩展字段。

        Returns
        -------
        UnifiedOrder
            下单后返回的统一订单对象。
        """
        executor = _executor(self)
        normalized_symbol = executor._normalize_symbol(symbol)
        try:
            direction, order_type = normalize_order_enums(direction, order_type)
            order = executor._place_order_impl(normalized_symbol, direction, order_type, volume, price, **kwargs)
        except Exception as exc:
            executor.account_orders.activity.record_failure(
                "place_order",
                normalized_symbol,
                None,
                getattr(exc, "execution_error", None) or "下单未确认成功，请查看执行详情；未据此认定订单已结束",
            )
            raise
        if executor.account_orders.apply(order):
            executor.get_execution_query_runtime_bridge().handle_place_order_result(
                order, fallback_symbol=normalized_symbol
            )
        return order

    @controlled_operation(
        "query_order",
        symbol_arg="symbol",
        success_outcome="fetched",
    )
    def get_pending_orders(self, symbol: str | None = None) -> list[UnifiedOrder]:
        """
        获取未完成订单.

        Parameters
        ----------
        symbol : str | None, default=None
            需要查询的品种代码；为空时查询全部未完成订单。

        Returns
        -------
        list[UnifiedOrder]
            当前仍未完成的订单列表。
        """
        executor = _executor(self)
        normalized_symbol = None if symbol is None else executor._normalize_symbol(symbol)
        watermark = executor.account_orders.begin_sync()
        orders = executor._get_pending_orders_impl(normalized_symbol)
        if normalized_symbol is None:
            executor.account_orders.replace(orders, watermark)
        return orders

    @controlled_operation(
        "query_trades",
        symbol_arg="symbol",
        metadata_resolver=lambda bound_arguments: {"order_id": str(bound_arguments.arguments["order_id"])},
        success_outcome="fetched",
    )
    def query_trades(self, symbol: str, order_id: str) -> list[TradeRecord]:
        """
        获取指定订单的成交明细.

        Parameters
        ----------
        symbol : str
            品种代码。
        order_id : str
            订单标识。

        Returns
        -------
        list[TradeRecord]
            指定订单关联的成交明细列表。
        """
        executor = _executor(self)
        return executor._query_trades_impl(executor._normalize_symbol(symbol), order_id)

    def cancel_all_orders(self) -> None:
        """
        撤销当前账户全部未完成订单.

        Raises
        ------
        RuntimeError
            当存在订单撤销失败时抛出。
        """
        executor = _executor(self)
        failed_order_ids: list[str] = []

        for order in executor.get_pending_orders(None):
            try:
                canceled = executor.cancel_order(order.symbol, order.order_id)
            except Exception as exc:
                if bool(getattr(exc, "requires_session_recovery", False)):
                    raise
                failed_order_ids.append(order.order_id)
                continue

            if not canceled:
                failed_order_ids.append(order.order_id)

        if failed_order_ids:
            raise RuntimeError(f"部分订单撤销失败: {'; '.join(failed_order_ids)}")

    def handle_termination_checkpoint(self, symbol: str | None = None) -> None:
        """
        在执行检查点响应 terminate 请求.

        Parameters
        ----------
        symbol : str | None, default=None
            当前执行检查点对应的品种代码。
        """
        executor = _executor(self)
        runtime = executor.get_active_execution_runtime()
        if runtime is not None:
            runtime.handle_termination_checkpoint(symbol)
            return

        executor.require_execution_runtime().handle_termination_checkpoint(symbol)

    def register_order_callback_for_symbol(
        self,
        callback: OrderUpdateCallback,
        symbol: str,
    ) -> None:
        """
        按品种注册订单回调.

        Parameters
        ----------
        callback : OrderUpdateCallback
            订单回调函数。
        symbol : str
            目标品种代码。

        Notes
        -----
        默认实现会回退到全局注册。
        """
        _ = symbol
        _executor(self).register_order_callback(callback)

    def register_price_callback_for_symbol(
        self,
        callback: PriceDataCallback,
        symbol: str,
    ) -> None:
        """
        按品种注册价格回调.

        Parameters
        ----------
        callback : PriceDataCallback
            价格回调函数。
        symbol : str
            目标品种代码。

        Notes
        -----
        默认实现会回退到全局注册。
        """
        _ = symbol
        _executor(self).register_price_callback(callback)

    def unregister_order_callback_for_symbol(
        self,
        callback: OrderUpdateCallback,
        symbol: str,
    ) -> None:
        """
        按品种注销订单回调.

        Parameters
        ----------
        callback : OrderUpdateCallback
            订单回调函数。
        symbol : str
            目标品种代码。

        Notes
        -----
        默认实现会回退到全局注销。
        """
        _ = symbol
        _executor(self).unregister_order_callback(callback)

    def unregister_price_callback_for_symbol(
        self,
        callback: PriceDataCallback,
        symbol: str,
    ) -> None:
        """
        按品种注销价格回调.

        Parameters
        ----------
        callback : PriceDataCallback
            价格回调函数。
        symbol : str
            目标品种代码。

        Notes
        -----
        默认实现会回退到全局注销。
        """
        _ = symbol
        _executor(self).unregister_price_callback(callback)

    def register_trade_callback_for_symbol(
        self,
        callback: TradeRecordCallback,
        symbol: str,
    ) -> None:
        """
        按品种注册成交回调.

        Parameters
        ----------
        callback : TradeRecordCallback
            成交回调函数。
        symbol : str
            目标品种代码。

        Notes
        -----
        默认实现会回退到全局注册。
        """
        _ = symbol
        register_trade_callback = getattr(_executor(self), "register_trade_callback", None)
        if callable(register_trade_callback):
            register_trade_callback(callback)

    def unregister_trade_callback_for_symbol(
        self,
        callback: TradeRecordCallback,
        symbol: str,
    ) -> None:
        """
        按品种注销成交回调.

        Parameters
        ----------
        callback : TradeRecordCallback
            成交回调函数。
        symbol : str
            目标品种代码。

        Notes
        -----
        默认实现会回退到全局注销。
        """
        _ = symbol
        unregister_trade_callback = getattr(_executor(self), "unregister_trade_callback", None)
        if callable(unregister_trade_callback):
            unregister_trade_callback(callback)

    def get_tick_size(self, symbol: str) -> float | None:
        """
        获取交易品种的最小价格变动单位.

        Parameters
        ----------
        symbol : str
            品种代码。

        Returns
        -------
        float | None
            最小价格变动单位；默认实现未知时返回 ``None``。
        """
        _ = symbol
        return None

    def get_min_notional(self, symbol: str) -> float | None:
        """
        获取交易品种下单的最小名义价值.

        Parameters
        ----------
        symbol : str
            品种代码。

        Returns
        -------
        float | None
            最小名义价值（单位为计价货币）；默认实现未知时返回 ``None``。
        """
        _ = symbol
        return None

    @controlled_operation(
        "cancel_order",
        symbol_arg="symbol",
        metadata_resolver=lambda bound_arguments: {"order_id": str(bound_arguments.arguments["order_id"])},
        success_outcome="submitted",
    )
    def cancel_order(self, symbol: str, order_id: str) -> bool:
        """
        撤销指定订单.

        Parameters
        ----------
        symbol : str
            品种代码。
        order_id : str
            需要撤销的订单标识。

        Returns
        -------
        bool
            撤单提交成功时返回 ``True``。
        """
        executor = _executor(self)
        normalized_symbol = executor._normalize_symbol(symbol)
        try:
            canceled = executor._cancel_order_impl(normalized_symbol, order_id)
        except Exception as exc:
            executor.account_orders.activity.record_failure(
                "cancel_order",
                normalized_symbol,
                order_id,
                getattr(exc, "execution_error", None) or "撤单未确认成功，请核对订单状态",
            )
            raise
        if not canceled:
            detail = getattr(executor, "get_cancel_failure_detail", lambda *_: {})(normalized_symbol, order_id)
            executor.account_orders.activity.record_failure(
                "cancel_order",
                normalized_symbol,
                order_id,
                detail.get("message") or "撤单未提交成功或结果待确认",
                outcome=detail.get("outcome", "unknown"),
            )
        if canceled:
            executor.get_execution_query_runtime_bridge().handle_cancel_order_result(normalized_symbol, order_id)
        return canceled

    @controlled_operation("query_order", symbol_arg="symbol", success_outcome="fetched")
    def query_recent_orders(self, symbol: str | None = None) -> list[UnifiedOrder]:
        """读取渠道可查询的近期委托，具体时间范围由渠道声明。"""
        return self._query_recent_orders_impl(symbol)

    def _query_recent_orders_impl(self, symbol: str | None = None) -> list[UnifiedOrder]:
        """外部渠道未实现历史查询时明确报告能力缺失。"""
        raise NotImplementedError("渠道尚未支持近期委托查询")

    @controlled_operation("query_trades", symbol_arg="symbol", success_outcome="fetched")
    def query_recent_trades(self, symbol: str | None = None) -> list[TradeRecord]:
        """读取账户近期逐笔成交，不按某一次 execution 过滤。"""
        return self._query_recent_trades_impl(symbol)

    def _query_recent_trades_impl(self, symbol: str | None = None) -> list[TradeRecord]:
        """外部渠道未实现历史查询时明确报告能力缺失。"""
        raise NotImplementedError("渠道尚未支持近期成交查询")

    def _get_operation_display(self, order: UnifiedOrder) -> str:
        """
        返回默认的订单操作显示文本.

        Parameters
        ----------
        order : UnifiedOrder
            需要展示的订单对象。

        Returns
        -------
        str
            默认的买卖方向展示文本。
        """
        return "买入" if order.direction == OrderDirection.BUY else "卖出"

    def empty_positions(
        self,
        cleanup: bool = True,
        retain_runtime: bool = False,
        **kwargs: object,
    ) -> UnifiedStandardOutput:
        """
        执行统一清仓流程.

        Parameters
        ----------
        cleanup : bool, default=True
            执行完成后是否清理资源。
        retain_runtime : bool, default=False
            执行完成后是否保留当前 runtime。
        **kwargs : object
            透传给清仓流程的扩展配置，例如算法、交易规则和通知配置。

        Returns
        -------
        UnifiedStandardOutput
            清仓流程返回的统一输出对象。

        Raises
        ------
        ValueError
            当账户配置缺失时抛出。
        """
        executor = _executor(self)
        notification_snapshot = kwargs.pop("notification_snapshot", None)
        if not isinstance(notification_snapshot, dict):
            notification_snapshot = None
        account_assets = executor.get_account_assets()
        curr_target = self._build_empty_positions_curr_target(account_assets.positions)
        if not curr_target:
            return self._create_empty_positions_noop_output(account_assets)

        account_config = executor.account_config
        if not account_config:
            raise ValueError("账户配置不能为空，请确保在初始化时传入account_config")

        plan = self._plan_empty_positions(
            positions=account_assets.positions,
            curr_target=curr_target,
            kwargs=kwargs,
        )
        standard_input = self._build_empty_positions_standard_input(
            account_config=account_config,
            plan=plan,
        )

        result = executor.execute(
            standard_input,
            cleanup=cleanup,
            retain_runtime=retain_runtime,
            **({"notification_snapshot": notification_snapshot} if notification_snapshot else {}),
        )
        return result

    def _plan_empty_positions(
        self,
        positions: list[Position],
        curr_target: dict[str, float],
        kwargs: dict[str, object],
    ) -> _EmptyPositionsPlan:
        """
        解析清仓流程需要的目标、规则与通知配置.

        Parameters
        ----------
        positions : list[Position]
            当前账户持仓列表。
        curr_target : dict[str, float]
            已根据当前可用持仓整理好的清仓目标。
        kwargs : dict[str, object]
            调用方传入的额外清仓配置。

        Returns
        -------
        _EmptyPositionsPlan
            构造标准输入前整理好的清仓计划。
        """
        executor = _executor(self)
        default_rules = executor._get_default_trade_rules_for_empty(list(curr_target))
        user_rules = _coerce_trade_rules(kwargs.get("trade_rules", {}))
        forbidden_symbols_raw = kwargs.get("forbidden_symbols", [])
        forbidden_symbols = (
            [symbol for symbol in forbidden_symbols_raw if isinstance(symbol, str)]
            if isinstance(forbidden_symbols_raw, list)
            else []
        )
        feishu_key_raw = kwargs.get("feishu_key")
        notification_code_raw = kwargs.get("execution_notification_code")
        timeout_raw = kwargs.get("execution_timeout")
        return _EmptyPositionsPlan(
            curr_target=curr_target,
            last_target=executor._calculate_last_target_unified(positions),
            trade_rules={**default_rules, **user_rules},
            futures_close_policy=_coerce_object_dict(kwargs.get("futures_close_policy", {})),
            algorithm=_coerce_object_dict(kwargs.get("algorithm", executor._get_default_algorithm())),
            forbidden_symbols=forbidden_symbols,
            feishu_key=feishu_key_raw if isinstance(feishu_key_raw, str) else None,
            execution_notification_code=notification_code_raw if isinstance(notification_code_raw, str) else None,
            feishu_account=_coerce_object_dict(kwargs.get("feishu_account", {})),
            extra=_coerce_object_dict(kwargs.get("extra", {})),
            execution_timeout=as_timeout_int(timeout_raw, DEFAULT_CLEAR_TIMEOUT),
        )

    def _build_empty_positions_standard_input(
        self,
        *,
        account_config: object,
        plan: _EmptyPositionsPlan,
    ) -> UnifiedStandardInput:
        """
        根据清仓计划构造标准输入对象.

        Parameters
        ----------
        account_config : object
            当前执行器持有的账户配置对象。
        plan : _EmptyPositionsPlan
            已整理好的清仓计划。

        Returns
        -------
        UnifiedStandardInput
            传给统一执行引擎的清仓标准输入。
        """
        executor = _executor(self)
        return UnifiedStandardInput(
            channel_type=executor.channel_type,
            account_config=account_config,
            algorithm=plan.algorithm,
            trade_rules=plan.trade_rules,
            futures_close_policy=plan.futures_close_policy,
            forbidden_symbols=plan.forbidden_symbols,
            risk_symbols=[],
            curr_target=plan.curr_target,
            last_target=plan.last_target,
            feishu_key=plan.feishu_key,
            execution_notification_code=plan.execution_notification_code,
            feishu_account=plan.feishu_account,
            extra=plan.extra,
            execution_timeout=plan.execution_timeout,
        )

    def _build_empty_positions_curr_target(self, positions: list[Position]) -> dict[str, float]:
        """
        根据当前可用持仓构造清仓目标.

        Parameters
        ----------
        positions : list[Position]
            当前持仓列表。

        Returns
        -------
        dict[str, float]
            以 ``0`` 为目标权重的清仓目标映射。
        """
        curr_target: dict[str, float] = {}
        for position in positions:
            if position.volume > 0 and position.available_volume > 0:
                curr_target[position.symbol] = 0.0
        return curr_target

    def _create_empty_positions_noop_output(self, account_assets: UnifiedAccountAssets) -> UnifiedStandardOutput:
        """
        在当前无持仓时返回 noop 输出.

        Parameters
        ----------
        account_assets : UnifiedAccountAssets
            当前账户资产快照。

        Returns
        -------
        UnifiedStandardOutput
            表示无需清仓的统一输出对象。
        """
        executor = _executor(self)
        account_config = executor.account_config
        empty_input = (
            UnifiedStandardInput(
                channel_type=executor.channel_type,
                account_config=account_config,
                curr_target={},
                last_target={},
                feishu_key=None,
            )
            if account_config
            else None
        )
        return UnifiedStandardOutput(
            account_assets=account_assets,
            memory={"message": "当前账户无持仓，无需清仓"},
            symbol_results={},
            outcome=ExecutionOutcome.COMPLETED,
            status=ExecutionStatus.NOOP,
            channel_type=executor.channel_type,
            inputs=empty_input,
        )

    def _get_default_algorithm(self) -> dict[str, object]:
        """
        返回默认算法配置.

        Returns
        -------
        dict[str, object]
            清仓流程使用的默认算法配置。
        """
        return {
            "method": "SINGLE-MAKER",
            "params": {"max_wait_seconds": 60},
        }
