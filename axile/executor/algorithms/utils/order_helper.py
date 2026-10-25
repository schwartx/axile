"""订单操作辅助函数.

提供算法中通用的订单操作工具函数，减少代码重复。
"""

from dataclasses import dataclass
from typing import Any, cast

from axile.common.order_param_model import OrderParamModel
from axile.domain.execution import ExecutionEventStatus, ExecutionEventType, ExecutionReasonFamily
from axile.executor.account_control.exceptions import AccountControlBlockedError
from axile.executor.algorithms.core.base import ExecutorProtocol
from axile.executor.algorithms.exceptions import (
    RECOVERABLE_ALGORITHM_EXCEPTIONS,
    SubMinQuantityError,
    execution_error_message,
    format_exception_message,
)
from axile.executor.algorithms.utils.clock import get_default_clock
from axile.executor.algorithms.utils.order_tracker import ChaseConfig, OrderTracker
from axile.executor.algorithms.utils.trading import cancel_pending_orders_via_query, determine_order_price
from axile.executor.models.unified_account_assets import PositionDirection, UnifiedAccountAssets
from axile.executor.models.unified_order import OrderDirection, OrderType, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData
from axile.executor.order_volume_limits import split_order_volumes
from axile.executor.termination import ExecutionTerminated


@dataclass(frozen=True)
class CloseIntent:
    """
    单向持仓下单的平仓意图裁决.

    Attributes
    ----------
    kwargs : dict[str, str]
        下单附加参数；涉及平已有仓位时包含 ``position_side``。
    opposite_volume : float
        当前可用于本单平仓的反向持仓总量；为 ``0`` 表示纯开仓。
    """

    kwargs: dict[str, str]
    opposite_volume: float


def determine_close_intent(
    executor: ExecutorProtocol,
    direction: OrderDirection,
    account_assets: UnifiedAccountAssets,
) -> CloseIntent:
    """
    推导本单的平仓意图与可平反向持仓量.

    Parameters
    ----------
    executor : ExecutorProtocol
        当前算法绑定的执行器会话。
    direction : OrderDirection
        当前订单方向。
    account_assets : UnifiedAccountAssets
        当前账户资产快照。

    Returns
    -------
    CloseIntent
        下单附加参数与可平反向持仓量；不涉及平仓时参数为空、可平量为 0。

    Notes
    -----
    该函数只在双向持仓语义下有意义。算法层不直接
    关心账户模式细节，而是根据当前持仓方向推导应平掉哪一边仓位。

    函数只表达渠道无关的意图词汇；期货开平标志等渠道母语由各渠道的
    ``place_order`` 翻译层负责推导，算法层不主动产生。
    """
    symbol = executor.symbol

    try:
        positions = executor.get_positions(account_assets)
    except Exception as e:
        if executor.order_param_model in (OrderParamModel.OFFSET, OrderParamModel.POSITION_SIDE):
            # 期货渠道上读不出持仓就无法判断本单是不是平仓意图;此时静默放行会把
            # 平仓单发成反向开仓、形成多空锁仓(issue #53),宁可失败也不猜测。
            raise RuntimeError(f"渠道无法读取 {symbol} 持仓,拒绝猜测开平语义") from e
        executor.logger.warning(f"获取 {symbol} 持仓失败: {e}")
        return CloseIntent(kwargs={}, opposite_volume=0.0)

    intent = CloseIntent(kwargs={}, opposite_volume=0.0)
    if direction == OrderDirection.SELL:
        for pos_vol, pos_dir in positions:
            if pos_dir == PositionDirection.LONG and pos_vol > 0:
                intent = CloseIntent(kwargs={"position_side": "LONG"}, opposite_volume=float(pos_vol))
                executor.logger.debug(f"{symbol} 卖出将平多头持仓")
                break
    elif direction == OrderDirection.BUY:
        for pos_vol, pos_dir in positions:
            if pos_dir == PositionDirection.SHORT and pos_vol > 0:
                intent = CloseIntent(kwargs={"position_side": "SHORT"}, opposite_volume=float(pos_vol))
                executor.logger.debug(f"{symbol} 买入将平空头持仓")
                break

    return intent


