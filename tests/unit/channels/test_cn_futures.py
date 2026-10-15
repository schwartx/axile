"""国内期货合约身份与手数量化。"""

from axile.channels.cn_futures import quantize_cn_futures_quantity


def test_quantize_truncates_toward_zero_like_executor() -> None:
    """与天勤 ``int(权益 × 权重 / (价格 × 乘数))`` 向零截断一致。"""
    equity = 992_670.6124999999
    assert quantize_cn_futures_quantity(0.13, equity, 30775.0) == 4.0
    assert quantize_cn_futures_quantity(0.03, equity, 22800.0) == 1.0
    assert quantize_cn_futures_quantity(-0.08, equity, 27500.0) == -2.0
    assert quantize_cn_futures_quantity(-0.06, equity, 32960.0) == -1.0


def test_quantize_invalid_notional_is_zero() -> None:
    assert quantize_cn_futures_quantity(0.13, 1000.0, 0.0) == 0.0
