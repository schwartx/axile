"""盘口数组、序列化与复制契约。"""

import pytest
from pydantic import ValidationError

from axile.executor.models.unified_price import UnifiedPriceData, clone_price_data


@pytest.mark.parametrize("bid_depth,ask_depth", [(0, 0), (0, 1), (1, 5), (5, 1), (20, 20), (30, 21)])
def test_depth_roundtrip_and_clone(bid_depth, ask_depth):
    bids = [(100 - i, i + 1) for i in range(bid_depth)]
    asks = [(101 + i, i + 2) for i in range(ask_depth)]
    price = UnifiedPriceData(symbol="A", last_price=100, bids=bids, asks=asks, volume=0, timestamp=1, update_time="")
    restored = UnifiedPriceData.model_validate_json(price.model_dump_json())
    assert restored.bids == bids and restored.asks == asks
    assert not {"bid_price", "ask_price", "bid_volume", "ask_volume"} & price.model_dump().keys()
    assert all(
        not hasattr(price, name) for name in ("bid_price", "ask_price", "get_price_levels", "get_all_bid_prices")
    )
    cloned = clone_price_data(restored)
    assert cloned is not None
    cloned.bids.append((1, 1))
    assert restored.bids == bids
    if not bids or not asks:
        assert price.get_spread() is None and price.get_mid_price() is None
        assert not price.is_valid()
    else:
        assert price.get_spread() == 1 and price.get_mid_price() == 100.5


@pytest.mark.parametrize("levels", [[[1]], [[1, 2, 3]], [[float("nan"), 1]], [[1, float("inf")]]])
def test_invalid_levels(levels):
    with pytest.raises(ValidationError):
        UnifiedPriceData(symbol="A", last_price=1, bids=levels, asks=[], volume=0, timestamp=1, update_time="")


def test_legacy_only_input_is_rejected():
    with pytest.raises(ValidationError):
        UnifiedPriceData(symbol="A", last_price=1, bid_price=1, ask_price=2, volume=0, timestamp=1, update_time="")