def determine_position_side(
    executor: ExecutorProtocol,
    direction: OrderDirection,
    account_assets: UnifiedAccountAssets,
) -> dict[str, str]:
    """
    为双向持仓模式推导 ``position_side`` 参数.

    Returns
    -------
    dict[str, str]
        下单附加参数。涉及平已有仓位时包含 ``position_side``（指明平哪一侧），
        否则返回空字典（纯开仓意图，由渠道按自身参数模型翻译）。
    """
    return determine_close_intent(executor, direction, account_assets).kwargs


@dataclass(frozen=True)
class OrderDecision:
    """
    单向持仓下单前的可行性裁决结果.

    Attributes
    ----------
    action : str
        ``"SEND"`` 表示照常提交；``"SKIP"`` 表示本单不可行、应跳过不下。
    reduce_only : bool
        是否以 reduceOnly 方式下单。纯减仓且名义价值不足最小值时置真，
        借交易所对减仓单的豁免突破最小名义价值限制。
    reason : str
        裁决原因码，用于审计事件与诊断日志。
    """

    action: str
    reduce_only: bool = False
    reason: str = ""


def _reference_price(executor: ExecutorProtocol, price: float) -> float:
    """
    解析用于估算名义价值的参考价.

    Parameters
    ----------
    executor : ExecutorProtocol
        当前算法绑定的执行器会话。
    price : float
        下单价格；市价单通常为 ``0``。

    Returns
    -------
    float
        参考价。限价单直接返回 ``price``；市价单回退到行情快照的最新价，
        再退到买一卖一中值；均不可得时返回 ``0.0``。
    """
    if price > 0:
        return price
    market = executor.get_market_data()
    if market is None:
        return 0.0
    if market.last_price > 0:
        return market.last_price
    if market.bid_price > 0 and market.ask_price > 0:
        return (market.bid_price + market.ask_price) / 2
    return 0.0


def resolve_reduce_intent(
    executor: ExecutorProtocol,
    direction: OrderDirection,
    volume: float,
    target_volume: float,
    current_volume: float,
    price: float,
) -> OrderDecision:
    """
    在单向持仓语义下裁决订单可行性并推导 reduceOnly.

    连续目标经 ``目标 - 现仓`` 得到的下单量，未必落在交易所可执行的格点上：
    交易所对每笔增加敞口的订单设有最小名义价值下界，减仓单则可借 reduceOnly
    豁免。本函数据此把订单归入三类之一：名义达标照常发、纯减仓借 reduceOnly
    突破、加仓或翻向余量过小则跳过。

    Parameters
    ----------
    executor : ExecutorProtocol
        当前算法绑定的执行器会话，用于读取最小名义价值与行情。
    direction : OrderDirection
        下单方向。
    volume : float
        本单下单数量（恒为正）。
    target_volume : float
        本轮规划的目标净持仓。
    current_volume : float
        下单前的当前净持仓（多为正、空为负）。
    price : float
        下单价格；市价单传 ``0`` 时回退到行情快照估算名义价值。

    Returns
    -------
    OrderDecision
        裁决结果。渠道无最小名义价值信息或参考价缺失时退化为 ``"SEND"``，
        保持既有行为不变。

    Notes
    -----
    穿零翻向单（``volume > |current_volume|``）同时含平仓与反向开仓两段，
    整单套 reduceOnly 会被交易所封顶在平到零、反向开仓段静默不成交，
    因此此类订单绝不置 ``reduce_only``。
    """
    min_notional = executor.get_min_notional()
    if not isinstance(min_notional, int | float) or min_notional <= 0:
        return OrderDecision(action="SEND")

    ref_price = _reference_price(executor, price)
    if ref_price <= 0:
        return OrderDecision(action="SEND")

    if volume * ref_price >= min_notional:
        return OrderDecision(action="SEND")

    # 名义价值不足最小值：仅"纯减仓"可借 reduceOnly 突破。
    is_reduce_direction = current_volume != 0 and (
        (direction == OrderDirection.SELL and current_volume > 0)
        or (direction == OrderDirection.BUY and current_volume < 0)
    )
    crosses_zero = volume > abs(current_volume)

    if is_reduce_direction and not crosses_zero:
        return OrderDecision(action="SEND", reduce_only=True, reason="pure_reduce_below_min_notional")

    _ = target_volume  # 目标仅用于语义完整；清仓（目标为 0）已落入纯减仓分支。
    return OrderDecision(action="SKIP", reason="increase_below_min_notional")


