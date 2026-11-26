"""
实现统一的订单跟踪、追单与市价兜底逻辑。

该模块为算法层提供一个“单次执行内的订单状态收敛器”。它负责接住执行器
回调、维护待完成订单集合，并在必要时追加定时查询、追单或市价兜底。
模块职责止于把订单与成交状态收敛成稳定结果，不负责决定目标持仓或交易
策略本身。

Notes
-----
执行器回调与等待线程可能并发访问同一批订单状态，因此实现明确依赖内部锁
来保护 ``pending_orders``、``completed_orders`` 和早到事件缓冲区。不同
渠道在订单主键上的命名并不完全一致，所以跟踪器会同时利用本地订单号、
客户端订单号与交易所订单号做归并。
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field, is_dataclass
from decimal import Decimal
from math import isclose, isfinite
from typing import Any, cast

from axile.common.logging import LogComponent, bind_log_context
from axile.common.serialization import copy_data
from axile.domain.execution import ExecutionEventStatus, ExecutionEventType, ExecutionReasonFamily
from axile.executor.account_control.exceptions import AccountControlBlockedError
from axile.executor.algorithms.core.base import ExecutorProtocol
from axile.executor.algorithms.exceptions import (
    RECOVERABLE_ALGORITHM_EXCEPTIONS,
    execution_error_message,
    format_exception_message,
)
from axile.executor.algorithms.utils.clock import Clock, get_default_clock
from axile.executor.algorithms.utils.final_position import save_order_changes
from axile.executor.algorithms.utils.outcome import confirmed_position, order_with_trade_fill
from axile.executor.algorithms.utils.trading import terminal_order_reconciler
from axile.executor.constants.order_status import OrderStatus
from axile.executor.market_data_validation import (
    DEFAULT_QUOTE_MAX_AGE_SECONDS,
    MarketDataUnavailableError,
    market_data_error,
)
from axile.executor.models.order_channel_health import OrderChannelHealth
from axile.executor.models.unified_order import OrderDirection, OrderType, TradeRecord, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData, clone_price_data
from axile.executor.order_cancellation import (
    CancellationResult,
    CancelNotAllowedError,
    CancelRequestStatus,
    OrderCancellation,
    cancel_orders,
    reconcile_cancellation,
)
from axile.executor.order_volume_limits import split_order_volumes
from axile.executor.termination import TERMINATION_TRIGGER_TIMEOUT, ExecutionTerminated

MARKET_FALLBACK_MAX_SLIPPAGE = 0.005


def _chase_price(quote: UnifiedPriceData | None, direction: OrderDirection) -> float:
    """买单跟买一，卖单跟卖一；缺少有效报价时跳过追单。"""
    if quote is None:
        return 0.0
    levels = quote.bids if direction == OrderDirection.BUY else quote.asks
    price = levels[0][0] if levels else 0.0
    return price if isfinite(price) and price > 0 else 0.0


class _OrderTerminalUnconfirmedError(RuntimeError):
    """停止后续报单，未确认原因由逐单状态维护。"""

    def __init__(self, unconfirmed: dict[str, str]) -> None:
        super().__init__(f"订单终态尚未确认: {unconfirmed}")
        self.execution_error = "; ".join(dict.fromkeys(unconfirmed.values()))


def _coerce_order_direction(direction: object) -> OrderDirection | None:
    """把订单上的方向字段归一成 OrderDirection。"""
    if direction is None or direction == "":
        return None
    if isinstance(direction, OrderDirection):
        return direction
    try:
        return OrderDirection(direction)
    except (TypeError, ValueError):
        return None


def _chase_place_kwargs(chase_info: dict[str, Any]) -> dict[str, Any]:
    """从追价上下文恢复换单时必须保留的开平与交易规则。"""
    kwargs: dict[str, Any] = {}
    position_side = chase_info.get("position_side")
    if position_side:
        kwargs["position_side"] = position_side
    offset_flag = chase_info.get("offset_flag")
    if offset_flag is not None and offset_flag != "":
        kwargs["offset_flag"] = offset_flag
    trade_rule = chase_info.get("trade_rule")
    if trade_rule is not None:
        kwargs["trade_rule"] = trade_rule
    return kwargs


def _summarize_trade_records(trades: list[TradeRecord]) -> tuple[float, float]:
    """根据成交列表计算累计成交量和成交均价."""
    if not trades:
        return 0.0, 0.0

    total_volume = sum(trade.trade_volume for trade in trades)
    total_value = sum(trade.trade_price * trade.trade_volume for trade in trades)
    avg_price = total_value / total_volume if total_volume > 0 else 0.0
    return total_volume, avg_price


@dataclass
class ChaseConfig:
    """
    描述单笔订单的追单策略配置。

    Attributes
    ----------
    enabled : bool
        是否启用追单。
    ticks : int
        盘口偏离多少个 tick 后触发追单。
    max_count : int
        单笔订单允许的最大追单次数。
    interval : float
        两次追单之间的最小间隔，单位为秒。
    cancel_confirm_timeout : float
        追价换单前等待旧单撤单生效的最长秒数。超时则放弃本次换单（保留旧单），
        绝不在未确认的情况下发出新单——宁可少挂一次，也不能新旧单同时在场。
    market_fallback_enabled : bool
        达到追单次数上限后是否允许市价兜底。
    """

    enabled: bool = False
    ticks: int = 1
    max_count: int = 5
    interval: float = 5.0
    cancel_confirm_timeout: float = 3.0
    market_fallback_enabled: bool = True

    @classmethod
    def from_params(cls, params: object) -> "ChaseConfig | None":
        """
        从算法参数模型提取追单配置。

        Parameters
        ----------
        params : BaseAlgorithmParams
            算法参数的 dataclass 模型实例。

        Returns
        -------
        ChaseConfig | None
            启用了追单时返回标准化配置，否则返回 ``None``。

        Raises
        ------
        TypeError
            传入对象不是 dataclass 模型实例时抛出。
        """
        if not is_dataclass(params):
            raise TypeError("ChaseConfig.from_params 只接受 dataclass 参数模型")

        chase_enabled = getattr(params, "chase_enabled", False)
        if not chase_enabled:
            return None

        return cls(
            enabled=True,
            ticks=getattr(params, "chase_ticks", 1),
            max_count=getattr(params, "max_chase_count", 5),
            interval=getattr(params, "chase_interval", 5.0),
            market_fallback_enabled=getattr(params, "market_fallback_enabled", True),
        )


@dataclass
class OrderTracker:
    """
    在一次算法执行内收敛订单、成交与追单状态。

    使用事件驱动等待策略，必要时退化为主动查询和市价单兜底。一个
    ``OrderTracker`` 只服务于一个执行上下文；调用方应在执行结束后释放
    对应回调，避免旧 tracker 继续消费新一轮订单事件。

    Attributes
    ----------
    executor : ExecutorProtocol
        执行器实例。
    chase_config : ChaseConfig | None
        追单配置；为空时只做状态跟踪，不做自动换单。
    clock : Clock
        时钟实例；仿真模式下会自动推进仿真时间。
    order_refresh_interval : float
        主动查询挂单状态的时间间隔，单位为秒；非正值表示禁用。

    Notes
    -----
    跟踪器把 ``pending_orders`` 中的键视为内部稳定主键。即便换单后交易所返回
    了新的订单号，旧单和新单的生命周期也要先在这里完成迁移，再允许等待线程
    观察到“没有待完成订单”。

    阅读主线建议：

    1. `add_order()` 注册待跟踪订单。
    2. `on_order_update()` / `on_trade_record()` 持续收敛订单与成交状态。
    3. `wait_for_completion()` 驱动等待流程。
    4. `_check_and_chase()` 在必要时执行追单或市价兜底。
    """

    executor: ExecutorProtocol
    chase_config: ChaseConfig | None = None
    clock: Clock = field(default_factory=get_default_clock)
    order_refresh_interval: float = 30.0
    # 盘口新鲜度兜底：WS 深度流静默失效时 latest_prices 会冻住，据此挂/改被动价可能越过
    # 点差吃成 taker（REST 对账只兜底订单终态、不兜底盘口）。追价读价前若快照年龄超过
    # price_stale_after 秒，则刷新行情快照，并按 rest_price_refresh_interval 限频。
    price_stale_after: float = DEFAULT_QUOTE_MAX_AGE_SECONDS
    rest_price_refresh_interval: float = 1.0
    start_volume: float | None = None
    _checkpoint_failed: bool = field(default=False, init=False)
    _checkpoint_started: bool = field(default=False, init=False)
    _checkpoint_updates: dict[str, UnifiedOrder] = field(default_factory=dict, init=False, repr=False)
    _checkpoint_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _explicit_error: str | None = field(default=None, init=False)
    _order_errors: dict[str, str] = field(default_factory=dict, init=False)
    _cancel_restrictions: dict[str, str] = field(default_factory=dict, init=False)
    _explicit_blocked_error: str | None = field(default=None, init=False)
    _error_reason: tuple[str | None, ExecutionReasonFamily | None] = field(default=(None, None), init=False)
    _blocked_reason: tuple[str | None, ExecutionReasonFamily | None] = field(default=(None, None), init=False)
    _order_error_reasons: dict[str, tuple[str | None, ExecutionReasonFamily | None]] = field(
        default_factory=dict, init=False
    )

    pending_orders: dict[str, UnifiedOrder] = field(default_factory=dict)
    completed_orders: dict[str, UnifiedOrder] = field(default_factory=dict)
    order_trades: dict[str, list[TradeRecord]] = field(default_factory=dict)
    all_done_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    latest_prices: dict[str, UnifiedPriceData] = field(default_factory=dict)
    _last_rest_price_refresh: dict[str, float] = field(default_factory=dict, init=False)
    _chase_info: dict[str, dict[str, Any]] = field(default_factory=dict)
    _chasing_order_id: str | None = field(default=None, init=False)
    _fallback_submitting: set[str] = field(default_factory=set, init=False)
    _fallback_started: set[str] = field(default_factory=set, init=False)
    _early_order_updates: dict[str, list[UnifiedOrder]] = field(default_factory=dict)
    _early_trade_records: dict[str, list[TradeRecord]] = field(default_factory=dict)
    _logger: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """绑定订单组件，同时保留执行会话已有的账户与品种上下文."""
        self._logger = bind_log_context(self.executor.logger, component=LogComponent.ORDER)

    @property
    def explicit_error(self) -> str | None:
        """返回执行级错误或尚未恢复的订单错误，不读取调试信息。"""
        with self.lock:
            return self._explicit_error or next(iter(self._order_errors.values()), None)

    @explicit_error.setter
    def explicit_error(self, message: str | None) -> None:
        """执行级重试仅更新自身错误，不能清除其他订单的失败。"""
        with self.lock:
            self._explicit_error = message
            self._error_reason = (None, None)

    @property
    def explicit_blocked_error(self) -> str | None:
        """返回执行级受阻说明，与 explicit_error 共用同一把锁。"""
        with self.lock:
            return self._explicit_blocked_error

    @explicit_blocked_error.setter
    def explicit_blocked_error(self, message: str | None) -> None:
        """替换受阻说明时清除旧原因，不能清除其他失败。"""
        with self.lock:
            self._explicit_blocked_error = message
            self._blocked_reason = (None, None)

    def record_exception(self, exc: BaseException, operation: str = "执行", *, order_id: str | None = None) -> None:
        """在捕获处保留异常含义；换单失败始终归属原订单。"""
        if isinstance(exc, _OrderTerminalUnconfirmedError):
            return
        reason_code, reason_family = None, None
        if isinstance(exc, MarketDataUnavailableError):
            reason_code, reason_family = exc.reason_code, ExecutionReasonFamily.MARKET_RULE
        elif isinstance(exc, AccountControlBlockedError):
            reason_code, reason_family = exc.reason_code, ExecutionReasonFamily.ACCOUNT_STATE
        message = execution_error_message(exc, operation)
        with self.lock:
            if order_id is not None:
                self._order_errors[order_id] = message
                self._order_error_reasons[order_id] = (reason_code, reason_family)
            elif reason_family is not None:
                self._explicit_blocked_error = message
                self._blocked_reason = (reason_code, reason_family)
            else:
                self._explicit_error = message
                self._error_reason = (None, None)

    def outcome_inputs(self) -> dict[str, Any]:
        """持锁选出同一错误的说明与原因，供 summarize_outcome 一次判定。"""
        with self.lock:
            error = self._explicit_error
            reason = self._error_reason
            if not error and self._order_errors:
                order_id = next(iter(self._order_errors))
                error = self._order_errors[order_id]
                reason = self._order_error_reasons.get(order_id, (None, None))
            if not error:
                error = self._pending_reason()
                reason = (None, None)
            return {
                "explicit_error": error,
                "error_reason_code": reason[0],
                "error_reason_family": reason[1],
                "explicit_blocked_error": self._explicit_blocked_error,
                "blocked_reason_code": self._blocked_reason[0],
                "blocked_reason_family": self._blocked_reason[1],
            }

    def _set_order_error(self, order_id: str, message: str | None) -> None:
        """按原订单身份记录或清除本次换单错误。"""
        with self.lock:
            self._order_error_reasons.pop(order_id, None)
            if message is None:
                self._order_errors.pop(order_id, None)
            else:
                self._order_errors[order_id] = message

    def add_order(
        self,
        order: UnifiedOrder,
        direction: OrderDirection | None = None,
        target_volume: float = 0,
        current_volume: float = 0,
        position_side: str | None = None,
        offset_flag: str | None = None,
        trade_rule: dict[str, Any] | None = None,
        chase_enabled: bool = True,
        submission_deadline: float | None = None,
    ) -> None:
        """
        将新订单纳入当前跟踪器。

        Parameters
        ----------
        order : UnifiedOrder
            需要开始跟踪的订单对象。
        direction : OrderDirection | None, default=None
            订单方向；启用追单时会写入追单元数据。缺省时回退到 ``order.direction``。
        target_volume : float, default=0
            本轮执行规划的目标持仓量。
        current_volume : float, default=0
            提交该订单前的当前持仓量。
        position_side : str | None, default=None
            双向持仓模式下的持仓方向附加参数。
        offset_flag : str | None, default=None
            原始开平标志；缺省时回退到 ``order.extra["offset_flag"]``。
        trade_rule : dict[str, Any] | None, default=None
            原始交易规则；换单时必须原样带回，避免平仓变开仓或丢失单笔上限。
        chase_enabled : bool, default=True
            是否为本订单登记追单状态；市价兜底子单关闭追单。
        submission_deadline : float | None, default=None
            本单追价和市价兜底的绝对截止时间，换单后继续沿用。
        """
        resolved_direction = direction or _coerce_order_direction(order.direction)
        resolved_offset = offset_flag
        if resolved_offset is None and isinstance(order.extra, dict):
            raw_offset = order.extra.get("offset_flag")
            resolved_offset = None if raw_offset is None else str(raw_offset)
        chase_info = None
        if chase_enabled and self.chase_config and resolved_direction:
            chase_info = {
                "symbol": order.symbol,
                "direction": resolved_direction,
                "target_volume": target_volume,
                "current_volume": current_volume,
                "original_price": order.price,
                "chase_count": 0,
                "last_chase_time": 0.0,
                "position_side": position_side,
                "offset_flag": resolved_offset,
                "trade_rule": dict(trade_rule) if trade_rule is not None else None,
                "submission_deadline": submission_deadline,
            }

        self._register_order(order, chase_info)

    def _register_order(self, order: UnifiedOrder, chase_info: dict[str, Any] | None) -> None:
        """登记受理、吸收早到回报，并在下一次渠道调用前保存本批证据。"""
        with self.lock:
            self.pending_orders[order.order_id] = order
            self._checkpoint_updates[order.order_id] = order
            self.order_trades.setdefault(order.order_id, [])
            if chase_info is not None:
                self._chase_info[order.order_id] = chase_info
            self.all_done_event.clear()
            buffered_trades, buffered_order_updates = self._pop_early_events(order)

        # 缓冲事件的回放必须放在锁外执行。对应回调内部还会再次尝试获取同一把锁，
        # 如果在锁内直接重放，就会把“注册订单 -> 吸收早到回调”这条补偿路径卡死。
        for trade in buffered_trades:
            self.on_trade_record(trade)

        for buffered_order in buffered_order_updates:
            self.on_order_update(buffered_order)

        if order.is_completed():
            self.on_order_update(order)
        if not self._save_checkpoint():
            self._finalize_timeout_cancellations(known_only=True)
            raise RuntimeError("待核对仓位保存失败")

        # 下单即探针：未收到期待的回报时报告通道可疑，由账户恢复核对。
        self._register_order_ack_probe(order.order_id)

    def on_order_update(self, order: UnifiedOrder) -> None:
        """收敛订单状态回调并驱动完成/兜底逻辑。"""
        fallback_submission: tuple[UnifiedOrder, dict[str, Any]] | None = None

        with self.lock:
            tracked_order_id = self._resolve_order_target_id(order)
            if tracked_order_id is None:
                # 迟到的终态可以上修累计成交，但不能把已结束的委托恢复成挂单。
                completed_id = next(
                    (key for key in self._extract_order_candidate_ids(order) if key in self.completed_orders), None
                )
                if completed_id is None:
                    self._buffer_early_order_update(order)
                elif order.is_completed() and order.filled_volume > self.completed_orders[completed_id].filled_volume:
                    updated = self._merge_order_update(completed_id, self.completed_orders[completed_id], order)
                    updated.order_id = completed_id
                    self.completed_orders[completed_id] = self._checkpoint_updates[completed_id] = updated
                return

            tracked_order = self.pending_orders[tracked_order_id]
            chase_info = self._chase_info.get(tracked_order_id)
            fallback_pending_cancel = bool(chase_info and chase_info.get("market_order_fallback_pending_cancel"))

            order = self._merge_order_update(tracked_order_id, tracked_order, order)
            order.order_id = tracked_order.order_id
            self.pending_orders[tracked_order_id] = order
            self._checkpoint_updates[order.order_id] = order

            if order.is_completed():
                self.completed_orders[tracked_order_id] = order
                del self.pending_orders[tracked_order_id]
                for candidate_id in self._extract_order_candidate_ids(order):
                    self._order_errors.pop(candidate_id, None)
                    self._order_error_reasons.pop(candidate_id, None)
                    self._cancel_restrictions.pop(candidate_id, None)

                # 追单中的撤单换单会先收到旧单终态，再写入新单。只有明确进入
                # market fallback 的路径才允许在这里带着 chase_info 继续往后走，
                # 否则旧单终态会把新单仍需要的追单上下文提前清掉。
                if fallback_pending_cancel and chase_info is not None:
                    remaining_volume = self._get_effective_remaining_volume(tracked_order_id, order)
                    if remaining_volume > 0:
                        fallback_submission = (order, dict(chase_info))
                        self._fallback_submitting.add(tracked_order_id)
                    if tracked_order_id in self._chase_info:
                        del self._chase_info[tracked_order_id]
                elif tracked_order_id in self._chase_info and tracked_order_id != self._chasing_order_id:
                    del self._chase_info[tracked_order_id]

                # all_done_event 只能在“没有 pending 且没有换单正在路上”的状态下置位，
                # 否则等待线程可能在旧单已撤、新单未建的瞬间误以为全部完成。
                if (
                    not self.pending_orders
                    and self._chasing_order_id is None
                    and fallback_submission is None
                    and not self._fallback_submitting
                ):
                    self.all_done_event.set()

        if fallback_submission is not None:
            completed_order, fallback_info = fallback_submission
            self._submit_market_fallback_order(completed_order, fallback_info)

    def on_trade_record(self, trade: TradeRecord) -> None:
        """成交回调，用于增量更新已跟踪订单的成交进度."""
        with self.lock:
            resolved_entry = self._resolve_trade_target_order_entry(trade)
            if resolved_entry is None:
                self._buffer_early_trade_record(trade)
                return
            tracked_order_id, tracked_order = resolved_entry
            if self._order_has_trade(tracked_order_id, trade):
                return

            tracked_trades = self.order_trades.setdefault(tracked_order_id, [])
            tracked_trades.append(copy_data(trade, deep=True, update={"order_id": tracked_order_id}))
            evidence = self._position_order(tracked_order)
            if evidence is not tracked_order:
                # 新对象可区分写库期间到达的成交，已提交的旧批次不能将其清除。
                self._checkpoint_updates[tracked_order_id] = evidence

    def on_price_update(self, price_data: UnifiedPriceData) -> None:
        """接收最新盘口快照，供追单和市价兜底保护使用。"""
        with self.lock:
            self.latest_prices[price_data.symbol] = price_data

    def _price_is_fresh(self, price_data: UnifiedPriceData, now: float) -> bool:
        """判断盘口快照是否在新鲜度阈值内.

        Parameters
        ----------
        price_data : UnifiedPriceData
            待判定的盘口快照。
        now : float
            当前时钟时间（秒），来自 ``self.clock.time()``。

        Returns
        -------
        bool
            快照带有效时间戳且年龄不超过 ``price_stale_after`` 秒时返回 ``True``。
        """
        validator = getattr(self.executor, "market_data_error", None)
        if callable(validator):
            return validator(price_data, now=now) is None
        return market_data_error(price_data, now=now, max_age=self.price_stale_after, purpose="order") is None

    def _rest_refresh_price(self, symbol: str, now: float) -> UnifiedPriceData | None:
        """盘口陈旧时刷新行情快照（限频、仅当前品种）.

        Parameters
        ----------
        symbol : str
            需要刷新的交易对代码。
        now : float
            当前时钟时间（秒）。

        Returns
        -------
        UnifiedPriceData | None
            成功且有效则返回新快照并写回 ``latest_prices``；限频命中、非当前品种、
            行情快照刷新失败或快照无效时返回 ``None``。

        Notes
        -----
        ``executor.get_market_data()`` 是 symbol 级代理（只取 ``executor.symbol``），
        故仅对当前品种刷新行情快照；其余品种保持既有 WS 快照。按
        ``rest_price_refresh_interval`` 限频，避免深度持续断流时每轮追价都刷新行情快照。
        """
        if symbol != getattr(self.executor, "symbol", None):
            return None
        last = self._last_rest_price_refresh.get(symbol, 0.0)
        if now - last < self.rest_price_refresh_interval:
            return None
        self._last_rest_price_refresh[symbol] = now
        try:
            refreshed = self.executor.get_market_data()
        except RECOVERABLE_ALGORITHM_EXCEPTIONS as exc:
            if isinstance(exc, (MemoryError, ExecutionTerminated)) or getattr(exc, "requires_session_recovery", False):
                raise
            self._logger.warning(f"盘口陈旧，行情快照刷新 {symbol} 失败: {format_exception_message(exc)}")
            return None
        if refreshed is None or not refreshed.is_valid() or not self._price_is_fresh(refreshed, self.clock.time()):
            return None
        self._logger.info(f"盘口陈旧超 {self.price_stale_after}s，已刷新行情快照 {symbol}")
        return refreshed

    def _usable_price(self, symbol: str) -> UnifiedPriceData | None:
        """取用于追价的可用盘口：新鲜则直接用，陈旧则刷新行情快照.

        Parameters
        ----------
        symbol : str
            交易对代码。

        Returns
        -------
        UnifiedPriceData | None
            新鲜的 WS 快照或刷新后的行情快照；两者皆不可得时返回 ``None``。
        """
        price = self.latest_prices.get(symbol)
        now = self.clock.time()
        if price is not None and self._price_is_fresh(price, now):
            return price
        refreshed = self._rest_refresh_price(symbol, now)
        if refreshed is not None:
            self.latest_prices[symbol] = refreshed
            return refreshed
        return None

    def _pending_chase_symbols(self) -> list[str]:
        """快照当前待追单订单涉及的品种（去重保序）.

        Returns
        -------
        list[str]
            供锁外预刷新盘口使用的品种列表；仅短暂持锁读取，不做任何阻塞调用。
        """
        with self.lock:
            symbols = [
                self._chase_info[order_id]["symbol"] for order_id in self.pending_orders if order_id in self._chase_info
            ]
        seen: set[str] = set()
        unique: list[str] = []
        for symbol in symbols:
            if symbol not in seen:
                seen.add(symbol)
                unique.append(symbol)
        return unique

    def assert_ready_for_submission(self) -> None:
        """先对账旧单，再拒绝仍未确认终态的下单、追单或兜底。"""
        if self._checkpoint_failed:
            raise RuntimeError("待核对仓位保存失败")
        # 查询可能触发订单回调，必须在锁外执行；缺失终态回调时依靠 REST 恢复。
        self._query_pending_orders()
        self.require_saved_orders()
        with self.lock:
            if self.pending_orders or self._chasing_order_id is not None or self._fallback_submitting:
                raise RuntimeError("旧单尚未确认终态，拒绝继续下单")

    def _restrict_submission_deadlines(self, deadline: float) -> None:
        """等待只能缩短已登记订单的提交期限，不能给旧单续期。"""
        with self.lock:
            for info in self._chase_info.values():
                existing = info.get("submission_deadline")
                info["submission_deadline"] = min(existing, deadline) if existing is not None else deadline

    def _submission_expired(self, chase_info: dict[str, Any]) -> bool:
        """判断追价或兜底是否已过提交期限；缺省不额外限制。"""
        deadline = chase_info.get("submission_deadline")
        return self._checkpoint_failed or (deadline is not None and self.clock.time() >= deadline)

    def wait_for_completion(self, timeout: float = 120, *, settle_on_timeout: bool = False) -> bool:
        """等待成交，按需在成交超时后继续确认撤单终态。"""
        try:
            return self._wait_callback(timeout, settle_on_timeout=settle_on_timeout)
        finally:
            self._save_checkpoint(new_terminals_only=True)

    def _wait_callback(self, timeout: float, *, settle_on_timeout: bool) -> bool:
        """
        以事件驱动为主、主动查询为辅地等待订单完成。

        Parameters
        ----------
        timeout : float
            成交等待上限，单位为秒。
        settle_on_timeout : bool
            超时撤单后是否继续确认终态，确认受整轮执行截止时间约束。

        Returns
        -------
        bool
            所有订单在超时前完成时返回 ``True``，否则返回 ``False``。
        """
        # 无待完成订单（例如本轮所有品种都被跳过 / 0 挂单，或订单已在调用前完成）：直接判定完成。
        # all_done_event 仅由订单回调在 pending 归零时置位，0 单场景该回调永不触发；若照常进入
        # 等待循环会空转到 timeout（最长 max_wait_seconds，可达 1 小时）才返回，令「已到位」的调仓挂起。
        with self.lock:
            nothing_to_wait = (
                not self.pending_orders and self._chasing_order_id is None and not self._fallback_submitting
            )
        if nothing_to_wait:
            self._logger.info("无待完成订单（全部跳过或无挂单），直接结束等待")
            return True

        start_time = self.clock.time()
        self._restrict_submission_deadlines(start_time + timeout)
        check_interval = 1.0
        fallback_threshold = 30.0  # 超时前30秒开始使用市价单兜底
        fallback_triggered = False
        last_pending_count = self.get_pending_count()
        effective_query_interval = self._current_query_interval(timeout, check_interval)
        last_query_elapsed = 0.0

        query_interval_desc = "disabled" if effective_query_interval == float("inf") else f"{effective_query_interval}s"
        self._logger.info(
            f"开始等待订单完成: pending={last_pending_count}, "
            f"timeout={timeout}s, chase={self.chase_config is not None}, "
            f"query_interval={query_interval_desc}"
        )

        while self.clock.time() - start_time < timeout:
            self.executor.handle_termination_checkpoint()
            elapsed = self.clock.time() - start_time
            remaining_time = timeout - elapsed

            if remaining_time <= 0:
                break
            if self.clock.event_wait(self.all_done_event, min(check_interval, remaining_time)):
                summary = self.get_status_summary()
                self._logger.info(
                    f"订单等待结束: result=completed, elapsed={elapsed:.1f}s, {self._format_status_summary(summary)}"
                )
                return True
            self.executor.handle_termination_checkpoint()

            if self.clock.time() - start_time >= timeout:
                break

            if self.chase_config:
                self._check_and_chase()

            elapsed = self.clock.time() - start_time

            if pending_count := self.get_pending_count():
                # 常驻账户依赖回报；没有账户内存簿的独立执行器沿用显式刷新间隔。
                current_query_interval = self._current_query_interval(timeout, check_interval)
                if current_query_interval != float("inf") and elapsed - last_query_elapsed >= current_query_interval:
                    self._query_pending_orders()
                    last_query_elapsed = elapsed

            # 市价兜底只在真正接近超时时触发，避免一开始就把“追单但仍想控制价格”
            # 的策略退化成无保护的立即扫单。
            remaining_time = timeout - (self.clock.time() - start_time)
            if (
                not fallback_triggered
                and 0 < remaining_time <= fallback_threshold
                and self.chase_config
                and self.chase_config.market_fallback_enabled
            ):
                fallback_triggered = True
                self._logger.info(f"距离超时还有 {remaining_time:.1f} 秒，对达到最大追单次数的订单使用市价单兜底")
                self.executor.handle_termination_checkpoint()
                self._fallback_to_market_order()

            pending_count = self.get_pending_count()
            if pending_count != last_pending_count:
                summary = self.get_status_summary()
                self._logger.debug(
                    f"订单进度变化: pending={last_pending_count}->{pending_count}, "
                    f"elapsed={elapsed:.1f}s, {self._format_status_summary(summary)}"
                )
                last_pending_count = pending_count

        summary = self.get_status_summary()
        self._logger.warning(f"等待订单完成超时: elapsed={timeout:.1f}s, {self._format_status_summary(summary)}")
        saved = self._save_checkpoint(refresh_pending=True)
        self._logger.info(f"正在撤销所有未成交订单: pending={self.get_pending_count()}")
        self.executor.handle_termination_checkpoint()
        self._finalize_timeout_cancellations(
            known_only=not saved, settlement_check_interval=check_interval if settle_on_timeout else None
        )
        return False

    def _save_checkpoint(self, *, new_terminals_only: bool = False, refresh_pending: bool = False) -> bool:
        start = self.start_volume
        if start is None or not isfinite(start):
            return True
        # 同一跟踪器串行写入，避免旧回报覆盖新状态。
        with self._checkpoint_lock:
            with self.lock:
                if refresh_pending:
                    self._checkpoint_updates.update((order.order_id, order) for order in self.pending_orders.values())
                updates = [
                    order
                    for order in self._checkpoint_updates.values()
                    if not new_terminals_only or order.is_completed()
                ]
                for order in updates:
                    if not order.is_completed():
                        order.extra["terminal_confirmation_pending"] = True
                evidence = [self._position_order(order) for order in updates]
            if not (
                getattr(self.executor, "has_audit_sink", False) is True
                and self.executor.audit_context.get("execution_id")
            ):
                with self.lock:
                    self._checkpoint_updates.clear()
                return True
            if not updates:
                return True
            if not save_order_changes(self.executor, evidence, None if self._checkpoint_started else start):
                self._checkpoint_failed = True
                self.explicit_error = "待核对仓位保存失败"
                return False
            self._checkpoint_started = True
            with self.lock:
                for order in updates:
                    if self._checkpoint_updates.get(order.order_id) is order:
                        del self._checkpoint_updates[order.order_id]
            return True

    def require_saved_orders(self) -> None:
        """每笔报单前保存已收到的回报。写入失败后，本阶段只允许收尾。"""
        if not self._save_checkpoint() or self._checkpoint_failed:
            raise RuntimeError("待核对仓位保存失败")

    def _finalize_timeout_cancellations(
        self, *, known_only: bool = False, settlement_check_interval: float | None = None
    ) -> None:
        """超时后核对订单终态，收尾期间不再报单。"""
        cancellation = CancellationResult()
        remaining = None
        if settlement_check_interval is not None:
            remaining = cast(
                "Callable[[], float | None] | None", getattr(self.executor, "deadline_remaining_seconds", None)
            )
        reconcile = terminal_order_reconciler(self.executor, remaining)
        try:
            orders = self._pending_for_cancellation(known_only, remaining)
            cancel_orders(
                orders,
                cancel=lambda _symbol, order_id: self.executor.cancel_order(order_id),
                reconcile=reconcile,
                on_terminal=self.record_terminal_order,
                result=cancellation,
                before_cancel=self.executor.handle_termination_checkpoint if remaining is not None else None,
            )
            self._reconcile_timeout_cancellations(cancellation, reconcile)
            if remaining is not None:
                self.executor.handle_termination_checkpoint()
            unconfirmed = self.record_cancellation_result(cancellation)
            if unconfirmed and settlement_check_interval is not None and remaining is not None and not known_only:
                self._wait_for_cancellation_terminals(settlement_check_interval, remaining, reconcile, cancellation)
                unconfirmed = self.record_cancellation_result(cancellation)
        except ExecutionTerminated as exc:
            if settlement_check_interval is not None:
                self._attach_cancellation_evidence(exc, cancellation)
            raise
        for order_id, reason in unconfirmed.items():
            self.executor.emit_audit_event(
                event_type=ExecutionEventType.ORDER_TERMINAL_PENDING,
                status=ExecutionEventStatus.WARNING,
                reason_family=ExecutionReasonFamily.ACCOUNT_STATE,
                reason_code="COMMON.ORDER.TERMINAL_UNCONFIRMED",
                symbol=self.executor.symbol,
                order_id=order_id,
                details={"message": reason, "order": {"status": "终态待确认"}},
            )
        if unconfirmed and not self.waiting_on_restriction():
            raise _OrderTerminalUnconfirmedError(unconfirmed)

    def _pending_for_cancellation(
        self, known_only: bool, remaining: Callable[[], float | None] | None
    ) -> list[UnifiedOrder]:
        if not known_only:
            timeout = remaining() if remaining is not None else None
            if timeout is None:
                return self.executor.get_pending_orders()
            query_pending = cast("Callable[..., list[UnifiedOrder]]", self.executor.get_pending_orders)
            try:
                return query_pending(timeout=timeout)
            except TimeoutError:
                self.executor.handle_termination_checkpoint()
                self._logger.warning("撤单前查询挂单超时，撤销仍在跟踪的委托")
        with self.lock:
            return [order for order in self.pending_orders.values() if not order.is_completed()]

    def _wait_for_cancellation_terminals(
        self,
        check_interval: float,
        remaining: Callable[[], float | None],
        reconcile: Callable[[str, str], UnifiedOrder | None],
        cancellation: CancellationResult,
    ) -> None:
        """保留回调并限频补查旧单，等待受整轮截止时间约束。"""
        if (seconds := remaining()) is None:
            return
        query_interval = self._get_effective_refresh_interval(seconds, check_interval)
        next_query = self.clock.monotonic() + query_interval
        tracked = {
            (order.symbol, alias)
            for order in self.get_all_orders()
            for alias in self._extract_order_candidate_ids(order)
        }
        external = cancellation.unconfirmed.keys() - tracked
        while True:
            self._apply_buffered_cancellation_terminals(cancellation, external)
            external.intersection_update(cancellation.unconfirmed)
            if not external and (not self.get_pending_count() or self.waiting_on_restriction()):
                return
            self.executor.handle_termination_checkpoint()
            seconds = remaining()
            if seconds is None or seconds <= 0:
                return
            until_query = next_query - self.clock.monotonic()
            if until_query <= 0:
                for symbol, order_id in sorted(external):
                    item = cancellation.orders[symbol, order_id]
                    reconcile_cancellation(symbol, order_id, item, reconcile)
                self._reconcile_missing_pending([], reconcile=reconcile)
                self.executor.handle_termination_checkpoint()
                next_query = self.clock.monotonic() + query_interval
                continue
            delay = min(check_interval, seconds, until_query)
            if external:
                # 本轮订单结束时 all_done_event 已置位，旧单仍按查询间隔等待。
                self.clock.sleep(delay)
                done = False
            else:
                done = self.clock.event_wait(self.all_done_event, delay)
            self.executor.handle_termination_checkpoint()
            if done:
                return

    def _apply_buffered_cancellation_terminals(
        self, result: CancellationResult, identities: set[tuple[str, str]]
    ) -> None:
        """柜台旧单只核对撤单结果，成交不计入本轮仓位。"""
        with self.lock:
            for symbol, order_id in identities:
                item = result.orders[symbol, order_id]
                if item.terminal_order is None:
                    item.terminal_order = next(
                        (
                            order
                            for order in reversed(self._early_order_updates.get(order_id, ()))
                            if order.symbol == symbol and order.is_completed()
                        ),
                        None,
                    )
                if item.terminal_order is not None:
                    item.query_error = None
                    self._order_errors.pop(order_id, None)
                    self._cancel_restrictions.pop(order_id, None)

    def _attach_cancellation_evidence(self, exc: ExecutionTerminated, cancellation: CancellationResult) -> None:
        """只用已取得的证据补齐终止结果，保留追踪期间确认的终态。"""
        self._apply_buffered_cancellation_terminals(cancellation, set(cancellation.unconfirmed))
        outcomes = cancellation.outcomes
        confirmed = {
            key
            for source in (outcomes, exc.cancel_outcomes)
            for key, value in source.items()
            if value.get("terminal") is True
        }
        with self.lock:
            for order in self.completed_orders.values():
                confirmed.update(
                    f"{order.symbol}:{candidate}" for candidate in self._extract_order_candidate_ids(order)
                )
            pending = {}
            for order_id, order in self.pending_orders.items():
                aliases = {f"{order.symbol}:{candidate}" for candidate in self._extract_order_candidate_ids(order)}
                if aliases & confirmed:
                    confirmed.update(aliases)
                else:
                    reason = self._cancel_restrictions.get(order_id) or self._order_errors.get(order_id)
                    pending[f"{order.symbol}:{order_id}"] = reason or "订单终态尚未确认"
        exc.cancel_outcomes = outcomes | exc.cancel_outcomes
        for key in confirmed & exc.cancel_outcomes.keys():
            exc.cancel_outcomes[key] = {**exc.cancel_outcomes[key], "terminal": True, "query_error": None}
        query_unconfirmed = exc.cancel_unconfirmed and not exc.cancel_details
        details = pending | cancellation.details | exc.cancel_details
        exc.cancel_details = {key: reason for key, reason in details.items() if key not in confirmed}
        exc.cancel_unconfirmed = query_unconfirmed or bool(exc.cancel_details)
        if cancellation.attempted:
            exc.cancel_attempted = True
        failed_orders = [
            order_id
            for (_, order_id), item in cancellation.orders.items()
            if item.request_status == CancelRequestStatus.FAILED
        ]
        exc.cancel_failed_order_ids = list(dict.fromkeys([*exc.cancel_failed_order_ids, *failed_orders]))

    def record_terminal_order(self, order: UnifiedOrder) -> None:
        """在执行线程写入已核对的终态，再继续下一笔渠道调用。"""
        self.on_order_update(order)
        self._save_checkpoint(new_terminals_only=True)

    def record_cancellation_result(self, result: CancellationResult) -> dict[str, str]:
        """收敛真实终态并记录仍在场的委托，后续成交可清除本单撤单错误。"""
        for order in result.terminal_orders:
            self.on_order_update(order)
        with self.lock:
            completed = {
                (order.symbol, candidate)
                for order in self.completed_orders.values()
                for candidate in self._extract_order_candidate_ids(order)
            }
            pending = {
                (order.symbol, candidate): key
                for key, order in self.pending_orders.items()
                for candidate in self._extract_order_candidate_ids(order)
            }
            unconfirmed = {}
            for identity, reason in result.unconfirmed.items():
                if identity in completed:
                    continue
                order_id = pending.get(identity, identity[1])
                item = result.orders[identity]
                self._record_cancellation_item(order_id, item)
                unconfirmed[order_id] = reason
            for order_id, order in self.pending_orders.items():
                order.extra["terminal_confirmation_pending"] = True
                reason = (
                    self._cancel_restrictions.get(order_id) or self._order_errors.get(order_id) or "订单终态尚未确认"
                )
                unconfirmed.setdefault(order_id, reason)
                if order_id not in self._cancel_restrictions:
                    self._order_errors.setdefault(order_id, reason)
        # 在调用线程保存回报，下一次查仓可能阻塞或导致 worker 被回收。
        self._save_checkpoint(new_terminals_only=True)
        return unconfirmed

    def _record_cancellation_item(self, order_id: str, item: OrderCancellation) -> None:
        """持锁更新逐单分类；规则限制不能清除此前真实发生的错误。"""
        if item.request_status == CancelRequestStatus.RESTRICTED and not item.query_error:
            self._cancel_restrictions[order_id] = item.reason
        else:
            self._cancel_restrictions.pop(order_id, None)
            self._order_errors[order_id] = item.query_error or item.reason
            self._order_error_reasons.pop(order_id, None)

    @property
    def pending_reason(self) -> str | None:
        """仅在结算结果时说明仍在场的受限委托，不当作运行中的错误。"""
        with self.lock:
            return self._pending_reason()

    def _pending_reason(self) -> str | None:
        """调用方持锁读取仍在场的受限委托。"""
        reasons = [self._cancel_restrictions[key] for key in self.pending_orders if key in self._cancel_restrictions]
        return f"委托撤单受限，仍需跟踪：{'；'.join(dict.fromkeys(reasons))}" if reasons else None

    def waiting_on_restriction(self) -> bool:
        """只有全部未决单均为正常撤单受限时才允许算法继续等待。"""
        with self.lock:
            return (
                bool(self.pending_orders)
                and not self._explicit_error
                and not self._order_errors
                and all(order_id in self._cancel_restrictions for order_id in self.pending_orders)
            )

    def _record_cancel_restriction(self, order_id: str, info: dict[str, Any], exc: CancelNotAllowedError) -> None:
        """保留旧单并按原追单间隔重试，终态回报仍可正常收敛。"""
        with self.lock:
            info.pop("market_order_fallback_pending_cancel", None)
            info["last_chase_time"] = self.clock.time()
            info["cancel_blocked_until"] = self.clock.time() + (self.chase_config.interval if self.chase_config else 0)
            if order_id in self.pending_orders:
                self._cancel_restrictions[order_id] = exc.execution_error
        self._logger.warning(f"撤单受限，保留原委托 {order_id}: {exc.execution_error}")

    def _reconcile_timeout_cancellations(
        self, result: CancellationResult, reconcile: Callable[[str, str], UnifiedOrder | None]
    ) -> None:
        """核对未出现在挂单查询中的委托。"""
        if not callable(getattr(self.executor, "reconcile_terminal_order", None)):
            return
        self.executor.handle_termination_checkpoint()
        checked = {order_id for _, order_id in result.unconfirmed}
        checked.update(order.order_id for order in result.terminal_orders)
        with self.lock:
            already_checked = [order for order_id, order in self.pending_orders.items() if order_id in checked]
        self._reconcile_missing_pending(already_checked, reconcile=reconcile)

    def _get_effective_refresh_interval(
        self,
        timeout: float,
        check_interval: float,
    ) -> float:
        """计算本次等待的有效订单刷新间隔，返回 ``inf`` 表示禁用定时查询."""
        if self.order_refresh_interval <= 0:
            return float("inf")

        if timeout <= self.order_refresh_interval:
            return max(check_interval, timeout / 2)

        return self.order_refresh_interval

    def _order_channel_health(self) -> OrderChannelHealth:
        """查询执行器订单回报通道健康度；执行器不支持时视为 ``UNKNOWN``."""
        getter = getattr(self.executor, "get_order_channel_health", None)
        if not callable(getter):
            return OrderChannelHealth.UNKNOWN
        try:
            return cast("OrderChannelHealth", getter())
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, (MemoryError, ExecutionTerminated)) or getattr(exc, "requires_session_recovery", False):
                raise
            self._logger.debug(f"查询订单通道健康度失败: {format_exception_message(exc)}")
            return OrderChannelHealth.UNKNOWN

    def _current_query_interval(self, timeout: float, check_interval: float) -> float:
        """
        常驻账户通过回报同步；显式超时、撤单和恢复核对仍使用渠道查询。

        Parameters
        ----------
        timeout : float
            本次等待允许的最长时长，单位为秒。
        check_interval : float
            主循环检查步长，单位为秒。

        Returns
        -------
        float
            本轮有效对账间隔；``inf`` 表示本轮不做定时对账。
        """
        owner = getattr(self.executor, "_owner", self.executor)
        if getattr(owner, "account_orders", None) is not None:
            return float("inf")
        return self._get_effective_refresh_interval(timeout, check_interval)

    def _register_order_ack_probe(self, order_id: str) -> None:
        """登记下单探针（仅提供 ``expect_order_ack`` 能力的执行器生效）."""
        expect = getattr(self.executor, "expect_order_ack", None)
        if callable(expect):
            expect(order_id)

    def _query_pending_orders(self) -> None:
        """按当前 symbol 主动查询挂单状态."""
        pending_before = self.get_pending_count()
        if pending_before <= 0:
            return

        self._logger.info(f"定时查询挂单状态: pending={pending_before}")

        try:
            pending_orders = self.executor.get_pending_orders()
        except Exception as exc:
            if isinstance(exc, (MemoryError, ExecutionTerminated)) or bool(
                getattr(exc, "requires_session_recovery", False)
            ):
                raise
            self._logger.warning(f"定时查询挂单状态失败: {format_exception_message(exc)}")
            return

        queried_count = 0
        for order in pending_orders:
            if order.order_id in self.pending_orders:
                queried_count += 1
            self.on_order_update(order)

        self._reconcile_missing_pending(pending_orders)

        summary = self.get_status_summary()
        self._logger.info(f"定时查询完成: queried={queried_count}, {self._format_status_summary(summary)}")

    def _reconcile_missing_pending(
        self,
        open_orders: list[UnifiedOrder],
        *,
        reconcile: Callable[[str, str], UnifiedOrder | None] | None = None,
    ) -> None:
        """
        对账已从挂单列表消失、但本地仍视为 pending 的订单.

        tracker 仍视为 pending、但已从交易所挂单列表消失的订单需靠 REST 收敛终态：WS
        启用时通常是终态帧丢失，WS 未启用时则是设计内的纯 REST 权威路径（REST 就是唯一
        终态来源）。对这些订单查询真实终态，若已终态则驱动 ``on_order_update`` 收敛并补发
        ORDER_TERMINAL 审计事件。仅当执行器提供 ``reconcile_terminal_order`` 能力（当前为
        该能力）时生效；其他渠道无此方法则直接跳过、行为不变。

        Parameters
        ----------
        open_orders : list[UnifiedOrder]
            本次查询到的交易所仍在挂的订单。
        reconcile : Callable[[str, str], UnifiedOrder | None] | None
            撤单收尾提供带整轮剩余时间的终态查询。
        """
        if reconcile is None:
            reconcile = terminal_order_reconciler(self.executor)

        open_ids = {order.order_id for order in open_orders}
        with self.lock:
            missing = [(oid, order.symbol) for oid, order in self.pending_orders.items() if oid not in open_ids]

        for order_id, symbol in missing:
            try:
                terminal_order = reconcile(symbol, order_id)
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, (MemoryError, ExecutionTerminated)) or getattr(
                    exc, "requires_session_recovery", False
                ):
                    raise
                self._logger.warning(f"订单 {order_id} 对账失败: {format_exception_message(exc)}")
                continue
            if terminal_order is not None and terminal_order.is_completed():
                # REST确认只说明查询得到终态，不能区分WS缺失与回调/对账并发。
                cause = (
                    "WS 未启用，纯 REST 对账"
                    if self._order_channel_health() == OrderChannelHealth.UNKNOWN
                    else "REST确认终态，可能与WS回调并发"
                )
                self._logger.info(f"对账发现订单 {order_id} 已终态（{cause}），收敛并补发终态")
                self.record_terminal_order(terminal_order)

    def _check_and_chase(self) -> None:
        """检查并执行追单."""
        if not self.chase_config:
            return

        orders_to_chase = self._get_orders_needing_chase()

        for order_id, order, chase_info in orders_to_chase:
            symbol = chase_info["symbol"]
            direction = chase_info["direction"]

            # 追价前取「可用盘口」：陈旧则刷新行情快照，避免据冻住的 WS 快照追出越点差的 taker 价。
            latest_price = self._usable_price(symbol)
            if latest_price is None or self._submission_expired(chase_info):
                continue

            new_price = _chase_price(latest_price, direction)
            if new_price <= 0 or new_price == order.price:
                continue

            self._logger.info(
                f"追单 {symbol}: "
                f"价格 {order.price} -> {new_price}, "
                f"追单次数 {chase_info['chase_count'] + 1}/{self.chase_config.max_count}"
            )

            try:
                # 撤单与新单提交之间存在一个短暂空窗；在这段时间里必须压住
                # “所有订单都完成”的信号，否则等待方会提早退出。
                with self.lock:
                    self._chasing_order_id = order_id

                self.executor.cancel_order(order_id)

                # 必须确认旧单真的进入终态后才能下新单。``cancel_order`` 返回 True
                # 各渠道语义并不一致：同步 REST 可能表示交易所已受理，而部分渠道只是
                # 「撤单请求已投递到 bridge」；在撤单尚未
                # 生效时就下新单，会出现新旧单同时在场的双份敞口。
                if not self._await_cancel_confirmed(order_id, deadline=chase_info.get("submission_deadline")):
                    self._logger.warning(
                        f"追单放弃 {symbol}: 撤单未在 "
                        f"{self.chase_config.cancel_confirm_timeout}s 内确认，保留旧单不换单"
                    )
                    self.executor.emit_audit_event(
                        event_type=ExecutionEventType.ORDER_SUBMITTED,
                        status=ExecutionEventStatus.WARNING,
                        reason_family=ExecutionReasonFamily.EXECUTION_STRATEGY,
                        reason_code="COMMON.ORDER_CHASE_CANCEL_UNCONFIRMED",
                        symbol=symbol,
                        order_id=order_id,
                        details={
                            "chase": {
                                "index": chase_info["chase_count"] + 1,
                                "max": self.chase_config.max_count,
                                "from_price": float(order.price),
                                "to_price": float(new_price),
                                "prev_order_id": order_id,
                                "timeout": float(self.chase_config.cancel_confirm_timeout),
                            }
                        },
                    )
                    continue

                # 剩余量必须在撤单确认之后再算：确认前的快照默认「旧单已死」，
                # 而撤单生效前旧单仍可能继续成交，据此下单会多挂。
                order = self._latest_known_order(order_id, order)
                remaining_volume = self._get_effective_remaining_volume(order_id, order)
                if remaining_volume > 0 and not self._submission_expired(chase_info):
                    # 撤单确认可能耗时并接收新行情；换单必须重新定价并携带实际报价证据。
                    latest_price = clone_price_data(self._usable_price(symbol))
                    new_price = _chase_price(latest_price, direction)
                    if new_price <= 0:
                        message = "追单失败：撤单确认后行情不可用或追单价格缺失"
                        self._set_order_error(order_id, message)
                        self.executor.emit_audit_event(
                            event_type=ExecutionEventType.ORDER_TERMINAL,
                            status=ExecutionEventStatus.ERROR,
                            reason_family=ExecutionReasonFamily.EXECUTION_STRATEGY,
                            reason_code="COMMON.ORDER_CHASE_MARKET_DATA_UNAVAILABLE",
                            symbol=symbol,
                            order_id=order_id,
                            details={"chase": {"remaining_volume": float(remaining_volume)}, "message": message},
                        )
                        continue
                    kwargs = _chase_place_kwargs(chase_info)

                    self.require_saved_orders()
                    if self._submission_expired(chase_info):
                        continue
                    new_order = self.executor.place_order(
                        direction,
                        OrderType.LIMIT,
                        remaining_volume,
                        new_price,
                        market_data=latest_price,
                        **kwargs,
                    )

                    chase_index = chase_info["chase_count"] + 1
                    with self.lock:
                        self._chase_info.pop(order_id, None)
                    self._register_order(
                        new_order, {**chase_info, "chase_count": chase_index, "last_chase_time": self.clock.time()}
                    )
                    self.executor.emit_audit_event(
                        event_type=ExecutionEventType.ORDER_SUBMITTED,
                        status=ExecutionEventStatus.INFO,
                        reason_family=ExecutionReasonFamily.EXECUTION_STRATEGY,
                        reason_code="COMMON.ORDER_CHASE",
                        symbol=symbol,
                        order_id=new_order.order_id,
                        details={
                            "chase": {
                                "index": chase_index,
                                "max": self.chase_config.max_count,
                                "from_price": float(order.price),
                                "to_price": float(new_price),
                                "remaining_volume": float(remaining_volume),
                                "prev_order_id": order_id,
                            }
                        },
                    )
                    self._set_order_error(order_id, None)

            except CancelNotAllowedError as exc:
                self._record_cancel_restriction(order_id, chase_info, exc)
            except (MemoryError, ExecutionTerminated):
                raise
            except RECOVERABLE_ALGORITHM_EXCEPTIONS as exc:
                if isinstance(exc, (MemoryError, ExecutionTerminated)) or bool(
                    getattr(exc, "requires_session_recovery", False)
                ):
                    raise
                self._logger.error(f"追单失败 {symbol}: {exc}")
                self.record_exception(exc, "追单", order_id=order_id)
            finally:
                with self.lock:
                    self._chasing_order_id = None
                    if not self.pending_orders and not self._fallback_submitting:
                        self.all_done_event.set()

        self._fallback_to_market_order()

    def _get_orders_needing_chase(
        self,
    ) -> list[tuple[str, UnifiedOrder, dict[str, Any]]]:
        """筛选当前满足追单条件的订单集合。"""
        if not self.chase_config:
            return []

        result: list[tuple[str, UnifiedOrder, dict[str, Any]]] = []
        current_time = self.clock.time()

        # 触发判定同样需要新鲜盘口：WS 冻住时若据陈旧价判为无需追价，会让挂单久滞于已偏离
        # 的价位。但行情快照刷新是阻塞调用、绝不能持锁进行，故先在锁外把候选品种刷新到
        # latest_prices，再持锁做纯内存判定（与执行阶段共享限频、不重复刷新行情快照）。
        for symbol in self._pending_chase_symbols():
            self._usable_price(symbol)

        with self.lock:
            for order_id, order in self.pending_orders.items():
                if order_id not in self._chase_info:
                    continue

                info = self._chase_info[order_id]
                symbol = info["symbol"]

                if self._submission_expired(info):
                    continue

                if info["chase_count"] >= self.chase_config.max_count:
                    continue

                if current_time - info["last_chase_time"] < self.chase_config.interval:
                    continue

                latest_price = self.latest_prices.get(symbol)
                if latest_price is None:
                    continue

                direction = info["direction"]

                price = _chase_price(latest_price, direction)
                if price <= 0 or not isfinite(order.price):
                    continue
                # 用十进制比较，避免浮点误差漏掉恰好达到阈值的报价。
                best_price = Decimal(str(price))
                order_price = Decimal(str(order.price))
                tick_size = self._get_tick_size(symbol, order.price)
                price_diff = best_price - order_price if direction == OrderDirection.BUY else order_price - best_price
                if price_diff >= self.chase_config.ticks * Decimal(str(tick_size)):
                    result.append((order_id, order, info))

        return result

    def _get_tick_size(self, symbol: str, fallback_price: float) -> float:
        """
        获取追单判断所需的最小价格变动单位。

        Parameters
        ----------
        symbol : str
            当前交易品种。
        fallback_price : float
            无法从执行器取得真实 tick size 时使用的估算参考价。

        Returns
        -------
        float
            当前品种的最小价格变动单位。

        Notes
        -----
        真实 tick size 更可靠；只有在执行器没有暴露该能力时，才按价格量级做保守估算。
        """
        try:
            tick_size = self.executor.get_tick_size()
            if tick_size and tick_size > 0:
                return tick_size
        except RECOVERABLE_ALGORITHM_EXCEPTIONS:
            pass

        return self._estimate_tick_size(fallback_price)

    def _estimate_tick_size(self, price: float) -> float:
        """
        按价格量级估算最小价格变动单位。

        Parameters
        ----------
        price : float
            当前参考价格。

        Returns
        -------
        float
            估算出的最小价格变动单位。
        """
        if price < 1:
            return 0.0001
        elif price < 10:
            return 0.001
        elif price < 100:
            return 0.01
        elif price < 1000:
            return 0.1
        else:
            return 1.0

    def _fallback_to_market_order(self) -> None:
        """对达到最大追单次数的订单使用市价单兜底.

        检查所有待完成订单，如果已达到最大追单次数但仍未成交，
        则先请求撤销原限价单，并在收到旧单终态回调后，
        仅对剩余未成交部分补市价单，避免重复下单。
        """
        if not self.chase_config or not self.chase_config.market_fallback_enabled:
            return

        orders_to_fallback: list[tuple[str, UnifiedOrder, dict[str, Any]]] = []

        with self.lock:
            for order_id, order in self.pending_orders.items():
                if order_id not in self._chase_info:
                    continue

                info = self._chase_info[order_id]
                if (
                    info["chase_count"] >= self.chase_config.max_count
                    and self.clock.time() >= info.get("cancel_blocked_until", 0)
                    and not self._submission_expired(info)
                    and not info.get("market_order_fallback_pending_cancel", False)
                    and not info.get("market_order_fallback_failed", False)
                ):
                    info["market_order_fallback_pending_cancel"] = True
                    orders_to_fallback.append((order_id, order, info))

        # 市价兜底必须遵守“先确认旧限价单终态，再补剩余量”的顺序，否则在旧单
        # 还可能成交的情况下直接补市价单，会把剩余量判断放大成重复下单。
        for order_id, order, chase_info in orders_to_fallback:
            if self._submission_expired(chase_info):
                continue
            symbol = chase_info["symbol"]
            direction = chase_info["direction"]
            remaining_volume = self._get_effective_remaining_volume(order_id, order)

            if remaining_volume <= 0:
                with self.lock:
                    if order_id in self._chase_info:
                        del self._chase_info[order_id]
                continue

            self._logger.warning(
                f"订单 {order_id} ({symbol}) 已达到最大追单次数 "
                f"({chase_info['chase_count']}/{self.chase_config.max_count})，"
                f"准备请求撤单后对剩余 {remaining_volume} 执行市价单兜底"
            )

            try:
                cancel_requested = self.executor.cancel_order(order_id)
                if not cancel_requested:
                    raise RuntimeError("cancel_order returned False")

                self._logger.info(f"已请求撤销限价单，等待终态后再执行市价单兜底: {symbol} 订单ID {order_id}")
            except CancelNotAllowedError as exc:
                self._record_cancel_restriction(order_id, chase_info, exc)
            except (MemoryError, ExecutionTerminated):
                with self.lock:
                    chase_info.pop("market_order_fallback_pending_cancel", None)
                raise
            except RECOVERABLE_ALGORITHM_EXCEPTIONS as exc:
                if isinstance(exc, (MemoryError, ExecutionTerminated)) or bool(
                    getattr(exc, "requires_session_recovery", False)
                ):
                    with self.lock:
                        chase_info.pop("market_order_fallback_pending_cancel", None)
                    raise
                self._logger.error(f"市价单兜底前撤单失败 {symbol}: {exc}")
                self.record_exception(exc, "撤单", order_id=order_id)
                self._mark_market_fallback_failed(
                    order_id,
                    reason_code="COMMON.MARKET_FALLBACK_CANCEL_FAILED",
                    message=f"撤单失败，已停止自动市价兜底: {format_exception_message(exc)}",
                    symbol=symbol,
                    details={
                        "direction": direction.value,
                        "remaining_volume": float(remaining_volume),
                        "error": format_exception_message(exc),
                    },
                )

    def _submit_market_fallback_order(
        self,
        completed_order: UnifiedOrder,
        chase_info: dict[str, Any],
    ) -> None:
        """终态确认后按市价边界提交一批子单，提交期间禁止提前完成。"""
        order_id = completed_order.order_id
        with self.lock:
            if order_id in self._fallback_started:
                return
            self._fallback_started.add(order_id)
            self._fallback_submitting.add(order_id)
            self.all_done_event.clear()
        try:
            symbol = chase_info["symbol"]
            direction = chase_info["direction"]
            remaining = self._get_effective_remaining_volume(order_id, completed_order)
            submitted = 0.0
            if remaining <= 0 or self._submission_expired(chase_info):
                return
            price_data = clone_price_data(self.latest_prices.get(symbol))
            if not self._is_market_fallback_price_safe(completed_order, chase_info, price_data):
                self._mark_market_fallback_failed(
                    order_id,
                    reason_code="COMMON.MARKET_FALLBACK_PRICE_PROTECTION",
                    message="当前盘口偏离原限价过大，已停止自动市价兜底",
                    symbol=symbol,
                    details={"remaining_volume": remaining},
                )
                return
            try:
                getter = getattr(self.executor, "get_order_volume_bounds", None)
                bounds = (
                    cast("tuple[int | None, int | None] | None", getter(OrderType.MARKET, chase_info.get("trade_rule")))
                    if callable(getter)
                    else None
                )
                slices = (
                    split_order_volumes(remaining, bounds[1], min_size=bounds[0]) if bounds is not None else [remaining]
                )
                for index, chunk in enumerate(slices, 1):
                    self.require_saved_orders()
                    if self._submission_expired(chase_info):
                        return
                    market_order = self.executor.place_order(
                        direction,
                        OrderType.MARKET,
                        chunk,
                        price=0,
                        market_data=price_data,
                        market_data_purpose="order",
                        **_chase_place_kwargs(chase_info),
                    )
                    submitted += chunk
                    self.add_order(
                        market_order,
                        direction=direction,
                        position_side=chase_info.get("position_side"),
                        offset_flag=chase_info.get("offset_flag"),
                        trade_rule=chase_info.get("trade_rule"),
                        chase_enabled=False,
                    )
                    self.executor.emit_audit_event(
                        event_type=ExecutionEventType.ORDER_SUBMITTED,
                        status=ExecutionEventStatus.WARNING,
                        reason_family=ExecutionReasonFamily.EXECUTION_STRATEGY,
                        reason_code="COMMON.MARKET_FALLBACK_ORDER_SUBMITTED",
                        symbol=symbol,
                        order_id=market_order.order_id,
                        details={
                            "fallback": {
                                "original_order_id": order_id,
                                "child_index": index,
                                "child_volume": chunk,
                                "submitted_volume": submitted,
                                "remaining_volume": remaining - submitted,
                                "direction": direction.value,
                                "position_side": chase_info.get("position_side"),
                            }
                        },
                    )
            except (MemoryError, *RECOVERABLE_ALGORITHM_EXCEPTIONS) as exc:
                self.record_exception(exc, "报单", order_id=order_id)
                self._mark_market_fallback_failed(
                    order_id,
                    reason_code="COMMON.MARKET_FALLBACK_ORDER_FAILED",
                    message=f"市价单兜底失败: {format_exception_message(exc)}",
                    symbol=symbol,
                    details={
                        "original_order_id": order_id,
                        "submitted_volume": submitted,
                        "remaining_volume": remaining - submitted,
                    },
                )
                if isinstance(exc, (MemoryError, ExecutionTerminated)) or bool(
                    getattr(exc, "requires_session_recovery", False)
                ):
                    raise
                return
            if submitted < remaining:
                self._mark_market_fallback_failed(
                    order_id,
                    reason_code="COMMON.MARKET_FALLBACK_VOLUME_GAP",
                    message="市价拆单存在未提交缺口",
                    symbol=symbol,
                    details={
                        "original_order_id": order_id,
                        "submitted_volume": submitted,
                        "remaining_volume": remaining - submitted,
                    },
                )
            else:
                self._set_order_error(order_id, None)
        finally:
            with self.lock:
                self._fallback_submitting.discard(order_id)
                if not self.pending_orders and self._chasing_order_id is None and not self._fallback_submitting:
                    self.all_done_event.set()

    def _is_market_fallback_price_safe(
        self, completed_order: UnifiedOrder, chase_info: dict[str, Any], price_data: UnifiedPriceData | None
    ) -> bool:
        """使用本次兜底消费的盘口做价格保护；提交时由会话重验同一行情。"""
        if price_data is None:
            return False

        original_price = float(chase_info.get("original_price", completed_order.price))
        if original_price <= 0:
            return False

        direction = chase_info["direction"]
        reference_price = (
            (price_data.asks[0][0] if price_data.asks else 0.0)
            if direction == OrderDirection.BUY
            else (price_data.bids[0][0] if price_data.bids else 0.0)
        )
        if reference_price <= 0:
            return False

        if direction == OrderDirection.BUY:
            slippage = (reference_price - original_price) / original_price
        else:
            slippage = (original_price - reference_price) / original_price
        return slippage <= MARKET_FALLBACK_MAX_SLIPPAGE

    def _mark_market_fallback_failed(
        self,
        order_id: str,
        *,
        reason_code: str,
        message: str,
        symbol: str,
        details: dict[str, object],
    ) -> None:
        """标记当前订单的自动兜底失败，并记录审计事件."""
        with self.lock:
            tracked_order = self.pending_orders.get(order_id)
            if tracked_order is None:
                tracked_order = self.completed_orders.get(order_id)
            if tracked_order is not None:
                tracked_order.extra["market_order_fallback_failed"] = True
                tracked_order.extra["market_order_fallback_reason"] = reason_code
                tracked_order.extra["market_order_fallback_message"] = message

            if order_id in self._chase_info:
                self._chase_info[order_id]["market_order_fallback_failed"] = True
                self._chase_info[order_id].pop("market_order_fallback_pending_cancel", None)

        self.executor.emit_audit_event(
            event_type=ExecutionEventType.ORDER_TERMINAL,
            status=ExecutionEventStatus.ERROR,
            reason_family=ExecutionReasonFamily.EXECUTION_STRATEGY,
            reason_code=reason_code,
            symbol=symbol,
            order_id=order_id,
            details={"fallback": details, "message": message},
        )

    def _merge_order_update(
        self,
        tracked_order_id: str,
        tracked_order: UnifiedOrder,
        incoming_order: UnifiedOrder,
    ) -> UnifiedOrder:
        """合并已有成交进度，避免订单状态回调回退成交数量."""
        merged_order = copy_data(incoming_order, deep=True)
        merged_order.extra = {**tracked_order.extra, **merged_order.extra}
        # 后续状态回报的事务时间不能覆盖已跟踪订单的创建时间。
        merged_order.create_time = tracked_order.create_time
        incoming_status = incoming_order.status
        baseline_filled_volume = max(tracked_order.filled_volume, incoming_order.filled_volume)
        merged_order.filled_volume = baseline_filled_volume
        if incoming_order.filled_volume < baseline_filled_volume:
            merged_order.avg_price = tracked_order.avg_price
        elif incoming_order.avg_price <= 0 and incoming_order.filled_volume == tracked_order.filled_volume:
            merged_order.avg_price = tracked_order.avg_price
        trade_volume, trade_average = _summarize_trade_records(self.order_trades.get(tracked_order_id, []))
        if merged_order.avg_price <= 0 and trade_volume == baseline_filled_volume and trade_volume > 0:
            merged_order.avg_price = trade_average

        if OrderStatus.is_completed(incoming_status):
            merged_order.status = incoming_status
        elif merged_order.filled_volume >= merged_order.volume > 0:
            merged_order.status = OrderStatus.FILLED
        elif merged_order.filled_volume > 0:
            merged_order.status = OrderStatus.PARTIALLY_FILLED
        else:
            merged_order.status = incoming_status

        return merged_order

    def _preserve_cumulative_fill(
        self,
        order: UnifiedOrder,
        *,
        baseline_filled_volume: float,
        baseline_avg_price: float,
    ) -> None:
        """保持累计成交量单调不减，避免较弱的订单更新回退已观察到的成交进度."""
        if order.filled_volume >= baseline_filled_volume:
            return

        order.filled_volume = baseline_filled_volume
        if baseline_avg_price > 0:
            order.avg_price = baseline_avg_price

    def _get_effective_filled_volume(self, order_id: str, order: UnifiedOrder) -> float:
        """返回用于追单/兜底决策的有效累计成交量."""
        trade_filled_volume, _ = _summarize_trade_records(self.order_trades.get(order_id, []))
        return max(order.filled_volume, trade_filled_volume)

    def _await_cancel_confirmed(self, order_id: str, *, deadline: float | None = None) -> bool:
        """
        等待旧单撤单生效（进入终态）.

        Parameters
        ----------
        order_id : str
            已提交撤单请求的订单标识。
        deadline : float | None, default=None
            本单提交期限，撤单等待不得超过此时间。

        Returns
        -------
        bool
            旧单已进入终态时返回 ``True``；超时仍未确认时返回 ``False``。

        Notes
        -----
        判定完全走**内存态**：终态由各渠道的订单回调写入 ``completed_orders``
        （见 :meth:`on_order_update`），因此这里不额外发起任何查询——同步确认撤单
        的渠道（REST ``cancel_order`` 返回即代表交易所已受理）通常
        在首次检查就命中，不会多付一次往返。

        ``cancel_order`` 的返回值**不能**作为确认依据：它在 GM 上只表示「撤单请求
        已投递到 bridge」，不代表订单已经撤掉。
        """
        timeout = self.chase_config.cancel_confirm_timeout if self.chase_config else 0.0
        if timeout <= 0:
            return order_id in self.completed_orders

        confirmation_deadline = self.clock.time() + timeout
        if deadline is not None:
            confirmation_deadline = min(confirmation_deadline, deadline)
        while True:
            with self.lock:
                if order_id in self.completed_orders:
                    return True
                if order_id not in self.pending_orders:
                    return False

            if self.clock.time() >= confirmation_deadline:
                return False
            self.clock.sleep(min(0.05, max(0.0, confirmation_deadline - self.clock.time())))

    def _latest_known_order(self, order_id: str, fallback: UnifiedOrder) -> UnifiedOrder:
        """
        返回该订单当前已知的最新快照.

        Parameters
        ----------
        order_id : str
            订单标识。
        fallback : UnifiedOrder
            未找到最新快照时返回的回退对象。

        Returns
        -------
        UnifiedOrder
            终态快照优先，其次为仍在跟踪的快照，最后回退到入参。

        Notes
        -----
        撤单确认期间旧单可能又有成交，用确认后的快照计算剩余量才不会多挂。
        """
        with self.lock:
            completed = self.completed_orders.get(order_id)
            if completed is not None:
                return completed
            pending = self.pending_orders.get(order_id)
            if pending is not None:
                return pending
        return fallback

    def _get_effective_remaining_volume(self, order_id: str, order: UnifiedOrder) -> float:
        """返回用于追单/兜底决策的有效剩余量."""
        return max(order.volume - self._get_effective_filled_volume(order_id, order), 0.0)

    def _buffer_early_order_update(self, order: UnifiedOrder) -> None:
        """
        缓存早于 `add_order()` 到达的同 symbol 订单更新。

        Notes
        -----
        某些通道会在 ``place_order()`` 返回后极短时间内推送第一条状态回调，
        甚至早于算法线程把订单注册进 tracker。这里先按候选主键暂存，等
        ``add_order()`` 拿到真实订单对象后再补回放。
        """
        if not self._matches_tracker_symbol(order.symbol):
            return

        candidate_ids = self._extract_order_candidate_ids(order)
        if not candidate_ids:
            return

        buffered_order = copy_data(order, deep=True)
        for candidate_id in candidate_ids:
            self._early_order_updates.setdefault(candidate_id, []).append(buffered_order)

    def _buffer_early_trade_record(self, trade: TradeRecord) -> None:
        """
        缓存早于 `add_order()` 到达的同 symbol 成交记录。

        Notes
        -----
        成交回报可能比订单终态更早到达。若直接丢弃，后续剩余量计算会低估已成交
        数量，进而放大追单或市价兜底的下单量。
        """
        if not self._matches_tracker_symbol(trade.symbol):
            return

        candidate_ids = self._extract_trade_order_ids(trade)
        if not candidate_ids:
            return

        buffered_trade = copy_data(trade, deep=True)
        for candidate_id in candidate_ids:
            self._early_trade_records.setdefault(candidate_id, []).append(buffered_trade)

    def _pop_early_events(self, order: UnifiedOrder) -> tuple[list[TradeRecord], list[UnifiedOrder]]:
        """弹出与新跟踪订单相关的早到成交和订单更新."""
        candidate_ids = self._extract_order_candidate_ids(order)
        buffered_trades: list[TradeRecord] = []
        buffered_order_updates: list[UnifiedOrder] = []
        seen_trade_ids: set[int] = set()
        seen_order_update_ids: set[int] = set()

        for candidate_id in candidate_ids:
            for buffered_trade in self._early_trade_records.pop(candidate_id, []):
                buffered_trade_id = id(buffered_trade)
                if buffered_trade_id in seen_trade_ids:
                    continue
                seen_trade_ids.add(buffered_trade_id)
                buffered_trades.append(buffered_trade)

            for buffered_order in self._early_order_updates.pop(candidate_id, []):
                buffered_order_id = id(buffered_order)
                if buffered_order_id in seen_order_update_ids:
                    continue
                seen_order_update_ids.add(buffered_order_id)
                buffered_order_updates.append(buffered_order)

        return buffered_trades, buffered_order_updates

    def _matches_tracker_symbol(self, symbol: str | None) -> bool:
        """只缓存当前 tracker 所属 symbol 的早到事件."""
        if not symbol:
            return False

        tracker_symbol = getattr(self.executor, "symbol", None)
        if not isinstance(tracker_symbol, str) or not tracker_symbol:
            return True
        return symbol == tracker_symbol

    def _resolve_trade_target_order_entry(self, trade: TradeRecord) -> tuple[str, UnifiedOrder] | None:
        """根据成交记录里的订单主键定位已跟踪订单和内部主键."""
        candidate_ids = self._extract_trade_order_ids(trade)
        for order_id in candidate_ids:
            if order_id in self.pending_orders:
                return order_id, self.pending_orders[order_id]
            if order_id in self.completed_orders:
                return order_id, self.completed_orders[order_id]

        if not candidate_ids:
            return None

        # 不同渠道对“订单主键”的含义并不稳定：有的回调给本地下单号，有的给
        # cl_ord_id，有的只给交易所订单号。直接主键命中失败后，再退回多候选
        # 集合匹配，尽量把同一笔成交归并到已跟踪订单上。
        tracked_orders = list(self.pending_orders.items()) + list(self.completed_orders.items())
        for tracked_order_id, tracked_order in tracked_orders:
            tracked_candidate_ids = {
                str(tracked_order.order_id or ""),
                str(tracked_order.extra.get("cl_ord_id", "") or ""),
                str(tracked_order.extra.get("exchange_order_id", "") or ""),
            }
            if any(order_id and order_id in tracked_candidate_ids for order_id in candidate_ids):
                return tracked_order_id, tracked_order

        return None

    def _resolve_order_target_id(self, order: UnifiedOrder) -> str | None:
        """根据订单更新里的候选主键定位已跟踪订单."""
        candidate_ids = self._extract_order_candidate_ids(order)
        for order_id in candidate_ids:
            if order_id in self.pending_orders:
                return order_id

        if not candidate_ids:
            return None

        # 这里故意只在 pending 集合里做模糊归并。订单一旦进入 completed，
        # 后续再来的异步更新不应把它重新拉回活动态，否则等待线程会观察到
        # “终态订单复活”的伪状态。
        for tracked_order_id, tracked_order in self.pending_orders.items():
            tracked_candidate_ids = {
                str(tracked_order_id or ""),
                str(tracked_order.order_id or ""),
                str(tracked_order.extra.get("cl_ord_id", "") or ""),
                str(tracked_order.extra.get("exchange_order_id", "") or ""),
            }
            if any(order_id and order_id in tracked_candidate_ids for order_id in candidate_ids):
                return tracked_order_id

        return None

    def _extract_order_candidate_ids(self, order: UnifiedOrder) -> list[str]:
        """
        从订单更新里提取可关联订单的候选主键。

        Returns
        -------
        list[str]
            按优先级去重后的候选主键列表；顺序保留原始来源的可信度。
        """
        raw_order_data = order.extra.get("raw_order_data")
        candidates: list[object] = [
            order.order_id,
            order.extra.get("cl_ord_id"),
            order.extra.get("exchange_order_id"),
        ]
        if isinstance(raw_order_data, dict):
            candidates.extend(
                [
                    raw_order_data.get("cl_ord_id"),
                    raw_order_data.get("order_id"),
                    raw_order_data.get("exchange_order_id"),
                ]
            )

        deduplicated_ids: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            candidate_str = str(candidate or "").strip()
            if not candidate_str or candidate_str in seen:
                continue
            seen.add(candidate_str)
            deduplicated_ids.append(candidate_str)
        return deduplicated_ids

    def _extract_trade_order_ids(self, trade: TradeRecord) -> list[str]:
        """
        从成交记录里提取可关联订单的候选主键。

        Notes
        -----
        这里会把 ``trade_id`` 一并纳入候选集，目的是兼容极端通道只在成交事件里
        暴露单一可追踪字段的情况；真正归并时仍会与已跟踪订单的候选键集合比对。
        """
        raw_trade_data = trade.extra.get("raw_trade_data")
        candidates: list[object] = [
            trade.order_id,
            trade.extra.get("cl_ord_id"),
            trade.extra.get("order_id"),
            trade.extra.get("exchange_order_id"),
        ]
        if isinstance(raw_trade_data, dict):
            candidates.extend(
                [
                    raw_trade_data.get("cl_ord_id"),
                    raw_trade_data.get("order_id"),
                    raw_trade_data.get("exchange_order_id"),
                ]
            )
        candidates.append(trade.trade_id)

        deduplicated_ids: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            candidate_str = str(candidate or "").strip()
            if not candidate_str or candidate_str in seen:
                continue
            seen.add(candidate_str)
            deduplicated_ids.append(candidate_str)
        return deduplicated_ids

    @staticmethod
    def _trusted_trade_id(trade_id: object) -> str:
        """返回可用于去重的可信成交 ID；空串视为不可信."""
        return str(trade_id or "").strip()

    def _order_has_trade(self, order_id: str, trade: TradeRecord) -> bool:
        """判断订单是否已包含等价成交记录，避免重复累计.

        双方都有可信 ``trade_id`` 时只按 ID 判重：同 ID 视为重复，不同 ID 即使
        同秒同价同量也保留。仅当任一侧缺少可信 ID 时，才回退到时间/价/量弱证据。
        """
        incoming_id = self._trusted_trade_id(trade.trade_id)
        for existing_trade in self.order_trades.get(order_id, []):
            existing_id = self._trusted_trade_id(existing_trade.trade_id)
            if incoming_id and existing_id:
                if incoming_id == existing_id:
                    return True
                continue
            if (
                existing_trade.trade_time == trade.trade_time
                and existing_trade.trade_volume == trade.trade_volume
                and existing_trade.trade_price == trade.trade_price
            ):
                return True
        return False

    def get_pending_count(self) -> int:
        """获取待完成订单数量."""
        with self.lock:
            return len(self.pending_orders)

    def get_status_summary(self) -> dict[str, int | float]:
        """获取订单状态摘要."""
        with self.lock:
            all_orders = list(self.completed_orders.values())
            all_orders.extend(self.pending_orders.values())
            pending_count = len(self.pending_orders)
            completed_count = len(self.completed_orders)

        summary: dict[str, int | float] = {
            "total_orders": len(all_orders),
            "pending_orders": pending_count,
            "completed_orders": completed_count,
            "active_orders": 0,
            "filled_orders": 0,
            "canceled_orders": 0,
            "rejected_orders": 0,
            "expired_orders": 0,
            "partially_filled_orders": 0,
            "filled_volume": 0.0,
        }

        for order in all_orders:
            summary["filled_volume"] += order.filled_volume
            status = order.status
            if status == OrderStatus.FILLED:
                summary["filled_orders"] += 1
            elif status == OrderStatus.CANCELED:
                summary["canceled_orders"] += 1
            elif status == OrderStatus.REJECTED:
                summary["rejected_orders"] += 1
            elif status == OrderStatus.EXPIRED:
                summary["expired_orders"] += 1
            elif status == OrderStatus.PARTIALLY_FILLED:
                summary["partially_filled_orders"] += 1
                summary["active_orders"] += 1
            elif OrderStatus.is_active(status):
                summary["active_orders"] += 1

        return summary

    def _format_status_summary(self, summary: dict[str, int | float]) -> str:
        """格式化订单状态摘要."""
        return (
            f"orders={summary['total_orders']}, "
            f"filled={summary['filled_orders']}, "
            f"partial={summary['partially_filled_orders']}, "
            f"canceled={summary['canceled_orders']}, "
            f"rejected={summary['rejected_orders']}, "
            f"expired={summary['expired_orders']}, "
            f"active={summary['active_orders']}, "
            f"pending={summary['pending_orders']}"
        )

    def _position_order(self, order: UnifiedOrder) -> UnifiedOrder:
        filled, _ = _summarize_trade_records(self.order_trades.get(order.order_id, []))
        return order_with_trade_fill(order, filled)

    def position_for_submission(
        self,
        current: float,
        direction: OrderDirection,
        *,
        needs_position_detail: bool,
        leg_order_ids: set[str] | None = None,
    ) -> float | None:
        """普通买卖扣除已知成交；开平类订单须等持仓明细赶上后再拆腿。"""
        if self.start_volume is None:
            return current
        orders = self.get_all_orders(for_position=True)
        if leg_order_ids:
            # 该腿已发分段共用一次持仓快照，成交已包含在本腿的数量预算内。
            orders = [order for order in orders if order.order_id not in leg_order_ids]
        if not any(order.filled_volume for order in orders):
            return current
        expected = confirmed_position(self.start_volume, orders)
        if expected is None or (needs_position_detail and not isclose(current, expected, rel_tol=0, abs_tol=1e-9)):
            return None
        return max(current, expected) if direction == OrderDirection.BUY else min(current, expected)

    def get_all_orders(self, *, for_position: bool = False) -> list[UnifiedOrder]:
        """读取订单回报；仓位核对时同时计入已归属本单的成交明细。"""
        with self.lock:
            all_orders = list(self.completed_orders.values())
            all_orders.extend(self.pending_orders.values())
            return [self._position_order(order) for order in all_orders] if for_position else all_orders

    def get_order_trades(self, order_id: str) -> list[TradeRecord]:
        """获取指定订单已跟踪的成交明细."""
        with self.lock:
            return [copy_data(trade, deep=True) for trade in self.order_trades.get(order_id, [])]

    def _terminal_orders_missing_trades(self) -> list[str]:
        with self.lock:
            return [
                order.order_id
                for key, order in self.completed_orders.items()
                if order.filled_volume - sum(trade.trade_volume for trade in self.order_trades.get(key, [])) > 1e-9
            ]

    def reconcile_terminal_trades(self, *, deadline: float) -> None:
        """收尾时按订单补查缺少的逐笔成交，复用回调去重。"""
        try:
            self._fetch_missing_terminal_trades(deadline)
        except ExecutionTerminated as exc:
            # 只有补查撞上总超时且下单已收尾时，继续返回已确认的执行结果。
            with self.lock:
                if (
                    exc.trigger != TERMINATION_TRIGGER_TIMEOUT
                    or self.pending_orders
                    or self._chasing_order_id is not None
                    or self._fallback_submitting
                ):
                    raise
        if missing := self._terminal_orders_missing_trades():
            self._logger.warning(f"订单成交明细仍未齐: {', '.join(missing)}")

    def _fetch_missing_terminal_trades(self, deadline: float) -> None:
        for wait in (0, 0.2):
            missing = self._terminal_orders_missing_trades()
            if not missing or self.clock.time() + wait >= deadline:
                return
            if wait:
                self.executor.sleep_or_terminate(wait)
                missing = self._terminal_orders_missing_trades()
            for order_id in missing:
                if self.clock.time() >= deadline:
                    return
                self.executor.handle_termination_checkpoint()
                try:
                    trades = self.executor.query_trades(order_id)
                except (MemoryError, ExecutionTerminated):
                    raise
                except Exception as exc:  # noqa: BLE001 - 渠道 SDK 补查失败不覆盖已确认的订单结果。
                    self._logger.warning(f"订单 {order_id} 成交明细补查失败: {format_exception_message(exc)}")
                    continue
                for trade in trades:
                    self.on_trade_record(trade)

    def get_all_trades(self) -> list[TradeRecord]:
        """获取当前跟踪器内所有已收集的成交明细."""
        with self.lock:
            trades: list[TradeRecord] = []
            for trade_list in self.order_trades.values():
                trades.extend(copy_data(trade, deep=True) for trade in trade_list)
            return trades
