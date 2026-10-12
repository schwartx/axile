"""CTP 原生请求字段构造与调用测试。"""

import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from openctp_ctp import thosttraderapi as td

from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.exceptions import AccountControlBlockedError
from axile.executor.account_control.guard import AccountControlGuard
from axile.executor.account_control.models import AccountControlDecision
from axile.executor.account_control.presets import resolve_account_control_policy
from axile.executor.account_control.snapshot import AccountControlCounterSnapshot
from axile.executor.ctp.ctp_execute import (
    CTPExecutor,
    CtpRequestError,
    CtpSessionRecoveryRequired,
    _PendingQuery,
    _Stage,
)
from axile.executor.ctp.requests import (
    build_order_cancel,
    build_order_insert,
    build_query_depth_market_data,
    build_query_settlement_confirm,
    resolve_offset,
)
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_input import CTPAccountConfig, UnifiedStandardInput
from axile.executor.models.unified_order import OrderDirection, OrderType
from tests.unit.executor.ctp._quote_test_support import fresh_quote


def test_position_valuation_subscribes_combination_legs_and_skips_zero(config):
    executor = CTPExecutor.__new__(CTPExecutor)
    executor._lock = threading.RLock()
    executor._timeout = 0.01
    executor._instruments = {"rb2610": object(), "rb2701": object()}
    executor.initialize_websocket = Mock()
    executor._get_ctp_session_block_reason = Mock(return_value=None)
    executor._fresh_valuation_quote_error = Mock(return_value=None)
    executor._valuation_quotes = {symbol: fresh_quote(symbol, "") for symbol in executor._instruments}
    executor._query_valuation_snapshot = Mock()
    rows = [
        SimpleNamespace(InstrumentID="SP rb2610&rb2701", PosiDirection="2", Position=1),
        SimpleNamespace(InstrumentID="rb2610", Position=0),
    ]
    quotes, errors = executor._position_valuation_quotes(rows)
    executor.initialize_websocket.assert_called_once_with(["rb2610", "rb2701"])
    assert set(quotes) == {"rb2610", "rb2701"}
    assert errors == {}
    executor._query_valuation_snapshot.assert_not_called()


@pytest.fixture
def config() -> CTPAccountConfig:
    """返回不连接柜台的最小 CTP 配置。"""
    return CTPAccountConfig(
        broker_id="9999",
        investor_id="100001",
        password="secret",
        td_front="tcp://td:10001",
        md_front="tcp://md:10002",
        app_id="app-id",
        auth_code="auth-code",
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("open", td.THOST_FTDC_OF_Open),
        ("平", td.THOST_FTDC_OF_Close),
        ("close_today", td.THOST_FTDC_OF_CloseToday),
        ("平昨", td.THOST_FTDC_OF_CloseYesterday),
    ],
)
def test_resolve_offset_maps_algorithm_values(value: str, expected: str) -> None:
    assert resolve_offset(value) == expected


def test_resolve_offset_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match="不支持的开平标志"):
        resolve_offset("invalid")


def test_build_order_insert_populates_native_fields(config: CTPAccountConfig) -> None:
    request = build_order_insert(
        config,
        symbol="rb2610",
        order_ref="42",
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        volume=3,
        price=3210.0,
        offset=td.THOST_FTDC_OF_CloseToday,
    )

    assert request.BrokerID == "9999"
    assert request.InstrumentID == "rb2610"
    assert request.Direction == td.THOST_FTDC_D_Buy
    assert request.CombOffsetFlag == td.THOST_FTDC_OF_CloseToday
    assert request.OrderPriceType == td.THOST_FTDC_OPT_LimitPrice
    assert request.VolumeTotalOriginal == 3
    assert request.LimitPrice == 3210.0


def test_build_query_depth_market_data_uses_contract_identity() -> None:
    request = build_query_depth_market_data("rb2610", "SHFE")
    assert request.InstrumentID == "rb2610"
    assert request.ExchangeID == "SHFE"


def test_target_weight_uses_contract_multiplier_and_integer_lots(config) -> None:
    executor = CTPExecutor(TradeChannel.CTP)
    executor.account_config = config
    executor._instruments = {"rb2610": SimpleNamespace(VolumeMultiple=10, PriceTick=1)}
    executor._quotes = {"rb2610": fresh_quote("rb2610", "")}
    assets = UnifiedAccountAssets(available_cash=21_000_000, total_asset=21_000_000, market_value=0, positions=[])

    volume = executor._calculate_generic_volume(0.0001, 3036.0, assets, {}, symbol="rb2610")

    assert volume == 0.0


