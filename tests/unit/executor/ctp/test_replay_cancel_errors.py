"""按真实 CTP 请求身份回放撤单失败，不连接交易柜台。"""

import json
import threading
from types import SimpleNamespace

import pytest
from openctp_ctp import thosttraderapi as td

from axile.executor.ctp import replay as ctp_replay
from axile.executor.replay import replay_command
from axile.server.execution_replay.service import replay_window
from tests.unit.executor.ctp.test_replay_native_callbacks import finish_and_read
from tests.unit.executor.ctp.test_replay_native_callbacks import recording as recording


def working_order(recording):
    executor = recording.executor
    row = SimpleNamespace(
        **(
            vars(recording.broker.order)
            | {
                "SessionID": executor._session_id,
                "OrderSysID": "cancel-working",
                "OrderStatus": td.THOST_FTDC_OST_NoTradeQueueing,
                "VolumeTraded": 0,
                "LimitPrice": 9000,
                "OrderPriceType": td.THOST_FTDC_OPT_LimitPrice,
            }
        )
    )
    recording.broker.trader_spi.OnRtnOrder(row)
    recording.broker.trader.ReqOrderAction.return_value = 0
    order_id = f"20260909:7:{executor._session_id}:10"
    return row, order_id


def read_all(recording, start):
    state, _ = finish_and_read(recording)
    window = replay_window(
        recording.root,
        17,
        "E-early",
        {"recording_id": recording.recording_id, "start_us": start, "end_us": recording.now[0] + 1},
    )
    return state, window["messages"]


def order_at(recording, order_id, time_us):
    state = replay_window(
        recording.root,
        17,
        "E-early",
        {"recording_id": recording.recording_id, "start_us": time_us, "end_us": time_us + 1},
    )["state_before_start"]
    return state["orders"][json.dumps(["ag2612", order_id], separators=(",", ":"))]


def cancel_errors(messages):
    return [event for event in messages if event["data"].get("source") == "ctp.cancel_error"]


def native_order_action(request, request_id):
    row = td.CThostFtdcOrderActionField()
    for field in ("InstrumentID", "ExchangeID", "OrderSysID", "OrderRef", "FrontID", "SessionID"):
        setattr(row, field, getattr(request, field))
    row.RequestID = request_id
    return row


@pytest.mark.parametrize("reentrant", [False, True])
@pytest.mark.parametrize("callback", ["rsp", "err_rtn"])
def test_known_native_cancel_error_resolves_only_the_proven_command(recording, reentrant, callback):
    start = recording.now[0]
    _, order_id = working_order(recording)
    error = SimpleNamespace(ErrorID=31, ErrorMsg="isolated cancel rejection")

    def reject(request, rid):
        if callback == "rsp":
            recording.broker.trader_spi.OnRspOrderAction(request, error, rid, True)
        else:
            recording.broker.trader_spi.OnErrRtnOrderAction(native_order_action(request, rid), error)

    def send(request, rid):
        if reentrant:
            reject(request, rid)
        return 0

    recording.broker.trader.ReqOrderAction.side_effect = send
    assert recording.executor.get_account_control_guard() is None
    assert recording.executor.cancel_order("ag2612", order_id) is True
    request, rid = recording.broker.trader.ReqOrderAction.call_args.args
    if not reentrant:
        recording.now[0] += 1
        reject(request, rid)
    state, messages = read_all(recording, start)
    native_request = next(
        event for event in messages if event["topic"] == "/command" and event["data"].get("phase") == "native_request"
    )
    rejected = cancel_errors(messages)[0]
    assert rejected["data"]["request_id"] == rid
    assert rejected["data"]["command_id"] == native_request["data"]["command_id"]
    assert rejected["data"]["operation"] == "cancel_order"
    assert rejected["data"]["phase"] == "native_error"
    assert rejected["data"]["success"] is False and rejected["data"]["accepted"] is False
    assert rejected["data"]["order_id"] == order_id
    result = next(
        event for event in messages if event["topic"] == "/command" and event["data"].get("phase") == "result"
    )
    assert result["data"]["success"] is True
    if reentrant:
        assert rejected["capture_seq"] < result["capture_seq"]
    order = state["orders"][json.dumps(["ag2612", order_id], separators=(",", ":"))]
    assert order["cancel_pending"] is False
    assert order["confirmed_book"] is True and order["status"] == "working"
    assert recording.broker.trader.ReqOrderAction.call_count == 1


@pytest.mark.parametrize("request_id", [None, 0, "unknown"])
def test_err_rtn_without_a_known_positive_request_id_keeps_cancel_pending(recording, request_id):
    start = recording.now[0]
    _, order_id = working_order(recording)
    assert recording.executor.cancel_order("ag2612", order_id)
    request, rid = recording.broker.trader.ReqOrderAction.call_args.args
    if request_id is None:
        row = SimpleNamespace(
            **{
                field: getattr(request, field)
                for field in ("InstrumentID", "ExchangeID", "OrderSysID", "OrderRef", "FrontID", "SessionID")
            }
        )
    else:
        row = native_order_action(request, rid + 10000 if request_id == "unknown" else request_id)
    recording.broker.trader_spi.OnErrRtnOrderAction(row, SimpleNamespace(ErrorID=31, ErrorMsg="unproven request"))
    state, messages = read_all(recording, start)
    rejected = cancel_errors(messages)[0]["data"]
    assert rejected["command_id"] is None
    assert rejected.get("accepted") is not False and rejected.get("success") is not False
    assert state["orders"][json.dumps(["ag2612", order_id], separators=(",", ":"))]["cancel_pending"] is True


