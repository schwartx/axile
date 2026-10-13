"""全量只发一次请求；按需查询必须指定合约。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from axile.common.trade_channel import TradeChannel
from axile.executor.ctp.ctp_execute import CTPExecutor


def _row():
    return SimpleNamespace(
        InstrumentID="rb2701",
        ExchangeID="SHFE",
        ProductClass="1",
        VolumeMultiple=10,
        PriceTick=1,
        ExpireDate="20270115",
    )


def test_ctp_full_refresh_uses_exactly_one_native_query():
    executor = CTPExecutor(TradeChannel.CTP)
    executor._query = Mock(return_value=[_row()])
    records = executor._query_catalog(None, 60)
    executor._query.assert_called_once()
    name, request = executor._query.call_args.args
    assert name == "ReqQryInstrument"
    assert request.InstrumentID == request.ExchangeID == request.ProductID == ""
    assert records[0].key == "SHFE.rb2701"


def test_ctp_demand_query_is_scoped_to_the_missing_contract():
    executor = CTPExecutor(TradeChannel.CTP)
    executor._query = Mock(return_value=[_row()])
    executor._query_catalog(["SHFE.rb2701"], 5)
    executor._query.assert_called_once()
    name, request = executor._query.call_args.args
    assert name == "ReqQryInstrument"
    assert request.ExchangeID == "SHFE"
    assert request.InstrumentID == "rb2701"
    assert request.ProductID == ""


def test_ctp_occupied_query_lock_has_a_short_wait_limit(monkeypatch):
    executor = CTPExecutor(TradeChannel.CTP)
    monkeypatch.setattr(executor, "_catalog_checkpoint", lambda: None)
    with executor._query_lock:
        with pytest.raises(TimeoutError, match="等待 CTP 查询权超时"):
            with executor._query_slot(0.02):
                pytest.fail("must not acquire an occupied query slot")