def test_lots_sizing_mode_preserves_explicit_integer_target() -> None:
    executor = CTPExecutor.__new__(CTPExecutor)
    executor._instruments = {}
    assets = UnifiedAccountAssets(available_cash=1, total_asset=1, market_value=0, positions=[])

    volume = executor._calculate_generic_volume(3, 0, assets, {"sizing_mode": "lots"}, symbol="option")

    assert volume == 3.0


def test_build_order_cancel_prefers_front_session_key(config: CTPAccountConfig) -> None:
    request = build_order_cancel(
        config,
        symbol="rb2610",
        key={
            "order_ref": "42",
            "front_id": 7,
            "session_id": 8,
            "exchange_id": "SHFE",
            "order_sys_id": "100",
        },
    )

    assert request.OrderRef == "42"
    assert request.FrontID == 7
    assert request.SessionID == 8
    assert request.ExchangeID == ""
    assert request.OrderSysID == ""


def test_build_order_cancel_falls_back_to_exchange_key(config: CTPAccountConfig) -> None:
    request = build_order_cancel(
        config,
        symbol="rb2610",
        key={
            "order_ref": "42",
            "front_id": 0,
            "session_id": 0,
            "exchange_id": "SHFE",
            "order_sys_id": "100",
        },
    )

    assert request.ExchangeID == "SHFE"
    assert request.OrderSysID == "100"


def test_trader_login_uses_openctp_6711_signature(config: CTPAccountConfig) -> None:
    executor = CTPExecutor(TradeChannel.CTP)
    executor.account_config = config
    executor._trader_api = Mock()
    executor._trader_api.ReqUserLogin.return_value = 0
    executor._lock = threading.RLock()
    executor._request_id = 0
    executor._auth = _Stage(threading.Event())
    executor._auth.request_id = 42
    executor._login = _Stage(threading.Event())

    executor._authenticated(None, None, 42)

    args = executor._trader_api.ReqUserLogin.call_args.args
    assert args[1:] == (1,)


def test_query_response_copies_reused_swig_frame() -> None:
    executor = CTPExecutor(TradeChannel.CTP)
    pending = _PendingQuery([], threading.Event())
    executor._pending_queries = {7: pending}
    row = td.CThostFtdcInstrumentField()
    row.InstrumentID = "rb2610"

    executor._query_response(row, None, 7, False)
    row.InstrumentID = "ag2612"
    executor._query_response(row, None, 7, True)

    assert [item.InstrumentID for item in pending.rows] == ["rb2610", "ag2612"]


def test_normalizes_czce_four_digit_year_alias_in_all_input_fields(config: CTPAccountConfig) -> None:
    executor = CTPExecutor.__new__(CTPExecutor)
    executor._trading_day = "20260824"
    executor._instruments = {
        "CF701": SimpleNamespace(ExchangeID="CZCE", ProductClass=td.THOST_FTDC_PC_Futures),
        "TA701C5000": SimpleNamespace(ExchangeID="CZCE", ProductClass=td.THOST_FTDC_PC_Options),
        "rb2610": SimpleNamespace(ExchangeID="SHFE", ProductClass=td.THOST_FTDC_PC_Futures),
    }
    standard_input = UnifiedStandardInput(
        channel_type="ctp",
        account_config=config,
        curr_target={"CF2701": 0.2, "TA2701C5000": -0.1, "rb2610": 0.1},
        last_target={"CF2701": 0.1},
        symbol_algorithms={"CF2701": {"method": "SINGLE-MAKER", "params": {}}},
        trade_rules={"CF2701": {"min_notional": 100}},
        forbidden_symbols=["CF2701", "CF701"],
        risk_symbols=["CF2701"],
    )

    normalized = executor._normalize_connected_standard_input(standard_input)

    assert normalized.curr_target == {"CF701": 0.2, "TA701C5000": -0.1, "rb2610": 0.1}
    assert normalized.last_target == {"CF701": 0.1}
    assert list(normalized.symbol_algorithms) == ["CF701"]
    assert normalized.trade_rules == {"CF701": {"min_notional": 100}}
    assert normalized.forbidden_symbols == ["CF701"]
    assert normalized.risk_symbols == ["CF701"]
    assert standard_input.curr_target == {"CF2701": 0.2, "TA2701C5000": -0.1, "rb2610": 0.1}


