from __future__ import annotations

from datetime import date, datetime, timedelta
from threading import get_ident
from zoneinfo import ZoneInfo

import pytest

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionReasonFamily
from axile.executor.account_control.exceptions import AccountControlBlockedError
from axile.executor.models.execution_result import AlgorithmResult, ExecutionStatus, TargetSizingStatus
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_input import TQAccountConfig, UnifiedStandardInput
from axile.executor.models.unified_order import OrderDirection, OrderType
from axile.executor.models.unified_price import UnifiedPriceData
from axile.executor.tq import TQExecutor
from axile.executor.tq.tq_execute import TQTradingTimeCheck, TQTradingTimeStatus

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class _Calendar:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def to_dict(self, orient: str) -> list[dict[str, object]]:
        assert orient == "records"
        return self.rows


from tests.unit.executor.tq._catalog_test_support import CatalogApiMixin


class FakeApi(CatalogApiMixin):
    futures = [
        "SHFE.rb2610",
        "SHFE.ag2612",
        "SHFE.au2612",
        "SHFE.bu2612",
        "SHFE.xx2601",
        "INE.sc2609",
        "DCE.a2609",
        "DCE.l_f2609",
        "DCE.pp_f2609",
        "DCE.v_f2609",
        "CZCE.TA609",
        "GFEX.si2609",
        "CFFEX.IF2609",
        "CFFEX.T2609",
    ]
    options = ["SHFE.rb2610C3200"]
    combines = ["SHFE.rb2610&rb2611"]

    def __init__(self) -> None:
        self.owner: int | None = None
        self.insert_calls = 0
        self.calendar_error: Exception | None = None
        self.calendar_rows: list[dict[str, object]] | None = None
        self.closed_dates: set[date] = set()

    def wait_update(self, *, deadline: float) -> bool:
        del deadline
        self.pump_catalog()
        return False

    def is_changing(self, _entity: object) -> bool:
        return False

    def get_trading_calendar(self, start: date, end: date) -> _Calendar:
        self.owner = get_ident()
        if self.calendar_error:
            raise self.calendar_error
        if self.calendar_rows is not None:
            return _Calendar(self.calendar_rows)
        rows = []
        current = start
        while current <= end:
            rows.append({"date": current, "trading": current.weekday() < 5 and current not in self.closed_dates})
            current += timedelta(days=1)
        return _Calendar(rows)

    def insert_order(self, **_kwargs: object) -> dict[str, object]:
        self.owner = get_ident()
        self.insert_calls += 1
        return {
            "order_id": "order-1",
            "exchange_id": "SHFE",
            "instrument_id": "rb2610",
            "direction": "BUY",
            "offset": "OPEN",
            "price_type": "LIMIT",
            "volume_orign": 1,
            "volume_left": 1,
            "limit_price": 3200,
            "status": "ALIVE",
        }

    def close(self) -> None:
        self.close_catalog()


@pytest.fixture
def executor(monkeypatch: pytest.MonkeyPatch) -> tuple[TQExecutor, FakeApi]:
    api = FakeApi()
    monkeypatch.setattr(TQExecutor, "_build_api", staticmethod(lambda _config: api))
    instance = TQExecutor(TQAccountConfig(account_mode="kq", tq_username="user", tq_password="secret"))
    try:
        yield instance, api
    finally:
        instance.close()


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=_SHANGHAI)


