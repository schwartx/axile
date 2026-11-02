"""
TWAP（时间加权平均价格）算法.

将一次调仓在给定时间窗口内均匀切成若干片，逐片下单，
以降低单笔大额订单对市场的冲击，使成交均价贴近区间时间加权价格。

设计要点
--------
- 目标语义为“净持仓”：算法先把 ``target_volume`` 转成相对当前持仓的增量，
  再对增量做时间切片，因此天然表达“调仓到位”而非“买入 N 手”。
- 采用累计进度表切片：第 ``i`` 片以“到该片结束应累计完成的量”减去“已成交量”
  作为本片目标。该策略一石三鸟——等距推进、对欠量自动追平（carry-over）、
  取整残差自动归入最后一片。
- 全渠道通用：不在算法层做 lot / 最小下单量取整，交由各渠道执行器兜底；
  取整误差会被累计进度表自动滚入后续片。
- 片间等待统一走 :func:`get_default_clock`，仿真环境下会被替换为仿真时钟并自动加速。

使用示例
--------
    input_data = UnifiedStandardInput(
        algorithm={
            "method": "TWAP",
            "params": {
                "total_duration": 300,
                "slices": 10,
                "price_strategy": "ACTIVE",
            },
        },
        ...
    )
"""

from typing import Any, Literal

from pydantic import Field, model_validator

from axile.executor.account_control.exceptions import AccountControlBlockedError
from axile.executor.algorithms.common.params import BaseAlgorithmParams
from axile.executor.algorithms.core.base import (
    ALL_ORDER_PARAM_MODELS,
    AlgorithmInput,
    AlgorithmResult,
    ExecutorProtocol,
    OrderDirection,
    register_algorithm,
)
from axile.executor.algorithms.exceptions import (
    RECOVERABLE_ALGORITHM_EXCEPTIONS,
    execution_error_message,
    format_exception_message,
)
from axile.executor.algorithms.utils import (
    determine_order_price,
    get_default_clock,
    setup_order_tracker,
    submit_and_track_split_orders,
    teardown_order_tracker,
)
from axile.executor.algorithms.utils.final_position import read_final_position
from axile.executor.algorithms.utils.order_tracker import OrderTracker
from axile.executor.algorithms.utils.outcome import summarize_outcome
from axile.executor.algorithms.utils.trading import (
    cancel_pending_orders_via_query,
    create_empty_result,
    create_unconfirmed_start_result,
)
from axile.executor.models.unified_account_assets import UnifiedAccountAssets, is_degraded_snapshot_source
from axile.executor.models.unified_price import clone_price_data
from axile.executor.termination import ExecutionTerminated

ALGORITHM_NAME = "TWAP"


class TwapParams(BaseAlgorithmParams):
    """
    TWAP 算法参数.

    Attributes
    ----------
    total_duration : int
        总执行时长（秒），范围 1-86400。
    slices : int
        均匀切片数量，范围 1-1000；单片间隔为 ``total_duration / slices``。
    price_strategy : {"ACTIVE", "PASSIVE"}
        单片下单价格策略。``ACTIVE`` 取对手价（优先成交，但仍可能剩量）；``PASSIVE`` 取本方价（挂单等待，
        可能欠量，更依赖后续片追平）。
    """

    max_wait_seconds: int = Field(
        default=60,
        title="单片最大等待",
        description="实际取此值与切片间隔中的较小值。",
        ge=1,
        le=3600,
        json_schema_extra={"x-order": 40, "x-unit": "秒", "x-control": "stepper"},
    )

    total_duration: int = Field(
        default=300,
        title="执行时长",
        description="安排拆单节奏的计划时长，不是严格截止时间。",
        ge=1,
        le=86400,
        json_schema_extra={"x-order": 10, "x-unit": "秒", "x-control": "presets", "x-presets": [60, 300, 600, 1800]},
    )
    slices: int = Field(
        default=10,
        title="切片数量",
        description="切片间隔＝执行时长÷片数，至少 0.1 秒。",
        ge=1,
        le=1000,
        json_schema_extra={"x-order": 20, "x-unit": "片", "x-control": "stepper"},
    )
    price_strategy: Literal["ACTIVE", "PASSIVE"] = Field(
        default="ACTIVE",
        title="报价方式",
        description="每片使用本方价或对手价；前片欠量滚入后片。",
        json_schema_extra={
            "x-order": 30,
            "x-control": "choice",
            "x-enum-labels": {"PASSIVE": "本方挂单", "ACTIVE": "对手价"},
        },
    )

    @property
    def interval_seconds(self) -> float:
        """
        单片时间间隔（秒）.

        Returns
        -------
        float
            ``total_duration / slices``。
        """
        return self.total_duration / self.slices

    @model_validator(mode="after")
    def _validate_interval(self) -> "TwapParams":
        """
        校验单片间隔不至于过密.

        Returns
        -------
        TwapParams
            通过校验的参数对象。

        Raises
        ------
        ValueError
            单片间隔小于 0.1 秒时抛出，避免触发交易所限频。
        """
        if self.interval_seconds < 0.1:
            raise ValueError(
                f"单片间隔（total_duration / slices = {self.interval_seconds:.4f}秒）过密，"
                f"不应小于 0.1 秒。请减少 slices 或增加 total_duration。"
            )
        return self

    def __str__(self) -> str:
        """便于记录日志的字符串表示."""
        return (
            "TwapParams("
            f"total_duration={self.total_duration}, "
            f"slices={self.slices}, "
            f"price_strategy={self.price_strategy})"
        )


