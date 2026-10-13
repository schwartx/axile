"""OpenCTP SPI 转发边界测试。"""

from unittest.mock import Mock

from axile.executor.ctp.spi import MarketSpi, TraderSpi


def test_trader_spi_forwards_query_frame_without_rewriting() -> None:
    owner = Mock()
    spi = TraderSpi(owner)
    row, info = object(), object()

    spi.OnRspQryOrder(row, info, 12, True)

    owner._query_response.assert_called_once_with(row, info, 12, True)


def test_trader_spi_forwards_settlement_confirmation_query() -> None:
    owner = Mock()
    spi = TraderSpi(owner)
    row, info = object(), object()

    spi.OnRspQrySettlementInfoConfirm(row, info, 13, True)

    owner._query_response.assert_called_once_with(row, info, 13, True)


def test_trader_spi_forwards_market_snapshot_query() -> None:
    owner = Mock()
    spi = TraderSpi(owner)
    row, info = object(), object()

    spi.OnRspQryDepthMarketData(row, info, 14, True)

    owner._query_response.assert_called_once_with(row, info, 14, True)


def test_trader_spi_routes_option_exchange_error() -> None:
    owner = Mock()
    spi = TraderSpi(owner)
    row, info = object(), object()

    spi.OnErrRtnExecOrderInsert(row, info)

    owner._option_error.assert_called_once_with(row, info)


def test_market_spi_forwards_quote_identity() -> None:
    owner = Mock()
    spi = MarketSpi(owner)
    row = object()

    spi.OnRtnDepthMarketData(row)

    owner._on_quote.assert_called_once_with(row)


def test_trader_spi_preserves_settlement_record() -> None:
    owner = Mock()
    spi = TraderSpi(owner)
    row, info = object(), object()
    spi.OnRspSettlementInfoConfirm(row, info, 15, True)
    owner._settled.assert_called_once_with(info, 15, row)