@pytest.mark.parametrize(
    ("symbol", "moment", "expected"),
    [
        ("rb2610", "2026-08-24T09:30:00", TQTradingTimeStatus.OPEN),
        ("rb2610", "2026-08-24T10:15:00", TQTradingTimeStatus.CLOSED),
        ("rb2610", "2026-08-24T11:45:00", TQTradingTimeStatus.CLOSED),
        ("rb2610", "2026-08-21T21:00:00", TQTradingTimeStatus.OPEN),
        ("rb2610", "2026-08-21T23:00:00", TQTradingTimeStatus.CLOSED),
        ("bu2612", "2026-08-21T22:59:59", TQTradingTimeStatus.OPEN),
        ("bu2612", "2026-08-21T23:00:00", TQTradingTimeStatus.CLOSED),
        ("ag2612", "2026-08-22T02:29:59", TQTradingTimeStatus.OPEN),
        ("ag2612", "2026-08-22T02:30:00", TQTradingTimeStatus.CLOSED),
        ("au2612", "2026-08-22T02:29:59", TQTradingTimeStatus.OPEN),
        ("au2612", "2026-08-22T02:30:00", TQTradingTimeStatus.CLOSED),
    ],
)
def test_symbol_trading_time_respects_product_sessions(
    executor: tuple[TQExecutor, FakeApi], symbol: str, moment: str, expected: TQTradingTimeStatus
) -> None:
    instance, api = executor

    assert instance._check_symbol_trading_time(symbol, _at(moment)).status is expected
    if expected is TQTradingTimeStatus.OPEN:
        assert api.owner == instance._require_runtime()._thread.ident


@pytest.mark.parametrize(
    ("symbol", "moment"),
    [
        ("sc2609", "2026-08-22T02:29:59"),
        ("a2609", "2026-08-21T22:59:59"),
        ("l_f2609", "2026-08-21T22:59:59"),
        ("pp_f2609", "2026-08-21T22:59:59"),
        ("v_f2609", "2026-08-24T09:30:00"),
        ("TA609", "2026-08-21T22:59:59"),
        ("si2609", "2026-08-24T09:30:00"),
        ("IF2609", "2026-08-24T09:30:00"),
        ("T2609", "2026-08-24T15:14:59"),
    ],
)
def test_static_sessions_cover_each_exchange(executor: tuple[TQExecutor, FakeApi], symbol: str, moment: str) -> None:
    instance, _ = executor

    assert instance._check_symbol_trading_time(symbol, _at(moment)).status is TQTradingTimeStatus.OPEN


def test_friday_night_continues_to_saturday_but_sunday_night_is_closed(executor: tuple[TQExecutor, FakeApi]) -> None:
    instance, _ = executor

    assert instance._check_symbol_trading_time("ag2612", _at("2026-08-21T21:30:00")).status is TQTradingTimeStatus.OPEN
    assert instance._check_symbol_trading_time("ag2612", _at("2026-08-22T00:30:00")).status is TQTradingTimeStatus.OPEN
    assert (
        instance._check_symbol_trading_time("ag2612", _at("2026-08-23T21:30:00")).status is TQTradingTimeStatus.CLOSED
    )
    assert (
        instance._check_symbol_trading_time("ag2612", _at("2026-08-24T00:30:00")).status is TQTradingTimeStatus.CLOSED
    )


def test_night_session_before_holiday_is_closed(executor: tuple[TQExecutor, FakeApi]) -> None:
    instance, api = executor
    api.closed_dates.add(date(2026, 8, 21))

    assert (
        instance._check_symbol_trading_time("rb2610", _at("2026-08-20T21:30:00")).status is TQTradingTimeStatus.CLOSED
    )


@pytest.mark.parametrize("symbol", ["xx2601", "rb2610C3200", "rb2610&rb2611", "missing2601"])
def test_uncovered_option_and_combine_fail_closed(executor: tuple[TQExecutor, FakeApi], symbol: str) -> None:
    instance, _ = executor

    assert instance._check_symbol_trading_time(symbol, _at("2026-08-24T09:30:00")).status is (
        TQTradingTimeStatus.QUOTE_TRADING_TIME_UNAVAILABLE
    )


