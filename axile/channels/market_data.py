"""渠道独立行情源的运行时契约。"""

from collections.abc import Callable, Mapping, Sequence
from typing import Literal, Protocol

from axile.executor.models.unified_price import UnifiedPriceData

type MarketDataStatus = Literal["uninitialized", "connecting", "ready", "unavailable", "disconnected"]


class MarketDataUnavailable(RuntimeError):
    """行情源配置或运行失败。"""


class MarketDataSource(Protocol):
    """公开行情读取及其独立连接状态。"""

    def snapshot(self, symbols: Sequence[str], *, timeout: float) -> dict[str, UnifiedPriceData]:
        """查询指定标的的行情快照。"""
        ...

    def status(self) -> MarketDataStatus:
        """只读连接状态。"""
        ...

    def close(self) -> None:
        """释放行情源资源。"""
        ...


type MarketDataFactory = Callable[[Mapping[str, object]], MarketDataConfiguration]


class MarketDataConfiguration(Protocol):
    """可哈希的行情配置，同一配置共用行情源。"""

    @classmethod
    def from_account_config(cls, values: Mapping[str, object]) -> "MarketDataConfiguration":
        """从已保存的账户设置提取行情字段。"""
        ...

    def open(self) -> MarketDataSource:
        """构造行情源，连接按首次读取启动。"""
        ...
