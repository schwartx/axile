"""行情快照复制和盘口档位的行为契约。"""

from axile.executor.models.unified_price import UnifiedPriceData, clone_price_data


class TestUnifiedPriceDataPriceLevels:
    """测试价格级别获取方法."""

    def test_get_price_levels(self) -> None:
        """测试获取指定深度的价格."""
        price = UnifiedPriceData(
            symbol="rb2610",
            last_price=30000.0,
            bid_price=29999.0,
            bid_price_2=29998.0,
            bid_price_3=29997.0,
            bid_price_4=29996.0,
            bid_price_5=29995.0,
            ask_price=30001.0,
            ask_price_2=30002.0,
            ask_price_3=30003.0,
            ask_price_4=30004.0,
            ask_price_5=30005.0,
            bid_volume=1.5,
            bid_volume_2=2.0,
            bid_volume_3=1.8,
            bid_volume_4=2.2,
            bid_volume_5=1.9,
            ask_volume=1.2,
            ask_volume_2=1.6,
            ask_volume_3=2.1,
            ask_volume_4=1.7,
            ask_volume_5=2.3,
            volume=1000.0,
            timestamp=1703123456789,
            update_time="2023-12-21T10:30:00.000Z",
        )

        # 测试获取3档
        levels_3 = price.get_price_levels(3)
        assert levels_3["bid_prices"] == [29999.0, 29998.0, 29997.0]
        assert levels_3["ask_prices"] == [30001.0, 30002.0, 30003.0]
        assert levels_3["bid_volumes"] == [1.5, 2.0, 1.8]
        assert levels_3["ask_volumes"] == [1.2, 1.6, 2.1]


def test_book_valid_defaults_true_and_survives_clone() -> None:
    """book_valid 默认 True，且经 clone_price_data 深拷贝后保留."""
    price = UnifiedPriceData(
        symbol="rb2610",
        last_price=30000.0,
        bid_price=29999.0,
        ask_price=30001.0,
        bid_volume=1.5,
        ask_volume=1.2,
        volume=1000.0,
        timestamp=1,
        update_time="2023-12-21T10:30:00.000Z",
    )
    assert price.book_valid is True

    invalid = price.model_copy(update={"book_valid": False})
    assert invalid.book_valid is False
    assert clone_price_data(invalid).book_valid is False
