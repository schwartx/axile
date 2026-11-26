"""管理服务内独立于交易 worker 的行情会话。"""

from collections.abc import Mapping, Sequence
from threading import Lock

from loguru import logger

from axile.channels import get_channel
from axile.channels.market_data import (
    MarketDataConfiguration,
    MarketDataSource,
    MarketDataStatus,
    MarketDataUnavailable,
)
from axile.executor.models.unified_price import UnifiedPriceData

_sources: dict[tuple[str, MarketDataConfiguration], MarketDataSource] = {}
_lock = Lock()


def _source_key(channel: str, values: Mapping[str, object]) -> tuple[str, MarketDataConfiguration]:
    configure = get_channel(channel).configure_market_data
    if configure is None:
        raise MarketDataUnavailable("该渠道尚未接入独立行情")
    return channel, configure(values)


def read_market_data(
    channel: str, values: Mapping[str, object], symbols: Sequence[str], *, timeout: float
) -> dict[str, UnifiedPriceData]:
    """仅使用行情配置获取快照。"""
    return read_configured_market_data(_source_key(channel, values), symbols, timeout=timeout)


def read_configured_market_data(
    key: tuple[str, MarketDataConfiguration], symbols: Sequence[str], *, timeout: float
) -> dict[str, UnifiedPriceData]:
    """使用调用方已构建的配置，从共用行情会话读取快照。"""
    with _lock:
        source = _sources.get(key)
        if source is None:
            source = _sources[key] = key[1].open()
    return source.snapshot(symbols, timeout=timeout)


def market_data_status(channel: str, values: Mapping[str, object]) -> MarketDataStatus:
    """观察已存在的行情源，不创建会话。"""
    key = _source_key(channel, values)
    with _lock:
        source = _sources.get(key)
    return source.status() if source is not None else "uninitialized"


def close_market_data() -> None:
    """释放当前服务使用的独立行情会话。"""
    with _lock:
        sources = tuple(_sources.values())
        _sources.clear()
    for source in sources:
        try:
            source.close()
        except Exception:  # noqa: BLE001 - 单个渠道失败不阻断其余资源退出
            logger.warning("独立行情源关闭失败")
