"""实际 facade/SPI 走原生 MCAP；不连接柜台或发送真实委托。"""

import json
from types import SimpleNamespace

import pytest
from openctp_ctp import thosttraderapi as td

from axile.channels.builtins import _ctp_plugin
from axile.executor.ctp import replay as ctp_replay
from axile.executor.models.unified_order import OrderDirection, OrderType
from axile.executor.order_submission_context import order_submission_context
from axile.executor.replay import current_command_id
from axile.server._native import _core
from axile.server.execution_replay.contracts import freeze_data
from axile.server.execution_replay.recorder import ReplayRecorder
from axile.server.execution_replay.service import replay_manifest, replay_window
from tests.unit.executor.ctp.test_connection_recovery import ScriptedBroker
from tests.unit.server.test_execution_replay_recorder import eventually


@pytest.fixture
def recording(monkeypatch, tmp_path):
    assert getattr(_core, "EXECUTION_REPLAY_SCHEMA", None) == 1, "需要本次源码构建的真实原生回放模块"
    broker = ScriptedBroker(monkeypatch)
    broker.start()
    executor = broker.executor
    executor._ensure_instrument_symbols(["ag2612"])
    monkeypatch.setattr(executor, "_require_new_order_ready", lambda *_: None)
    monkeypatch.setattr(executor, "_get_ctp_session_block_reason", lambda *_: None)
    monkeypatch.setattr(executor, "_validate_order_quote", lambda *_: None)
    context = _ctp_plugin().descriptor.replay.model_dump(mode="json")
    context["stream_defaults"] = context.pop("streams")
    context["channel"] = "ctp"
    now = [1_793_404_800_000_000]
    root = tmp_path / "replay"
    recorder = ReplayRecorder(17, root, context, now_us=lambda: now[0])
    recorder.attach(executor)
    recorder.configure(True, 1)
    recording_id = recorder.status()["recording_id"]
    recorder.begin_execution("E-early", ["ag2612"])
    yield SimpleNamespace(
        broker=broker, executor=executor, recorder=recorder, now=now, root=root, recording_id=recording_id
    )
    recorder.close()
    executor.close()


def finish_and_read(recording):
    # starting 可被关闭取消；读文件的测试须先等 writer 就绪。
    eventually(lambda: recording.recorder.status()["state"] == "recording")
    recording.now[0] += 1
    recording.recorder.observe("/execution", {"execution_id": "E-early", "symbol": "ag2612", "target_quantity": 1})
    status = recording.recorder.close()
    assert status["state"] == "disabled", status
    manifest = replay_manifest(recording.root, 17, "E-early")
    assert any(item["recording_id"] == recording.recording_id for item in manifest["recordings"])
    request = {
        "recording_id": recording.recording_id,
        "start_us": recording.now[0],
        "end_us": recording.now[0] + 1,
    }
    state = replay_window(recording.root, 17, "E-early", request)["state_before_start"]
    request["start_us"] -= 1
    messages = replay_window(recording.root, 17, "E-early", request)["messages"]
    return state, messages


def test_facade_reentrant_fill_keeps_native_order_and_execution_ownership(recording):
    broker, executor = recording.broker, recording.executor
    delivered = []
    executor.register_trade_callback(delivered.append)
    before_queries = {name: getattr(broker.trader, name).call_count for name in broker.rows}

    def accept(request, _rid):
        row = SimpleNamespace(
            **(
                vars(broker.order)
                | {
                    "OrderRef": request.OrderRef,
                    "SessionID": executor._session_id,
                    "OrderSysID": "early-order",
                    "OrderPriceType": td.THOST_FTDC_OPT_LimitPrice,
                    "LimitPrice": 9000,
                    "CombOffsetFlag": td.THOST_FTDC_OF_Open,
                }
            )
        )
        trade = SimpleNamespace(
            **(
                vars(broker.trade)
                | {
                    "OrderRef": request.OrderRef,
                    "OrderSysID": "early-order",
                    "TradeID": "EARLY",
                    "Direction": td.THOST_FTDC_D_Buy,
                    "OffsetFlag": td.THOST_FTDC_OF_Open,
                }
            )
        )
        broker.trader_spi.OnRtnOrder(row)
        broker.trader_spi.OnRtnTrade(trade)
        return 0

    broker.trader.ReqOrderInsert.side_effect = accept
    with order_submission_context({"execution_id": "E-early"}):
        order = executor.place_order("ag2612", OrderDirection.BUY, OrderType.LIMIT, 1, 9000)
    assert len(delivered) == 1 and delivered[0].order_id == order.order_id
    assert broker.trader.ReqOrderInsert.call_count == 1
    assert {name: getattr(broker.trader, name).call_count for name in broker.rows} == before_queries
    state, messages = finish_and_read(recording)
    commands = [event for event in messages if event["topic"] == "/command"]
    public_request = next(event for event in commands if event["data"].get("phase") == "request")
    native_request = next(event for event in commands if event["data"].get("phase") == "native_request")
    result = next(event for event in commands if event["data"].get("phase") == "result")
    assert native_request["data"]["command_id"] == public_request["data"]["command_id"]
    assert native_request["data"]["order_id"] == order.order_id
    assert native_request["data"]["request_id"] is not None
    trade_event = next(event for event in messages if event["topic"] == "/trade")
    assert trade_event["capture_seq"] < result["capture_seq"]
    accounting = state["trade_accounting"]
    key = "ctp:17:20260909:SHFE:ag2612:EARLY"
    assert accounting["own_seen_keys"] == [key]
    assert accounting["own_records"][key]["data"]["execution_id"] == "E-early"
    assert accounting["execution_totals"]["E-early"]["by_symbol"]["ag2612"]["quantity"] == 1


