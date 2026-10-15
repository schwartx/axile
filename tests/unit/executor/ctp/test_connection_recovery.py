"""通过实际 SPI 和执行器回放连接边界，不连接柜台、不发送真实订单。"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from openctp_ctp import thostmduserapi as md
from openctp_ctp import thosttraderapi as td

from axile.common.trade_channel import TradeChannel
from axile.executor.ctp.ctp_execute import CTPExecutor, CtpRequestError, CtpSessionRecoveryRequired
from axile.executor.instrument_catalog import InstrumentCatalog, LocalInstrumentCatalogProvider
from axile.executor.models.unified_input import CTPAccountConfig
from axile.executor.models.unified_order import OrderDirection, OrderType
from axile.server.execution.worker_backend import worker_state
from axile.server.execution.worker_backend.manager import WorkerBackendManager, WorkerBackendTimeoutError
from tests.unit.server._execution_test_support import build_account


@pytest.mark.parametrize(
    "code, expected", [(31, "资金不足"), (3, "CTP 登录校验失败"), (4097, "报单失败，渠道返回错误码 4097")]
)
def test_ctp_result_reason_uses_exact_error_id(code, expected):
    error = CTPExecutor._error(
        SimpleNamespace(ErrorID=code, ErrorMsg="原文: invalid token margin insufficient"), "报单"
    )
    assert error.execution_error == expected
    assert "原文: invalid token margin insufficient" in str(error)


def test_ctp_unknown_error_text_does_not_classify():
    error = CTPExecutor._error(SimpleNamespace(ErrorID=999, ErrorMsg="4097 资金不足 token invalid"), "查询")
    assert error.execution_error == "查询失败，渠道返回错误码 999"


class ScriptedBroker:
    """按 OpenCTP 请求/回调签名回放启动流程，并允许在任一请求注入故障。"""

    def __init__(self, monkeypatch, *, day="20260909", session=2):
        self.executor = CTPExecutor(TradeChannel.CTP)
        self.executor.set_instrument_catalog_provider(LocalInstrumentCatalogProvider(InstrumentCatalog()))
        self.executor.account_config = CTPAccountConfig(
            broker_id="9999",
            investor_id="100001",
            password="test",
            td_front="tcp://unused:1",
            md_front="tcp://unused:2",
            app_id="test",
            auth_code="test",
        )
        self.day = day
        self.session = session
        self.trader = Mock()
        self.market = Mock()
        self.events = []
        self.errors = {}
        self.hooks = {}
        self.order = SimpleNamespace(
            TradingDay=day,
            FrontID=7,
            SessionID=1,
            OrderRef="10",
            InstrumentID="ag2612",
            ExchangeID="SHFE",
            OrderSysID="100",
            VolumeTotalOriginal=1,
            VolumeTraded=1,
            OrderStatus=td.THOST_FTDC_OST_AllTraded,
            Direction=td.THOST_FTDC_D_Buy,
        )
        self.trade = SimpleNamespace(
            TradingDay=day,
            OrderRef="10",
            InstrumentID="ag2612",
            ExchangeID="SHFE",
            OrderSysID="100",
            TradeID="T1",
            Volume=1,
            Price=9000,
        )
        self.rows = {
            "ReqAuthenticate": [None],
            "ReqUserLogin": [SimpleNamespace(TradingDay=day, FrontID=7, SessionID=session, MaxOrderRef="10")],
            "ReqQrySettlementInfoConfirm": [],
            "ReqSettlementInfoConfirm": [None],
            "ReqQryInstrument": [
                SimpleNamespace(
                    InstrumentID="ag2612",
                    ExchangeID="SHFE",
                    ProductID="ag",
                    PriceTick=1,
                    VolumeMultiple=15,
                    ProductClass=td.THOST_FTDC_PC_Futures,
                )
            ],
            "ReqQryOrder": [self.order],
            "ReqQryTrade": [self.trade],
            "ReqQryInvestorPosition": [],
            "ReqQryTradingAccount": [SimpleNamespace(TradingDay=day, Balance=100000, Available=90000)],
        }
        monkeypatch.setattr(td.CThostFtdcTraderApi, "CreateFtdcTraderApi", lambda _path: self.trader)
        monkeypatch.setattr(md.CThostFtdcMdApi, "CreateFtdcMdApi", lambda _path: self.market)
        self.trader.Init.side_effect = lambda: self.trader_spi.OnFrontConnected()
        self.market.Init.side_effect = lambda: self.market_spi.OnFrontConnected()
        for name in self.rows:
            getattr(self.trader, name).side_effect = self._response(name)
        self.trader.ReqOrderInsert.return_value = 0
        self.market.ReqUserLogin.side_effect = self._market_login
        self.market.SubscribeMarketData.side_effect = self._subscribe

    @property
    def trader_spi(self):
        return self.trader.RegisterSpi.call_args_list[0].args[0]

    @property
    def market_spi(self):
        return self.market.RegisterSpi.call_args_list[0].args[0]

    def _response(self, name):
        def respond(_request, rid):
            self.events.append(name)
            if name != "ReqQryInstrument":
                assert not self.executor._verify_connection()
            if name in self.hooks:
                self.hooks[name]()
            callback = getattr(self.trader_spi, name.replace("Req", "OnRsp", 1))
            rows = self.rows[name] or [None]
            for index, row in enumerate(rows):
                callback(row, self.errors.get(name), rid, index == len(rows) - 1)
            return 0

        return respond

    def _market_login(self, _request, rid):
        self.events.append("MarketLogin")
        assert not self.executor._verify_connection()
        self.market_spi.OnRspUserLogin(SimpleNamespace(TradingDay=self.day), None, rid, True)
        return 0

    def start(self):
        self.executor._initialize_connection(self.executor.account_config)
        return self.executor

    def _subscribe(self, symbols, _count):
        for symbol in symbols:
            self.market_spi.OnRspSubMarketData(SimpleNamespace(InstrumentID=symbol.decode()), None, 0, True)
        return 0

    def quote(self, *, day=None):
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        self.market_spi.OnRtnDepthMarketData(
            SimpleNamespace(
                InstrumentID="ag2612",
                TradingDay=day or self.day,
                ActionDay=now.strftime("%Y%m%d"),
                UpdateTime=now.strftime("%H:%M:%S"),
                UpdateMillisec=now.microsecond // 1000,
                BidVolume1=1,
                AskVolume1=1,
                LowerLimitPrice=8000,
                UpperLimitPrice=10000,
                LastPrice=9000,
                BidPrice1=8999,
                AskPrice1=9001,
            )
        )


@pytest.fixture
def broker(monkeypatch):
    driver = ScriptedBroker(monkeypatch)
    yield driver
    driver.executor.close()


def test_startup_waits_for_all_stages_and_recovers_old_session_trade(broker):
    executor = broker.start()

    assert broker.events == [
        "ReqAuthenticate",
        "ReqUserLogin",
        "ReqQrySettlementInfoConfirm",
        "ReqSettlementInfoConfirm",
        "MarketLogin",
        "ReqQryOrder",
        "ReqQryTrade",
        "ReqQryInvestorPosition",
        "ReqQryTradingAccount",
    ]
    assert executor._verify_connection()
    assert executor._recovery_snapshot["trades"][0].order_id == "20260909:7:1:10"
    assert executor._session_id == 2
    assert executor._recovery_snapshot["assets"].total_asset == 100000
    assert executor._order_keys["20260909:7:1:10"]["session_id"] == 1


def test_transport_connected_before_authentication_is_not_ready(broker):
    broker.trader.ReqAuthenticate.side_effect = None
    broker.trader.ReqAuthenticate.return_value = 0
    executor = broker.executor
    executor._trader_api = broker.trader
    executor._market_connected = True

    executor._trader_connected_cb()

    assert not executor._auth.done.is_set()
    assert not executor._login.done.is_set()
    assert not executor._verify_connection()
    with pytest.raises(CtpRequestError, match="尚未完成"):
        executor._place_order_impl("ag2612", OrderDirection.BUY, OrderType.LIMIT, 1, 9000, offset_flag="open")
    broker.trader.ReqOrderInsert.assert_not_called()


@pytest.mark.parametrize("stage", ["ReqAuthenticate", "ReqUserLogin", "ReqSettlementInfoConfirm"])
def test_stage_failure_closes_instance_and_ignores_late_success(broker, stage):
    broker.errors[stage] = SimpleNamespace(ErrorID=3, ErrorMsg="denied")

    with pytest.raises(CtpSessionRecoveryRequired, match="denied"):
        broker.start()

    broker.trader_spi.OnRspUserLogin(broker.rows["ReqUserLogin"][0], None, 2, True)
    broker.trader_spi.OnFrontConnected()
    assert not broker.executor._verify_connection()
    assert broker.executor._closed
    broker.trader.Release.assert_called_once()


@pytest.mark.parametrize("kind", ["交易", "行情"])
def test_disconnect_is_permanent_even_after_reconnect_and_late_callbacks(broker, kind):
    executor = broker.start()
    executor.initialize_websocket(["ag2612"])
    broker.quote()
    old_session = executor._session_id
    executor._disconnected(kind, 4097)

    broker.trader_spi.OnFrontConnected()
    broker.market_spi.OnFrontConnected()
    broker.trader_spi.OnRspUserLogin(
        SimpleNamespace(TradingDay="20260910", FrontID=7, SessionID=99, MaxOrderRef="0"),
        None,
        executor._login.request_id,
        True,
    )
    broker.quote()

    assert not executor._verify_connection()
    assert executor._quotes == {}
    assert executor._session_id == old_session
    assert not executor.is_monitoring()
    assert broker.trader.ReqAuthenticate.call_count == 1
    assert broker.market.ReqUserLogin.call_count == 1
    with pytest.raises(CtpSessionRecoveryRequired, match="前置断线"):
        executor._call_trader_request(
            "ReqOrderInsert",
            SimpleNamespace(InstrumentID="ag2612", LimitPrice=9000, OrderPriceType=td.THOST_FTDC_OPT_LimitPrice),
        )
    broker.trader.ReqOrderInsert.assert_not_called()


def test_wrong_and_duplicate_stage_responses_cannot_replace_session(broker):
    executor = broker.start()
    row = SimpleNamespace(TradingDay="20260910", FrontID=99, SessionID=99, MaxOrderRef="0")
    broker.trader_spi.OnRspUserLogin(row, None, 9999, True)
    broker.trader_spi.OnRspUserLogin(row, None, executor._login.request_id, True)
    broker.trader_spi.OnRspAuthenticate(None, SimpleNamespace(ErrorID=3), executor._auth.request_id, True)

    assert executor._verify_connection()
    assert executor._trading_day == "20260909"
    assert executor._session_id == 2
    assert broker.trader.ReqUserLogin.call_count == 1


def test_stage_timeout_is_sticky_and_late_login_does_not_restore_readiness(broker, monkeypatch):
    broker.trader.ReqUserLogin.side_effect = None
    broker.trader.ReqUserLogin.return_value = 0
    monkeypatch.setattr(broker.executor._login.done, "wait", lambda _timeout: False)
    with pytest.raises(TimeoutError, match="登录超时"):
        broker.start()

    broker.trader_spi.OnRspUserLogin(broker.rows["ReqUserLogin"][0], None, 2, True)
    assert not broker.executor._verify_connection()
    assert broker.executor._trading_day == ""


@pytest.mark.parametrize("name", ["ReqQryTradingAccount"])
def test_missing_required_snapshot_never_becomes_ready(broker, name):
    broker.rows[name] = []
    with pytest.raises(CtpRequestError, match="空结果"):
        broker.start()
    assert not broker.executor._verify_connection()


def test_unmatched_recovery_trade_blocks_startup(broker):
    broker.rows["ReqQryOrder"] = []
    with pytest.raises(CtpRequestError, match="订单关联"):
        broker.start()
    assert not broker.executor._verify_connection()


def test_disconnect_during_last_query_cannot_publish_ready(broker):
    broker.hooks["ReqQryTradingAccount"] = lambda: broker.executor._disconnected("交易", 1)
    with pytest.raises(CtpSessionRecoveryRequired, match="前置断线"):
        broker.start()
    assert not broker.executor._verify_connection()


def test_new_instance_needs_explicit_subscription_and_new_quote_before_submit(broker):
    executor = broker.start()
    with pytest.raises(CtpRequestError, match="新行情"):
        executor._call_trader_request(
            "ReqOrderInsert",
            SimpleNamespace(InstrumentID="ag2612", LimitPrice=9000, OrderPriceType=td.THOST_FTDC_OPT_LimitPrice),
        )
    broker.quote()  # 未订阅的数据不能作为恢复证据。
    assert executor._quotes == {}
    executor.initialize_websocket(["ag2612"])
    broker.quote()
    executor._call_trader_request(
        "ReqOrderInsert",
        SimpleNamespace(InstrumentID="ag2612", LimitPrice=9000, OrderPriceType=td.THOST_FTDC_OPT_LimitPrice),
    )

    broker.market.SubscribeMarketData.assert_called_once_with([b"ag2612"], 1)
    broker.trader.ReqOrderInsert.assert_called_once()
    assert executor.get_market_data(["ag2612"])["ag2612"].last_price == 9000


def test_subscription_rejection_does_not_return_cached_quote(broker):
    executor = broker.start()
    executor.initialize_websocket(["ag2612"])
    broker.quote()
    broker.market_spi.OnRspSubMarketData(
        SimpleNamespace(InstrumentID="ag2612"),
        SimpleNamespace(ErrorID=3, ErrorMsg="denied"),
        0,
        True,
    )
    with pytest.raises(CtpRequestError, match="订阅失败"):
        executor.get_market_data(["ag2612"])
    broker.trader.ReqOrderInsert.assert_not_called()


def test_trading_day_change_in_quote_invalidates_entire_instance(broker):
    executor = broker.start()
    executor.initialize_websocket(["ag2612"])
    broker.quote(day="20260910")

    assert not executor._verify_connection()
    assert executor._quotes == {}


def test_quote_without_subscription_ack_cannot_allow_native_submit(broker):
    executor = broker.start()
    broker.market.SubscribeMarketData.side_effect = None
    broker.market.SubscribeMarketData.return_value = 0
    executor.initialize_websocket(["ag2612"])
    broker.quote()

    with pytest.raises(CtpRequestError, match="新行情"):
        executor._call_trader_request(
            "ReqOrderInsert",
            SimpleNamespace(InstrumentID="ag2612", LimitPrice=9000, OrderPriceType=td.THOST_FTDC_OPT_LimitPrice),
        )
    broker.trader.ReqOrderInsert.assert_not_called()


def test_disconnect_between_order_validation_and_native_send_blocks_request(broker, monkeypatch):
    executor = broker.start()
    executor.initialize_websocket(["ag2612"])
    broker.quote()
    monkeypatch.setattr(executor, "_get_ctp_session_block_reason", lambda _symbol: None)

    def disconnect_before_ref():
        executor._disconnected("交易", 1)
        return "11"

    monkeypatch.setattr(executor, "_new_ref", disconnect_before_ref)
    with pytest.raises(CtpSessionRecoveryRequired, match="前置断线"):
        executor._place_order_impl("ag2612", OrderDirection.BUY, OrderType.LIMIT, 1, 9000, offset_flag="open")

    broker.trader.ReqOrderInsert.assert_not_called()
    assert "20260909:7:2:11" not in executor._order_keys


def test_query_failure_is_not_overwritten_by_late_success(broker):
    def respond(_request, rid):
        broker.trader_spi.OnRspQryOrder(None, SimpleNamespace(ErrorID=3, ErrorMsg="denied"), rid, False)
        broker.trader_spi.OnRspQryOrder(broker.order, None, rid, True)
        return 0

    broker.trader.ReqQryOrder.side_effect = respond
    with pytest.raises(CtpRequestError, match="denied"):
        broker.start()
    assert not broker.executor._verify_connection()


def test_trade_callback_before_order_snapshot_is_resolved_before_ready(broker):
    broker.hooks["ReqQryOrder"] = lambda: broker.trader_spi.OnRtnTrade(broker.trade)
    executor = broker.start()

    assert executor._verify_connection()
    assert executor._startup_trades == []
    assert executor._recovery_snapshot["trades"][0].order_id == "20260909:7:1:10"


def test_recovery_rejects_snapshot_from_different_trading_day(broker):
    broker.rows["ReqQryTradingAccount"][0].TradingDay = "20260908"
    with pytest.raises(CtpRequestError, match="交易日不一致"):
        broker.start()
    assert not broker.executor._verify_connection()


def test_market_login_day_mismatch_blocks_startup(broker):
    broker.day = "20260910"
    with pytest.raises(CtpSessionRecoveryRequired, match="行情登录交易日"):
        broker.start()
    assert not broker.executor._verify_connection()


def test_quote_timeout_can_recover_with_fresh_quote_without_disconnect(broker):
    executor = broker.start()
    executor._ensure_instrument_symbols(["ag2612"])
    executor._timeout = 0
    with pytest.raises(TimeoutError, match="行情等待超时"):
        executor.get_market_data(["ag2612"])
    broker.quote()

    assert executor._verify_connection()
    executor._timeout = 0.1
    assert executor.get_market_data(["ag2612"])["ag2612"].last_price == 9000


@pytest.mark.parametrize(("day", "session"), [("20260909", 3), ("20260910", 4)])
def test_worker_rebuilds_disconnected_ctp_with_fresh_stages_and_metadata(broker, monkeypatch, day, session):
    old = broker.start()
    account = build_account(trade_channel=TradeChannel.CTP)
    state = worker_state._WorkerBackendState(
        executor=old,
        account_id=account.id,
        config_signature=worker_state._config_signature(account),
    )
    old._disconnected("交易", 1)
    replacement = ScriptedBroker(monkeypatch, day=day, session=session)
    factory = Mock(side_effect=lambda _account: replacement.start())
    monkeypatch.setattr(worker_state, "create_executor_instance", factory)
    try:
        current = worker_state._resolve_executor(state, account, expected_trading_day=day)
        assert current is replacement.executor
        assert current._verify_connection()
        assert current._trading_day == day
        assert current._session_id == session
        assert current._instruments is not old._instruments
        assert current._quotes == {}
        assert old._closed
        assert worker_state._resolve_executor(state, account, expected_trading_day=day) is current
        factory.assert_called_once()
    finally:
        replacement.executor.close()


def test_two_accounts_share_catalog_but_keep_independent_login_and_recovery(monkeypatch):
    from axile.executor.instrument_catalog import InstrumentCatalog, LocalInstrumentCatalogProvider

    catalog = InstrumentCatalog()
    first = ScriptedBroker(monkeypatch)
    first.executor.set_instrument_catalog_provider(LocalInstrumentCatalogProvider(catalog))
    try:
        first.start()
        second = ScriptedBroker(monkeypatch, session=3)
        second.executor.account_config.investor_id = "100002"
        second.executor.set_instrument_catalog_provider(LocalInstrumentCatalogProvider(catalog))
        try:
            second.start()
            first.executor.get_tick_size("ag2612")
            assert first.events.count("ReqQryInstrument") == 1
            assert "ReqQryInstrument" not in second.events
            assert "ReqAuthenticate" in second.events
            assert "ReqUserLogin" in second.events
            assert "ReqQryTradingAccount" in second.events
            assert second.executor._verify_connection()
            assert second.executor.get_tick_size("ag2612") == 1
            assert second.executor._instruments is not first.executor._instruments
        finally:
            second.executor.close()
        next_day = ScriptedBroker(monkeypatch, day="20260910")
        next_day.executor.set_instrument_catalog_provider(LocalInstrumentCatalogProvider(catalog))
        try:
            next_day.start()
            assert next_day.events.count("ReqQryInstrument") == 0
        finally:
            next_day.executor.close()
    finally:
        first.executor.close()


def test_reconciliation_progress_keeps_watchdog_alive_over_sixty_seconds(broker, monkeypatch):
    now = [100.0]
    updated = [100.0]
    phases = []
    monkeypatch.setattr("axile.executor.ctp.ctp_execute.time.monotonic", lambda: now[0])
    provider = LocalInstrumentCatalogProvider(InstrumentCatalog())

    def progress(phase):
        phases.append(phase)
        updated[0] = now[0]

    monkeypatch.setattr(provider, "progress", progress)
    broker.executor.set_instrument_catalog_provider(provider)
    handle = SimpleNamespace(catalog=SimpleNamespace(snapshot=lambda _: (True, updated[0], 100.0, 0.0)))
    request = SimpleNamespace(request_id="test")

    def advance():
        # 每一阶段有处理进展；总时间超过旧的 60 秒截止时间。
        now[0] += 20
        assert WorkerBackendManager._remaining_response_time(handle, request, 160, now[0]) > 0

    names = ("ReqQryOrder", "ReqQryTrade", "ReqQryInvestorPosition", "ReqQryTradingAccount")
    for name in names:
        broker.hooks[name] = advance
    broker.start()
    assert now[0] == 180
    assert all(f"{name}完成" in phases for name in names)
    now[0] += 60
    with pytest.raises(WorkerBackendTimeoutError, match="无进展"):
        WorkerBackendManager._remaining_response_time(handle, request, 160, now[0])


@pytest.mark.parametrize(
    "code, stage, reason_code",
    [
        (7, "ReqAuthenticate", "CTP_NOT_INITIALIZED"),
        (3, "ReqAuthenticate", None),
        (999, "ReqAuthenticate", None),
        (7, "ReqUserLogin", None),
    ],
)
def test_stage_error_contract_survives_invalidation_and_ipc(broker, monkeypatch, code, stage, reason_code):
    import asyncio
    import pickle

    from axile.server.execution.worker_backend import worker
    from axile.server.execution.worker_backend.manager import WorkerBackendExecutionError
    from axile.server.execution.worker_backend.protocol import WorkerBackendRequest

    broker.errors[stage] = SimpleNamespace(ErrorID=code, ErrorMsg="SDK technical original")
    with pytest.raises(CtpSessionRecoveryRequired) as caught:
        broker.start()
    error = caught.value
    assert error.error_id == code
    assert error.reason_code == reason_code
    assert error.execution_error
    assert "SDK" not in error.execution_error
    if reason_code:
        assert error.execution_error == "交易柜台尚未初始化，账户通道暂未就绪"
    broker.executor._disconnected("交易", 123)
    with pytest.raises(CtpSessionRecoveryRequired) as repeated:
        broker.executor._require_active_connection()
    assert repeated.value.execution_error == error.execution_error
    assert repeated.value.reason_code == reason_code
    assert not broker.executor._verify_connection()
    broker.trader.ReqOrderInsert.assert_not_called()

    def fail_prepare(**kwargs):
        raise error

    monkeypatch.setattr(worker, "_resolve_prepared_executor", fail_prepare)
    request = WorkerBackendRequest(
        request_id="prepare",
        command="prepare",
        account_payload=build_account(id=1).model_dump(mode="json"),
        execution_id=None,
        payload={},
    )
    response = pickle.loads(pickle.dumps(worker._handle_prepare(request, worker._WorkerBackendState())))
    manager = WorkerBackendManager()
    monkeypatch.setattr(manager, "_request_blocking", lambda *args: response)
    with pytest.raises(WorkerBackendExecutionError) as result:
        asyncio.run(manager.prepare_account(build_account(id=1)))
    assert result.value.reason_code == reason_code
    assert result.value.error_id == code
    assert result.value.execution_error == error.execution_error
    assert result.value.retryable


def test_worker_retries_authentication_with_a_new_executor_after_not_initialized(broker, monkeypatch):
    account = build_account(id=1)
    state = worker_state._WorkerBackendState()
    broker.errors["ReqAuthenticate"] = SimpleNamespace(ErrorID=7, ErrorMsg="not initialized")
    monkeypatch.setattr(worker_state, "create_executor_instance", lambda account: broker.start())
    with pytest.raises(CtpSessionRecoveryRequired):
        worker_state._resolve_executor(state, account)
    assert state.executor is None
    assert broker.executor._closed

    replacement = ScriptedBroker(monkeypatch)
    monkeypatch.setattr(worker_state, "create_executor_instance", lambda account: replacement.start())
    try:
        current = worker_state._resolve_executor(state, account)
        assert current is replacement.executor
        assert current._verify_connection()
        assert worker_state._resolve_executor(state, account) is current
    finally:
        replacement.executor.close()


@pytest.mark.parametrize(
    "operation,is_connection",
    [
        ("ReqAuthenticate", True),
        ("ReqUserLogin", True),
        ("ReqSettlementInfoConfirm", False),
        ("ReqQryTradingAccount", False),
    ],
)
def test_connection_status_does_not_absorb_business_startup_failures(monkeypatch, operation, is_connection):
    from axile.executor.connection_state import connection_monitor

    broker = ScriptedBroker(monkeypatch)
    observations = []
    connection_monitor(broker.executor).bind(observations.append)
    broker.errors[operation] = SimpleNamespace(ErrorID=3, ErrorMsg="柜台原文")
    with pytest.raises(CtpRequestError):
        broker.start()
    assert any(item.status == "error" for item in observations) is is_connection


def test_connection_status_reports_login_disconnect_and_ignores_active_close(monkeypatch):
    from axile.executor.connection_state import connection_monitor

    broker = ScriptedBroker(monkeypatch)
    observations = []
    connection_monitor(broker.executor).bind(observations.append)
    executor = broker.start()
    assert observations[-1].status == "ok"
    broker.trader_spi.OnFrontDisconnected(4097)
    assert observations[-1].status == "error"
    executor.close()
    count = len(observations)
    broker.market_spi.OnFrontDisconnected(4097)
    assert len(observations) == count


def test_native_account_query_recovers_without_new_login_callback(broker):
    from axile.executor.connection_state import connection_monitor

    executor = broker.start()
    observations = []
    monitor = connection_monitor(executor)
    monitor.bind(observations.append)
    monitor.failed(RuntimeError("旧连接故障"))

    def respond(_request, rid):
        broker.trader_spi.OnRspQryTradingAccount(SimpleNamespace(Balance=100), None, rid, True)
        return 0

    # 原 fixture 专门断言启动期尚未 ready；这里模拟已就绪后的柜台响应。
    broker.trader.ReqQryTradingAccount.side_effect = respond
    # 原生账户请求确实完成；后续统一模型转换不是连通性条件。
    executor._query("ReqQryTradingAccount", SimpleNamespace())
    assert observations[-1].status == "ok"
    assert observations[-1].error is None


@pytest.mark.parametrize("kind", ["交易", "行情"])
def test_disconnect_presentation_preserves_native_reason_through_invalidation(broker, kind):
    from axile.executor.connection_state import connection_monitor

    observations = []
    connection_monitor(broker.executor).bind(observations.append)
    executor = broker.start()
    executor._disconnected(kind, 4097)
    observation = observations[-1]
    assert observation.label == f"{kind}前置断开"
    assert "4097" in str(observation.presentation)
    assert "已失效" in str(observation.presentation)
    assert observation.error["reason_code"] == "4097"
    assert "_disconnected" in observation.error["reporting_stack"]
    with pytest.raises(CtpSessionRecoveryRequired) as raised:
        executor._require_active_connection()
    connection_monitor(executor).failed(raised.value)
    assert observations[-1].label == observation.label
    assert observations[-1].presentation == observation.presentation
    assert observations[-1].observed_at == observation.observed_at
    assert observations[-1].error["technical_detail"] == observation.error["technical_detail"]
    assert "_require_active_connection" in observations[-1].error["propagation_traceback"]


@pytest.mark.parametrize("operation,label", [("ReqAuthenticate", "认证失败"), ("ReqUserLogin", "交易登录失败")])
def test_stage_rejection_keeps_channel_content_and_original_evidence(broker, operation, label):
    from axile.executor.connection_state import connection_monitor

    observations = []
    connection_monitor(broker.executor).bind(observations.append)
    broker.errors[operation] = SimpleNamespace(ErrorID=999, ErrorMsg="柜台原始证据")
    with pytest.raises(CtpRequestError):
        broker.start()
    assert observations[-1].label == label
    assert "柜台原始证据" in observations[-1].error["technical_detail"]
    assert "999" in str(observations[-1].presentation)


@pytest.mark.parametrize(
    "operation,code,label",
    [
        ("ReqAuthenticate", -3, "认证失败"),
        ("ReqUserLogin", -3, "交易登录失败"),
        ("ReqQryTradingAccount", -3, None),
        ("ReqSettlementInfoConfirm", -3, None),
        ("ReqQryTradingAccount", -1, "交易网络异常"),
        ("ReqQryTradingAccount", -2, "前置请求队列超限"),
    ],
)
def test_native_rejections_keep_existing_stage_and_query_boundaries(broker, operation, code, label):
    from axile.executor.connection_state import connection_monitor

    observations = []
    connection_monitor(broker.executor).bind(observations.append)
    getattr(broker.trader, operation).side_effect = lambda *_args: code
    with pytest.raises(CtpRequestError):
        broker.start()
    failures = [observation for observation in observations if observation.status == "error"]
    if label is None:
        assert failures == []
    else:
        assert failures[-1].label == label
        assert failures[-1].error["return_code"] == code
        assert (
            str(code) in str(failures[-1].presentation)
            or operation.startswith("ReqAuth")
            or operation == "ReqUserLogin"
        )


def test_stage_timeout_content_survives_session_wrapper(broker, monkeypatch):
    from axile.executor.connection_state import connection_monitor

    observations = []
    connection_monitor(broker.executor).bind(observations.append)
    broker.trader.ReqUserLogin.side_effect = None
    broker.trader.ReqUserLogin.return_value = 0
    monkeypatch.setattr(broker.executor._login.done, "wait", lambda _timeout: False)
    with pytest.raises(TimeoutError):
        broker.start()
    assert observations[-1].label == "交易登录超时"
    assert observations[-1].error["exception_type"] == "TimeoutError"


@pytest.mark.parametrize("kind", ["交易", "行情"])
def test_native_api_initialization_failure_has_stage_content(broker, monkeypatch, kind):
    from axile.executor.connection_state import connection_monitor

    observations = []
    connection_monitor(broker.executor).bind(observations.append)

    def fail(_path):
        raise OSError("native API initialization original")

    api_type = td.CThostFtdcTraderApi if kind == "交易" else md.CThostFtdcMdApi
    monkeypatch.setattr(api_type, "CreateFtdcTraderApi" if kind == "交易" else "CreateFtdcMdApi", fail)
    with pytest.raises(OSError):
        broker.start()
    assert observations[-1].label == f"{kind}连接初始化失败"
    assert observations[-1].error["technical_detail"] == "native API initialization original"


def test_missing_configuration_names_fields_without_values(broker):
    from axile.executor.connection_state import connection_monitor

    observations = []
    connection_monitor(broker.executor).bind(observations.append)
    broker.executor.account_config = broker.executor.account_config.model_copy(update={"password": ""})
    with pytest.raises(ValueError):
        broker.start()
    assert observations[-1].label == "连接配置不完整"
    assert "password" in str(observations[-1].presentation)
    assert "test" not in str(observations[-1].presentation)
