"""
交易辅助模块.

提供空结果构造、下单价格计算和算法层订单操作辅助函数。
"""

from typing import Any

from axile.executor.algorithms.core.base import AlgorithmResult, ExecutorProtocol
from axile.executor.models.execution_result import ExecutionOutcome, ExecutionStatus
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_order import OrderDirection, OrderType
from axile.executor.models.unified_price import UnifiedPriceData


def create_empty_result(
    account_assets: UnifiedAccountAssets,
    algorithm_name: str,
    message: str = "没有需要执行的交易",
    execution_memory: dict[str, Any] | None = None,
) -> AlgorithmResult:
    """
    构造“无需下单”的标准算法结果。

    Parameters
    ----------
    account_assets : UnifiedAccountAssets
        当前账户资产快照。
    algorithm_name : str
        生成该结果的算法名称。
    message : str, default="没有需要执行的交易"
        返回给上层的说明消息。
    execution_memory : dict[str, Any] | None, default=None
        需要带回执行结果的附加上下文。

    Returns
    -------
    AlgorithmResult
        不包含订单，但保留账户快照和执行说明的结果对象。
    """
    memory: dict[str, Any] = {
        "algorithm": algorithm_name,
        "message": message,
    }
    if execution_memory:
        memory["execution_details"] = execution_memory

    return AlgorithmResult(
        outcome=ExecutionOutcome.COMPLETED,
        status=ExecutionStatus.NOOP,
        orders=[],
        account_assets=account_assets,
        target_volume=None,
        first_tick=None,
        memory=memory,
    )


def create_unconfirmed_start_result(
    account_assets: UnifiedAccountAssets,
    algorithm_name: str,
    *,
    symbol: str,
    target_volume: float,
) -> AlgorithmResult:
    """初始快照不可读时直接交还上层，禁止把未知持仓当成零去下单。"""
    return AlgorithmResult(
        symbol=symbol,
        algorithm=algorithm_name,
        status=ExecutionStatus.FAILED,
        error="初始持仓尚未确认",
        orders=[],
        account_assets=account_assets,
        target_volume=target_volume,
        memory={"algorithm": algorithm_name, "message": "初始持仓尚未确认"},
    )


def determine_order_price(
    direction: OrderDirection,
    market_data: UnifiedPriceData | None,
    price_strategy: str = "PASSIVE",
) -> tuple[OrderType, float]:
    """
    根据价格策略推导下单类型与报价。

    Parameters
    ----------
    direction : OrderDirection
        当前订单方向。
    market_data : UnifiedPriceData | None
        当前品种的盘口快照；为空时直接回退到市价单。
    price_strategy : str, default="PASSIVE"
        价格策略。``PASSIVE`` 倾向挂单等待，``ACTIVE`` 倾向主动成交。

    Returns
    -------
    tuple[OrderType, float]
        ``(order_type, price)`` 元组。

    Notes
    -----
    ``ACTIVE`` 与 ``PASSIVE`` 的核心区别不是订单类型，而是优先取对手价还是本方价。
    如果缺少可用盘口，算法层直接回退到市价单，避免把 ``0`` 价限价单送给下游执行器。
    """
    order_type = OrderType.LIMIT
    price = 0.0

    if market_data is None:
        return OrderType.MARKET, 0.0

    tick = market_data

    if price_strategy.upper() == "ACTIVE":
        if direction == OrderDirection.BUY:
            price = (
                (tick.asks[0][0] if tick.asks else 0.0)
                if (tick.asks[0][0] if tick.asks else 0.0) > 0
                else (tick.bids[0][0] if tick.bids else 0.0)
            )
        else:
            price = (
                (tick.bids[0][0] if tick.bids else 0.0)
                if (tick.bids[0][0] if tick.bids else 0.0) > 0
                else (tick.asks[0][0] if tick.asks else 0.0)
            )
    else:  # PASSIVE
        if direction == OrderDirection.BUY:
            price = (
                (tick.bids[0][0] if tick.bids else 0.0)
                if (tick.bids[0][0] if tick.bids else 0.0) > 0
                else (tick.asks[0][0] if tick.asks else 0.0)
            )
        else:
            price = (
                (tick.asks[0][0] if tick.asks else 0.0)
                if (tick.asks[0][0] if tick.asks else 0.0) > 0
                else (tick.bids[0][0] if tick.bids else 0.0)
            )

    if price <= 0:
        order_type = OrderType.MARKET

    return order_type, price


def cancel_pending_orders_via_query(executor: ExecutorProtocol) -> list[str]:
    """
    通过主动查询结果逐笔撤销当前品种的挂单。

    Parameters
    ----------
    executor : ExecutorProtocol
        当前算法绑定的执行器会话。

    Returns
    -------
    list[str]
        撤单失败的订单编号列表。

    Notes
    -----
    该辅助函数用于超时或终止等兜底场景。这里显式先 query 再 cancel，而不是依赖
    tracker 内存态，是为了尽量收敛执行器侧真实仍然挂着的订单。
    """
    failed_order_ids: list[str] = []
    for order in executor.get_pending_orders():
        try:
            canceled = executor.cancel_order(order.order_id)
        except Exception as exc:
            if bool(getattr(exc, "requires_session_recovery", False)):
                raise
            failed_order_ids.append(order.order_id)
            continue

        if not canceled:
            failed_order_ids.append(order.order_id)

    return failed_order_ids