def test_async_cancel_error_without_command_identity_stays_observable(recording):
    broker, executor = recording.broker, recording.executor
    row = SimpleNamespace(
        **(
            vars(broker.order)
            | {
                "OrderRef": "12",
                "SessionID": executor._session_id,
                "OrderSysID": "working-order",
                "OrderStatus": td.THOST_FTDC_OST_NoTradeQueueing,
                "VolumeTraded": 0,
                "OrderPriceType": td.THOST_FTDC_OPT_LimitPrice,
                "LimitPrice": 9000,
            }
        )
    )
    broker.trader_spi.OnRtnOrder(row)
    assert current_command_id() is None
    broker.trader_spi.OnErrRtnOrderAction(row, SimpleNamespace(ErrorID=31, ErrorMsg="cancel rejected"))
    state, messages = finish_and_read(recording)
    error = next(event for event in messages if event["data"].get("source") == "ctp.cancel_error")
    assert error["topic"] == "/command"
    assert error["data"]["command_id"] is None
    assert error["data"]["error_id"] == 31
    assert any(data.get("source") == "ctp.cancel_error" for data in state["commands"].values())
    order_id = f"20260909:7:{executor._session_id}:12"
    order = state["orders"][json.dumps(["ag2612", order_id], separators=(",", ":"))]
    assert order["status"] == "working"
    assert order["confirmed_book"] is True


@pytest.mark.parametrize("exit_kind", ["invalidate", "close"])
def test_connection_exit_clears_native_order_confirmation_and_quote_validity(recording, exit_kind):
    broker, executor = recording.broker, recording.executor
    row = SimpleNamespace(
        **(
            vars(broker.order)
            | {"OrderStatus": td.THOST_FTDC_OST_NoTradeQueueing, "VolumeTraded": 0, "LimitPrice": 9000}
        )
    )
    broker.trader_spi.OnRtnOrder(row)
    recording.recorder.observe(
        "/quote/ag2612",
        {
            "symbol": "ag2612",
            "bids": [{"price": 9000, "quantity": 1}],
            "asks": [{"price": 9001, "quantity": 1}],
            "book_valid": True,
            "valid_until_us": recording.now[0] + 3_000_000,
        },
    )
    generation = recording.recorder._sync_generation
    if exit_kind == "close":
        executor.close()
    else:
        executor._invalidate_connection("native identity invalid")
    assert recording.recorder._sync_generation == generation + 1
    state, messages = finish_and_read(recording)
    connection = next(event["data"] for event in messages if event["topic"] == "/connection")
    assert all(connection[field] is False for field in ("connected", "ready", "available", "synchronized"))
    assert connection["sync_generation"] == generation + 1
    order = state["orders"][json.dumps(["ag2612", "20260909:7:1:10"], separators=(",", ":"))]
    assert order["confirmed_book"] is False
    assert state["quotes"]["ag2612"]["available"] is False