@pytest.mark.parametrize(
    ("symbol", "instrument", "expected"),
    [
        ("CF2701", None, "CF2701"),
        ("CF2701", SimpleNamespace(ExchangeID="DCE", ProductClass=td.THOST_FTDC_PC_Futures), "CF2701"),
        ("CF2701", SimpleNamespace(ExchangeID="CZCE", ProductClass=td.THOST_FTDC_PC_Options), "CF2701"),
        (
            "CF3701",
            SimpleNamespace(ExchangeID="CZCE", ProductClass=td.THOST_FTDC_PC_Futures),
            "CF3701",
        ),
    ],
)
def test_does_not_misnormalize_unknown_or_non_czce_future(symbol: str, instrument: object, expected: str) -> None:
    executor = CTPExecutor.__new__(CTPExecutor)
    executor._trading_day = "20260824"
    executor._instruments = {} if instrument is None else {"CF701": instrument}

    assert executor._normalize_ctp_symbol(symbol) == expected


def test_rejects_conflicting_czce_alias_values(config: CTPAccountConfig) -> None:
    executor = CTPExecutor.__new__(CTPExecutor)
    executor._trading_day = "20260824"
    executor._instruments = {"CF701": SimpleNamespace(ExchangeID="CZCE", ProductClass=td.THOST_FTDC_PC_Futures)}
    standard_input = UnifiedStandardInput(
        channel_type="ctp",
        account_config=config,
        curr_target={"CF2701": 0.1, "CF701": 0.2},
    )

    with pytest.raises(ValueError, match="配置不一致"):
        executor._normalize_connected_standard_input(standard_input)


def test_market_subscription_encodes_instrument_ids() -> None:
    executor = CTPExecutor(TradeChannel.CTP)
    executor._ready = executor._trader_connected = executor._market_connected = True
    executor._instruments = {"rb2610": object()}
    executor._market_api = Mock()
    executor._market_api.SubscribeMarketData.return_value = 0
    executor._monitoring = False

    executor.initialize_websocket(["rb2610"])

    executor._market_api.SubscribeMarketData.assert_called_once_with([b"rb2610"], 1)
    assert executor._monitoring is True


def test_query_settlement_confirm_populates_account_key(config: CTPAccountConfig) -> None:
    request = build_query_settlement_confirm(config)

    assert request.BrokerID == config.broker_id
    assert request.InvestorID == config.investor_id


def _settlement_executor(config: CTPAccountConfig) -> CTPExecutor:
    executor = CTPExecutor(TradeChannel.CTP)
    executor.account_config = config
    executor._trading_day = "20260824"
    executor._trader_api = Mock()
    executor._timeout = 0.1
    executor._lock = threading.RLock()
    executor._request_id = 0
    executor._settlement = _Stage(threading.Event())
    return executor


def test_settlement_query_skips_confirmation_for_current_trading_day(config: CTPAccountConfig) -> None:
    executor = _settlement_executor(config)
    executor._query = Mock(return_value=[SimpleNamespace(ConfirmDate="20260824")])

    executor._ensure_settlement_confirmed()

    executor._trader_api.ReqSettlementInfoConfirm.assert_not_called()


def test_settlement_query_confirms_when_current_day_is_missing(config: CTPAccountConfig) -> None:
    executor = _settlement_executor(config)
    executor._query = Mock(return_value=[SimpleNamespace(ConfirmDate="20260821")])

    def confirm(_request: object, _request_id: int) -> int:
        executor._settled(None, _request_id)
        return 0

    executor._trader_api.ReqSettlementInfoConfirm.side_effect = confirm

    executor._ensure_settlement_confirmed()

    executor._trader_api.ReqSettlementInfoConfirm.assert_called_once()


_SHANGHAI = ZoneInfo("Asia/Shanghai")


class _CalendarStub:
    def __init__(self, *, is_open: bool = True) -> None:
        self._is_open = is_open

    def is_open(self, _calendar_id: str, _day: object) -> bool:
        return self._is_open


