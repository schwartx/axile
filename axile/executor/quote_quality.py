"""盘口结构与本地接收有效期；不比较不同机器的时钟。"""

from collections.abc import Sequence
from math import isfinite

from axile.executor.algorithms.utils.clock import clock_now_us


def valid_book(bids: Sequence[tuple[float, float]], asks: Sequence[tuple[float, float]]) -> bool:
    """统一校验真实双边价量，不把时间或连接状态混入结构有效性。"""
    if not bids or not asks:
        return False
    for side in (bids, asks):
        if any(not isfinite(p) or not isfinite(q) or p <= 0 or q < 0 for p, q in side):
            return False
        if side[0][1] <= 0:
            return False
    return (
        bids[0][0] <= asks[0][0]
        and all(left[0] > right[0] for left, right in zip(bids, bids[1:]))
        and all(left[0] < right[0] for left, right in zip(asks, asks[1:]))
    )


def quote_observation(
    bids: list[dict[str, float]], asks: list[dict[str, float]], *, validity_us: int | None = None
) -> dict[str, object]:
    """记录结构事实和本地接收有效期；未声明期限不推断持续新鲜。"""
    received = clock_now_us()
    return {
        "book_valid": valid_book(
            [(level["price"], level["quantity"]) for level in bids],
            [(level["price"], level["quantity"]) for level in asks],
        ),
        "received_time_us": received,
        "valid_until_us": received + validity_us if type(validity_us) is int and validity_us > 0 else None,
    }
