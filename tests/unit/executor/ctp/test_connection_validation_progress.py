"""连接验证阶段必须来自真实 SPI 回调，并保持原有初始化顺序。"""

import pytest

from tests.unit.executor.ctp.test_connection_recovery import ScriptedBroker


def _validation_broker(monkeypatch):
    broker = ScriptedBroker(monkeypatch)
    instrument = broker.rows["ReqQryInstrument"][0]
    instrument.InstrumentID = "rb2701"
    instrument.ProductID = "rb"
    return broker


def test_connection_progress_matches_real_startup(monkeypatch):
    broker = ScriptedBroker(monkeypatch)
    events = []
    broker.executor.set_connection_progress(lambda step, status: events.append((step, status)))
    try:
        broker.start()
        assert events == [
            (step, status)
            for step in (
                "trader_connection",
                "authenticate",
                "trader_login",
                "settlement",
                "instruments",
                "market_login",
                "query_orders",
                "query_trades",
                "query_positions",
                "query_account",
                "ready",
            )
            for status in ("running", "passed")
        ]
        assert "ReqOrderInsert" not in broker.events
        assert "ReqQryTradingAccount" in broker.events
    finally:
        broker.executor.close()


def test_connected_transport_does_not_pass_authentication(monkeypatch):
    broker = ScriptedBroker(monkeypatch)
    events = []
    broker.executor.set_connection_progress(lambda step, status: events.append((step, status)))
    broker.trader.ReqAuthenticate.side_effect = None
    broker.trader.ReqAuthenticate.return_value = -1
    try:
        with pytest.raises(Exception):
            broker.start()
        assert events == [
            ("trader_connection", "running"),
            ("trader_connection", "passed"),
            ("authenticate", "running"),
        ]
    finally:
        broker.executor.close()


def test_validator_binds_effective_policy_and_releases_executor(monkeypatch):
    from unittest.mock import Mock

    from axile.executor.ctp import connection_validation
    from axile.executor.models.unified_input_accounts import CTPAccountConfig

    executor = Mock()
    monkeypatch.setattr(connection_validation, "CTPExecutor", lambda *_args: executor)
    config = CTPAccountConfig(
        broker_id="9999",
        investor_id="offline-test",
        password="secret",
        td_front="tcp://test.invalid:1234",
        md_front="tcp://test.invalid:5678",
        app_id="offline-app",
        auth_code="secret",
    )
    connection_validation.validate_connection(config, lambda *_args: None)
    guard = executor.set_account_control_guard.call_args.args[0]
    assert guard.policy.preset_key == "ctp"
    executor._initialize_connection.assert_called_once_with(config)
    executor.close.assert_called_once()


def test_validator_releases_executor_when_preparation_fails(monkeypatch):
    from unittest.mock import Mock

    from axile.executor.ctp import connection_validation
    from axile.executor.models.unified_input_accounts import CTPAccountConfig

    executor = Mock()
    executor.set_account_control_guard.side_effect = RuntimeError("offline preparation failure")
    monkeypatch.setattr(connection_validation, "CTPExecutor", lambda *_args: executor)
    config = CTPAccountConfig(
        broker_id="9999",
        investor_id="offline-test",
        password="secret",
        td_front="tcp://test.invalid:1234",
        md_front="tcp://test.invalid:5678",
        app_id="offline-app",
        auth_code="secret",
    )
    with pytest.raises(RuntimeError):
        connection_validation.validate_connection(config, lambda *_args: None)
    executor._initialize_connection.assert_not_called()
    executor.close.assert_called_once()


def test_broker_error_preserves_exact_original_text():
    from types import SimpleNamespace

    from axile.executor.ctp.ctp_execute import CTPExecutor

    original = "不合法的登录\n原始字段=unmodified-value"
    error = CTPExecutor._error(SimpleNamespace(ErrorID=3, ErrorMsg=original), "登录")
    assert error.error_id == 3
    assert error.broker_message == original