def setup_order_tracker(
    executor: ExecutorProtocol,
    market_data: UnifiedPriceData | None,
    chase_config: ChaseConfig | None,
) -> OrderTracker:
    """
    创建订单跟踪器并完成回调绑定。

    Parameters
    ----------
    executor : ExecutorProtocol
        当前算法绑定的执行器会话。
    market_data : UnifiedPriceData | None
        当前品种的行情快照；启用追单时会作为初始盘口。
    chase_config : ChaseConfig | None
        追单配置；为空时仅启用订单/成交跟踪。

    Returns
    -------
    OrderTracker
        已完成标准回调注册的订单跟踪器。

    Notes
    -----
    订单与成交回调始终注册：CTP 等渠道的成交手数在报单回报上，成交明细在成交
    回报上，漏挂成交回调会让算法结果 ``trades`` 永远为空。价格回调仅追单时注册。
    """
    tracker = OrderTracker(
        executor=executor,
        chase_config=chase_config,
        order_refresh_interval=30.0,
    )

    executor.register_order_callback(tracker.on_order_update)
    executor.register_trade_callback(tracker.on_trade_record)

    # 追单逻辑依赖最新盘口；若算法启动时已经拿到一份快照，这里先放入 tracker，
    # 避免在第一笔订单尚未收到价格回调前就失去追单判断依据。
    if chase_config:
        executor.register_price_callback(tracker.on_price_update)
        if market_data is not None:
            tracker.latest_prices[executor.symbol] = market_data

    executor.logger.debug(f"订单跟踪器已设置: symbol={executor.symbol}, chase={chase_config is not None}")

    return tracker


def teardown_order_tracker(
    executor: ExecutorProtocol,
    tracker: OrderTracker,
    chase_config: ChaseConfig | None,
) -> None:
    """
    注销订单跟踪器相关回调并做尾部清理。

    Parameters
    ----------
    executor : ExecutorProtocol
        当前算法绑定的执行器会话。
    tracker : OrderTracker
        本次算法使用的订单跟踪器。
    chase_config : ChaseConfig | None
        追单配置；为空时跳过价格回调清理。

    Notes
    -----
    清理阶段保持 best-effort。回调注销失败不应覆盖算法主流程的真实执行结果，
    因此这里只记录告警。
    """
    try:
        executor.unregister_order_callback(tracker.on_order_update)
    except Exception as e:
        executor.logger.warning(f"注销订单回调失败: {e}")

    try:
        executor.unregister_trade_callback(tracker.on_trade_record)
    except Exception as e:
        executor.logger.warning(f"注销成交回调失败: {e}")

    if chase_config:
        try:
            executor.unregister_price_callback(tracker.on_price_update)
        except Exception as e:
            executor.logger.warning(f"注销价格回调失败: {e}")

    executor.logger.debug("订单跟踪器已清理")