def compute_slice_quantity(
    total_qty: float,
    slices: int,
    slice_index: int,
    filled: float,
) -> float:
    """
    按累计进度表推导单片下单量.

    Parameters
    ----------
    total_qty : float
        本次执行需要成交的总量（增量绝对值）。
    slices : int
        总切片数。
    slice_index : int
        当前片序号，从 1 开始计数。
    filled : float
        截至当前片开始时的累计已成交量。

    Returns
    -------
    float
        当前片应下的数量；不足以下单时返回 ``0.0``。

    Notes
    -----
    非最后一片取“到该片结束应累计完成的量”减去“已成交量”；最后一片直接吃掉
    全部剩余量，以吸收取整残差与前序欠量；这只决定计划下单量，不保证实际成交。
    """
    if slice_index >= slices:
        return max(total_qty - filled, 0.0)
    scheduled_cum = total_qty * slice_index / slices
    return max(scheduled_cum - filled, 0.0)


def _execute_one_slice(
    executor: ExecutorProtocol,
    tracker: OrderTracker,
    direction: OrderDirection,
    slice_qty: float,
    params: TwapParams,
    account_assets: UnifiedAccountAssets,
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
    params : TwapParams
        TWAP 参数。
    account_assets : UnifiedAccountAssets
        本片开始时的账户资产快照。
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
    未成交余量会由下一片的累计进度自动补回。
    """
    order_type, price = determine_order_price(
        direction,
        executor.get_market_data(),
        price_strategy=params.price_strategy,
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
    cancellation = cancel_pending_orders_via_query(executor)
    unconfirmed = tracker.record_cancellation_result(cancellation)
    if cancellation.orders:
        detail["cancel_outcomes"] = cancellation.outcomes
    if unconfirmed and not tracker.waiting_on_restriction():
        detail["cancel_error"] = unconfirmed
    return detail


def _wait_restricted_slice(
    executor: ExecutorProtocol, tracker: OrderTracker, fill_wait: float, deadline: float, last_slice: bool
) -> bool:
    """受限单沿用当前片的截止时间，仍未终态时跳过本片报单。"""
    if not tracker.waiting_on_restriction():
        return False
    clock = get_default_clock()
    tracker.wait_for_completion(timeout=min(fill_wait, max(0.0, deadline - clock.time())))
    if not tracker.get_pending_count():
        return False
    if not last_slice:
        executor.sleep_or_terminate(max(0.0, deadline - clock.time()))
    return True


@register_algorithm(
    ALGORITHM_NAME,
    order_param_models=ALL_ORDER_PARAM_MODELS,
    params_class=TwapParams,
    label="时间加权",
    description="按设定时长分片下单，前片欠量滚入后片。尾片可能集中补量，不保证全部成交。",
)
def twap(
    executor: ExecutorProtocol,
    algorithm_input: AlgorithmInput,
) -> AlgorithmResult:
    """
    TWAP（时间加权平均价格）算法.

    将目标净持仓相对当前持仓的增量，在 ``total_duration`` 秒内均匀切成
    ``slices`` 片逐片下单。

    Parameters
    ----------
    executor : ExecutorProtocol
        当前算法绑定的执行器会话。
    algorithm_input : AlgorithmInput
        单品种执行输入，``params`` 应为 :class:`TwapParams`。

    Returns
    -------
    AlgorithmResult
        本次执行的订单、成交与账户快照汇总。

    Raises
    ------
    TypeError
        当 ``algorithm_input.params`` 不是 :class:`TwapParams` 时抛出。
    """
    params = algorithm_input.params
    if not isinstance(params, TwapParams):
        raise TypeError(f"TWAP 参数应为 TwapParams，实际为 {type(params).__name__}")

    clock = get_default_clock()
    symbol = algorithm_input.symbol
    target_volume = algorithm_input.target_volume

    account_assets = executor.get_account_assets()
    if is_degraded_snapshot_source(account_assets.source):
        return create_unconfirmed_start_result(
            account_assets, ALGORITHM_NAME, symbol=symbol, target_volume=target_volume
        )
    start_volume = executor.get_current_volume(account_assets)
    first_tick = clone_price_data(executor.get_market_data())

    delta = target_volume - start_volume
    if delta == 0:
        executor.logger.info(f"{symbol} 当前持仓已等于目标 {target_volume}，无需执行")
        return create_empty_result(account_assets, ALGORITHM_NAME, "当前持仓已等于目标，无需执行")

    direction = OrderDirection.BUY if delta > 0 else OrderDirection.SELL
    total_qty = abs(delta)
    interval = params.interval_seconds
    fill_wait = min(interval, float(params.max_wait_seconds))

    executor.logger.info(f"{symbol} 开始执行: 目标={target_volume}, 当前={start_volume}, 增量={delta}, {params}")

    tracker = setup_order_tracker(executor, executor.get_market_data(), None)
    slice_details: list[dict[str, Any]] = []

    started = clock.time()
    try:
        for slice_index in range(1, params.slices + 1):
            executor.handle_termination_checkpoint()

            slice_deadline = started + slice_index * interval
            if clock.time() >= slice_deadline:
                continue
            if _wait_restricted_slice(executor, tracker, fill_wait, slice_deadline, slice_index == params.slices):
                slice_details.append(
                    {"slice_index": slice_index, "skipped": True, "reason": "原委托撤单受限，继续跟踪"}
                )
                continue
            account_assets = executor.get_account_assets()
            current_volume = executor.get_current_volume(account_assets)
            filled = abs(current_volume - start_volume)
            slice_qty = compute_slice_quantity(total_qty, params.slices, slice_index, filled)

            if slice_qty > 0:
                executor.logger.info(
                    f"{symbol} 第 {slice_index}/{params.slices} 片: 下单 {slice_qty}（已成交 {filled}/{total_qty}）"
                )
                detail = _execute_one_slice(
                    executor,
                    tracker,
                    direction,
                    slice_qty,
                    params,
                    account_assets,
                    current_volume,
                    target_volume,
                    min(fill_wait, max(0.0, slice_deadline - clock.time())),
                )
            else:
                detail = {"skipped": True, "reason": "累计进度已达标，本片无需下单"}
            detail["slice_index"] = slice_index
            slice_details.append(detail)

            # 末片无需再等待，直接结束以贴合总时长语义。
            if slice_index < params.slices:
                remaining = slice_deadline - clock.time()
                if remaining > 0:
                    executor.sleep_or_terminate(remaining)
    except ExecutionTerminated:
        raise
    except RECOVERABLE_ALGORITHM_EXCEPTIONS as exc:
        if getattr(exc, "requires_session_recovery", False):
            raise
        if not isinstance(tracker.explicit_error, str):
            tracker.explicit_error = execution_error_message(exc)
        executor.logger.exception("TWAP 执行失败")
    finally:
        teardown_order_tracker(executor, tracker, None)

    account_assets, final_volume, explicit_error = read_final_position(executor, executor.get_current_volume)
    if explicit_error and not isinstance(tracker.explicit_error, str):
        tracker.explicit_error = explicit_error
    orders = tracker.get_all_orders()
    trades = tracker.get_all_trades()

    executor.logger.info(
        f"{symbol} 执行完成: 最终持仓={final_volume}, 目标={target_volume}, 订单数={len(orders)}, 成交数={len(trades)}"
    )

    return AlgorithmResult(
        **summarize_outcome(
            start_volume,
            final_volume,
            target_volume,
            orders,
            trades,
            explicit_error=(tracker.explicit_error if isinstance(tracker.explicit_error, str) else None)
            or (tracker.pending_reason if isinstance(tracker.pending_reason, str) else None),
            explicit_blocked_error=tracker.explicit_blocked_error
            if isinstance(tracker.explicit_blocked_error, str)
            else None,
        ),
        orders=orders,
        trades=trades,
        account_assets=account_assets,
        target_volume=target_volume,
        first_tick=first_tick,
        memory={
            "algorithm": ALGORITHM_NAME,
            "target_volume": target_volume,
            "start_volume": start_volume,
            "final_volume": final_volume,
            "remaining_volume": target_volume - final_volume,
            "total_quantity": total_qty,
            "slices": params.slices,
            "total_duration": params.total_duration,
            "slice_details": slice_details,
            "orders_generated": len(orders),
            "trades_generated": len(trades),
            "total_asset": account_assets.total_asset,
        },
    )
