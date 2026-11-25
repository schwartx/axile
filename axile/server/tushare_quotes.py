"""通过 Tushare HTTP 协议读取股票和 ETF 实时报价。"""

import re
from datetime import datetime
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from axile.channels.market_data import MarketDataUnavailable
from axile.executor.algorithms.utils.clock import clock_monotonic
from axile.executor.models.unified_price import UnifiedPriceData

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_QUOTE_FIELDS = "ts_code,close,vol,amount,trade_time"
_STOCK_BOOK_FIELDS = ",bid_price1,bid_volume1,ask_price1,ask_volume1"


def read_tushare_quotes(
    url: str, token: str, symbols: list[str], timeout: float, *, allow_partial: bool = False
) -> dict[str, UnifiedPriceData]:
    """按股票、沪市 ETF 和深市 ETF 分批查询，保留请求标识。"""
    batches: dict[tuple[str, str | None], list[str]] = {}
    for symbol in symbols:
        code = symbol.upper()
        if re.fullmatch(r"[0-9]{6}\.(?:SH|SZ|BJ)", code) is None:
            raise ValueError(f"Tushare 实时报价需要股票或 ETF 代码（含交易所后缀）：{symbol}")
        if code.startswith("5") and code.endswith(".SH"):
            endpoint = ("rt_etf_k", "HQ_FND_TICK")
        elif code.startswith("15") and code.endswith(".SZ"):
            endpoint = ("rt_etf_k", None)
        else:
            endpoint = ("rt_k", None)
        batches.setdefault(endpoint, []).append(code)

    deadline = clock_monotonic() + timeout
    quotes: dict[str, UnifiedPriceData] = {}
    with httpx.Client() as client:
        for (api_name, topic), codes in batches.items():
            remaining = deadline - clock_monotonic()
            if remaining <= 0:
                raise TimeoutError("Tushare 行情查询超时")
            rows = _query(client, url, token, api_name, topic, codes, remaining)
            by_code = {row.get("ts_code"): row for row in rows if isinstance(row.get("ts_code"), str)}
            for code in codes:
                if code not in by_code:
                    if allow_partial:
                        continue
                    raise MarketDataUnavailable(f"Tushare 暂无行情：{code}")
                quotes[code] = _snapshot(code, by_code[code])
    return {symbol: quotes[symbol.upper()] for symbol in symbols if symbol.upper() in quotes}


def _query(
    client: httpx.Client, url: str, token: str, api_name: str, topic: str | None, codes: list[str], timeout: float
) -> list[dict[str, Any]]:
    params = {"ts_code": ",".join(codes)}
    if topic is not None:
        params["topic"] = topic
    fields = _QUOTE_FIELDS + _STOCK_BOOK_FIELDS if api_name == "rt_k" else _QUOTE_FIELDS
    try:
        response = client.post(
            url, json={"api_name": api_name, "token": token, "params": params, "fields": fields}, timeout=timeout
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.TimeoutException:
        raise TimeoutError("Tushare 行情请求超时") from None
    except httpx.HTTPStatusError as exc:
        raise MarketDataUnavailable(f"Tushare 行情请求失败（HTTP {exc.response.status_code}）") from None
    except (httpx.RequestError, ValueError):
        raise MarketDataUnavailable("Tushare 行情服务连接失败或响应格式错误") from None

    if not isinstance(payload, dict) or type(code := payload.get("code")) is not int:
        raise MarketDataUnavailable("Tushare 行情响应缺少有效返回码")
    if code == 2002:
        raise MarketDataUnavailable("Tushare Token 未开通所请求的实时行情权限")
    if code != 0:
        raise MarketDataUnavailable(f"Tushare 行情请求失败（返回码 {code}）")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise MarketDataUnavailable("Tushare 行情响应缺少数据")
    names, items = data.get("fields"), data.get("items")
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names) or not isinstance(items, list):
        raise MarketDataUnavailable("Tushare 行情响应的字段与数据格式无效")
    if any(not isinstance(item, list) or len(item) != len(names) for item in items):
        raise MarketDataUnavailable("Tushare 行情响应的字段与数据列数不匹配")
    return [dict(zip(names, item, strict=True)) for item in items]


def _level(price: object, size: object) -> list[tuple[float, float]]:
    if (
        isinstance(price, (int, float))
        and isinstance(size, (int, float))
        and not isinstance(price, bool)
        and not isinstance(size, bool)
        and isfinite(price)
        and isfinite(size)
        and price > 0
        and size > 0
    ):
        return [(float(price), float(size))]
    return []


def _snapshot(symbol: str, row: dict[str, Any]) -> UnifiedPriceData:
    try:
        price, volume, turnover = (float(row[field]) for field in ("close", "vol", "amount"))
        at = datetime.fromisoformat(row["trade_time"])
    except (KeyError, TypeError, ValueError, OverflowError):
        raise MarketDataUnavailable(f"Tushare 行情缺少有效价格、成交量或快照时间：{symbol}") from None
    if not all(isfinite(value) for value in (price, volume, turnover)) or price <= 0 or volume < 0 or turnover < 0:
        raise MarketDataUnavailable(f"Tushare 行情价格或成交数据无效：{symbol}")
    if at.tzinfo is None:
        at = at.replace(tzinfo=_SHANGHAI)
    return UnifiedPriceData(
        symbol=symbol,
        last_price=price,
        bids=_level(row.get("bid_price1"), row.get("bid_volume1")),
        asks=_level(row.get("ask_price1"), row.get("ask_volume1")),
        volume=volume,
        turnover=turnover,
        timestamp=int(at.timestamp() * 1000),
        update_time=at.isoformat(),
    )
