"""基于单线程 TqApi 运行时的天勤统一执行器."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any, cast, override
from uuid import uuid4
from zoneinfo import ZoneInfo

from axile.common.trade_channel import TradeChannel
from axile.executor.abstract_executor.base import AbstractExecutor
from axile.executor.account_control.exceptions import AccountControlBlockedError
from axile.executor.catalog_execution import CatalogExecutionMixin
from axile.executor.china_futures_session import is_within_possible_china_futures_session
from axile.executor.execution_engine import ExecutionEngine
from axile.executor.futures_order_intent import is_close_intent, plan_futures_close_orders, single_close_offset
from axile.executor.instrument_catalog import InstrumentCatalogProvider
from axile.executor.instrument_catalog_records import normalize_catalog_symbol
from axile.executor.models.execution_result import AlgorithmResult, ExecutionStatus, TargetSizingDecision
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_callback import OrderUpdateCallback, PriceDataCallback, TradeRecordCallback
from axile.executor.models.unified_input import AccountConfig, TQAccountConfig, UnifiedStandardInput
from axile.executor.models.unified_order import OrderDirection, OrderType, TradeRecord, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData
from axile.executor.session_closed import COMMON_SESSION_CLOSED, SESSION_CLOSED_MESSAGE
from axile.executor.tq.converters import account_to_unified, order_to_unified, quote_to_unified, trade_to_unified
from axile.executor.tq.runtime import TQRuntime, snapshot_entity
from axile.executor.tq.symbols import TQInstrument

# TqSdk 数值标志 + CTP 风格语义标志（算法层统一按 CTP 语义传递）。
_OFFSET_MAP = {
    "0": "OPEN",
    "1": "CLOSE",
    "3": "CLOSETODAY",
    "4": "CLOSE",
    "open": "OPEN",
    "close": "CLOSE",
    "close_today": "CLOSETODAY",
    "close_yesterday": "CLOSE",
}
_SHANGHAI = ZoneInfo("Asia/Shanghai")

_DAY = ((9 * 3600, 10 * 3600 + 15 * 60), (10 * 3600 + 30 * 60, 11 * 3600 + 30 * 60), (13 * 3600 + 30 * 60, 15 * 3600))
_INDEX_DAY = ((9 * 3600 + 30 * 60, 11 * 3600 + 30 * 60), (13 * 3600, 15 * 3600))
_BOND_DAY = ((9 * 3600 + 30 * 60, 11 * 3600 + 30 * 60), (13 * 3600, 15 * 3600 + 15 * 60))
_NIGHT_23 = ((21 * 3600, 23 * 3600),)
_NIGHT_01 = ((21 * 3600, 25 * 3600),)
_NIGHT_0230 = ((21 * 3600, 26 * 3600 + 30 * 60),)

# 静态快照，最后核对：2026-08-25。
# 官方交易时间来源：
# - SHFE: https://www.shfe.com.cn/services/standard/
# - INE: https://www.ine.cn/rule/
# - DCE: https://www.dce.com.cn/dalianshangpin/ywfw/jygl/jysj/index.html
# - CZCE: https://www.czce.com.cn/cn/jysj/
# - GFEX: https://www.gfex.com.cn/gfex/jysj/index.html
# - CFFEX: https://www.cffex.com.cn/jygl/
# 仅覆盖下表列出的期货品种；期权、组合及表外品种一律拒绝新单。GFEX 的 pd、pt
# 目前有意排除。交易所调整交易时段时，必须手动更新此表和上述核对日期；这不是对
# “当前可交易”的承诺。本表数据与 CTP 渠道完全独立，不读取 CTP 的时段数据或连接
# 状态。
_TQ_TRADING_SESSIONS = {
    **{("SHFE", product): (_DAY, _NIGHT_23) for product in ("rb", "hc", "fu", "bu", "ru", "br", "sp", "op")},
    **{("SHFE", product): (_DAY, _NIGHT_01) for product in ("cu", "al", "ao", "ad", "zn", "pb", "ni", "sn", "ss")},
    **{("SHFE", product): (_DAY, _NIGHT_0230) for product in ("au", "ag")},
    ("SHFE", "wr"): (_DAY, ()),
    **{("INE", product): (_DAY, _NIGHT_23) for product in ("nr", "lu")},
    ("INE", "bc"): (_DAY, _NIGHT_01),
    ("INE", "sc"): (_DAY, _NIGHT_0230),
    ("INE", "ec"): (_DAY, ()),
    **{
        ("DCE", product): (_DAY, _NIGHT_23)
        for product in (
            "a",
            "b",
            "m",
            "y",
            "p",
            "c",
            "cs",
            "rr",
            "jm",
            "j",
            "i",
            "pg",
            "l",
            "v",
            "eg",
            "pp",
            "l_f",
            "pp_f",
            "eb",
            "bz",
        )
    },
    **{("DCE", product): (_DAY, ()) for product in ("jd", "lh", "lg", "fb", "bb", "v_f")},
    **{
        ("CZCE", product): (_DAY, _NIGHT_23)
        for product in ("FG", "TA", "PR", "PX", "PL", "MA", "SA", "SH", "SR", "CF", "CY", "OI", "RM", "PF", "ZC")
    },
    **{
        ("CZCE", product): (_DAY, ())
        for product in ("UR", "SF", "SM", "AP", "CJ", "PK", "WH", "PM", "RI", "LR", "JR", "RS")
    },
    **{("GFEX", product): (_DAY, ()) for product in ("si", "lc", "ps")},
    **{("CFFEX", product): (_INDEX_DAY, ()) for product in ("IF", "IH", "IC", "IM")},
    **{("CFFEX", product): (_BOND_DAY, ()) for product in ("T", "TF", "TS", "TL")},
}


class TQTradingTimeStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    QUOTE_TRADING_TIME_UNAVAILABLE = "QUOTE_TRADING_TIME_UNAVAILABLE"
    CALENDAR_UNAVAILABLE = "CALENDAR_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class TQTradingTimeCheck:
    status: TQTradingTimeStatus

    @property
    def error(self) -> str | None:
        return None if self.status is TQTradingTimeStatus.OPEN else self.status.value

    @property
    def message(self) -> str | None:
        """返回面向用户的时段检查说明，机器码保留在 status 中。"""
        return {
            TQTradingTimeStatus.OPEN: None,
            TQTradingTimeStatus.CLOSED: "非交易时段",
            TQTradingTimeStatus.CALENDAR_UNAVAILABLE: "交易日历不可用",
            TQTradingTimeStatus.QUOTE_TRADING_TIME_UNAVAILABLE: "无法获取品种交易时段",
        }[self.status]


class TQExecutionEngine(ExecutionEngine):
    """TQ 品种时段筛选编排器。"""

    def _symbol_session_block(self, symbol: str) -> tuple[str, str] | None:
        check = cast("TQExecutor", self._owner)._check_symbol_trading_time(symbol)
        if check.status is TQTradingTimeStatus.CLOSED:
            return COMMON_SESSION_CLOSED, SESSION_CLOSED_MESSAGE
        if check.error is None:
            return None
        return check.error, check.message or "无法确认品种交易时段"

    def _derive_dispatch_error(
        self,
        status: ExecutionStatus,
        symbol_results: dict[str, AlgorithmResult],
    ) -> str | None:
        return super()._derive_dispatch_error(status, symbol_results)


class TQExecutor(CatalogExecutionMixin, AbstractExecutor):
    """将 TqSdk 原语适配到 Axile 通用执行器契约."""

    def __init__(self, account_config: TQAccountConfig) -> None:
        self._init_instrument_catalog()
        self._runtime: TQRuntime | None = None
        self._quotes: dict[str, UnifiedPriceData] = {}
        self._order_callbacks: list[OrderUpdateCallback] = []
        self._trade_callbacks: list[TradeRecordCallback] = []
        self._price_callbacks: list[PriceDataCallback] = []
        self._lock = threading.RLock()
        self._monitoring = False
        self._closed = False
        super().__init__(TradeChannel.TQ, account_config)

    def _config(self) -> TQAccountConfig:
        if not isinstance(self.account_config, TQAccountConfig):
            raise RuntimeError("TqSdk 配置不可用")
        return self.account_config

    @staticmethod
    def _build_api(config: TQAccountConfig) -> object:
        from tqsdk import TqAccount, TqApi, TqAuth, TqKq, TqSim

        auth = TqAuth(config.tq_username, config.tq_password)
        if config.account_mode == "live":
            account = TqAccount(config.broker_name, config.account_id, config.account_password)
        elif config.account_mode == "sim":
            account = TqSim(init_balance=config.initial_balance)
        else:
            account = TqKq()
        return TqApi(account=account, auth=auth)

    @override
    def _initialize_connection(self, account_config: AccountConfig) -> None:
        if not isinstance(account_config, TQAccountConfig):
            raise TypeError("TQExecutor requires TQAccountConfig")
        runtime = TQRuntime(lambda: self._build_api(account_config))
        runtime.add_listener(self._on_runtime_event)
        self._runtime = runtime
        self._instrument_provider.bind(runtime.query_catalog, channel="tq")
        runtime.install_catalog([], self._resolve_catalog_symbol)

    def _install_catalog(self, rows) -> None:
        self._require_runtime().install_catalog(list(self._catalog_records.values()), self._resolve_catalog_symbol)

    @override
    def set_instrument_catalog_provider(self, provider: InstrumentCatalogProvider) -> None:
        """TQ 构造时已连接；注入主服务目录时迁移现有查询来源。"""
        if self._instrument_provider is provider:
            return
        self._instrument_provider.close()
        super().set_instrument_catalog_provider(provider)
        if self._runtime is not None:
            provider.bind(self._runtime.query_catalog, channel="tq")

    def _resolve_catalog_symbol(self, symbol: str) -> list[TQInstrument]:
        from axile.executor.instrument_catalog_records import EXCHANGES

        if "." in symbol and symbol.split(".", 1)[0] not in EXCHANGES:
            exchange, local = symbol.split(".", 1)
            return [TQInstrument(symbol, local, exchange, "QUOTE_ONLY")]
        self._ensure_instrument_symbols([symbol])
        row = self._catalog_record(symbol)
        if row is None:
            raise ValueError(f"缺少合约资料: {symbol}")
        return [TQInstrument(row.key, row.symbol, row.exchange, row.kind)]

    def _catalog_related_symbols(self) -> list[str]:
        def collect(api):
            positions = getattr(api, "get_position")()
            orders = getattr(api, "get_order")()
            symbols = [
                str(symbol)
                for symbol, row in positions.items()
                if any(
                    float(getattr(row, name, 0) or 0) != 0
                    for name in ("pos_long_today", "pos_long_his", "pos_short_today", "pos_short_his")
                )
            ]
            symbols.extend(
                f"{row.exchange_id}.{row.instrument_id}"
                for row in orders.values()
                if getattr(row, "status", "") == "ALIVE"
            )
            return symbols

        return self._require_runtime().call(collect)

    def _require_runtime(self) -> TQRuntime:
        if self._runtime is None:
            raise RuntimeError("TqSdk 运行时尚未初始化")
        return self._runtime

    @override
    def _verify_connection(self) -> bool:
        return not self._closed and self._runtime is not None and self._runtime.is_alive()

    @override
    def _check_trading_time(self) -> bool:
        # 日夜盘缝用钟整市场判断；盘中混合持仓仍按品种拦。
        return is_within_possible_china_futures_session(datetime.now(_SHANGHAI))

    @staticmethod
    def _matched_trading_session(
        day_ranges: tuple[tuple[int, int], ...],
        night_ranges: tuple[tuple[int, int], ...],
        now: datetime,
    ) -> tuple[date, bool] | None:
        seconds = now.hour * 3600 + now.minute * 60 + now.second
        if any(begin <= seconds < end for begin, end in day_ranges):
            return now.date(), False
        if any(begin <= seconds < end for begin, end in night_ranges):
            return now.date(), True
        if any(begin <= seconds + 24 * 3600 < end for begin, end in night_ranges):
            return now.date() - timedelta(days=1), True
        return None

    @staticmethod
    def _calendar_trading_dates(calendar: object) -> dict[date, bool]:
        rows = getattr(calendar, "to_dict")("records")
        if not isinstance(rows, list):
            raise ValueError("交易日历不可读取")
        trading_dates: dict[date, bool] = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("trading"), bool):
                raise ValueError("交易日历缺少有效字段")
            raw_date = row.get("date")
            if isinstance(raw_date, datetime):
                calendar_date = raw_date.date()
            elif isinstance(raw_date, date):
                calendar_date = raw_date
            elif isinstance(raw_date, str):
                calendar_date = date.fromisoformat(raw_date[:10])
            else:
                raise ValueError("交易日历日期无效")
            trading_dates[calendar_date] = row["trading"]
        return trading_dates

    def _trading_sessions(
        self, tq_symbol: str
    ) -> tuple[tuple[tuple[int, int], ...], tuple[tuple[int, int], ...]] | None:
        instrument = self._require_runtime().resolver.instrument(tq_symbol)
        if instrument is None or instrument.ins_class != "FUTURE":
            return None
        product = instrument.instrument_id.rstrip("0123456789")
        return _TQ_TRADING_SESSIONS.get((instrument.exchange_id, product)) if product else None

    @staticmethod
    def _night_session_check(calendar: dict[date, bool], session_day: date) -> TQTradingTimeCheck:
        if not calendar.get(session_day):
            return TQTradingTimeCheck(
                TQTradingTimeStatus.CLOSED if session_day in calendar else TQTradingTimeStatus.CALENDAR_UNAVAILABLE
            )
        for offset in range(1, 15):
            candidate = session_day + timedelta(days=offset)
            if candidate not in calendar:
                return TQTradingTimeCheck(TQTradingTimeStatus.CALENDAR_UNAVAILABLE)
            if calendar[candidate]:
                return TQTradingTimeCheck(
                    TQTradingTimeStatus.OPEN
                    if all((session_day + timedelta(days=day_offset)).weekday() >= 5 for day_offset in range(1, offset))
                    else TQTradingTimeStatus.CLOSED
                )
        return TQTradingTimeCheck(TQTradingTimeStatus.CALENDAR_UNAVAILABLE)

    def _check_tq_symbol_trading_time(
        self,
        api: object,
        sessions: tuple[tuple[tuple[int, int], ...], tuple[tuple[int, int], ...]] | None,
        now: datetime,
    ) -> TQTradingTimeCheck:
        if sessions is None:
            return TQTradingTimeCheck(TQTradingTimeStatus.QUOTE_TRADING_TIME_UNAVAILABLE)
        session = self._matched_trading_session(*sessions, now)
        if session is None:
            return TQTradingTimeCheck(TQTradingTimeStatus.CLOSED)
        session_day, is_night = session
        try:
            calendar = self._calendar_trading_dates(
                getattr(api, "get_trading_calendar")(session_day, session_day + timedelta(days=14 if is_night else 0))
            )
        except (AttributeError, TypeError, ValueError, RuntimeError):
            return TQTradingTimeCheck(TQTradingTimeStatus.CALENDAR_UNAVAILABLE)
        if not is_night:
            if session_day not in calendar:
                return TQTradingTimeCheck(TQTradingTimeStatus.CALENDAR_UNAVAILABLE)
            return TQTradingTimeCheck(TQTradingTimeStatus.OPEN if calendar[session_day] else TQTradingTimeStatus.CLOSED)
        return self._night_session_check(calendar, session_day)

    def _check_symbol_trading_times(
        self,
        symbols: list[str],
        now: datetime,
    ) -> dict[str, TQTradingTimeCheck]:
        runtime = self._require_runtime()
        pending: dict[str, tuple[tuple[tuple[int, int], ...], tuple[tuple[int, int], ...]] | None] = {}
        results: dict[str, TQTradingTimeCheck] = {}
        for symbol in dict.fromkeys(symbols):
            try:
                tq_symbol = runtime.resolver.to_tq(symbol, for_trade=True)
            except (ValueError, RuntimeError, TimeoutError):
                results[symbol] = TQTradingTimeCheck(TQTradingTimeStatus.QUOTE_TRADING_TIME_UNAVAILABLE)
            else:
                pending[symbol] = self._trading_sessions(tq_symbol)
        if not pending:
            return results
        try:
            results.update(
                runtime.call(
                    lambda api: {
                        symbol: self._check_tq_symbol_trading_time(api, sessions, now)
                        for symbol, sessions in pending.items()
                    }
                )
            )
        except (RuntimeError, TimeoutError):
            results.update({symbol: TQTradingTimeCheck(TQTradingTimeStatus.CALENDAR_UNAVAILABLE) for symbol in pending})
        return results

    def _check_symbol_trading_time(self, symbol: str, now: datetime | None = None) -> TQTradingTimeCheck:
        local_now = now or datetime.now(_SHANGHAI)
        if local_now.tzinfo is None:
            local_now = local_now.replace(tzinfo=_SHANGHAI)
        return self._check_symbol_trading_times([symbol], local_now.astimezone(_SHANGHAI))[symbol]

    @override
    def _normalize_connected_standard_input(self, standard_input: UnifiedStandardInput) -> UnifiedStandardInput:
        """使用 TqSdk 合约目录将输入代码统一为 Axile 合约代码."""
        return self._require_runtime().resolver.normalize_input(standard_input)

    @override
    def _execution_engine(self) -> ExecutionEngine:
        return TQExecutionEngine(self, self.require_execution_runtime())

    @override
    def _validate_input(self, standard_input: UnifiedStandardInput) -> None:
        super()._validate_input(standard_input)

    @override
    def get_account_assets(self) -> UnifiedAccountAssets:
        runtime = self._require_runtime()

        def query(api: object) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
            account = snapshot_entity(getattr(api, "get_account")())
            native_positions = getattr(api, "get_position")()
            positions = {
                str(symbol): snapshot_entity(row)
                for symbol, row in native_positions.items()
                if any(
                    float(getattr(row, name, 0) or 0) != 0
                    for name in ("pos_long_today", "pos_long_his", "pos_short_today", "pos_short_his")
                )
            }
            return account, positions

        account, positions = runtime.call(query)
        self._ensure_instrument_symbols(list(positions))
        quotes = runtime.quote_snapshots(list(positions)) if positions else {}
        return account_to_unified(account, positions, runtime.resolver, quotes)

    def _quote_snapshot(self, symbol: str) -> dict[str, object]:
        runtime = self._require_runtime()
        tq_symbol = normalize_catalog_symbol(symbol) if "." in symbol else runtime.resolver.to_tq(symbol)
        return runtime.quote_snapshot(tq_symbol)

    @override
    def get_market_data(self, symbols: list[str]) -> dict[str, UnifiedPriceData]:
        runtime = self._require_runtime()
        tq_symbols = [
            normalize_catalog_symbol(symbol) if "." in symbol else runtime.resolver.to_tq(symbol) for symbol in symbols
        ]
        rows = runtime.quote_snapshots(tq_symbols)
        result: dict[str, UnifiedPriceData] = {}
        for row in rows.values():
            quote = quote_to_unified(row, runtime.resolver)
            self._quotes[quote.symbol] = quote
            result[quote.symbol] = quote
        return result

    def plan_close_orders(
        self, symbol: str, direction: OrderDirection, volume: float, account_assets: UnifiedAccountAssets
    ) -> list[tuple[float, dict[str, object]]]:
        """使用渠道交易所信息与持仓快照拆分平今、平昨订单。"""
        exchange = self._require_runtime().resolver.to_tq(symbol, for_trade=True).split(".", 1)[0]
        return plan_futures_close_orders(symbol, direction, volume, account_assets, exchange)

    @override
    def _place_order_impl(
        self,
        symbol: str,
        direction: OrderDirection,
        order_type: OrderType,
        volume: float,
        price: float = 0,
        **kwargs: object,
    ) -> UnifiedOrder:
        if not float(volume).is_integer() or volume <= 0:
            raise ValueError("TqSdk 委托数量必须为正整数手")
        self._ensure_instrument_symbols([symbol])
        runtime = self._require_runtime()
        tq_symbol = runtime.resolver.to_tq(symbol, for_trade=True)
        sessions = self._trading_sessions(tq_symbol)
        offset_flag = kwargs.get("offset_flag")
        if offset_flag is None:
            offset_flag = "0"
            if is_close_intent(direction, kwargs.get("position_side")):
                offset_flag = single_close_offset(
                    self.plan_close_orders(symbol, direction, volume, self.get_account_assets())
                )
        offset_flag = str(offset_flag)
        offset = _OFFSET_MAP.get(offset_flag)
        if offset is None:
            raise ValueError(f"TqSdk 不支持开平标志: {offset_flag}")

        order_id = uuid4().hex

        def submit(api: object) -> TQTradingTimeCheck | dict[str, object]:
            check = self._check_tq_symbol_trading_time(api, sessions, datetime.now(_SHANGHAI))
            if check.error is not None:
                return check
            insert_order = getattr(api, "insert_order")
            arguments: dict[str, object] = {
                "symbol": tq_symbol,
                "direction": direction.value,
                "offset": offset,
                "volume": int(volume),
                "limit_price": None if order_type is OrderType.MARKET else float(price),
                "order_id": order_id,
            }
            native_order = insert_order(**arguments)
            row = snapshot_entity(native_order)
            row["order_id"] = order_id
            return row

        result = runtime.call(submit)
        if isinstance(result, TQTradingTimeCheck):
            raise AccountControlBlockedError(
                result.message or "非交易时段",
                reason_code=result.error,
                account_id=None,
                execution_id=None,
                channel=TradeChannel.TQ,
                operation="place_order",
                symbol=symbol,
            )
        order = order_to_unified(result, runtime.resolver)
        # 意图翻译结果落进 extra,供订单跟踪器追价重报时回读开平标志。
        order.extra["offset_flag"] = offset_flag
        return order

    @override
    def _cancel_order_impl(self, symbol: str, order_id: str) -> bool:
        runtime = self._require_runtime()
        runtime.resolver.to_tq(symbol, for_trade=True)
        runtime.call(lambda api: getattr(api, "cancel_order")(order_id))
        return True

    @override
    def _get_pending_orders_impl(self, symbol: str | None = None) -> list[UnifiedOrder]:
        runtime = self._require_runtime()
        symbol = normalize_catalog_symbol(symbol) if symbol is not None else None
        rows = runtime.call(lambda api: [snapshot_entity(row) for row in getattr(api, "get_order")().values()])
        orders = [order_to_unified(row, runtime.resolver) for row in rows]
        return [
            order
            for order in orders
            if order.is_active()
            and (
                symbol is None
                or order.symbol == symbol
                or normalize_catalog_symbol(str(order.extra["tq_symbol"])) == symbol
            )
        ]

    @override
    def _query_trades_impl(self, symbol: str, order_id: str) -> list[TradeRecord]:
        runtime = self._require_runtime()
        runtime.resolver.to_tq(symbol)
        rows = runtime.call(lambda api: [snapshot_entity(row) for row in getattr(api, "get_trade")().values()])
        trades = [trade_to_unified(row, runtime.resolver) for row in rows]
        return [trade for trade in trades if trade.order_id == order_id and trade.symbol == symbol]

    @override
    def get_tick_size(self, symbol: str) -> float | None:
        value = self._quote_snapshot(symbol).get("price_tick", 0)
        try:
            tick = float(value or 0)
        except (TypeError, ValueError):
            return None
        return tick if tick > 0 else None

    @override
    def _calculate_generic_sizing(
        self,
        weight: float,
        price: float,
        account_assets: UnifiedAccountAssets,
        trade_rule: dict[str, object],
        *,
        symbol: str | None = None,
    ) -> TargetSizingDecision:
        """按天勤合约规格生成整数手目标及换算证据."""
        sizing_mode = str(trade_rule.get("sizing_mode", "weight"))
        if sizing_mode == "lots":
            target = float(int(weight))
            reason = "COMMON.SIZING.EXACT" if target == weight else "COMMON.SIZING.QUANTIZED"
            return TargetSizingDecision(
                symbol=symbol or "",
                sizing_mode=sizing_mode,
                reason_code=reason,
                account_weight=weight,
                equity=account_assets.total_asset,
                raw_quantity=weight,
                target_quantity=target,
                quantity_step=1.0,
                min_quantity=1.0,
            )
        if not symbol or price <= 0:
            return TargetSizingDecision(
                symbol=symbol or "",
                status="UNAVAILABLE",
                reason_code="COMMON.SIZING.INVALID_PRICE",
                account_weight=weight,
                equity=account_assets.total_asset,
                reference_price=price,
            )
        native = self._quote_snapshot(symbol).get("volume_multiple", 0)
        fallback = trade_rule.get("contract_multiplier", 0)
        try:
            multiplier = float(native or fallback or 0)
        except (TypeError, ValueError):
            multiplier = 0
        if multiplier <= 0:
            return TargetSizingDecision(
                symbol=symbol,
                status="UNAVAILABLE",
                reason_code="COMMON.SIZING.MISSING_UNIT_MULTIPLIER",
                account_weight=weight,
                equity=account_assets.total_asset,
                reference_price=price,
            )
        raw_quantity = account_assets.total_asset * weight / (price * multiplier)
        target_quantity = float(int(raw_quantity))
        reason = "COMMON.SIZING.EXACT"
        if raw_quantity != 0 and target_quantity == 0:
            reason = "COMMON.SIZING.BELOW_MIN_QUANTITY"
        elif abs(raw_quantity - target_quantity) > 1e-12:
            reason = "COMMON.SIZING.QUANTIZED"
        return TargetSizingDecision(
            symbol=symbol,
            sizing_mode=sizing_mode,
            reason_code=reason,
            account_weight=weight,
            equity=account_assets.total_asset,
            reference_price=price,
            unit_multiplier=multiplier,
            unit_notional=price * multiplier,
            target_notional=abs(account_assets.total_asset * weight),
            raw_quantity=raw_quantity,
            target_quantity=target_quantity,
            quantity_step=1.0,
            min_quantity=1.0,
        )

    @override
    def initialize_websocket(self, symbols: list[str] | None = None) -> None:
        if symbols:
            runtime = self._require_runtime()
            runtime.subscribe(
                [
                    normalize_catalog_symbol(symbol) if "." in symbol else runtime.resolver.to_tq(symbol)
                    for symbol in symbols
                ]
            )
            self._monitoring = True

    @override
    def is_monitoring(self) -> bool:
        return self._monitoring and self._verify_connection()

    def _dispatch(self, callbacks: list[Any], value: object) -> None:
        for callback in tuple(callbacks):
            try:
                callback(value)
            except Exception:
                self.logger.exception("TqSdk callback 执行失败")

    def _on_runtime_event(self, kind: str, row: dict[str, object]) -> None:
        runtime = self._require_runtime()
        if kind == "quote":
            value = quote_to_unified(row, runtime.resolver)
            self._quotes[value.symbol] = value
            self._dispatch(self._price_callbacks, value)
        elif kind == "order":
            self._dispatch(self._order_callbacks, order_to_unified(row, runtime.resolver))
        elif kind == "trade":
            self._dispatch(self._trade_callbacks, trade_to_unified(row, runtime.resolver))

    def _register(self, callbacks: list[Any], callback: Any) -> None:
        with self._lock:
            if callback not in callbacks:
                callbacks.append(callback)

    def _unregister(self, callbacks: list[Any], callback: Any) -> None:
        with self._lock:
            if callback in callbacks:
                callbacks.remove(callback)

    @override
    def register_order_callback(self, callback: OrderUpdateCallback) -> None:
        self._register(self._order_callbacks, callback)

    def register_trade_callback(self, callback: TradeRecordCallback) -> None:
        """注册成交回调."""
        self._register(self._trade_callbacks, callback)

    @override
    def register_price_callback(self, callback: PriceDataCallback) -> None:
        self._register(self._price_callbacks, callback)

    @override
    def unregister_order_callback(self, callback: OrderUpdateCallback) -> None:
        self._unregister(self._order_callbacks, callback)

    def unregister_trade_callback(self, callback: TradeRecordCallback) -> None:
        """注销成交回调."""
        self._unregister(self._trade_callbacks, callback)

    @override
    def unregister_price_callback(self, callback: PriceDataCallback) -> None:
        self._unregister(self._price_callbacks, callback)

    @override
    def _get_account_mark(self) -> str:
        config = self._config()
        return config.account_id or config.tq_username

    @override
    def _get_default_trade_rules_for_empty(self, symbols: list[str]) -> dict[str, dict[str, object]]:
        return {symbol: {} for symbol in symbols}

    @override
    def _cleanup(self) -> None:
        """单次执行结束不关闭常驻 TqSdk 会话."""

    def close(self) -> None:
        """关闭 TqSdk owner thread 与 API."""
        if self._closed:
            return
        self._closed = True
        self._instrument_provider.close()
        if self._runtime is not None:
            self._runtime.close()

    def stop(self) -> None:
        """停止执行器并关闭 TqSdk 会话."""
        self.close()


__all__ = ["TQExecutor"]
