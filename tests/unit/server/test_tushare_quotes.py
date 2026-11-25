import json

import httpx
import pytest

from axile.channels.market_data import MarketDataUnavailable
from axile.server import tushare_quotes


def test_tushare_http_maps_stock_book_and_splits_etf_exchanges(monkeypatch):
    requests = []
    url = "https://quotes.example.test/v1/tushare"

    def respond(request):
        assert request.method == "POST"
        assert str(request.url) == url
        payload = json.loads(request.content)
        assert payload["token"] == "test-token"
        fields = payload["fields"].split(",")
        assert {"close", "vol", "amount", "trade_time", "ts_code"}.issubset(fields)
        params = payload["params"]
        requests.append((payload["api_name"], params))
        row = {
            "ts_code": params["ts_code"],
            "close": 10.0,
            "vol": 125,
            "amount": 1200,
            "trade_time": "2026-11-23 15:00:00",
        }
        if payload["api_name"] == "rt_k":
            row |= {"bid_price1": 9.9, "bid_volume1": 200.0, "ask_price1": 10.2, "ask_volume1": 300.0}
            assert set(fields) == row.keys()
        fields = list(reversed(row))
        return httpx.Response(
            200, json={"code": 0, "data": {"fields": fields, "items": [[row[key] for key in fields]]}}
        )

    client = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(tushare_quotes.httpx, "Client", lambda: client)
    quotes = tushare_quotes.read_tushare_quotes(url, "test-token", ["600000.SH", "510300.SH", "159919.SZ"], 5)
    assert requests == [
        ("rt_k", {"ts_code": "600000.SH"}),
        ("rt_etf_k", {"ts_code": "510300.SH", "topic": "HQ_FND_TICK"}),
        ("rt_etf_k", {"ts_code": "159919.SZ"}),
    ]
    stock = quotes["600000.SH"]
    assert stock.bids == [(9.9, 200.0)] and stock.asks == [(10.2, 300.0)]
    assert stock.book_valid
    assert stock.update_time == "2026-11-23T15:00:00+08:00"
    assert stock.timestamp == 1795417200000
    assert stock.volume == 125 and stock.turnover == 1200
    for symbol in ("510300.SH", "159919.SZ"):
        assert quotes[symbol].last_price == 10
        assert quotes[symbol].bids == quotes[symbol].asks == []


def test_tushare_permission_error_omits_the_remote_body(monkeypatch):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"code": 2002, "msg": "test-token"}))
    client = httpx.Client(transport=transport)
    monkeypatch.setattr(tushare_quotes.httpx, "Client", lambda: client)
    with pytest.raises(MarketDataUnavailable, match="未开通") as exc:
        tushare_quotes.read_tushare_quotes("https://api.tushare.pro", "test-token", ["600000.SH"], 5)
    assert "test-token" not in str(exc.value)
