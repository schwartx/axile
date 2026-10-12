"""原生模块生命周期必须正常退出；子进程保留 faulthandler 诊断。"""

import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.slow


@pytest.mark.parametrize(
    "code",
    [
        "from openctp_ctp import thosttraderapi, thostmduserapi",
        "from openctp_ctp import thostmduserapi, thosttraderapi",
        "from axile.executor.ctp.converters import quote_to_unified; quote_to_unified({})",
        "from axile.executor.ctp.spi import TraderSpi, MarketSpi; a=TraderSpi(None); b=MarketSpi(None); del a,b",
        """
import tempfile
from openctp_ctp import thosttraderapi as td
with tempfile.TemporaryDirectory() as path:
    trader = td.CThostFtdcTraderApi.CreateFtdcTraderApi(path + '/td')
    trader.Init()
    try:
        result = trader.ReqUserLogin(td.CThostFtdcReqUserLoginField(), 1)
        assert isinstance(result, int)
    finally:
        trader.Release()
""",
        """
import tempfile
from axile.executor.ctp.spi import TraderSpi, MarketSpi
from openctp_ctp import thosttraderapi as td, thostmduserapi as md
with tempfile.TemporaryDirectory() as path:
    trader = td.CThostFtdcTraderApi.CreateFtdcTraderApi(path + '/td')
    market = md.CThostFtdcMdApi.CreateFtdcMdApi(path + '/md')
    a, b = TraderSpi(None), MarketSpi(None)
    trader.RegisterSpi(a)
    market.RegisterSpi(b)
    trader.Init()
    market.Init()
    market.RegisterSpi(None)
    market.Release()
    trader.RegisterSpi(None)
    trader.Release()
""",
    ],
    ids=["import-trader-first", "import-market-first", "conversion", "spi", "native-login", "release"],
)
def test_native_process_exits_cleanly(code):
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONFAULTHANDLER": "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