def test_validation_applies_shared_throttle_to_actual_native_queries(monkeypatch):
    from datetime import datetime

    from axile.executor.algorithms.utils.clock import get_default_clock, set_default_clock
    from axile.executor.ctp import connection_validation
    from tests.unit.executor.test_account_control_guard import _AdvancingClock

    broker = _validation_broker(monkeypatch)
    monkeypatch.setattr(connection_validation, "CTPExecutor", lambda *_args: broker.executor)
    previous_clock = get_default_clock()
    clock = _AdvancingClock(datetime(2026, 10, 13, 10, 0))
    sent = []
    names = ("ReqQryOrder", "ReqQryTrade", "ReqQryInvestorPosition", "ReqQryTradingAccount")
    for name in names:
        broker.hooks[name] = lambda name=name: sent.append((name, int(clock.time() * 1000)))
    set_default_clock(clock)
    try:
        connection_validation.validate_connection(broker.executor.account_config, lambda *_args: None)
    finally:
        set_default_clock(previous_clock)
        broker.executor.close()
    assert [name for name, _timestamp in sent] == list(names)
    assert all(later[1] - earlier[1] >= 1500 for earlier, later in zip(sent, sent[1:]))
    assert clock.sleep_calls


def test_validator_reports_settlement_record_before_later_stages(monkeypatch):
    from axile.executor.ctp import connection_validation

    broker = _validation_broker(monkeypatch)
    monkeypatch.setattr(connection_validation, "CTPExecutor", lambda *_args: broker.executor)
    events = []
    connection_validation.validate_connection(
        broker.executor.account_config,
        lambda step, status, details=None: events.append((step, status, details)),
    )
    passed = next(event for event in events if event[:2] == ("settlement", "passed"))
    assert passed[2]["title"] == "结算确认记录"
    assert passed[2]["rows"][0] == {"key": "确认方式", "value": "本次连接自动确认"}
    assert events.index(passed) < next(i for i, event in enumerate(events) if event[0] == "instruments")


@pytest.mark.parametrize("source, label", [("existing", "当日已有确认"), ("submitted", "本次连接自动确认")])
def test_settlement_details_preserve_values_and_order(source, label):
    from axile.executor.ctp.connection_validation import _settlement_details

    details = _settlement_details({"source": source, "account_id": "full-account", "settlement_id": 0})
    assert details == {
        "title": "结算确认记录",
        "rows": [
            {"key": "确认方式", "value": label},
            {"key": "确认日期", "value": None},
            {"key": "确认时间", "value": None},
            {"key": "结算编号", "value": 0},
            {"key": "经纪公司代码", "value": None},
            {"key": "投资者代码", "value": None},
            {"key": "资金账号", "value": "full-account"},
            {"key": "币种", "value": None},
        ],
    }


@pytest.mark.parametrize(
    "request_name, step",
    [
        ("ReqQryOrder", "query_orders"),
        ("ReqQryTrade", "query_trades"),
        ("ReqQryInvestorPosition", "query_positions"),
        ("ReqQryTradingAccount", "query_account"),
    ],
)
@pytest.mark.parametrize("failure", ["broker_error", "wrong_day"])
def test_session_query_failure_stays_on_actual_step(monkeypatch, request_name, step, failure):
    from types import SimpleNamespace

    from axile.executor.ctp.ctp_execute import CtpRequestError

    broker = ScriptedBroker(monkeypatch)
    events = []
    broker.executor.set_connection_progress(lambda stage, status: events.append((stage, status)))
    if failure == "broker_error":
        broker.errors[request_name] = SimpleNamespace(ErrorID=999, ErrorMsg="查询拒绝")
    else:
        broker.rows[request_name] = [SimpleNamespace(TradingDay="20000101")]
    with pytest.raises(CtpRequestError):
        broker.start()
    assert events[-1] == (step, "running")
    assert (step, "passed") not in events
    assert ("ready", "running") not in events
    assert not broker.executor._verify_connection()


