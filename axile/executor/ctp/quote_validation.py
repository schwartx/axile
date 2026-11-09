"""CTP 新单与目标定量共享的行情证据校验。"""

import math

from axile.executor.market_data_validation import quote_time_error
from axile.executor.models.unified_price import UnifiedPriceData


def quote_error(
    quote: UnifiedPriceData | None,
    *,
    now: float,
    trading_day: str,
    max_age: float,
    tick: float,
) -> str | None:
    """要求同交易日、新鲜双边盘口及有效涨跌停；缺一侧时保守阻断。"""
    if quote is None:
        return "missing_quote"
    if quote.extra.get("trading_day") != trading_day:
        return "trading_day_mismatch"
    received = quote.extra.get("received_at", 0)
    if not isinstance(received, (int, float)) or not math.isfinite(received):
        return "invalid_receive_time"
    if quote_time_error(quote.timestamp, now=now, max_age=max_age) is not None:
        return "stale_exchange_time"
    if received <= 0 or not 0 <= now - received <= max_age:
        return "stale_receive_time"
    if (
        not quote.book_valid
        or (quote.bids[0][1] if quote.bids else 0.0) <= 0
        or (quote.asks[0][1] if quote.asks else 0.0) <= 0
    ):
        return "missing_two_sided_book"
    lower = quote.extra.get("lower_limit_price", 0)
    upper = quote.extra.get("upper_limit_price", 0)
    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in (lower, upper, tick)):
        return "invalid_price_limits"
    if not 0 < lower <= upper or tick <= 0:
        return "missing_price_limits_or_tick"
    for price in (
        quote.last_price,
        (quote.bids[0][0] if quote.bids else 0.0),
        (quote.asks[0][0] if quote.asks else 0.0),
    ):
        if not price_in_bounds(price, tick=tick, lower=lower, upper=upper):
            return "invalid_book_price"
    if (quote.bids[0][0] if quote.bids else 0.0) > (quote.asks[0][0] if quote.asks else 0.0):
        return "crossed_book"
    return None


def valuation_quote_error(quote: UnifiedPriceData | None) -> str | None:
    """估值只要求有效最新价；休市后的最近成交价仍可作为参考价。"""
    if quote is None:
        return "missing_quote"
    if not math.isfinite(quote.last_price) or quote.last_price <= 0:
        return "invalid_last_price"
    return None


def fresh_valuation_quote_error(
    quote: UnifiedPriceData | None,
    *,
    now: float,
    trading_day: str,
    max_age: float,
) -> str | None:
    """等待盘中 tick 时校验最新价、交易日与行情时效。"""
    error = valuation_quote_error(quote)
    if error or quote is None:
        return error
    if quote.extra.get("trading_day") != trading_day:
        return "trading_day_mismatch"
    received = quote.extra.get("received_at", 0)
    if not isinstance(received, (int, float)) or not math.isfinite(received):
        return "invalid_receive_time"
    if quote_time_error(quote.timestamp, now=now, max_age=max_age) is not None:
        return "stale_exchange_time"
    if received <= 0 or not 0 <= now - received <= max_age:
        return "stale_receive_time"
    return None


def price_in_bounds(price: float, *, tick: float, lower: float, upper: float) -> bool:
    """仅接受有限、正值、符合 tick 且在涨跌停范围内的价格。"""
    if not all(math.isfinite(value) for value in (price, tick, lower, upper)):
        return False
    if tick <= 0 or not 0 < lower <= price <= upper:
        return False
    units = price / tick
    return math.isfinite(units) and math.isclose(units, round(units), rel_tol=0, abs_tol=1e-7)
