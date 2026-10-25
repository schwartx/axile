"""
POV（Percentage of Volume，参与率）算法.

按「市场实时成交量」的固定比例跟单：市场成交活跃时多下、清淡时少下，
使自身始终只占市场成交的一小部分，从而降低冲击成本、隐蔽大单。

设计要点
--------
- 目标语义为「净持仓」：先把 ``target_volume`` 转成相对当前持仓的增量，再对增量按
  市场量参与，因此表达「调仓到位」而非「买入 N 手」。
- 市场成交量来源：注册价格回调，累加每次行情更新携带的**增量成交量**
  （``UnifiedPriceData.volume``）。仿真逐 tick 增量；累计型渠道（如 CTP 日内累计量）
  需另行适配，暂不在本实现范围。
- 累计跟踪：目标累计成交 = ``参与率 × 已见市场量``，本轮下单量 = 该目标减去已成交量，
  与 TWAP 的累计进度表同思想——天然实现对欠量的追平，且不超过整体目标。
- 无量安全：市场无量则不下单，跑到 ``max_duration`` 上限；``complete_on_timeout`` 为真时
  到期按原报价方式尝试补下剩余量，不再受参与率限制，也不保证成交。
- 全渠道通用：不在算法层做 lot / 最小下单量取整，交由各渠道执行器兜底。

使用示例
--------
    input_data = UnifiedStandardInput(
        algorithm={
            "method": "POV",
            "params": {
                "participation_rate": 0.1,
                "interval_seconds": 5,
                "max_duration": 600,
                "price_strategy": "ACTIVE",
                "complete_on_timeout": True,
            },
        },
        ...
    )
"""

from typing import Any, Literal

from pydantic import Field, model_validator

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
)
from axile.executor.algorithms.utils import (
    get_default_clock,
    setup_order_tracker,
    teardown_order_tracker,
)
from axile.executor.algorithms.utils.final_position import read_final_position
from axile.executor.algorithms.utils.order_helper import execute_order_slice
from axile.executor.algorithms.utils.order_tracker import OrderTracker
from axile.executor.algorithms.utils.outcome import summarize_outcome
from axile.executor.algorithms.utils.trading import (
    create_empty_result,
    create_unconfirmed_start_result,
)
from axile.executor.models.unified_account_assets import is_degraded_snapshot_source
from axile.executor.models.unified_price import UnifiedPriceData, clone_price_data
from axile.executor.termination import ExecutionTerminated

ALGORITHM_NAME = "POV"


class PovParams(BaseAlgorithmParams):
    """
    POV 算法参数.

    Attributes
    ----------
    participation_rate : float
        目标市场成交量参与比例，范围 (0, 1]。
    interval_seconds : float
        轮询与下单节奏（秒），不小于 0.1。
    max_duration : int
        跟量阶段时间上限（秒），范围 1-86400，且不小于 ``interval_seconds``；补单可延长总耗时。
    price_strategy : {"ACTIVE", "PASSIVE"}
        单片下单价格策略。``ACTIVE`` 取对手价（marketable，跟量成交更确定），
        ``PASSIVE`` 取本方价等待成交，可能欠量。
    complete_on_timeout : bool
        到达时间上限且仍有欠量时，是否尝试按原报价方式补单。
    """

    max_wait_seconds: int = Field(
        default=60,
        title="单笔最大等待",
        description="实际取此值与下单间隔中的较小值。",
        ge=1,
        le=3600,
        json_schema_extra={"x-order": 60, "x-unit": "秒", "x-control": "stepper"},
    )

    participation_rate: float = Field(
        default=0.1,
        title="参与率",
        description="按收到的市场增量成交量计算目标参与量。",
        gt=0.0,
        le=1.0,
        json_schema_extra={
            "x-order": 10,
            "x-unit": "%",
            "x-control": "numberflow",
            "x-display-scale": 100,
            "x-display-step": 1,
        },
    )
    interval_seconds: float = Field(
        default=5.0,
        title="下单间隔",
        description="检查成交进度和下单的间隔。",
        ge=0.1,
        json_schema_extra={"x-order": 20, "x-unit": "秒", "x-control": "stepper"},
    )
    max_duration: int = Field(
        default=600,
        title="跟量时限",
        description="跟量阶段的时间上限，结束处理和补单可能延长总耗时。",
        ge=1,
        le=86400,
        json_schema_extra={"x-order": 30, "x-unit": "秒", "x-control": "presets", "x-presets": [60, 300, 600, 1800]},
    )
    price_strategy: Literal["ACTIVE", "PASSIVE"] = Field(
        default="ACTIVE",
        title="报价方式",
        description="每笔使用本方价或对手价，主动报价也可能剩量。",
        json_schema_extra={
            "x-order": 40,
            "x-control": "choice",
            "x-enum-labels": {"PASSIVE": "本方挂单", "ACTIVE": "对手价"},
        },
    )
    complete_on_timeout: bool = Field(
        default=True,
        title="到期补单",
        description="按原报价方式尝试补下剩余量，不再受参与率约束，不保证成交。",
        json_schema_extra={"x-order": 50},
    )

    @model_validator(mode="after")
    def _validate_duration(self) -> "PovParams":
        """
        校验时间上限至少容纳一个轮询间隔.

        Returns
        -------
        PovParams
            通过校验的参数对象。

        Raises
        ------
        ValueError
            ``max_duration`` 小于 ``interval_seconds`` 时抛出。
        """
        if self.max_duration < self.interval_seconds:
            raise ValueError(
                f"max_duration（{self.max_duration}秒）不应小于 interval_seconds"
                f"（{self.interval_seconds}秒），否则无法完成一个轮询。"
            )
        return self

    def __str__(self) -> str:
        """便于记录日志的字符串表示."""
        return (
            "PovParams("
            f"participation_rate={self.participation_rate}, "
            f"interval_seconds={self.interval_seconds}, "
            f"max_duration={self.max_duration}, "
            f"price_strategy={self.price_strategy}, "
            f"complete_on_timeout={self.complete_on_timeout})"
        )


