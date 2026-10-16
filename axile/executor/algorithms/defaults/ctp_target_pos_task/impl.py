"""
实现 CTP 渠道的 TARGET-POS-TASK 目标持仓算法。

该模块负责把上层已经规划好的单品种目标净持仓，转换为符合 CTP
昨仓 / 今仓规则的订单序列。它只关心单个 ``symbol`` 的落地执行，
不负责组合级别的目标拆解、权重解释或调度。

Notes
-----
实现刻意保持“先平反向仓，再开同向仓”的顺序，以避免净持仓在执行
过程中先朝错误方向偏离，并尽量保留交易所原生 ``offset_flag`` 语义。
当昨仓 / 今仓精确腿不足时，才退回通用平仓标记作为最后补偿。
"""

import math
from typing import Any, Dict, List, Literal, Tuple, cast

from pydantic import ConfigDict, Field

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionEventStatus, ExecutionEventType, ExecutionReasonFamily
from axile.executor.account_control.exceptions import AccountControlBlockedError
from axile.executor.algorithms.common.params import BaseAlgorithmParams, ChaseParamsMixin
from axile.executor.algorithms.core.base import (
    AlgorithmInput,
    AlgorithmResult,
    ExecutorProtocol,
    OrderDirection,
    OrderType,
    register_algorithm,
)
from axile.executor.algorithms.exceptions import execution_error_message
from axile.executor.algorithms.utils import setup_order_tracker, teardown_order_tracker
from axile.executor.algorithms.utils.final_position import read_final_position
from axile.executor.algorithms.utils.order_tracker import ChaseConfig, OrderTracker
from axile.executor.algorithms.utils.outcome import summarize_outcome
from axile.executor.models.execution_result import ExecutionStatus
from axile.executor.models.unified_account_assets import (
    Position,
    PositionDirection,
    UnifiedAccountAssets,
    is_degraded_snapshot_source,
)
from axile.executor.models.unified_order import UnifiedOrder
from axile.executor.models.unified_price import clone_price_data
from axile.executor.order_volume_limits import split_order_volumes
from axile.executor.termination import ExecutionTerminated


class CTPTargetPosTaskParams(BaseAlgorithmParams, ChaseParamsMixin):
    """TARGET-POS-TASK 算法参数."""

    model_config = ConfigDict(extra="forbid")

    max_wait_seconds: int = Field(
        default=60,
        title="最大等待",
        description="等待本轮委托完成的上限，不保证持仓到位。",
        ge=1,
        le=3600,
        json_schema_extra={"x-order": 30, "x-unit": "秒", "x-control": "stepper"},
    )

    price_strategy: Literal["PASSIVE", "ACTIVE"] = Field(
        default="PASSIVE",
        title="报价方式",
        description="限价单使用本方价或对手价；品种 price 规则优先。",
        json_schema_extra={
            "x-order": 10,
            "x-control": "choice",
            "x-enum-labels": {"PASSIVE": "本方挂单", "ACTIVE": "对手价"},
        },
    )
    close_order_type: Literal["LIMIT", "MARKET"] = Field(
        default="LIMIT",
        title="平仓订单",
        description="平仓使用限价或市价；报价方式仅对限价单生效。",
        json_schema_extra={
            "x-order": 11,
            "x-control": "choice",
            "x-enum-labels": {"LIMIT": "限价", "MARKET": "市价"},
        },
    )
    open_order_type: Literal["LIMIT", "MARKET"] = Field(
        default="LIMIT",
        title="开仓订单",
        description="开仓使用限价或市价；报价方式仅对限价单生效。",
        json_schema_extra={
            "x-order": 12,
            "x-control": "choice",
            "x-enum-labels": {"LIMIT": "限价", "MARKET": "市价"},
        },
    )

    def __str__(self) -> str:
        """便于记录日志的字符串表示."""
        return f"CTPTargetPosTaskParams(max_wait_seconds={self.max_wait_seconds}, chase_enabled={self.chase_enabled})"


