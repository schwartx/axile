"""报单前的方向与订单类型校验。"""

from axile.executor.models.unified_order import OrderDirection, OrderType


class OrderInputValidationError(ValueError):
    """报单意图无效，尚未进入渠道发送。"""

    @property
    def execution_error(self) -> str:
        """提供可用于执行记录及操作失败记录的明确拦截说明。"""
        return f"报单参数无效，已在发送前拦截：{self}"


def normalize_order_enums(direction: object, order_type: object) -> tuple[OrderDirection, OrderType]:
    """将合法枚举或其精确字符串值规范化，拒绝猜测未知报单意图。"""
    try:
        normalized_direction = OrderDirection(direction)
    except (TypeError, ValueError) as exc:
        raise OrderInputValidationError(f"不支持的买卖方向: {repr(direction)[:80]}") from exc
    try:
        normalized_type = OrderType(order_type)
    except (TypeError, ValueError) as exc:
        raise OrderInputValidationError(f"不支持的订单类型: {repr(order_type)[:80]}") from exc
    return normalized_direction, normalized_type
