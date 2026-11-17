"""
统一价格快照模型.

盘口按买卖两侧的价量数组保存，档数由渠道实际提供的深度决定。
``extra`` 仅保留渠道特有数据。
"""

from dataclasses import dataclass, field
from datetime import datetime as dt
from math import isfinite
from typing import Annotated, Any

from msgspec import Meta
from typing_extensions import override

from axile.common.serialization import copy_data, to_payload

# ExtraData用于存储渠道特有的数据，使用Any是合理的
# 因为不同渠道可能有不同类型的数据需要存储
# type: ignore[reportExplicitAny]
ExtraData = dict[str, Any]


@dataclass(kw_only=True)
class UnifiedPriceData:
    """
    统一价格快照模型.

    Attributes
    ----------
    symbol : str
        品种代码。
    last_price : float
        最新成交价。
    bids : list[tuple[float, float]]
        买盘价量数组，按价格从高到低排列。
    asks : list[tuple[float, float]]
        卖盘价量数组，按价格从低到高排列。
    volume : float
        当前成交量字段。
    timestamp : int
        毫秒级时间戳。
    extra : ExtraData
        渠道特有扩展字段。
    """

    # 核心字段（所有渠道都有）
    symbol: Annotated[str, Meta(description="品种代码（统一格式）")]
    last_price: Annotated[float, Meta(description="最新价")]

    bids: Annotated[list[tuple[float, float]], Meta(description="买盘 [价格, 数量] 数组，买一在前")]
    asks: Annotated[list[tuple[float, float]], Meta(description="卖盘 [价格, 数量] 数组，卖一在前")]

    # 其他核心字段
    volume: Annotated[float, Meta(description="成交量")]
    turnover: Annotated[float, Meta(description="成交额")] = field(default=0.0)
    timestamp: Annotated[int, Meta(description="时间戳（毫秒）")]
    update_time: Annotated[str, Meta(description="更新时间（ISO格式）")]

    @property
    def book_valid(self) -> bool:
        """盘口结构事实，不含时钟、新鲜度或连接状态。"""
        from axile.executor.quote_quality import valid_book

        return valid_book(self.bids, self.asks)

    # 渠道特有数据
    extra: Annotated[ExtraData, Meta(description="渠道特有数据，包含原始数据和渠道特定字段")] = field(
        default_factory=lambda: {}
    )

    @override
    def __str__(self) -> str:
        """返回便于记录日志的简要字符串表示."""
        return (
            f"UnifiedPriceData(symbol={self.symbol}, "
            f"last={self.last_price}, bid={(self.bids[0][0] if self.bids else 0.0)}, ask={(self.asks[0][0] if self.asks else 0.0)})"
        )

    def get_spread(self) -> float | None:
        """
        计算买一与卖一之间的价差.

        Returns
        -------
        float
            当前买卖价差。
        """
        return self.asks[0][0] - self.bids[0][0] if self.bids and self.asks else None

    def get_mid_price(self) -> float | None:
        """
        计算买一与卖一之间的中间价.

        Returns
        -------
        float
            当前盘口中间价。
        """
        return self.bids[0][0] / 2 + self.asks[0][0] / 2 if self.bids and self.asks else None

    def is_valid(self) -> bool:
        """
        检查当前行情快照是否有效.

        Returns
        -------
        bool
            当关键价格与时间字段有效时返回 ``True``。
        """
        return self.book_valid and isfinite(self.last_price) and self.last_price > 0 and self.timestamp > 0

    @property
    def timestamp_datetime(self) -> dt:
        """
        将毫秒时间戳转换为 ``datetime`` 对象.

        Returns
        -------
        dt
            由 ``timestamp`` 推导出的时间对象。
        """
        return dt.fromtimestamp(self.timestamp / 1000.0)

    def to_dict(self) -> dict[str, Any]:
        """
        导出为普通字典.

        Returns
        -------
        dict[str, Any]
            适合序列化或调试输出的字典结果。
        """
        return to_payload(self)

    __json_properties__ = ("book_valid", "timestamp_datetime")

    def validate(self) -> None:
        """外部载荷解析时拒绝非有限盘口数值。"""
        if any(not isfinite(number) for side in (self.bids, self.asks) for level in side for number in level):
            raise ValueError("盘口价格和数量必须是有限数值")


def clone_price_data(price_data: UnifiedPriceData | None) -> UnifiedPriceData | None:
    """
    深拷贝一份价格快照.

    Parameters
    ----------
    price_data : UnifiedPriceData | None
        待复制的价格快照对象。

    Returns
    -------
    UnifiedPriceData | None
        深拷贝后的价格快照；输入为 ``None`` 时返回 ``None``。
    """
    if price_data is None:
        return None
    return copy_data(price_data, deep=True)