def submit_and_track_order(
    executor: ExecutorProtocol,
    tracker: OrderTracker,
    direction: OrderDirection,
    order_type: OrderType,
    volume: float,
    price: float,
    target_volume: float,
    current_volume: float,
    deadline: float | None = None,
    **kwargs: object,
) -> UnifiedOrder | None:
    """
    提交订单并纳入统一跟踪流程。

    Parameters
    ----------
    executor : ExecutorProtocol
        当前算法绑定的执行器会话。
    tracker : OrderTracker
        负责收敛订单与成交状态的跟踪器。
    direction : OrderDirection
        下单方向。
    order_type : OrderType
        订单类型。
    volume : float
        下单数量。
    price : float
        下单价格；市价单通常传 ``0``。
    target_volume : float
        本轮执行规划出的目标持仓量。
    current_volume : float
        提交订单前的当前持仓量。
    deadline : float | None, default=None
        本单及其追单、兜底共用的绝对截止时间；为空时不额外限制。
    **kwargs : object
        额外的下单参数，例如 ``position_side``。

    Returns
    -------
    UnifiedOrder | None
        执行器返回的订单对象；当本单因名义价值不足最小值且不可借 reduceOnly
        突破而被跳过时返回 ``None``。

    Notes
    -----
    先下单再写入 tracker，是因为订单主键只能由执行器返回；tracker 里的追单与
    剩余量计算都依赖这份真实订单对象。

    下单前先经 :func:`resolve_reduce_intent` 做单向持仓可行性裁决：纯减仓且
    名义价值不足时注入 ``reduce_only``；加仓或翻向余量过小则记审计事件后跳过。
    """
    symbol = executor.symbol

    decision = resolve_reduce_intent(executor, direction, volume, target_volume, current_volume, price)
    if decision.action == "SKIP":
        executor.emit_audit_event(
            event_type=ExecutionEventType.SYMBOL_SKIPPED,
            status=ExecutionEventStatus.WARNING,
            reason_family=ExecutionReasonFamily.MARKET_RULE,
            reason_code="COMMON.SUB_MIN_NOTIONAL",
            symbol=symbol,
            details={
                "direction": direction.value,
                "order_type": order_type.value,
                "volume": float(volume),
                "price": float(price),
                "target_volume": float(target_volume),
                "current_volume": float(current_volume),
                "reason": decision.reason,
            },
        )
        executor.logger.info(f"{symbol} {direction.value} {volume} 名义价值不足最小值，跳过下单（{decision.reason}）")
        return None
    if decision.reduce_only:
        kwargs["reduce_only"] = True

    if deadline is not None and get_default_clock().time() >= deadline:
        executor.logger.info(f"{symbol} 下单时间额度已耗尽，停止提交")
        return None
    try:
        order = executor.place_order(
            direction,
            order_type,
            volume,
            price,
            **kwargs,
        )
    except SubMinQuantityError as exc:
        # 碎量按步长取整后归零：视作「该品种无可执行动作」而非下单失败，记跳过事件并放行。
        executor.emit_audit_event(
            event_type=ExecutionEventType.SYMBOL_SKIPPED,
            status=ExecutionEventStatus.WARNING,
            reason_family=ExecutionReasonFamily.MARKET_RULE,
            reason_code="COMMON.SUB_MIN_QTY",
            symbol=symbol,
            details={
                "direction": direction.value,
                "order_type": order_type.value,
                "volume": float(volume),
                "price": float(price),
                "target_volume": float(target_volume),
                "current_volume": float(current_volume),
                "reason": str(exc),
            },
        )
        executor.logger.info(f"{symbol} {direction.value} {volume} 取整后数量为 0，跳过下单")
        return None

    # 补发「挂单」审计事件，供前端逐只动作流呈现「决策→挂单→追价→成交」中的挂单一步。
    # 无审计上下文时 emit_audit_event 自身静默跳过，故对无上下文的渠道零影响。
    client_order_id = order.extra.get("cl_ord_id") if isinstance(order.extra, dict) else None
    executor.emit_audit_event(
        event_type=ExecutionEventType.ORDER_SUBMITTED,
        status=ExecutionEventStatus.INFO,
        reason_family=ExecutionReasonFamily.EXECUTION_STRATEGY,
        reason_code="COMMON.ORDER_SUBMITTED",
        symbol=symbol,
        order_id=order.order_id,
        client_order_id=client_order_id if isinstance(client_order_id, str) else None,
        details={
            "order": {
                "direction": direction.value,
                "order_type": order_type.value,
                "volume": float(volume),
                "price": float(price),
            }
        },
    )

    position_side = kwargs.get("position_side")
    offset_flag = kwargs.get("offset_flag")
    tracker.add_order(
        order,
        direction=direction,
        target_volume=float(target_volume),
        current_volume=float(current_volume),
        position_side=position_side if isinstance(position_side, str) else None,
        offset_flag=offset_flag if isinstance(offset_flag, str) else None,
        submission_deadline=deadline,
    )

    executor.logger.debug(f"订单已提交: {symbol} {direction.value} {volume} @{price}, 订单ID: {order.order_id}")

    return order


_OFFSET_SPLIT_MAX_LEGS = 4


def _resolve_order_param_model(executor: ExecutorProtocol) -> OrderParamModel:
    """
    归一化执行器声明的订单参数模型.

    Parameters
    ----------
    executor : ExecutorProtocol
        当前算法绑定的执行器会话。

    Returns
    -------
    OrderParamModel
        声明的模型；轻量替身未声明或取值非法时回退 ``UNKNOWN``,
        由调用方按"歧义即失败"处理。
    """
    raw = getattr(executor, "order_param_model", None)
    if isinstance(raw, OrderParamModel):
        return raw
    try:
        return OrderParamModel(str(raw)) if raw is not None else OrderParamModel.UNKNOWN
    except ValueError:
        return OrderParamModel.UNKNOWN


