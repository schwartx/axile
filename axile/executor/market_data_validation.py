"""行情使用前的公共校验；渠道负责解释自己的时间和市场规则。"""

import math
from typing import Literal

from axile.executor.models.unified_price import UnifiedPriceData

MarketDataPurpose = Literal["sizing", "order"]
DEFAULT_QUOTE_MAX_AGE_SECONDS = 10.0
"""与 OrderTracker 共用默认十秒时效；渠道已有配置时覆盖此默认值。"""

QUOTE_FUTURE_TOLERANCE_SECONDS = 1.0


class MarketDataUnavailableError(ValueError):
    """行情不足以支持新增交易，查询与撤单不受影响。"""

    reason_code = "COMMON.MARKET_DATA.UNAVAILABLE"

    def __init__(self, symbol: str, reason: str) -> None:
        self.execution_error = f"{symbol} 行情不可用于交易：{reason}"
        super().__init__(self.execution_error)


def quote_time_error(
    timestamp_ms: int,
    *,
    now: float,
    max_age: float,
    future_tolerance: float = QUOTE_FUTURE_TOLERANCE_SECONDS,
) -> str | None:
    """按校验时钟判断行情事件时间是否在允许范围内。"""
    if not math.isfinite(now) or timestamp_ms <= 0:
        return "行情时间无效"
    age = now - timestamp_ms / 1000
    if age < -future_tolerance:
        return "行情时间超前"
    if age > max_age:
        return "行情已过期"
    return None


def market_data_error(
    quote: UnifiedPriceData | None,
    *,
    now: float,
    max_age: float,
    purpose: MarketDataPurpose,
    future_tolerance: float = QUOTE_FUTURE_TOLERANCE_SECONDS,
) -> str | None:
    """定量只要求参考价；下单要求真实且未交叉的可用盘口。"""
    if quote is None:
        return "行情未就绪"
    time_error = quote_time_error(quote.timestamp, now=now, max_age=max_age, future_tolerance=future_tolerance)
    if time_error is not None:
        return time_error
    if purpose == "sizing":
        return None if math.isfinite(quote.last_price) and quote.last_price > 0 else "参考价格无效"
    if not quote.book_valid:
        return "缺少真实盘口"
    return None