def test_calendar_failure_or_missing_date_fail_closed(executor: tuple[TQExecutor, FakeApi]) -> None:
    instance, api = executor
    api.calendar_error = RuntimeError("calendar unavailable")
    assert instance._check_symbol_trading_time("rb2610", _at("2026-08-24T09:30:00")).status is (
        TQTradingTimeStatus.CALENDAR_UNAVAILABLE
    )

    api.calendar_error = None
    api.calendar_rows = [{"date": "2026-08-25", "trading": True}]
    assert instance._check_symbol_trading_time("rb2610", _at("2026-08-24T09:30:00")).status is (
        TQTradingTimeStatus.CALENDAR_UNAVAILABLE
    )


def test_engine_blocks_symbol_sessions_without_market_io(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeApi()
    monkeypatch.setattr(TQExecutor, "_build_api", staticmethod(lambda _config: api))
    instance = TQExecutor(TQAccountConfig(account_mode="kq", tq_username="user", tq_password="secret"))
    market_requests: list[list[str]] = []
    try:
        monkeypatch.setattr(instance, "_check_trading_time", lambda: True)
        monkeypatch.setattr(
            instance,
            "_check_symbol_trading_time",
            lambda symbol: TQTradingTimeCheck(
                TQTradingTimeStatus.CLOSED if symbol == "rb2610" else TQTradingTimeStatus.OPEN
            ),
        )
        monkeypatch.setattr(
            instance,
            "get_account_assets",
            lambda: UnifiedAccountAssets(available_cash=1_000, total_asset=1_000, market_value=0, positions=[]),
        )
        monkeypatch.setattr(instance, "_calculate_generic_volume", lambda *_args, **_kwargs: 1.0)
        monkeypatch.setattr(
            instance,
            "get_market_data",
            lambda symbols: (
                market_requests.append(symbols)
                or {
                    symbol: UnifiedPriceData(
                        symbol=symbol,
                        last_price=100,
                        bid_price=99,
                        ask_price=101,
                        bid_volume=1,
                        ask_volume=1,
                        volume=1,
                        turnover=100,
                        timestamp=1,
                        update_time="2026-08-24T09:30:00",
                    )
                    for symbol in symbols
                }
            ),
        )
        monkeypatch.setattr(instance, "initialize_websocket", lambda _symbols: None)
        monkeypatch.setattr(instance, "cancel_all_orders", lambda: None)
        monkeypatch.setattr(
            "axile.executor.execution_engine.resolve_algorithm",
            lambda _name, _session: (
                lambda _session, algorithm_input: AlgorithmResult(
                    symbol=algorithm_input.symbol,
                    algorithm="SINGLE-MAKER",
                    target_volume=algorithm_input.target_volume,
                )
            ),
        )
        instance._quotes["rb2610"] = UnifiedPriceData(
            symbol="rb2610",
            last_price=100,
            bid_price=99,
            ask_price=101,
            bid_volume=1,
            ask_volume=1,
            volume=1,
            turnover=100,
            timestamp=1,
            update_time="2026-08-24T11:29:00",
        )

        output = instance.execute(
            UnifiedStandardInput(
                channel_type=TradeChannel.TQ,
                account_config=TQAccountConfig(account_mode="kq", tq_username="user", tq_password="secret"),
                curr_target={"rb2610": 0.1, "ag2612": 0.2},
                algorithm={"method": "SINGLE-MAKER"},
            ),
            cleanup=False,
        )
    finally:
        instance.close()

    blocked = output.symbol_results["rb2610"]
    assert output.status is ExecutionStatus.PARTIAL
    assert blocked.status is ExecutionStatus.BLOCKED
    assert blocked.error == "非交易时段"
    assert blocked.reason_code == "COMMON.SESSION.CLOSED"
    assert blocked.memory == {
        "symbol_decision_reason_code": "COMMON.SESSION.CLOSED",
        "symbol_decision_reason_family": ExecutionReasonFamily.MARKET_RULE.value,
    }
    assert blocked.sizing is not None
    assert blocked.sizing.target_quantity == 1.0
    assert blocked.target_volume == 1.0
    assert market_requests == [["ag2612"]]


def test_execute_missing_required_contract_fails_before_any_order(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeApi()
    monkeypatch.setattr(TQExecutor, "_build_api", staticmethod(lambda _config: api))
    instance = TQExecutor(TQAccountConfig(account_mode="kq", tq_username="user", tq_password="secret"))
    market_requests: list[list[str]] = []
    try:
        monkeypatch.setattr(instance, "_check_trading_time", lambda: True)
        monkeypatch.setattr(
            instance,
            "_check_tq_symbol_trading_time",
            lambda _api, _sessions, _now: TQTradingTimeCheck(TQTradingTimeStatus.OPEN),
        )
        monkeypatch.setattr(
            instance,
            "get_account_assets",
            lambda: UnifiedAccountAssets(available_cash=1_000, total_asset=1_000, market_value=0, positions=[]),
        )
        monkeypatch.setattr(instance, "_calculate_generic_volume", lambda *_args, **_kwargs: 1.0)
        monkeypatch.setattr(
            instance,
            "get_market_data",
            lambda symbols: (
                market_requests.append(symbols)
                or {
                    symbol: UnifiedPriceData(
                        symbol=symbol,
                        last_price=100,
                        bid_price=99,
                        ask_price=101,
                        bid_volume=1,
                        ask_volume=1,
                        volume=1,
                        turnover=100,
                        timestamp=1,
                        update_time="2026-08-24T09:30:00",
                    )
                    for symbol in symbols
                }
            ),
        )
        monkeypatch.setattr(instance, "initialize_websocket", lambda _symbols: None)
        monkeypatch.setattr(instance, "cancel_all_orders", lambda: None)
        monkeypatch.setattr(
            "axile.executor.execution_engine.resolve_algorithm",
            lambda _name, _session: (
                lambda _session, algorithm_input: AlgorithmResult(
                    symbol=algorithm_input.symbol,
                    algorithm="SINGLE-MAKER",
                    target_volume=algorithm_input.target_volume,
                )
            ),
        )

        from axile.executor.instrument_catalog import CatalogError

        with pytest.raises(CatalogError):
            instance.execute(
                UnifiedStandardInput(
                    channel_type=TradeChannel.TQ,
                    account_config=TQAccountConfig(account_mode="kq", tq_username="user", tq_password="secret"),
                    curr_target={"rb2610": 0.1, "SHFE.missing2601": 0.1},
                    algorithm={"method": "SINGLE-MAKER"},
                ),
                cleanup=False,
            )
    finally:
        instance.close()

    assert api.insert_calls == 0
    assert market_requests == []


def test_engine_blocks_all_symbol_sessions_without_execution_io(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeApi()
    monkeypatch.setattr(TQExecutor, "_build_api", staticmethod(lambda _config: api))
    instance = TQExecutor(TQAccountConfig(account_mode="kq", tq_username="user", tq_password="secret"))
    cancel_calls = 0
    try:
        monkeypatch.setattr(instance, "_check_trading_time", lambda: True)
        monkeypatch.setattr(
            instance,
            "_check_symbol_trading_time",
            lambda _symbol: TQTradingTimeCheck(TQTradingTimeStatus.CLOSED),
        )
        monkeypatch.setattr(
            instance,
            "get_account_assets",
            lambda: UnifiedAccountAssets(available_cash=1_000, total_asset=1_000, market_value=0, positions=[]),
        )
        monkeypatch.setattr(instance, "get_market_data", lambda _symbols: pytest.fail("不应读取行情"))

        def cancel_all_orders() -> None:
            nonlocal cancel_calls
            cancel_calls += 1

        monkeypatch.setattr(instance, "cancel_all_orders", cancel_all_orders)
        output = instance.execute(
            UnifiedStandardInput(
                channel_type=TradeChannel.TQ,
                account_config=TQAccountConfig(account_mode="kq", tq_username="user", tq_password="secret"),
                curr_target={"rb2610": 0.1, "ag2612": 0.2},
                algorithm={"method": "SINGLE-MAKER"},
            ),
            cleanup=False,
        )
    finally:
        instance.close()

    assert output.status is ExecutionStatus.BLOCKED
    assert output.error is not None
    assert "2 个品种未执行" in output.error
    assert output.error == "非交易时段，2 个品种未执行"
    assert output.reason_code == "COMMON.SESSION.CLOSED"
    assert cancel_calls == 0
    for symbol in ("rb2610", "ag2612"):
        result = output.symbol_results[symbol]
        assert result.status is ExecutionStatus.BLOCKED
        assert result.sizing is not None
        assert result.sizing.status is TargetSizingStatus.UNAVAILABLE
        assert result.target_volume is None


def test_engine_sizes_closed_symbols_from_cached_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeApi()
    monkeypatch.setattr(TQExecutor, "_build_api", staticmethod(lambda _config: api))
    instance = TQExecutor(TQAccountConfig(account_mode="kq", tq_username="user", tq_password="secret"))
    try:
        monkeypatch.setattr(instance, "_check_trading_time", lambda: True)
        monkeypatch.setattr(
            instance,
            "_check_symbol_trading_time",
            lambda _symbol: TQTradingTimeCheck(TQTradingTimeStatus.CLOSED),
        )
        monkeypatch.setattr(
            instance,
            "get_account_assets",
            lambda: UnifiedAccountAssets(available_cash=1_000, total_asset=1_000, market_value=0, positions=[]),
        )
        monkeypatch.setattr(instance, "_calculate_generic_volume", lambda *_args, **_kwargs: 1.0)
        monkeypatch.setattr(instance, "get_market_data", lambda _symbols: pytest.fail("不应读取行情"))
        monkeypatch.setattr(instance, "cancel_all_orders", lambda: pytest.fail("不应撤单"))
        instance._quotes["rb2610"] = UnifiedPriceData(
            symbol="rb2610",
            last_price=100,
            bid_price=99,
            ask_price=101,
            bid_volume=1,
            ask_volume=1,
            volume=1,
            turnover=100,
            timestamp=1,
            update_time="2026-08-24T11:29:00",
        )
        output = instance.execute(
            UnifiedStandardInput(
                channel_type=TradeChannel.TQ,
                account_config=TQAccountConfig(account_mode="kq", tq_username="user", tq_password="secret"),
                curr_target={"rb2610": 0.1, "ag2612": 0.2},
                algorithm={"method": "SINGLE-MAKER"},
            ),
            cleanup=False,
        )
    finally:
        instance.close()

    assert output.status is ExecutionStatus.BLOCKED
    sized = output.symbol_results["rb2610"]
    missing = output.symbol_results["ag2612"]
    assert sized.status is ExecutionStatus.BLOCKED
    assert sized.sizing is not None
    assert sized.sizing.target_quantity == 1.0
    assert sized.target_volume == 1.0
    assert missing.sizing is not None
    assert missing.sizing.status is TargetSizingStatus.UNAVAILABLE


def test_market_gap_blocks_before_symbol_planning(monkeypatch: pytest.MonkeyPatch) -> None:
    """日夜盘缝走渠道级 BLOCKED，不按品种规划、不读行情。"""
    api = FakeApi()
    monkeypatch.setattr(TQExecutor, "_build_api", staticmethod(lambda _config: api))
    instance = TQExecutor(TQAccountConfig(account_mode="kq", tq_username="user", tq_password="secret"))
    try:
        monkeypatch.setattr(
            "axile.executor.tq.tq_execute.is_within_possible_china_futures_session",
            lambda _now: False,
        )
        monkeypatch.setattr(instance, "get_market_data", lambda _symbols: pytest.fail("不应读取行情"))
        monkeypatch.setattr(
            instance,
            "_check_symbol_trading_time",
            lambda _symbol: pytest.fail("不应按品种判断时段"),
        )
        output = instance.execute(
            UnifiedStandardInput(
                channel_type=TradeChannel.TQ,
                account_config=TQAccountConfig(account_mode="kq", tq_username="user", tq_password="secret"),
                curr_target={"rb2610": 0.1},
                algorithm={"method": "SINGLE-MAKER"},
            ),
            cleanup=False,
        )
    finally:
        instance.close()

    assert output.status is ExecutionStatus.BLOCKED
    assert output.error == "非交易时段"
    assert output.reason_code == "COMMON.SESSION.CLOSED"
    assert output.symbol_results == {}


def test_place_order_rechecks_current_symbol_before_submitting(
    executor: tuple[TQExecutor, FakeApi], monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, api = executor
    monkeypatch.setattr(
        instance,
        "_check_tq_symbol_trading_time",
        lambda _api, _sessions, _now: TQTradingTimeCheck(TQTradingTimeStatus.CLOSED),
    )

    with pytest.raises(AccountControlBlockedError, match="非交易时段") as exc_info:
        instance._place_order_impl("rb2610", OrderDirection.BUY, OrderType.LIMIT, 1, 3200, offset_flag="0")

    assert exc_info.value.reason_code == "CLOSED"

    assert api.insert_calls == 0


def test_place_order_checks_and_submits_in_one_runtime_command(
    executor: tuple[TQExecutor, FakeApi], monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, api = executor
    instance._ensure_instrument_symbols(["rb2610"])
    runtime = instance._require_runtime()
    call = runtime.call
    calls = 0

    def counted_call(operation: object, *, timeout: float | None = None) -> object:
        nonlocal calls
        calls += 1
        return call(operation, timeout=timeout)  # type: ignore[arg-type]

    monkeypatch.setattr(runtime, "call", counted_call)
    monkeypatch.setattr(
        instance,
        "_check_tq_symbol_trading_time",
        lambda _api, _sessions, _now: TQTradingTimeCheck(TQTradingTimeStatus.OPEN),
    )
    instance._place_order_impl("rb2610", OrderDirection.BUY, OrderType.LIMIT, 1, 3200, offset_flag="0")

    assert calls == 1
    assert api.insert_calls == 1


def test_no_night_session_products_are_rejected_at_night(executor: tuple[TQExecutor, FakeApi]) -> None:
    instance, _ = executor

    for symbol in ("v_f2609", "si2609", "IF2609"):
        assert instance._check_symbol_trading_time(symbol, _at("2026-08-21T22:59:59")).status is (
            TQTradingTimeStatus.CLOSED
        )


def test_place_order_rejects_no_night_session_product_at_night(
    executor: tuple[TQExecutor, FakeApi], monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, api = executor
    real_check = instance._check_tq_symbol_trading_time
    monkeypatch.setattr(
        instance,
        "_check_tq_symbol_trading_time",
        lambda api_, sessions, _now: real_check(api_, sessions, _at("2026-08-21T22:59:59")),
    )

    with pytest.raises(AccountControlBlockedError, match="非交易时段"):
        instance._place_order_impl("v_f2609", OrderDirection.BUY, OrderType.LIMIT, 1, 3000, offset_flag="0")

    assert api.insert_calls == 0


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (TQTradingTimeStatus.OPEN, None),
        (TQTradingTimeStatus.CLOSED, "非交易时段"),
        (TQTradingTimeStatus.CALENDAR_UNAVAILABLE, "交易日历不可用"),
        (TQTradingTimeStatus.QUOTE_TRADING_TIME_UNAVAILABLE, "无法获取品种交易时段"),
    ],
)
def test_trading_time_reason_is_separate_from_code(status: TQTradingTimeStatus, reason: str | None) -> None:
    check = TQTradingTimeCheck(status)
    assert check.message == reason
    assert check.error == (None if status is TQTradingTimeStatus.OPEN else status.value)