def _remaining_toward_target(direction: OrderDirection, target_volume: float, current_volume: float) -> float:
    """
    按下单方向计算距离目标还需推进的数量（恒为非负）.

    Parameters
    ----------
    direction : OrderDirection
        下单方向。
    target_volume : float
        目标净持仓。
    current_volume : float
        复核后的当前净持仓。

    Returns
    -------
    float
        仍需卖出的量（SELL）或仍需买入的量（BUY）；已越过目标时返回 0。
    """
    if direction == OrderDirection.SELL:
        return max(0.0, current_volume - target_volume)
    return max(0.0, target_volume - current_volume)


def _leg_chunks(executor: ExecutorProtocol, order_type: OrderType, volume: float) -> list[float]:
    """
    按渠道单笔数量上限切分拆单腿.

    Parameters
    ----------
    executor : ExecutorProtocol
        当前算法绑定的执行器会话。
    order_type : OrderType
        订单类型。
    volume : float
        本腿总量。

    Returns
    -------
    list[float]
        切分后的各段数量；渠道未提供边界、数量非整数手或不超过上限时
        原样返回单段。
    """
    if not float(volume).is_integer():
        return [volume]
    bounds_getter = getattr(executor, "get_order_volume_bounds", None)
    bounds = bounds_getter(order_type) if callable(bounds_getter) else None
    if not isinstance(bounds, tuple):
        return [volume]
    max_size = bounds[1]
    if max_size is None or volume <= max_size:
        return [volume]
    return [float(chunk) for chunk in split_order_volumes(int(volume), max_size, min_size=bounds[0])]


def _place_offset_leg(
    executor: ExecutorProtocol,
    tracker: OrderTracker,
    direction: OrderDirection,
    order_type: OrderType,
    volume: float,
    price: float,
    target_volume: float,
    current_volume: float,
    leg_kwargs: dict[str, object],
    deadline: float,
    account_assets: UnifiedAccountAssets,
) -> list[UnifiedOrder]:
    """
    提交单条拆单腿；超过渠道单笔上限时按上限分段顺序提交.

    Returns
    -------
    list[UnifiedOrder]
        本腿已提交的订单；某段被跳过（碎量/低于下限）时返回已提交的前段,
        调用方据此停止拆单,不补反向腿。
    """
    orders: list[UnifiedOrder] = []
    planner = getattr(executor, "plan_close_orders", None)
    plan: list[tuple[float, dict[str, object]]] = [(volume, {})]
    if leg_kwargs.get("position_side") and callable(planner) and "offset_flag" not in leg_kwargs:
        plan = cast("list[tuple[float, dict[str, object]]]", planner(direction, volume, account_assets))
    chunks = [(chunk, extra) for quantity, extra in plan for chunk in _leg_chunks(executor, order_type, quantity)]
    for chunk, extra in chunks:
        order = submit_and_track_order(
            executor,
            tracker,
            direction,
            order_type,
            chunk,
            price,
            target_volume=target_volume,
            current_volume=current_volume,
            deadline=deadline,
            **{**leg_kwargs, **extra},
        )
        if order is None:
            break
        orders.append(order)
    return orders


