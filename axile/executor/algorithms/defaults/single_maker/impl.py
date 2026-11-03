"""
单一做市商算法（回调版本）.

使用事件驱动等待，支持追单功能。
适用于提供统一订单与行情能力的执行器。

使用示例：
    input_data = UnifiedStandardInput(
        algorithm={
            "method": "SINGLE-MAKER",
            "params": {
                "max_wait_seconds": 60,
                "chase_enabled": True,
                "chase_ticks": 1,
                "max_chase_count": 5,
                "chase_interval": 5,
            }
        },
        ...
    )
"""

from typing import Any, Literal

from pydantic import Field

from axile.domain.execution import ExecutionReasonFamily
from axile.executor.algorithms.common.params import BaseAlgorithmParams, ChaseParamsMixin
from axile.executor.algorithms.core.base import (
    ALL_ORDER_PARAM_MODELS,
    AlgorithmInput,
    AlgorithmResult,
    ExecutorProtocol,
    OrderDirection,
    OrderType,
    register_algorithm,
)
from axile.executor.algorithms.exceptions import (
    RECOVERABLE_ALGORITHM_EXCEPTIONS,
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
from axile.executor.algorithms.utils.order_tracker import ChaseConfig, OrderTracker
from axile.executor.algorithms.utils.outcome import summarize_outcome
from axile.executor.algorithms.utils.trading import create_unconfirmed_start_result
from axile.executor.models.execution_result import ExecutionStatus
from axile.executor.models.unified_account_assets import UnifiedAccountAssets, is_degraded_snapshot_source
from axile.executor.models.unified_price import UnifiedPriceData, clone_price_data
from axile.executor.termination import ExecutionTerminated


class SingleMakerParams(BaseAlgorithmParams, ChaseParamsMixin):
    """SINGLE-MAKER 算法参数."""

    price_strategy: Literal["PASSIVE", "ACTIVE"] = Field(
        default="ACTIVE",
        title="报价方式",
        description="本方挂单等成交；对手价优先成交并承担点差。",
        json_schema_extra={
            "x-order": 10,
            "x-control": "choice",
            "x-enum-labels": {"PASSIVE": "本方挂单", "ACTIVE": "对手价"},
        },
    )
    on_missing_book: Literal["skip", "active", "market"] = Field(
        default="skip",
        title="盘口缺失处理",
        description="盘口无效时可跳过或请求市价单；active 仅兼容旧配置，行为与跳过相同。",
        json_schema_extra={
            "x-order": 30,
            "x-control": "cards",
            "x-option-descriptions": {
                "skip": "跳过本次下单。",
                "active": "仅兼容旧配置：盘口无效时跳过，不会转市价。",
                "market": "提交市价单，成交价格由市场决定。",
            },
            "x-enum-labels": {"skip": "跳过", "active": "尝试主动报价", "market": "市价单"},
        },
    )

    def __str__(self) -> str:
        """便于记录日志的字符串表示."""
        return (
            "SingleMakerParams("
            f"max_wait_seconds={self.max_wait_seconds}, "
            f"chase_enabled={self.chase_enabled}, "
            f"price_strategy={self.price_strategy}, "
            f"on_missing_book={self.on_missing_book})"
        )


def _resolve_pricing_on_book(
    direction: OrderDirection,
    market_data: UnifiedPriceData | None,
    params: SingleMakerParams,
) -> tuple[OrderType, float] | None:
    """
    结合盘口有效性与 ``on_missing_book`` 策略推导挂价.

    Parameters
    ----------
    direction : OrderDirection
        订单方向。
    market_data : UnifiedPriceData | None
        当前品种盘口快照。
    params : SingleMakerParams
        算法参数。

    Returns
    -------
    tuple[OrderType, float] | None
        ``(order_type, price)``；盘口无效且未显式请求市价时返回 ``None``。
    """
    if market_data is not None and market_data.book_valid:
        pricing = determine_order_price(direction, market_data, price_strategy=params.price_strategy)
        if pricing[0] != OrderType.MARKET:
            return pricing
    if params.on_missing_book == "market":
        return OrderType.MARKET, 0.0
    return None


def _missing_book_result(
    executor: ExecutorProtocol,
    account_assets: UnifiedAccountAssets,
    algorithm_name: str,
    symbol: str,
    target_volume: float,
    first_tick: UnifiedPriceData | None,
    execution_memory: dict[str, Any],
    chase_config: ChaseConfig | None,
) -> AlgorithmResult:
    """盘口无效且未请求市价时构造本轮阻断结果。"""
    executor.logger.warning(f"{symbol} 盘口买卖一无效，本轮不下单")
    execution_memory[f"{symbol}_skipped"] = "missing_book"
    return AlgorithmResult(
        orders=[],
        trades=[],
        account_assets=account_assets,
        target_volume=target_volume,
        first_tick=first_tick,
        status=ExecutionStatus.BLOCKED,
        error="盘口买卖一无效，本轮未执行",
        reason_code="COMMON.MARKET_DATA.UNAVAILABLE",
        reason_family=ExecutionReasonFamily.MARKET_RULE,
        memory={
            "algorithm": algorithm_name,
            "target_volume": target_volume,
            "execution_details": execution_memory,
            "symbols_processed": 1,
            "orders_generated": 0,
            "trades_generated": 0,
            "total_asset": account_assets.total_asset,
            "chase_enabled": chase_config is not None,
        },
    )


def _record_submit_failure(
    executor: ExecutorProtocol,
    tracker: OrderTracker,
    execution_memory: dict[str, Any],
    symbol: str,
    exc: BaseException,
) -> None:
    """把报单阶段的可恢复异常落到显式错误通道与执行记忆；原文进日志。"""
    tracker.record_exception(exc, "报单")
    error_message = format_exception_message(exc)
    executor.logger.error(f"{symbol} 下单失败: {error_message}")
    execution_memory[f"{symbol}_error"] = error_message


def _recover_outer_failure(
    executor: ExecutorProtocol,
    tracker: OrderTracker,
    exc: BaseException,
) -> tuple[UnifiedAccountAssets, float]:
    """外层可恢复异常的收尾：补显式错误并重读最终持仓。"""
    if not tracker.explicit_error:
        tracker.record_exception(exc)
    executor.logger.exception("SINGLE-MAKER 执行失败")
    assets, final_volume, query_error = read_final_position(executor, executor.get_current_volume)
    if query_error:
        tracker.explicit_error = query_error
    return assets, final_volume


@register_algorithm(
    "SINGLE-MAKER",
    order_param_models=ALL_ORDER_PARAM_MODELS,
    params_class=SingleMakerParams,
    label="单边挂单",
    description="按本方或对手价下单，可撤单追价。适合不需要分时拆单的调仓；主动报价也可能剩量。",
)
def single_maker_callback(
    executor: ExecutorProtocol,
    algorithm_input: AlgorithmInput,
) -> AlgorithmResult:
    """单一做市商算法（回调版本）.

    使用事件驱动等待，支持追单功能。
    适用于提供统一订单与行情能力的执行器。

    算法参数（通过 algorithm_input.params 配置）：
        - max_wait_seconds: 最大等待时间（秒），默认 60
        - chase_enabled: 是否启用追单，默认 False
        - chase_ticks: 价格偏离多少跳后追单，默认 1
        - max_chase_count: 单个订单最大追单次数，默认 5
        - chase_interval: 追单间隔（秒），默认 5
        - price_strategy: 下单价格策略，默认 ACTIVE
    """
    algorithm_name = "SINGLE-MAKER"

    params = algorithm_input.params
    if not isinstance(params, SingleMakerParams):
        raise TypeError(f"SINGLE-MAKER 参数应为 SingleMakerParams，实际为 {type(params).__name__}")
    max_wait_seconds = params.max_wait_seconds
    chase_config = ChaseConfig.from_params(params)

    executor.logger.info(f"追单配置: {chase_config is not None}")

    account_assets = executor.get_account_assets()
    log_debug = getattr(executor.logger, "debug", executor.logger.info)
    log_debug(f"账户总资产: {account_assets.total_asset:.2f}")

    symbol = algorithm_input.symbol
    market_data = clone_price_data(executor.get_market_data())
    if market_data:
        log_debug(f"获取到 {symbol} 的价格数据")

    target_volume = algorithm_input.target_volume
    first_tick = clone_price_data(market_data)
    # 初始快照降级时尚未产生订单，直接交还上层；不能把未知持仓当成零去比较目标并下单。
    if is_degraded_snapshot_source(account_assets.source):
        return create_unconfirmed_start_result(
            account_assets, algorithm_name, symbol=symbol, target_volume=target_volume
        )
    current_volume = executor.get_current_volume(account_assets)
    final_volume = current_volume

    # 使用工具函数设置订单跟踪器
    tracker = setup_order_tracker(executor, market_data, chase_config)

    execution_memory: dict[str, Any] = {}

    try:
        executor.logger.info(f"开始执行 {symbol} 的交易")

        # 执行调仓
        if target_volume != current_volume:
            direction = OrderDirection.BUY if target_volume > current_volume else OrderDirection.SELL
            needed_volume = abs(target_volume - current_volume)
            pricing = _resolve_pricing_on_book(direction, market_data, params)
            if pricing is None:
                return _missing_book_result(
                    executor,
                    account_assets,
                    algorithm_name,
                    symbol,
                    target_volume,
                    first_tick,
                    execution_memory,
                    chase_config,
                )
            order_type, price = pricing

            executor.logger.info(
                f"{symbol} {direction.value} {needed_volume}, 当前={current_volume}, 目标={target_volume}"
            )

            deadline = get_default_clock().time() + max_wait_seconds
            try:
                # 使用工具函数提交和跟踪订单;期货渠道穿零调仓自动拆成先平后开两腿
                orders = submit_and_track_split_orders(
                    executor,
                    tracker,
                    direction,
                    order_type,
                    needed_volume,
                    price,
                    target_volume=float(target_volume),
                    current_volume=float(current_volume),
                    leg_timeout_seconds=max_wait_seconds,
                    deadline=deadline,
                    market_data=market_data,
                )
                if not orders:
                    execution_memory[f"{symbol}_skipped"] = "no_order_submitted"
                else:
                    execution_memory[f"{symbol}_adjustment"] = {
                        "from": current_volume,
                        "to": target_volume,
                        "diff": target_volume - current_volume,
                        "direction": direction.value,
                        "volume": needed_volume,
                        "order_id": orders[0].order_id,
                        "order_ids": [order.order_id for order in orders],
                    }
            except (MemoryError, ExecutionTerminated):
                raise
            except RECOVERABLE_ALGORITHM_EXCEPTIONS as e:
                if getattr(e, "requires_session_recovery", False):
                    raise
                _record_submit_failure(executor, tracker, execution_memory, symbol, e)

            # 等待订单完成
            tracker.wait_for_completion(timeout=max(0.0, deadline - get_default_clock().time()))

            # 重新获取账户资产
            account_assets, final_volume, explicit_error = read_final_position(executor, executor.get_current_volume)
            if explicit_error:
                tracker.explicit_error = explicit_error

    except ExecutionTerminated:
        raise
    except RECOVERABLE_ALGORITHM_EXCEPTIONS as exc:
        if getattr(exc, "requires_session_recovery", False):
            raise
        account_assets, final_volume = _recover_outer_failure(executor, tracker, exc)
    finally:
        # 使用工具函数清理订单跟踪器
        teardown_order_tracker(executor, tracker, chase_config)

    orders = tracker.get_all_orders()
    trades = tracker.get_all_trades()

    return AlgorithmResult(
        **summarize_outcome(
            current_volume,
            final_volume,
            target_volume,
            orders,
            trades,
            **tracker.outcome_inputs(),
        ),
        orders=orders,
        trades=trades,
        account_assets=account_assets,
        target_volume=target_volume,
        first_tick=first_tick,
        memory={
            "algorithm": algorithm_name,
            "target_volume": algorithm_input.target_volume,
            "execution_details": execution_memory,
            "start_volume": current_volume,
            "final_volume": final_volume,
            "remaining_volume": target_volume - final_volume,
            "symbols_processed": 1,
            "orders_generated": len(orders),
            "trades_generated": len(trades),
            "total_asset": account_assets.total_asset,
            "chase_enabled": chase_config is not None,
        },
    )
