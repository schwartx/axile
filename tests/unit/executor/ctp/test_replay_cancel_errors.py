"""按真实 CTP 请求身份回放撤单失败，不连接交易柜台。"""

import threading
from types import SimpleNamespace

from openctp_ctp import thosttraderapi as td
from tests.unit.executor.ctp.test_replay_native_callbacks import recording as recording

from axile.executor.ctp import replay as ctp_replay
from axile.executor.replay import replay_command


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


def cancel_errors(messages):
    return [event for event in messages if event["data"].get("source") == "ctp.cancel_error"]


def native_order_action(request, request_id):
    row = td.CThostFtdcOrderActionField()
    for field in ("InstrumentID", "ExchangeID", "OrderSysID", "OrderRef", "FrontID", "SessionID"):
        setattr(row, field, getattr(request, field))
    row.RequestID = request_id
    return row


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