def _submit_close_open_orders(
    executor: ExecutorProtocol,
    tracker: OrderTracker,
    direction: OrderDirection,
    order_type: OrderType,
    volume: float,
    price: float,
    target_volume: float,
    current_volume: float,
    account_assets: UnifiedAccountAssets,
    deadline: float,
    kwargs: dict[str, object],
) -> list[UnifiedOrder]:
    """
    按持仓侧或开平标志先平后开，并遵守本次调用的数量额度.

    Notes
    -----
    每张期货订单只能携带一种开平属性,穿零调仓必须拆成"平仓腿 + 开仓腿"。
    逐腿门控:每腿成交后复核净持仓再决定下一腿,平仓腿被跳过或失败时绝不
    补开仓,避免部分平仓后反向开仓形成多空锁仓(issue #53)。
    """
    symbol = executor.symbol
    orders: list[UnifiedOrder] = []
    remaining = float(volume)
    # 分腿只能推进本次调用的额度，不能把 TWAP/POV 的整体目标当作本片目标。
    slice_target = current_volume + (volume if direction == OrderDirection.BUY else -volume)
    clock = get_default_clock()
    for leg_index in range(_OFFSET_SPLIT_MAX_LEGS):
        if remaining <= 0 or clock.time() >= deadline:
            break
        tracker.assert_ready_for_submission()
        intent = determine_close_intent(executor, direction, account_assets)
        leg_volume = min(remaining, intent.opposite_volume) if intent.opposite_volume > 0 else remaining
        leg_orders = _place_offset_leg(
            executor,
            tracker,
            direction,
            order_type,
            leg_volume,
            price,
            target_volume,
            current_volume,
            {**kwargs, **intent.kwargs},
            deadline,
            account_assets,
        )
        if not leg_orders:
            executor.logger.info(f"{symbol} 拆单第 {leg_index + 1} 腿无可执行量,停止拆单")
            break
        orders.extend(leg_orders)
        # 超时撤单仅表示请求已发出；绝不能据此重算余量并提交替代单。
        if not tracker.wait_for_completion(timeout=max(0.0, deadline - clock.time())):
            break
        account_assets = executor.get_account_assets()
        current_volume = executor.get_current_volume(account_assets)
        remaining = min(
            _remaining_toward_target(direction, target_volume, current_volume),
            _remaining_toward_target(direction, slice_target, current_volume),
        )
    else:
        executor.logger.warning(f"{symbol} 拆单达到最大腿数仍未完成,剩余 {remaining}")
    return orders


def submit_and_track_split_orders(
    executor: ExecutorProtocol,
    tracker: OrderTracker,
    direction: OrderDirection,
    order_type: OrderType,
    volume: float,
    price: float,
    target_volume: float,
    current_volume: float,
    leg_timeout_seconds: float = 60.0,
    deadline: float | None = None,
    **kwargs: object,
) -> list[UnifiedOrder]:
    """
    按渠道订单参数模型提交订单，必要时先平后开拆单.

    Parameters
    ----------
    executor : ExecutorProtocol
        当前算法绑定的执行器会话。
    tracker : OrderTracker
        负责收敛订单与成交状态的跟踪器。
    direction : OrderDirection
        下单方向。
    order_type : OrderType
        订单类型。
    volume : float
        本步下单总量。
    price : float
        下单价格；市价单通常传 ``0``。
    target_volume : float
        本轮执行规划出的目标持仓量。
    current_volume : float
        本片下单规划依据的当前持仓量，用于锁定本片额度；实际下单前会在
        旧单终态确认后重新拉取账户快照并复核持仓。
    leg_timeout_seconds : float, default=60.0
        本次拆单共享的等待时间额度，后续腿仅使用剩余时间。
    deadline : float | None, default=None
        调用方提供的绝对截止时间，与外层等待共用；缺省时按时间额度计算。
    **kwargs : object
        额外的下单参数，例如 ``trade_rule``。

    Returns
    -------
    list[UnifiedOrder]
        已提交的订单列表；因名义价值/数量限制被跳过时返回空列表。

    Raises
    ------
    RuntimeError
        渠道未声明订单参数模型（UNKNOWN）时拒绝猜测下单语义。

    Notes
    -----
    DIRECTIONAL 模型直接提交单笔；POSITION_SIDE 模型保留平仓侧，
    穿零时与 OFFSET 模型一样由 :func:`_submit_close_open_orders`
    拆成平仓腿和开仓腿，逐腿复核持仓且不超过本片额度。
    """
    if deadline is None:
        deadline = get_default_clock().time() + leg_timeout_seconds
    model = _resolve_order_param_model(executor)
    if model is OrderParamModel.UNKNOWN:
        raise RuntimeError(f"渠道未声明订单参数模型,拒绝猜测下单语义: {executor.symbol}")
    tracker.assert_ready_for_submission()
    # 快照必须在终态确认之后读取：回调也可能先于门控到达，此时 pending 已为空。
    slice_target = current_volume + (volume if direction == OrderDirection.BUY else -volume)
    account_assets = executor.get_account_assets()
    current_volume = executor.get_current_volume(account_assets)
    volume = min(
        _remaining_toward_target(direction, target_volume, current_volume),
        _remaining_toward_target(direction, slice_target, current_volume),
    )
    if volume <= 0 or get_default_clock().time() >= deadline:
        return []
    if model is OrderParamModel.POSITION_SIDE:
        intent = determine_close_intent(executor, direction, account_assets)
        if 0 < intent.opposite_volume < volume:
            return _submit_close_open_orders(
                executor,
                tracker,
                direction,
                order_type,
                volume,
                price,
                target_volume,
                current_volume,
                account_assets,
                deadline,
                kwargs,
            )
        kwargs = {**kwargs, **intent.kwargs}
    if model in (OrderParamModel.POSITION_SIDE, OrderParamModel.DIRECTIONAL):
        order = submit_and_track_order(
            executor,
            tracker,
            direction,
            order_type,
            volume,
            price,
            target_volume=target_volume,
            current_volume=current_volume,
            deadline=deadline,
            **kwargs,
        )
        return [order] if order is not None else []
    return _submit_close_open_orders(
        executor,
        tracker,
        direction,
        order_type,
        volume,
        price,
        target_volume,
        current_volume,
        account_assets,
        deadline,
        kwargs,
    )


