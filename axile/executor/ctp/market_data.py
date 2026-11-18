"""独立连接 CTP 行情前置，订阅并读取行情快照。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
from tempfile import TemporaryDirectory
from threading import Condition, Lock

from openctp_ctp import thostmduserapi as md

from axile.channels.market_data import MarketDataStatus, MarketDataUnavailable
from axile.common.serialization import copy_data
from axile.executor.algorithms.utils.clock import clock_monotonic, get_default_clock
from axile.executor.ctp.converters import quote_to_unified
from axile.executor.ctp.quote_time import resolve_quote_time, resolve_quote_trading_day
from axile.executor.ctp_input_validation import validate_credential, validate_instrument
from axile.executor.ctp_product_sessions import CTP_PRODUCT_SESSIONS, get_ctp_product_sessions
from axile.executor.instrument_catalog_records import EXCHANGES
from axile.executor.models.unified_input_accounts import validate_connection_endpoint
from axile.executor.models.unified_price import UnifiedPriceData
from axile.executor.trading_calendar import CHINA_CALENDAR_ID, ShinnyTradingCalendar


@dataclass(frozen=True, slots=True)
class CtpMarketDataConfig:
    """行情连接的独立配置。"""

    front: str
    broker_id: str
    investor_id: str
    password: str = field(repr=False)

    @classmethod
    def from_account_config(cls, values: Mapping[str, object]) -> CtpMarketDataConfig:
        """读取行情地址和行情登录字段。"""
        front, password = values.get("md_front"), values.get("password")
        if not isinstance(front, str):
            raise ValueError("缺少 CTP 行情前置地址")
        if not isinstance(password, str) or not password or any(char in password for char in "\r\n\0"):
            raise ValueError("CTP 行情密码格式不正确")
        return cls(
            validate_connection_endpoint(front, scheme="required", allowed_schemes=frozenset({"tcp"}), port="required"),
            validate_credential("broker_id", values.get("broker_id")),
            validate_credential("investor_id", values.get("investor_id")),
            password,
        )

    def open(self) -> CtpMarketData:
        """构造未连接的行情源。"""
        return CtpMarketData(self)


class CtpMarketData(md.CThostFtdcMdSpi):
    """由行情 SDK 自行重连，并在每个连接代次重新确认订阅。"""

    def __init__(self, config: CtpMarketDataConfig) -> None:
        super().__init__()
        self._config = config
        self._io = Lock()
        self._state = Condition()
        self._api = None
        self._flow: TemporaryDirectory[str] | None = None
        self._status: MarketDataStatus = "uninitialized"
        self._closed = False
        self._generation = 0
        self._login_request: int | None = None
        self._login_error: str | None = None
        self._subscriptions: set[str] = set()
        self._confirmed: set[str] = set()
        self._errors: dict[str, int] = {}
        self._quotes: dict[str, UnifiedPriceData] = {}
        self._calendar = ShinnyTradingCalendar()

    def status(self) -> MarketDataStatus:
        """返回行情状态，不发起连接。"""
        with self._state:
            return self._status

    def snapshot(self, symbols: Sequence[str], *, timeout: float) -> dict[str, UnifiedPriceData]:
        """在共享登录和订阅会话中读取指定合约。"""
        requests = {}
        for raw in symbols:
            prefix, dot, code = raw.partition(".")
            exchange = prefix.upper() if dot and prefix.upper() in EXCHANGES else ""
            requests[raw] = (exchange, validate_instrument(code if exchange else raw))
        requested = {symbol for _, symbol in requests.values()}
        if not requested:
            return {}
        deadline = clock_monotonic() + timeout
        if not self._io.acquire(timeout=max(0.0, timeout)):
            raise TimeoutError("等待 CTP 行情查询超时")
        try:
            rows = self._read(requested, deadline)
            result = {}
            for raw, (exchange, symbol) in requests.items():
                quote = rows[symbol]
                if exchange and quote.extra.get("exchange_id") != exchange:
                    raise MarketDataUnavailable(f"无法确认 {raw} 的行情归属该交易所")
                result[raw] = quote
            return result
        finally:
            self._io.release()

    def _read(self, symbols: set[str], deadline: float) -> dict[str, UnifiedPriceData]:
        if self._closed:
            raise MarketDataUnavailable("CTP 行情源已关闭")
        if self._api is None:
            self._start()
        with self._state:
            if not self._state.wait_for(
                lambda: self._status == "ready" or self._login_error is not None, self._remaining(deadline)
            ):
                raise TimeoutError("等待 CTP 行情登录超时")
            if self._login_error:
                raise MarketDataUnavailable(self._login_error)
            generation = self._generation
            new = symbols - self._subscriptions
            self._subscriptions.update(new)
        if new:
            self._subscribe(new)
        with self._state:
            if not self._state.wait_for(
                lambda: (
                    self._generation != generation
                    or bool(symbols.intersection(self._errors))
                    or symbols <= self._confirmed
                    and symbols <= self._quotes.keys()
                ),
                self._remaining(deadline),
            ):
                self._subscriptions.difference_update(symbols - self._confirmed)
                raise TimeoutError("等待 CTP 行情回报超时")
            if self._generation != generation:
                raise MarketDataUnavailable("查询期间 CTP 行情连接已切换")
            if rejected := {symbol: self._errors.pop(symbol) for symbol in symbols if symbol in self._errors}:
                self._subscriptions.difference_update(rejected)
                raise MarketDataUnavailable(f"CTP 行情订阅遭拒：{rejected}")
            return {symbol: copy_data(self._quotes[symbol], deep=True) for symbol in symbols}

    @staticmethod
    def _remaining(deadline: float) -> float:
        return max(0.0, deadline - clock_monotonic())

    def _start(self) -> None:
        flow = TemporaryDirectory(prefix="axile-market-ctp-")
        self._flow = flow
        try:
            api = md.CThostFtdcMdApi.CreateFtdcMdApi(f"{flow.name}/")
            self._api = api
            with self._state:
                self._status = "connecting"
                self._login_error = None
            api.RegisterSpi(self)
            api.RegisterFront(self._config.front)
            api.Init()
        except Exception:
            self._release()
            with self._state:
                self._status = "unavailable"
            raise MarketDataUnavailable("CTP 行情前置初始化失败") from None

    def _subscribe(self, symbols: set[str]) -> None:
        api = self._api
        assert api is not None
        encoded = [symbol.encode("ascii") for symbol in sorted(symbols)]
        try:
            code = api.SubscribeMarketData(encoded, len(encoded))
        except Exception:
            with self._state:
                self._subscriptions.difference_update(symbols)
            raise MarketDataUnavailable("CTP 行情订阅发送失败") from None
        if code:
            with self._state:
                self._subscriptions.difference_update(symbols)
            raise MarketDataUnavailable(f"CTP 行情订阅未受理（{code}）")

    def _release(self) -> None:
        api, self._api = self._api, None
        flow, self._flow = self._flow, None
        try:
            if api is not None:
                api.Release()
        finally:
            if flow is not None:
                flow.cleanup()

    def close(self) -> None:
        """关闭行情前置及其流目录。"""
        with self._io:
            with self._state:
                self._closed = True
                self._status = "disconnected"
            self._release()

    # ruff: noqa: D102, N802 - OpenCTP SPI 回调签名
    def OnFrontConnected(self):
        with self._state:
            if self._closed or self._api is None:
                return
            self._generation += 1
            api = self._api
            request_id = self._login_request = self._generation
            self._status = "connecting"
            self._login_error = None
            self._clear_subscriptions()
        req = md.CThostFtdcReqUserLoginField()
        req.BrokerID, req.UserID, req.Password = self._config.broker_id, self._config.investor_id, self._config.password
        try:
            code = api.ReqUserLogin(req, request_id)
        except Exception:
            self._finish_login(request_id, "CTP 行情登录发送失败")
        else:
            if code:
                self._finish_login(request_id, f"CTP 行情登录未受理（{code}）")

    def _finish_login(self, request_id: int, error: str | None) -> None:
        with self._state:
            if self._closed or self._login_request is None or request_id not in (0, self._login_request):
                return
            self._login_request = None
            self._login_error = error
            self._status = "unavailable" if error else "ready"
            self._state.notify_all()

    def OnRspUserLogin(self, row, info, request_id, is_last):
        code = int(getattr(info, "ErrorID", 0) or 0)
        self._finish_login(request_id, f"CTP 行情登录被拒绝（{code}）" if code else None)

    def OnRspError(self, info, request_id, is_last):
        code = int(getattr(info, "ErrorID", 0) or 0)
        if code:
            self._finish_login(request_id, f"CTP 行情登录被拒绝（{code}）")

    def OnFrontDisconnected(self, reason):
        with self._state:
            if self._closed:
                return
            self._generation += 1
            self._login_request = None
            self._login_error = None
            self._status = "disconnected"
            self._clear_subscriptions()
            self._state.notify_all()

    def _clear_subscriptions(self) -> None:
        self._subscriptions.clear()
        self._confirmed.clear()
        self._errors.clear()
        self._quotes.clear()

    def OnRspSubMarketData(self, row, info, request_id, is_last):
        symbol = str(getattr(row, "InstrumentID", "") or "")
        code = int(getattr(info, "ErrorID", 0) or 0)
        with self._state:
            if self._status != "ready":
                return
            if code:
                rejected = {symbol} if symbol else self._subscriptions - self._confirmed
                self._errors.update(dict.fromkeys(rejected, code))
            elif symbol in self._subscriptions:
                self._confirmed.add(symbol)
            self._state.notify_all()

    def OnRtnDepthMarketData(self, row):
        with self._state:
            if self._status != "ready" or self._closed:
                return
            generation = self._generation
        quote = quote_to_unified(row)
        if not quote.symbol or not isfinite(quote.last_price) or quote.last_price <= 0:
            return
        self._normalize_quote(quote)
        with self._state:
            if generation != self._generation or quote.symbol not in self._subscriptions:
                return
            previous = self._quotes.get(quote.symbol)
            if previous is None or quote.timestamp >= previous.timestamp:
                self._quotes[quote.symbol] = quote
                self._state.notify_all()

    def _normalize_quote(self, quote: UnifiedPriceData) -> None:
        exchange = quote.extra["exchange_id"]
        product = quote.symbol.rstrip("0123456789").lower()
        if not exchange:
            matches = {venue for venue, code in CTP_PRODUCT_SESSIONS if code == product}
            if len(matches) == 1:
                exchange = quote.extra["exchange_id"] = matches.pop()
        sessions = get_ctp_product_sessions(exchange, product)
        calendar = self._calendar

        def is_open(day) -> bool | None:
            return calendar.is_open(CHINA_CALENDAR_ID, day)

        native_day = quote.extra["trading_day"]
        trading_day = resolve_quote_trading_day(quote.update_time, native_day, sessions, is_open)
        normalized, status, reason = resolve_quote_time(quote.update_time, trading_day, sessions, is_open)
        quote.update_time = normalized
        quote.timestamp = int(datetime.fromisoformat(normalized).timestamp() * 1000) if normalized else 0
        quote.extra.update(
            raw_trading_day=native_day,
            trading_day=trading_day,
            event_time_status=status,
            event_time_reason=reason,
            source="ctp.market_data",
            received_at=get_default_clock().time(),
        )