@pytest.mark.parametrize(
    ("flag", "offset"),
    [
        (td.THOST_FTDC_OF_ForceClose, "CLOSE"),
        (td.THOST_FTDC_OF_ForceOff, "CLOSE"),
        (td.THOST_FTDC_OF_LocalForceClose, "CLOSE"),
        ("unmapped", None),
    ],
)
def test_native_enum_values_and_frozen_display_survive_real_mcap(recording, flag, offset):
    row = SimpleNamespace(
        **(vars(recording.broker.trade) | {"Direction": td.THOST_FTDC_D_Sell, "OffsetFlag": flag, "TradeID": "RAW"})
    )
    recording.recorder.observe(
        "/trade", ctp_replay.trade(recording.executor, row, source="ctp.trade_return", order_id="raw-order")
    )
    recording.recorder.observe(
        "/order",
        ctp_replay.order(
            SimpleNamespace(
                **(
                    vars(recording.broker.order)
                    | {"Direction": "unmapped", "OrderStatus": "new", "CombOffsetFlag": flag}
                )
            )
        ),
    )
    state, messages = finish_and_read(recording)
    observed_trade = next(event["data"] for event in messages if event["topic"] == "/trade")
    assert observed_trade["offset"] == offset
    assert observed_trade["attributes"]["native_offset_flag"] == flag
    assert state["trade_accounting"]["own_records"]["ctp:17:20260909:SHFE:ag2612:RAW"]["data"]["attributes"] == {
        "native_direction": td.THOST_FTDC_D_Sell,
        "native_offset_flag": flag,
    }
    observed_order = next(event["data"] for event in messages if event["topic"] == "/order")
    assert observed_order["attributes"]["native_direction"] == "unmapped"
    assert observed_order["attributes"]["native_status"] == "new"
    assert observed_order["attributes"]["native_comb_offset_flag"] == flag
    context = replay_manifest(recording.root, 17, "E-early")["recordings"][0]["context"]
    assert context["adapter_version"] == "2"
    display = next(field for field in context["display"]["trade"] if field["key"] == "native_offset_flag")
    assert display["values"][td.THOST_FTDC_OF_ForceClose] == "强平"


def test_normal_sdk_position_query_identity_survives_freeze_and_real_mcap(recording, monkeypatch):
    account = td.CThostFtdcTradingAccountField()
    account.BrokerID = "BROKERLEAK"
    account.AccountID = "ACCOUNTLEAK"
    account.Balance = 100
    account.Available = 80
    account.FrozenMargin = 2
    position = td.CThostFtdcInvestorPositionField()
    position.BrokerID = "BROKERLEAK"
    position.InvestorID = "INVESTORLEAK"
    position.InstrumentID = "ag2612"
    position.TradingDay = "20261031"
    position.ExchangeID = "SHFE"
    position.PosiDirection = td.THOST_FTDC_PD_Long
    position.Position = 4
    position.TodayPosition = 2
    position.LongFrozen = 1
    expected_identity = {"trading_day": "20261031", "exchange_id": "SHFE"}
    frame = ctp_replay.account_observation([account], [position], source="ctp.account_query")
    context = _ctp_plugin().descriptor.replay.model_dump(mode="json")
    frozen = freeze_data("/account_observation", frame, context)
    assert frozen["positions"][0]["native_identity"] == expected_identity
    requested = []

    def query(name, _request):
        requested.append(name)
        assert name in {"ReqQryTradingAccount", "ReqQryInvestorPosition"}
        return [account] if name == "ReqQryTradingAccount" else [position]

    monkeypatch.setattr(recording.executor, "_query", query)
    monkeypatch.setattr(recording.executor, "_position_valuation_quotes", lambda _rows: ({}, {}))
    assert recording.executor.get_account_assets().available_cash == 80
    assert requested == ["ReqQryTradingAccount", "ReqQryInvestorPosition"]
    state, messages = finish_and_read(recording)
    observation = next(event["data"] for event in messages if event["topic"] == "/account_observation")
    assert observation["scope_complete"] is True
    assert observation["positions"][0]["native_identity"] == expected_identity
    assert observation["positions"][0]["quantity"] == 4
    observed_position = next(row for row in state["observed_positions"].values() if row["symbol"] == "ag2612")
    assert observed_position["native_identity"] == expected_identity
    assert observed_position["quantity"] == 4
    serialized = json.dumps({"observation": observation, "position": observed_position})
    assert all(marker not in serialized for marker in ("BROKERLEAK", "ACCOUNTLEAK", "INVESTORLEAK", "swig"))
