"""OpenCTP SPI 转发边界测试。"""

from unittest.mock import Mock

from axile.executor.ctp.spi import TraderSpi


def test_trader_spi_routes_option_exchange_error() -> None:
    owner = Mock()
    spi = TraderSpi(owner)
    row, info = object(), object()

    spi.OnErrRtnExecOrderInsert(row, info)

    owner._option_error.assert_called_once_with(row, info)


def test_trader_spi_preserves_settlement_record() -> None:
    owner = Mock()
    spi = TraderSpi(owner)
    row, info = object(), object()
    spi.OnRspSettlementInfoConfirm(row, info, 15, True)
    owner._settled.assert_called_once_with(info, 15, row)