def _submit_point_executor(config: CTPAccountConfig) -> CTPExecutor:
    executor = CTPExecutor(TradeChannel.CTP)
    executor._ready = executor._trader_connected = executor._market_connected = True
    executor._quotes = {symbol: fresh_quote(symbol, "20260824") for symbol in ("ag2612", "bu2612")}
    executor._subscription_acks = {"ag2612", "bu2612"}
    executor.account_config = config
    executor.channel_type = TradeChannel.CTP
    executor._trading_calendar = _CalendarStub()
    executor._channel_calendar_id = "china"
    executor._instruments = {
        "ag2612": SimpleNamespace(
            ExchangeID="SHFE", ProductID="ag", ProductClass=td.THOST_FTDC_PC_Futures, PriceTick=1.0
        ),
        "bu2612": SimpleNamespace(
            ExchangeID="SHFE", ProductID="bu", ProductClass=td.THOST_FTDC_PC_Futures, PriceTick=1.0
        ),
    }
    executor._trader_api = Mock()
    executor._trader_api.ReqOrderInsert.return_value = 0
    executor._order_keys = {}
    executor._order_ref = 1
    executor._request_id = 0
    executor._lock = threading.RLock()
    executor._trading_day = "20260824"
    executor._front_id = 0
    executor._session_id = 0
    return executor