def test_two_cancels_keep_independent_pending_intents_and_ignore_unknown_or_zero_errors(recording):
    start = recording.now[0]
    _, order_id = working_order(recording)
    recording.now[0] += 1
    assert recording.executor.cancel_order("ag2612", order_id)
    first_request, first_id = recording.broker.trader.ReqOrderAction.call_args.args
    recording.now[0] += 1
    assert recording.executor.cancel_order("ag2612", order_id)
    second_request, second_id = recording.broker.trader.ReqOrderAction.call_args.args
    assert first_id != second_id
    error = SimpleNamespace(ErrorID=31, ErrorMsg="isolated cancel rejection")
    recording.now[0] += 1
    recording.broker.trader_spi.OnRspOrderAction(second_request, error, second_id + 10000, True)
    recording.now[0] += 1
    recording.broker.trader_spi.OnRspOrderAction(first_request, SimpleNamespace(ErrorID=0), first_id, True)
    recording.now[0] += 1
    recording.broker.trader_spi.OnRspOrderAction(first_request, error, first_id, True)
    recording.now[0] += 1
    recording.broker.trader_spi.OnRspOrderAction(second_request, error, second_id, True)
    _, messages = read_all(recording, start)
    assert order_at(recording, order_id, start + 4)["cancel_pending"] is True
    assert order_at(recording, order_id, start + 5)["cancel_pending"] is True
    assert order_at(recording, order_id, start + 6)["cancel_pending"] is True
    assert order_at(recording, order_id, start + 7)["cancel_pending"] is False
    errors = cancel_errors(messages)
    unknown = next(event["data"] for event in errors if event["data"]["request_id"] == second_id + 10000)
    assert unknown["command_id"] is None
    assert unknown.get("accepted") is not False and unknown.get("success") is not False
    proven = [event["data"] for event in errors if event["data"].get("accepted") is False]
    assert len(proven) == 2
    assert len({event["command_id"] for event in proven}) == 2


def test_reentrant_unknown_request_cannot_take_the_active_command_identity(recording):
    start = recording.now[0]
    _, order_id = working_order(recording)

    def send(request, rid):
        recording.broker.trader_spi.OnRspOrderAction(
            request, SimpleNamespace(ErrorID=31, ErrorMsg="other request"), rid + 10000, True
        )
        return 0

    recording.broker.trader.ReqOrderAction.side_effect = send
    assert recording.executor.cancel_order("ag2612", order_id)
    state, messages = read_all(recording, start)
    assert cancel_errors(messages)[0]["data"]["command_id"] is None
    assert state["orders"][json.dumps(["ag2612", order_id], separators=(",", ":"))]["cancel_pending"] is True


@pytest.mark.parametrize("boundary", ["generation", "recording"])
def test_old_cancel_response_cannot_reuse_a_new_observer_or_generation(recording, boundary):
    start = recording.now[0]
    _, order_id = working_order(recording)
    assert recording.executor.cancel_order("ag2612", order_id)
    old_request, old_id = recording.broker.trader.ReqOrderAction.call_args.args
    recording.now[0] += 1
    if boundary == "generation":
        recording.recorder.observe("/connection", {"connected": False})
        recording.recorder.observe("/connection", {"connected": True})
    else:
        recording.recorder.configure(False, 2)
        wake = threading.Event()
        for _ in range(300):
            if recording.recorder.status()["state"] == "disabled":
                break
            wake.wait(0.01)
        assert recording.recorder.status()["state"] == "disabled"
        recording.recorder.configure(True, 3)
        recording.recording_id = recording.recorder.status()["recording_id"]
        start = recording.now[0]
    working_order(recording)
    assert recording.executor.cancel_order("ag2612", order_id)
    new_request, new_id = recording.broker.trader.ReqOrderAction.call_args.args
    error = SimpleNamespace(ErrorID=31, ErrorMsg="isolated cancel rejection")
    recording.broker.trader_spi.OnRspOrderAction(old_request, error, old_id, True)
    recording.broker.trader_spi.OnRspOrderAction(new_request, error, new_id, True)
    _, messages = read_all(recording, start)
    errors = cancel_errors(messages)
    assert next(event["data"] for event in errors if event["data"]["request_id"] == old_id)["command_id"] is None
    current = next(event["data"] for event in errors if event["data"]["request_id"] == new_id)
    assert current["command_id"] is not None and current["accepted"] is False


def test_disabled_cancel_correlation_allocates_no_replay_state():
    owner = SimpleNamespace(_replay_observer=None, _replay_sync_generation=1)
    before = vars(owner).copy()
    ctp_replay.link_cancel_request(owner, 1, "ag2612", "20260909:7:2:10")
    assert vars(owner) == before


def test_cancel_request_correlation_is_bounded_and_evicted_ids_stay_unknown():
    def observer(*_):
        pass

    owner = SimpleNamespace(_replay_observer=observer, _replay_sync_generation=1, _lock=threading.RLock())
    for rid in range(1, 514):
        with replay_command(owner, "cancel_order", {"symbol": "ag2612", "order_id": "20260909:7:2:10"}):
            ctp_replay.link_cancel_request(owner, rid, "ag2612", "20260909:7:2:10")
    error = SimpleNamespace(ErrorID=31)
    row = SimpleNamespace(InstrumentID="ag2612")
    assert ctp_replay.cancel_rejection(owner, row, error, request_id=1)["command_id"] is None
    latest = ctp_replay.cancel_rejection(owner, row, error, request_id=513)
    assert latest["command_id"] is not None and latest["accepted"] is False
