"""行情使用前的公共校验；渠道负责解释自己的时间和市场规则。"""

import math
from typing import Literal

from axile.executor.models.unified_price import UnifiedPriceData

MarketDataPurpose = Literal["sizing", "order"]
DEFAULT_QUOTE_MAX_AGE_SECONDS = 5.0
"""沿用 OrderTracker 的默认五秒时效；渠道已有配置时覆盖此默认值。"""


class MarketDataUnavailableError(ValueError):
    """行情不足以支持新增交易，查询与撤单不受影响。"""

    reason_code = "COMMON.MARKET_DATA.UNAVAILABLE"

    def __init__(self, symbol: str, reason: str) -> None:
        self.execution_error = f"{symbol} 行情不可用于交易：{reason}"
        super().__init__(self.execution_error)


def market_data_error(
    quote: UnifiedPriceData | None,
    *,
    now: float,
    max_age: float,
    purpose: MarketDataPurpose,
) -> str | None:
    """定量只要求参考价；下单要求真实且未交叉的可用盘口。"""
    if quote is None:
        return "行情未就绪"
    if not math.isfinite(now) or quote.timestamp <= 0:
        return "行情时间无效"
    age = now - quote.timestamp / 1000
    if age < 0:
        return "行情时间超前"
    if age > max_age:
        return "行情已过期"
    if purpose == "sizing":
        return None if math.isfinite(quote.last_price) and quote.last_price > 0 else "参考价格无效"
    if not quote.book_valid:
        return "缺少真实盘口"
    prices = (quote.bid_price, quote.ask_price)
    if not all(math.isfinite(price) and price >= 0 for price in prices) or not any(prices):
        return "盘口价格无效"
    if all(prices) and quote.bid_price > quote.ask_price:
        return "盘口买卖价交叉"
    return None