def test_empty_funds_does_not_pass_funds_stage(monkeypatch):
    from axile.executor.ctp.ctp_execute import CtpRequestError

    broker = ScriptedBroker(monkeypatch)
    broker.rows["ReqQryTradingAccount"] = []
    events = []
    broker.executor.set_connection_progress(lambda stage, status: events.append((stage, status)))
    with pytest.raises(CtpRequestError, match="资金查询返回空结果"):
        broker.start()
    assert events[-1] == ("query_account", "running")
    for step in ("query_orders", "query_trades", "query_positions"):
        assert (step, "passed") in events


def test_validation_emits_only_useful_details_before_following_queries(monkeypatch):
    from axile.executor.ctp import connection_validation

    broker = _validation_broker(monkeypatch)
    monkeypatch.setattr(connection_validation, "CTPExecutor", lambda *_args: broker.executor)
    details = {}

    def report(step, status, data=None):
        if data is not None:
            assert status == "passed"
            details[step] = data
            assert not broker.executor._verify_connection()
        if step == "query_trades" and status == "running":
            assert "query_orders" in details

    connection_validation.validate_connection(broker.executor.account_config, report)
    assert set(details) == {
        "trader_login",
        "settlement",
        "instruments",
        "query_orders",
        "query_trades",
        "query_positions",
        "query_account",
    }
    assert details["query_positions"]["rows"] == []
    assert details["query_orders"]["kind"] == "table"
    assert details["query_trades"]["rows"]
    assert details["trader_login"]["rows"][0]["value"] == broker.day
    assert "本次柜台查询" in details["instruments"]["description"]


def test_validation_queries_rb_without_reusing_or_overwriting_full_catalog(monkeypatch):
    from axile.executor.ctp import connection_validation

    broker = _validation_broker(monkeypatch)
    original_provider = broker.executor._catalog_provider
    key = ("9999", broker.executor.account_config.td_front, broker.day)
    original_provider.get(key, lambda: {"ag2612": {"InstrumentID": "ag2612", "ProductID": "ag"}})
    monkeypatch.setattr(connection_validation, "CTPExecutor", lambda *_args: broker.executor)
    events = []
    connection_validation.validate_connection(
        broker.executor.account_config,
        lambda step, status, details=None: events.append((step, status, details)),
    )
    request = broker.trader.ReqQryInstrument.call_args.args[0]
    assert request.ProductID == "rb"
    assert request.ExchangeID == "SHFE"
    broker.trader.ReqQryInstrument.assert_called_once()
    assert list(broker.executor._instruments) == ["rb2701"]
    details = next(data for step, status, data in events if step == "instruments" and status == "passed")
    assert "螺纹钢" in details["description"]
    assert details["rows"][0][0] == "rb2701"

    def no_reload():
        pytest.fail("原有全量目录缓存应保持有效")

    assert list(original_provider.get(key, no_reload)) == ["ag2612"]


@pytest.mark.parametrize("wrong_product", [False, True])
def test_validation_empty_rb_query_fails_without_full_market_fallback(monkeypatch, wrong_product):
    from axile.executor.ctp import connection_validation
    from axile.executor.ctp.ctp_execute import CtpRequestError

    broker = ScriptedBroker(monkeypatch) if wrong_product else _validation_broker(monkeypatch)
    if not wrong_product:
        broker.rows["ReqQryInstrument"] = []
    monkeypatch.setattr(connection_validation, "CTPExecutor", lambda *_args: broker.executor)
    events = []
    with pytest.raises(CtpRequestError, match="螺纹钢合约查询返回空结果"):
        connection_validation.validate_connection(
            broker.executor.account_config,
            lambda step, status, details=None: events.append((step, status)),
        )
    assert events[-1] == ("instruments", "running")
    broker.trader.ReqQryInstrument.assert_called_once()
    assert broker.trader.ReqQryInstrument.call_args.args[0].ProductID == "rb"


def test_regular_executor_still_requests_full_catalog(monkeypatch):
    broker = ScriptedBroker(monkeypatch)
    try:
        broker.start()
        request = broker.trader.ReqQryInstrument.call_args.args[0]
        assert request.ProductID == ""
        assert request.ExchangeID == ""
        assert "ag2612" in broker.executor._instruments
    finally:
        broker.executor.close()