# ==================== CTP常量定义 ====================

# CTP买卖方向常量
THOST_FTDC_D_Buy = "0"  # 买入
THOST_FTDC_D_Sell = "1"  # 卖出

# CTP开平标志常量
THOST_FTDC_OF_Open = "0"  # 开仓
THOST_FTDC_OF_Close = "1"  # 平仓（通用）
THOST_FTDC_OF_CloseToday = "3"  # 平今仓
THOST_FTDC_OF_CloseYesterday = "4"  # 平昨仓

# CTP价格类型常量
THOST_FTDC_OPT_LimitPrice = "2"  # 限价


# ==================== CTP持仓数据结构 ====================


class CTPPositionDetail:
    """
    表示单个合约的 CTP 风格持仓拆分结果。

    Attributes
    ----------
    symbol : str
        合约代码。
    long_yesterday : int
        多头昨仓手数。
    long_today : int
        多头今仓手数。
    short_yesterday : int
        空头昨仓手数。
    short_today : int
        空头今仓手数。
    long_total : int
        多头总持仓手数。
    short_total : int
        空头总持仓手数。
    net_position : int
        净持仓手数，按 ``long_total - short_total`` 解释。

    Notes
    -----
    该对象是算法内部的归一化视图，用于屏蔽不同通道实现或历史数据
    结构在字段命名上的差异。实例构建完成后，各字段应保持自洽：
    ``*_total`` 等于对应的剩余昨仓与今仓之和。
    """

    def __init__(self, symbol: str) -> None:
        """
        初始化某个合约的持仓详情容器。

        Parameters
        ----------
        symbol : str
            合约代码。
        """
        self.symbol = symbol
        self.long_yesterday = 0  # 多头昨仓
        self.long_today = 0  # 多头今仓
        self.short_yesterday = 0  # 空头昨仓
        self.short_today = 0  # 空头今仓
        self.long_total = 0  # 多头总持仓
        self.short_total = 0  # 空头总持仓
        self.net_position = 0  # 净持仓

    @property
    def total_yesterday(self) -> int:
        """昨仓总量."""
        return self.long_yesterday + self.short_yesterday

    @property
    def total_today(self) -> int:
        """今仓总量."""
        return self.long_today + self.short_today

    @property
    def total_available_close(self) -> int:
        """可平仓总量."""
        return self.total_yesterday + self.total_today

    def __str__(self) -> str:
        """返回持仓对象的调试字符串."""
        return (
            f"CTPPosition({self.symbol}: 多昨{self.long_yesterday} 今{self.long_today}, "
            f"空昨{self.short_yesterday} 今{self.short_today}, 净{self.net_position})"
        )


# ==================== 辅助函数 ====================


def _extract_ctp_position_details(
    account_assets: UnifiedAccountAssets, symbols: List[str]
) -> Dict[str, CTPPositionDetail]:
    """
    从统一账户资产中提取 CTP 风格持仓详情。

    Parameters
    ----------
    account_assets : UnifiedAccountAssets
        当前账户资产快照。
    symbols : List[str]
        需要提取持仓详情的品种列表。

    Returns
    -------
    Dict[str, CTPPositionDetail]
        品种到 CTP 持仓详情的映射。
    """
    position_details = {symbol: CTPPositionDetail(symbol) for symbol in symbols}
    for position in account_assets.positions:
        if position.symbol not in position_details:
            continue
        side, total, today, yesterday = _position_quantities(position)
        detail = position_details[position.symbol]
        for suffix, quantity in (("total", total), ("today", today), ("yesterday", yesterday)):
            field = f"{side}_{suffix}"
            setattr(detail, field, getattr(detail, field) + quantity)
        detail.net_position = detail.long_total - detail.short_total

    return position_details