def execute_order_slice(
    executor: ExecutorProtocol,
    tracker: OrderTracker,
    direction: OrderDirection,
    slice_qty: float,
    price_strategy: str,
    current_volume: float,
    target_volume: float,
    fill_wait_seconds: float,
) -> dict[str, Any]:
    """
    执行单个切片：下单、等待成交、撤除未成交余量.

    Parameters
    ----------
    executor : ExecutorProtocol
        当前算法绑定的执行器会话。
    tracker : OrderTracker
        跨切片复用的订单跟踪器。
    direction : OrderDirection
        下单方向。
    slice_qty : float
        本片下单量。
    price_strategy : str
        本片报价策略，由算法显式传入。
    current_volume : float
        本片开始时的当前持仓量。
    target_volume : float
        整体目标持仓量。
    fill_wait_seconds : float
        本片等待成交的最长时间（秒）。

    Returns
    -------
    dict[str, Any]
        本片执行明细；下单失败时包含 ``error`` 字段。

    Notes
    -----
    片末主动撤除未成交余量，避免其在后续片间等待期间成交，破坏累计进度表的量核对。
    未成交余量由算法的后续切片按原计划补回。
    """
    order_type, price = determine_order_price(
        direction,
        executor.get_market_data(),
        price_strategy=price_strategy,
    )
    detail: dict[str, Any] = {
        "direction": direction.value,
        "volume": slice_qty,
        "order_type": order_type.value,
        "price": price,
    }

    deadline = get_default_clock().time() + fill_wait_seconds
    try:
        orders = submit_and_track_split_orders(
            executor,
            tracker,
            direction,
            order_type,
            slice_qty,
            price,
            target_volume=float(target_volume),
            current_volume=float(current_volume),
            leg_timeout_seconds=fill_wait_seconds,
            deadline=deadline,
        )
        if not orders:
            detail["skipped"] = "no_order_submitted"
            return detail
        detail["order_id"] = orders[0].order_id
        detail["order_ids"] = [order.order_id for order in orders]
        tracker.explicit_error = None
        tracker.explicit_blocked_error = None
    except MemoryError:
        executor.logger.exception(f"{executor.symbol} 下单遇到不可恢复异常")
        raise
    except ExecutionTerminated:
        raise
    except RECOVERABLE_ALGORITHM_EXCEPTIONS as exc:
        if getattr(exc, "requires_session_recovery", False):
            raise
        if isinstance(exc, AccountControlBlockedError):
            tracker.explicit_blocked_error = exc.execution_error
        else:
            tracker.explicit_error = execution_error_message(exc, "报单")
        error_message = format_exception_message(exc)
        executor.logger.error(f"{executor.symbol} 下单失败: {error_message}")
        detail["error"] = error_message
        return detail

    tracker.wait_for_completion(timeout=max(0.0, deadline - get_default_clock().time()))
    failed_cancels = cancel_pending_orders_via_query(executor)
    if failed_cancels:
        tracker.explicit_error = "撤单失败，订单终态尚未确认"
        detail["cancel_error"] = f"撤单失败: {failed_cancels}"
    return detail