class _VolumeAccumulator:
    """累加价格回调携带的增量市场成交量.

    Notes
    -----
    仿真下回调与算法同线程、同步触发；真实渠道下由 WebSocket 线程写、算法线程读。
    仅有单一写入方且累加单调，读取允许轻微滞后，故不加锁。
    """

    def __init__(self) -> None:
        """初始化累加器."""
        self.total: float = 0.0

    def on_price(self, price_data: UnifiedPriceData) -> None:
        """价格回调：累加非负增量成交量.

        Parameters
        ----------
        price_data : UnifiedPriceData
            一次行情更新快照。
        """
        volume = price_data.volume
        if volume and volume > 0:
            self.total += volume


def compute_participation_want(
    participation_rate: float,
    market_seen: float,
    filled: float,
    total_qty: float,
) -> float:
    """
    按累计参与率推导本轮应补下的数量.

    Parameters
    ----------
    participation_rate : float
        目标市场成交量参与比例。
    market_seen : float
        截至当前累计观测到的市场成交量。
    filled : float
        截至当前的累计已成交量。
    total_qty : float
        本次执行需要成交的总量（增量绝对值）。

    Returns
    -------
    float
        本轮应下的数量；不足以下单时返回 ``0.0``。

    Notes
    -----
    目标累计成交 = ``参与率 × 已见市场量``；本轮补下量为该目标减去已成交量，
    并以「距目标的剩余量」封顶，使本轮计划量追平欠量且不超过剩余目标量；不承诺实际成交。
    """
    desired_cum = participation_rate * market_seen
    want = min(desired_cum - filled, total_qty - filled)
    return max(want, 0.0)


@register_algorithm(
    ALGORITHM_NAME,
    order_param_models=ALL_ORDER_PARAM_MODELS,
    params_class=PovParams,
    label="成交量参与率",
    description="按收到的市场增量成交量乘参与率跟单，无量等待。到期补单不再受参与率约束，仍可能剩量。",
)
def pov(
    executor: ExecutorProtocol,
    algorithm_input: AlgorithmInput,
) -> AlgorithmResult:
    """
    POV（参与率）算法.

    按收到的市场增量成交量计算参与量，逐步尝试完成目标持仓差量。

    Parameters
    ----------
    executor : ExecutorProtocol
        当前算法绑定的执行器会话。
    algorithm_input : AlgorithmInput
        单品种执行输入，``params`` 应为 :class:`PovParams`。

    Returns
    -------
    AlgorithmResult
        本次执行的订单、成交与账户快照汇总。

    Raises
    ------
    TypeError
        当 ``algorithm_input.params`` 不是 :class:`PovParams` 时抛出。
    """
    params = algorithm_input.params
    if not isinstance(params, PovParams):
        raise TypeError(f"POV 参数应为 PovParams，实际为 {type(params).__name__}")

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

    accumulator = _VolumeAccumulator()
    tracker = setup_order_tracker(executor, executor.get_market_data(), None)
    executor.register_price_callback(accumulator.on_price)
    slice_details: list[dict[str, Any]] = []

    try:
        _run_participation_loop(
            executor,
            tracker,
            accumulator,
            params,
            direction,
            total_qty,
            start_volume,
            target_volume,
            fill_wait,
            slice_details,
        )
    except ExecutionTerminated:
        raise
    except RECOVERABLE_ALGORITHM_EXCEPTIONS as exc:
        if getattr(exc, "requires_session_recovery", False):
            raise
        if not isinstance(tracker.explicit_error, str):
            tracker.explicit_error = execution_error_message(exc)
        executor.logger.exception("POV 执行失败")
    finally:
        executor.unregister_price_callback(accumulator.on_price)
        teardown_order_tracker(executor, tracker, None)

    account_assets, final_volume, explicit_error = read_final_position(executor, executor.get_current_volume)
    if explicit_error and not isinstance(tracker.explicit_error, str):
        tracker.explicit_error = explicit_error
    orders = tracker.get_all_orders()
    trades = tracker.get_all_trades()

    executor.logger.info(
        f"{symbol} 执行完成: 最终持仓={final_volume}, 目标={target_volume}, "
        f"市场累计量={accumulator.total:.4f}, 订单数={len(orders)}, 成交数={len(trades)}"
    )

    return AlgorithmResult(
        **summarize_outcome(
            start_volume,
            final_volume,
            target_volume,
            orders,
            trades,
            explicit_error=tracker.explicit_error if isinstance(tracker.explicit_error, str) else None,
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
            "participation_rate": params.participation_rate,
            "market_volume_seen": accumulator.total,
            "slice_details": slice_details,
            "orders_generated": len(orders),
            "trades_generated": len(trades),
            "total_asset": account_assets.total_asset,
        },
    )