def _at_clock(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    monkeypatch.setattr("axile.executor.ctp.ctp_execute.clock_now", lambda **_kwargs: moment)


def test_place_order_blocks_at_submit_during_lunch_break(
    config: CTPAccountConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _submit_point_executor(config)
    _at_clock(monkeypatch, datetime(2026, 8, 25, 12, 0, tzinfo=_SHANGHAI))

    with pytest.raises(AccountControlBlockedError, match="CTP.SESSION.CLOSED"):
        executor._place_order_impl("ag2612", OrderDirection.BUY, OrderType.LIMIT, 1, 9000, offset_flag="open")

    executor._trader_api.ReqOrderInsert.assert_not_called()
    assert executor._order_keys == {}


def test_place_order_blocks_at_submit_after_session_end(
    config: CTPAccountConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _submit_point_executor(config)
    _at_clock(monkeypatch, datetime(2026, 8, 25, 20, 0, tzinfo=_SHANGHAI))

    with pytest.raises(AccountControlBlockedError, match="CTP.SESSION.CLOSED"):
        executor._place_order_impl("ag2612", OrderDirection.BUY, OrderType.LIMIT, 1, 9000, offset_flag="open")

    executor._trader_api.ReqOrderInsert.assert_not_called()
    assert executor._order_keys == {}


def test_place_order_blocks_when_calendar_day_is_closed(
    config: CTPAccountConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _submit_point_executor(config)
    executor._trading_calendar = _CalendarStub(is_open=False)
    _at_clock(monkeypatch, datetime(2026, 8, 22, 21, 29, tzinfo=_SHANGHAI))

    with pytest.raises(AccountControlBlockedError, match="CTP.SESSION.CLOSED"):
        executor._place_order_impl("ag2612", OrderDirection.BUY, OrderType.LIMIT, 1, 9000, offset_flag="open")

    executor._trader_api.ReqOrderInsert.assert_not_called()


def test_place_order_recheck_blocks_after_planning_crosses_session_boundary(
    config: CTPAccountConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _submit_point_executor(config)
    _at_clock(monkeypatch, datetime(2026, 8, 24, 21, 29, tzinfo=_SHANGHAI))
    assert executor._get_ctp_session_block_reason("bu2612") is None

    _at_clock(monkeypatch, datetime(2026, 8, 24, 23, 0, tzinfo=_SHANGHAI))
    with pytest.raises(AccountControlBlockedError, match="CTP.SESSION.CLOSED"):
        executor._place_order_impl("bu2612", OrderDirection.BUY, OrderType.LIMIT, 1, 3600, offset_flag="open")

    executor._trader_api.ReqOrderInsert.assert_not_called()
    assert executor._order_keys == {}


def test_place_order_submits_when_session_open(config: CTPAccountConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    executor = _submit_point_executor(config)
    _at_clock(monkeypatch, datetime(2026, 8, 24, 21, 29, tzinfo=_SHANGHAI))

    executor._place_order_impl("ag2612", OrderDirection.BUY, OrderType.LIMIT, 1, 9000, offset_flag="open")

    executor._trader_api.ReqOrderInsert.assert_called_once()
    assert len(executor._order_keys) == 1


def test_public_place_order_controls_ctp_submit_once(
    config: CTPAccountConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CTP 报单复用公开下单控制，不再产生 insert_order 二次事件。"""
    executor = _submit_point_executor(config)
    guard = AccountControlGuard(
        account_id=1,
        execution_id="exec-ctp-order",
        channel=TradeChannel.CTP,
        policy=resolve_account_control_policy("ctp"),
        baseline=AccountControlCounterSnapshot(),
        clock=lambda: datetime(2026, 8, 24, 21, 29, tzinfo=_SHANGHAI),
    )
    executor.set_account_control_guard(guard)
    executor._send_trader_request = Mock(side_effect=AssertionError("不应进入 insert_order guard"))
    _at_clock(monkeypatch, datetime(2026, 8, 24, 21, 29, tzinfo=_SHANGHAI))

    order = executor.place_order("ag2612", OrderDirection.BUY, OrderType.LIMIT, 1, 9000, offset_flag="open")

    assert order.symbol == "ag2612"
    executor._trader_api.ReqOrderInsert.assert_called_once()
    executor._send_trader_request.assert_not_called()
    _, events = guard.flush_records()
    assert [event.operation for event in events] == ["place_order"]
    assert events[0].decision == AccountControlDecision.ALLOWED
    assert events[0].metadata["groups"] == ["ctp_td_global"]


def test_place_order_does_not_retry_rate_limit(config: CTPAccountConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    executor = _submit_point_executor(config)
    executor._trader_api.ReqOrderInsert.return_value = -3
    executor.logger = Mock()
    _at_clock(monkeypatch, datetime(2026, 8, 24, 21, 29, tzinfo=_SHANGHAI))
    with pytest.raises(CtpRequestError, match="返回码=-3"):
        executor._place_order_impl("ag2612", OrderDirection.BUY, OrderType.LIMIT, 1, 9000, offset_flag="open")

    executor._trader_api.ReqOrderInsert.assert_called_once()
    assert executor._order_keys == {}


@pytest.mark.parametrize(
    ("code", "error_type", "meaning"),
    [
        (-1, CtpRequestError, "网络连接失败"),
        (-2, CtpSessionRecoveryRequired, "前置未处理请求队列超限"),
        (-3, CtpRequestError, "每秒发送请求数超过柜台许可"),
        (-9, CtpRequestError, "柜台返回未知同步拒绝"),
    ],
)
def test_query_sync_rejection_is_not_retried(code: int, error_type: type[Exception], meaning: str) -> None:
    executor = CTPExecutor(TradeChannel.CTP)
    executor._trader_api = Mock()
    executor._trader_api.ReqQryTradingAccount.return_value = code
    executor._lock = threading.RLock()
    executor._query_lock = threading.Lock()
    executor._request_id = 0
    executor._pending_queries = {}
    with pytest.raises(error_type, match=meaning) as error:
        executor._query("ReqQryTradingAccount", object())

    assert getattr(error.value, "return_code") == code
    executor._trader_api.ReqQryTradingAccount.assert_called_once()
    assert executor._request_id == 1
    assert executor._pending_queries == {}


def test_cancel_with_missing_key_propagates_single_query_queue_rejection(config: CTPAccountConfig) -> None:
    executor = CTPExecutor(TradeChannel.CTP)
    executor.account_config = config
    executor._trader_api = Mock()
    executor._trader_api.ReqQryOrder.return_value = -2
    executor._lock = threading.RLock()
    executor._query_lock = threading.Lock()
    executor._request_id = 0
    executor._pending_queries = {}
    executor._order_keys = {}
    executor._trading_day = "20260824"
    executor._front_id = 1
    executor._session_id = 2
    with pytest.raises(CtpSessionRecoveryRequired, match="返回码=-2"):
        executor._cancel_order_impl("rb2610", "missing-order")

    request_ids = [call.args[1] for call in executor._trader_api.ReqQryOrder.call_args_list]
    assert request_ids == [1]
    assert executor._pending_queries == {}
    executor._trader_api.ReqOrderAction.assert_not_called()