def _position_quantities(position: Position) -> tuple[str, int, int, int]:
    """校验单方向拆分；总量只采用统一持仓数量，昨仓表示当前剩余量。"""
    sides = {PositionDirection.LONG: "long", PositionDirection.SHORT: "short"}
    if position.direction not in sides:
        raise ValueError(f"{position.symbol}: CTP 持仓必须指定多空方向")
    side = sides[position.direction]

    def quantity(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{position.symbol}: 持仓数量必须是非负整数")
        if not math.isfinite(value) or value < 0 or int(value) != value:
            raise ValueError(f"{position.symbol}: 持仓数量必须是非负整数")
        return int(value)

    def extra_quantity(*keys: str) -> int | None:
        values = [quantity(position.extra[key]) for key in keys if key in position.extra]
        if len(set(values)) > 1:
            raise ValueError(f"{position.symbol}: 持仓字段不一致 {keys}")
        return values[0] if values else None

    total = quantity(position.volume)
    reported_total = extra_quantity(f"{side}_total", f"{side}_position")
    today = extra_quantity(f"{side}_td", f"{side}_today_position")
    yesterday = extra_quantity(f"{side}_yd", f"{side}_yd_position")
    if reported_total is not None and reported_total != total:
        raise ValueError(f"{position.symbol}: 持仓总量与 volume 不一致")
    if today is None and yesterday is None:
        if total:
            raise ValueError(f"{position.symbol}: 缺少今昨持仓拆分")
        return side, 0, 0, 0
    if today is None:
        today = total - cast(int, yesterday)
    if yesterday is None:
        yesterday = total - today
    if today < 0 or yesterday < 0 or today + yesterday != total:
        raise ValueError(f"{position.symbol}: 今昨持仓与总量不一致")
    return side, total, today, yesterday


def _calculate_order_price(
    market_data: Any | None,
    price_type: str,
    direction: str,
) -> float:
    """
    根据价格策略计算 CTP 下单价格。

    Parameters
    ----------
    market_data : Any | None
        当前品种的市场数据。
    price_type : str
        价格策略，支持 ``PASSIVE`` 和 ``ACTIVE``。
    direction : str
        订单方向，支持 ``BUY`` 和 ``SELL``。

    Returns
    -------
    float
        计算得出的限价；缺少有效行情时返回 ``0``，由调用方拒绝限价报单。
    """
    if market_data is None:
        return 0.0

    price_data = market_data

    # PASSIVE 使用本方最优价等待成交；ACTIVE 使用对手方最优价主动成交。
    if price_type.upper() == "PASSIVE":
        quote_name = "bid_price" if direction == OrderDirection.BUY else "ask_price"
    else:
        quote_name = "ask_price" if direction == OrderDirection.BUY else "bid_price"
    quote = float(getattr(price_data, quote_name, 0.0) or 0.0)
    return quote if quote > 0 else float(getattr(price_data, "last_price", 0.0) or 0.0)


def _get_close_position_availability(
    position_detail: CTPPositionDetail,
    direction: str,
) -> Tuple[int, int, str]:
    """根据平仓方向返回可平昨仓、今仓和描述信息."""
    if direction == OrderDirection.BUY:
        return position_detail.short_yesterday, position_detail.short_today, "平空"

    return position_detail.long_yesterday, position_detail.long_today, "平多"


def _place_volume_slices(
    executor: ExecutorProtocol,
    direction: OrderDirection,
    volume: float,
    limit_price: float,
    *,
    offset_flag: str,
    trade_rule: Dict[str, Any] | None,
    tracker: OrderTracker | None = None,
    order_type: OrderType = OrderType.LIMIT,
) -> Tuple[List[UnifiedOrder], float]:
    """按有效单笔上限拆单提交；返回订单列表与成功提交手数。"""
    orders: List[UnifiedOrder] = []
    if volume <= 0:
        return orders, 0.0
    getter = getattr(executor, "get_order_volume_bounds", None)
    bounds = cast("tuple[int | None, int | None] | None", getter(order_type, trade_rule)) if callable(getter) else None
    slices = split_order_volumes(volume, bounds[1], min_size=bounds[0]) if bounds is not None else [volume]
    submitted = 0.0
    for chunk in slices:
        try:
            order = executor.place_order(
                direction,
                order_type,
                chunk,
                limit_price,
                offset_flag=offset_flag,
                trade_rule=trade_rule,
            )
        except Exception as exc:
            if tracker is not None:
                if isinstance(exc, AccountControlBlockedError):
                    tracker.explicit_blocked_error = exc.execution_error
                else:
                    tracker.explicit_error = execution_error_message(exc, "报单")
            if isinstance(exc, (MemoryError, ExecutionTerminated)) or bool(
                getattr(exc, "requires_session_recovery", False)
            ):
                raise
            executor.logger.error(
                f"拆单提交失败: {direction.value} {chunk}手 {order_type.value}@{limit_price} offset={offset_flag}, 错误: {exc}"
            )
            break
        orders.append(order)
        emit_audit_event = getattr(executor, "emit_audit_event", None)
        if callable(emit_audit_event):
            emit_audit_event(
                event_type=ExecutionEventType.ORDER_SUBMITTED,
                status=ExecutionEventStatus.INFO,
                reason_family=ExecutionReasonFamily.EXECUTION_STRATEGY,
                reason_code="COMMON.ORDER_SUBMITTED",
                symbol=order.symbol,
                order_id=order.order_id,
                details={
                    "order": {
                        "direction": direction.value,
                        "order_type": order_type.value,
                        "volume": float(chunk),
                        "price": float(limit_price),
                        "offset_flag": offset_flag,
                    }
                },
            )
        if tracker is not None:
            tracker.add_order(
                order,
                direction=direction,
                offset_flag=offset_flag,
                trade_rule=trade_rule,
                chase_enabled=order_type == OrderType.LIMIT,
            )
            tracker.explicit_error = None
            tracker.explicit_blocked_error = None
        submitted += float(chunk)
    if submitted < volume:
        executor.logger.warning(f"拆单未提交缺口: {volume - submitted}，目标 {volume}，已提交 {submitted}")
    return orders, submitted


def _submit_close_leg(
    executor: ExecutorProtocol,
    symbol: str,
    direction: str,
    close_volume: float,
    limit_price: float,
    offset_flag: str,
    start_message: str,
    success_message: str,
    failure_message: str,
    failure_level: str = "warning",
    trade_rule: Dict[str, Any] | None = None,
    tracker: OrderTracker | None = None,
    order_type: OrderType = OrderType.LIMIT,
) -> Tuple[List[UnifiedOrder], float]:
    """提交单腿平仓订单（可按单笔上限拆单），并按调用方要求记录日志."""
    _ = symbol
    try:
        executor.logger.info(start_message)
        orders, submitted = _place_volume_slices(
            executor,
            OrderDirection(direction),
            close_volume,
            limit_price,
            offset_flag=offset_flag,
            trade_rule=trade_rule,
            tracker=tracker,
            order_type=order_type,
        )
        if not orders:
            raise RuntimeError("平仓拆单后无任何订单提交成功")
        executor.logger.info(success_message.format(order_id=orders[-1].order_id))
        return orders, submitted
    except Exception as e:
        if isinstance(e, (MemoryError, ExecutionTerminated)) or bool(getattr(e, "requires_session_recovery", False)):
            raise
        getattr(executor.logger, failure_level)(failure_message.format(error=e))
        return [], 0


def _smart_close_position(
    executor: ExecutorProtocol,
    symbol: str,
    direction: str,
    close_volume: float,
    limit_price: float,
    trade_rule: Dict[str, Any] | None = None,
    tracker: OrderTracker | None = None,
    order_type: OrderType = OrderType.LIMIT,
) -> Tuple[List[UnifiedOrder], float, bool]:
    """
    按昨仓 / 今仓可用量智能拆分平仓顺序。

    Parameters
    ----------
    executor : ExecutorProtocol
        当前执行器会话。
    symbol : str
        合约代码。
    direction : str
        买卖方向，支持 ``BUY`` 和 ``SELL``。
    close_volume : float
        计划平掉的手数。
    limit_price : float
        本次平仓使用的下单价格；市价单传入 ``0``。

    Returns
    -------
    Tuple[List[UnifiedOrder], float, bool]
        依次为订单列表、成功提交的平仓手数，以及是否完全满足目标。
    """
    orders: List[UnifiedOrder] = []

    if close_volume <= 0:
        return orders, 0, True

    executor.logger.info(f"开始平仓: {symbol} {direction} {close_volume}手，读取账户平仓规则")

    account_assets = executor.get_account_assets()
    position_detail = _extract_ctp_position_details(account_assets, [symbol]).get(symbol)

    if not position_detail:
        executor.logger.warning(f"无法获取{symbol}的持仓详情")
        return orders, 0, False

    available_yesterday, available_today, direction_desc = _get_close_position_availability(position_detail, direction)
    total_available = available_yesterday + available_today

    executor.logger.info(
        f"{symbol} {direction_desc}持仓分析: "
        f"昨仓={available_yesterday}手, 今仓={available_today}手, 可平={total_available}手"
    )

    if total_available < close_volume:
        executor.logger.warning(f"可平仓手数不足: 需要{close_volume}手, 可平{total_available}手")
        close_volume = total_available

    if close_volume <= 0:
        return orders, 0, True

    executed_volume = 0
    remaining_volume = close_volume

    close_plan = executor.plan_close_orders(OrderDirection(direction), close_volume, account_assets)
    offsets = {
        "close_yesterday": THOST_FTDC_OF_CloseYesterday,
        "close_today": THOST_FTDC_OF_CloseToday,
        "close": THOST_FTDC_OF_Close,
    }
    for leg_volume, extra in close_plan:
        offset_flag = offsets[str(extra["offset_flag"])]
        offset_name = {"4": "平昨", "3": "平今", "1": "平仓"}[offset_flag]
        leg_orders, submitted_volume = _submit_close_leg(
            executor=executor,
            symbol=symbol,
            direction=direction,
            close_volume=leg_volume,
            limit_price=limit_price,
            offset_flag=offset_flag,
            start_message=f"执行{offset_name}: {leg_volume}手",
            success_message=f"{offset_name}订单提交成功: {leg_volume}手, 订单ID: {{order_id}}",
            failure_message=f"{offset_name}失败: {{error}}",
            trade_rule=trade_rule,
            tracker=tracker,
            order_type=order_type,
        )
        if not leg_orders:
            break

        orders.extend(leg_orders)
        executed_volume += submitted_volume
        remaining_volume -= submitted_volume

    success = remaining_volume == 0
    if success:
        executor.logger.info(f"智能平仓完成: {symbol} 成功平仓 {executed_volume}手")
    else:
        executor.logger.warning(f"智能平仓部分成功: {symbol} 已平仓 {executed_volume}手, 剩余 {remaining_volume}手")

    return orders, executed_volume, success


def _execute_position_adjustment(
    executor: ExecutorProtocol,
    symbol: str,
    target_volume: float,
    position_detail: CTPPositionDetail,
    market_data: Any | None,
    price_type: str,
    *,
    trade_rule: Dict[str, Any] | None = None,
    tracker: OrderTracker | None = None,
    close_order_type: OrderType = OrderType.LIMIT,
    open_order_type: OrderType = OrderType.LIMIT,
) -> List[UnifiedOrder]:
    """
    按目标净持仓执行单品种持仓调整。

    Parameters
    ----------
    executor : ExecutorProtocol
        当前执行器会话。
    symbol : str
        品种代码。
    target_volume : float
        目标净持仓量。
    position_detail : CTPPositionDetail
        当前品种的 CTP 持仓详情。
    market_data : Any | None
        当前品种市场数据。
    price_type : str
        价格策略。
    trade_rule : Dict[str, Any] | None, optional
        当前品种的交易规则；其中 ``margin_ratio`` 在期权卖方场景下作为
        CTP 合约表保证金率为 0 时的兜底，会通过 ``executor.place_order``
        kwargs 透传到 trader 风控模块。

    Returns
    -------
    List[UnifiedOrder]
        生成的订单列表，可能同时包含平仓腿和开仓腿。
    """
    current_net = position_detail.net_position
    adjust_volume = target_volume - current_net
    orders: List[UnifiedOrder] = []

    if adjust_volume == 0:
        executor.logger.info(f"{symbol} 持仓已达目标，无需调整")
        return orders

    # 同方向的开平腿共用报价侧；仅限价腿读取行情。
    direction = OrderDirection.BUY if adjust_volume > 0 else OrderDirection.SELL

    def price_for(order_type: OrderType, action: str) -> float | None:
        if order_type == OrderType.MARKET:
            return 0.0
        limit_price = _calculate_order_price(market_data, price_type, direction)
        if math.isfinite(limit_price) and limit_price > 0:
            return limit_price
        message = f"{symbol} {action}缺少有效行情，未提交限价单"
        executor.logger.error(message)
        if tracker is not None:
            tracker.explicit_error = message
        return None

    executor.logger.info(
        f"{symbol}: 当前净持仓={current_net}, 目标={target_volume}, "
        f"调整={adjust_volume}, 报价策略={price_type}, 平仓类型={close_order_type.value}, 开仓类型={open_order_type.value}"
    )

    # 先平反向仓，再开同向仓；平仓未完整提交时不继续开仓。
    remaining = abs(adjust_volume)
    opposite_volume = position_detail.short_total if direction == OrderDirection.BUY else position_detail.long_total
    if opposite_volume > 0:
        close_volume = min(remaining, opposite_volume)
        close_price = price_for(close_order_type, "平仓")
        if close_price is None:
            return orders
        close_orders, submitted, close_ok = _smart_close_position(
            executor,
            symbol,
            direction.value,
            close_volume,
            close_price,
            trade_rule=trade_rule,
            tracker=tracker,
            order_type=close_order_type,
        )
        orders.extend(close_orders)
        if not close_ok:
            close_side = "空" if direction == OrderDirection.BUY else "多"
            executor.logger.error(
                f"{symbol}: 平{close_side}未完成（目标平仓 {close_volume} 手，已提交 {submitted} 手），停止后续开仓"
            )
            return orders
        remaining -= submitted

    if remaining > 0:
        open_price = price_for(open_order_type, "开仓")
        if open_price is None:
            return orders
        open_orders, submitted = _place_volume_slices(
            executor,
            direction,
            remaining,
            open_price,
            offset_flag=THOST_FTDC_OF_Open,
            trade_rule=trade_rule,
            tracker=tracker,
            order_type=open_order_type,
        )
        orders.extend(open_orders)
        if open_orders:
            open_side = "多" if direction == OrderDirection.BUY else "空"
            executor.logger.info(
                f"开{open_side}仓: {symbol} 提交 {submitted}手@{open_price}, 末单ID: {open_orders[-1].order_id}"
            )

    return orders


@register_algorithm(
    "TARGET-POS-TASK",
    channels=[TradeChannel.CTP, TradeChannel.TQ],
    params_class=CTPTargetPosTaskParams,
    label="期货目标持仓",
    description="按目标净持仓安排开平仓，并按设置处理今仓、昨仓。适用于 CTP 和 TQ；报价支持合约规则覆盖；平仓顺序统一使用账户平仓规则。",
)
def ctp_target_pos_task_algorithm(executor: ExecutorProtocol, algorithm_input: AlgorithmInput) -> AlgorithmResult:
    """
    执行 CTP 目标持仓调整算法。

    Parameters
    ----------
    executor : ExecutorProtocol
        当前执行器会话。
    algorithm_input : AlgorithmInput
        单品种算法输入；目标持仓量、交易规则与参数都已由上层规划完成。

    Returns
    -------
    AlgorithmResult
        本次单品种调仓的执行结果。

    Notes
    -----
    算法参数通过 ``algorithm_input.params`` 提供，主要包括 ``max_wait_seconds``、
    ``chase_enabled``、``chase_ticks``、``max_chase_count`` 和 ``chase_interval``。
    算法主线遵循“读取当前持仓 -> 先平反向仓 -> 再开同向仓 -> 回调驱动等待 ->
    最终再拉一次账户资产确认结果”。
    """
    algorithm_name = "TARGET-POS-TASK"

    executor.logger.info(f"开始{algorithm_name}算法执行")
    executor.logger.info(f"算法输入: symbol={algorithm_input.symbol}, target_volume={algorithm_input.target_volume}")

    params = algorithm_input.params
    if not isinstance(params, CTPTargetPosTaskParams):
        raise TypeError(f"{algorithm_name} 参数应为 CTPTargetPosTaskParams，实际为 {type(params).__name__}")
    max_wait_seconds = params.max_wait_seconds
    chase_config = ChaseConfig.from_params(params)

    execution_memory: Dict[str, Any] = {}
    target_reached = False
    current_net_position = 0
    planned_symbol = algorithm_input.symbol
    market_data: Any | None = None

    account_assets = executor.get_account_assets()
    if is_degraded_snapshot_source(account_assets.source):
        return AlgorithmResult(
            symbol=planned_symbol,
            algorithm=algorithm_name,
            status=ExecutionStatus.FAILED,
            error="初始持仓尚未确认",
            account_assets=account_assets,
            target_volume=algorithm_input.target_volume,
            memory={"algorithm": algorithm_name, "error": "初始持仓尚未确认"},
        )
    current_net_position = sum(
        -p.volume if p.direction == PositionDirection.SHORT else p.volume
        for p in account_assets.positions
        if p.symbol == planned_symbol
    )
    executor.logger.info(f"账户总资产: {account_assets.total_asset:.2f}, 可用资金: {account_assets.available_cash:.2f}")

    symbol = algorithm_input.symbol
    market_data = executor.get_market_data()
    if market_data:
        executor.logger.info(f"获取到 {symbol} 的市场数据")

    # 回调驱动等待：先绑 tracker（订单/成交/追价）再下单，避免第一笔回报早于注册丢失。
    # 成交必须走同一入口；只挂订单回调时，报单已成但 ``trades`` 仍为空。
    tracker = setup_order_tracker(executor, market_data, chase_config)

    try:
        position_details = _extract_ctp_position_details(account_assets, [symbol])
        symbol_position_detail = position_details.get(symbol)
        if symbol_position_detail:
            current_net_position = symbol_position_detail.net_position

        # 目标数量已经在通用执行层规划完成；CTP 算法这里不再重新解释组合权重，
        # 只负责把这一个 symbol 的目标落地成昨今仓友好的订单序列。
        target_volume = algorithm_input.target_volume
        first_tick = clone_price_data(market_data)

        executor.logger.info(f"目标持仓数量: {{'{symbol}': {target_volume}}}")

        trade_rule = algorithm_input.trade_rule
        price_type = str(trade_rule.get("price", params.price_strategy))

        executor.logger.info(f"{symbol}: 目标={target_volume}, 价格策略={price_type}")

        position_detail = position_details.get(symbol)
        if not position_detail:
            executor.logger.warning(f"无法获取{symbol}持仓详情，跳过")
        else:
            symbol_orders = _execute_position_adjustment(
                executor,
                symbol,
                target_volume,
                position_detail,
                market_data,
                price_type,
                trade_rule=trade_rule,
                tracker=tracker,
                close_order_type=OrderType(params.close_order_type),
                open_order_type=OrderType(params.open_order_type),
            )

            execution_memory[f"{symbol}_adjustment"] = {
                "current_net": position_detail.net_position,
                "target_volume": target_volume,
                "orders_generated": len(symbol_orders),
                "price_type": price_type,
                "close_order_type": params.close_order_type,
                "open_order_type": params.open_order_type,
            }

        if tracker.get_pending_count() > 0:
            executor.logger.info(f"等待 {tracker.get_pending_count()} 个订单完成...")
            tracker.wait_for_completion(timeout=max_wait_seconds)

        # 最终是否达标以重新拉取的账户资产为准，而不是依赖本地订单累计推断；
        # 这样可以覆盖交易所异步回报延迟、部分成交和通道侧补单等情况。
        final_account_assets, final_net_position, explicit_error = read_final_position(
            executor,
            lambda assets: sum(
                -p.volume if p.direction == PositionDirection.SHORT else p.volume
                for p in assets.positions
                if p.symbol == symbol
            ),
        )
        if explicit_error and not isinstance(tracker.explicit_error, str):
            tracker.explicit_error = explicit_error
        target_reached = final_net_position == target_volume
        target_gap = target_volume - final_net_position

        if not target_reached:
            executor.logger.warning(
                f"{symbol} 最终持仓未达目标: 当前{final_net_position} 目标{target_volume} 差值{target_gap}"
            )

        executor.logger.info(f"{algorithm_name}算法执行完成")

        orders = tracker.get_all_orders()
        trades = tracker.get_all_trades()

        conclusion = summarize_outcome(
            current_net_position,
            final_net_position,
            target_volume,
            orders,
            trades,
            tracker.explicit_error if isinstance(tracker.explicit_error, str) else None,
            tracker.explicit_blocked_error if isinstance(tracker.explicit_blocked_error, str) else None,
        )
        return AlgorithmResult(
            **conclusion,
            orders=orders,
            trades=trades,
            account_assets=final_account_assets,
            target_volume=target_volume,
            first_tick=first_tick,
            memory={
                "algorithm": algorithm_name,
                "symbols_processed": 1,
                "orders_generated": len(orders),
                "trades_generated": len(trades),
                "symbol": planned_symbol,
                "target_volume": target_volume,
                "current_net_position": current_net_position,
                "final_net_position": final_net_position,
                "target_gap": target_gap if not target_reached else 0,
                "target_reached": target_reached,
                "execution_details": execution_memory,
                "total_asset_before": account_assets.total_asset,
                "total_asset_after": final_account_assets.total_asset,
                "chase_enabled": chase_config is not None,
            },
        )

    except Exception as e:
        if isinstance(e, (MemoryError, ExecutionTerminated)) or bool(getattr(e, "requires_session_recovery", False)):
            raise
        error_msg = f"{algorithm_name}算法执行失败: {e}"
        executor.logger.error(f"{error_msg}")
        execution_memory["error"] = str(e)
        final_account_assets, final_volume, explicit_error = read_final_position(
            executor,
            lambda assets: sum(
                -p.volume if p.direction == PositionDirection.SHORT else p.volume
                for p in assets.positions
                if p.symbol == symbol
            ),
        )
        tracker_error = tracker.explicit_error if isinstance(tracker.explicit_error, str) else None
        tracker_blocked = tracker.explicit_blocked_error if isinstance(tracker.explicit_blocked_error, str) else None
        explicit_blocked_error = tracker_blocked or (
            e.execution_error if isinstance(e, AccountControlBlockedError) else None
        )
        explicit_error = (
            explicit_error
            or tracker_error
            or (None if explicit_blocked_error else execution_error_message(e, "持仓调整"))
        )
        orders, trades = tracker.get_all_orders(), tracker.get_all_trades()
        return AlgorithmResult(
            **summarize_outcome(
                current_net_position,
                final_volume,
                algorithm_input.target_volume,
                orders,
                trades,
                explicit_error,
                explicit_blocked_error,
            ),
            orders=orders,
            trades=trades,
            account_assets=final_account_assets,
            target_volume=algorithm_input.target_volume,
            first_tick=clone_price_data(market_data),
            memory={
                "algorithm": algorithm_name,
                "success": False,
                "error": error_msg,
                "execution_details": execution_memory,
            },
        )

    finally:
        # 回调注册属于一次执行的临时资源；无论成功失败都必须释放，避免污染后续
        # symbol 执行或让旧 tracker 继续消费新订单事件。
        teardown_order_tracker(executor, tracker, chase_config)