def _run_participation_loop(
    executor: ExecutorProtocol,
    tracker: OrderTracker,
    accumulator: _VolumeAccumulator,
    params: PovParams,
    direction: OrderDirection,
    total_qty: float,
    start_volume: float,
    target_volume: float,
    fill_wait: float,
    slice_details: list[dict[str, Any]],
) -> None:
    """
    运行 POV 参与主循环，就地追加逐片执行明细.

    Parameters
    ----------
    executor : ExecutorProtocol
        当前算法绑定的执行器会话。
    tracker : OrderTracker
        跨切片复用的订单跟踪器。
    accumulator : _VolumeAccumulator
        市场成交量累加器。
    params : PovParams
        POV 参数。
    direction : OrderDirection
        下单方向。
    total_qty : float
        需要成交的总量（增量绝对值）。
    start_volume : float
        执行开始时的当前持仓量。
    target_volume : float
        整体目标持仓量。
    fill_wait : float
        单片等待成交的最长时间（秒）。
    slice_details : list[dict[str, Any]]
        执行明细列表，就地追加逐片与补单明细。

    Notes
    -----
    循环按 ``interval`` 节奏轮询：读取已成交量与已见市场量，按累计参与率补下；
    达到目标即提前退出；到达时间上限后，``complete_on_timeout`` 为真则尝试提交剩余量。
    """
    clock = get_default_clock()
    interval = params.interval_seconds
    deadline = clock.time() + params.max_duration

    while clock.time() < deadline:
        executor.handle_termination_checkpoint()

        account_assets = executor.get_account_assets()
        current_volume = executor.get_current_volume(account_assets)
        filled = abs(current_volume - start_volume)
        if filled >= total_qty:
            break

        want = compute_participation_want(params.participation_rate, accumulator.total, filled, total_qty)
        if want > 0:
            executor.logger.info(
                f"{executor.symbol} 参与下单 {want}（已见市场量 {accumulator.total:.4f}, 已成交 {filled}/{total_qty}）"
            )
            detail = execute_order_slice(
                executor, tracker, direction, want, params.price_strategy, current_volume, target_volume, fill_wait
            )
            detail["market_seen"] = accumulator.total
            slice_details.append(detail)

        executor.sleep_or_terminate(interval)

    _maybe_complete_on_timeout(
        executor, tracker, params, direction, total_qty, start_volume, target_volume, fill_wait, slice_details
    )


def _maybe_complete_on_timeout(
    executor: ExecutorProtocol,
    tracker: OrderTracker,
    params: PovParams,
    direction: OrderDirection,
    total_qty: float,
    start_volume: float,
    target_volume: float,
    fill_wait: float,
    slice_details: list[dict[str, Any]],
) -> None:
    """
    到达时间上限后按需尝试提交剩余量，不再受参与率限制.

    Parameters
    ----------
    executor : ExecutorProtocol
        当前算法绑定的执行器会话。
    tracker : OrderTracker
        跨切片复用的订单跟踪器。
    params : PovParams
        POV 参数。
    direction : OrderDirection
        下单方向。
    total_qty : float
        需要成交的总量（增量绝对值）。
    start_volume : float
        执行开始时的当前持仓量。
    target_volume : float
        整体目标持仓量。
    fill_wait : float
        补单等待成交的最长时间（秒）。
    slice_details : list[dict[str, Any]]
        执行明细列表，就地追加补单明细。
    """
    if not params.complete_on_timeout:
        return

    account_assets = executor.get_account_assets()
    current_volume = executor.get_current_volume(account_assets)
    filled = abs(current_volume - start_volume)
    remaining = total_qty - filled
    if remaining <= 0:
        return

    executor.logger.info(f"{executor.symbol} 到期补齐剩余量 {remaining}（已成交 {filled}/{total_qty}）")
    detail = execute_order_slice(
        executor, tracker, direction, remaining, params.price_strategy, current_volume, target_volume, fill_wait
    )
    detail["reason"] = "complete_on_timeout"
    slice_details.append(detail)
