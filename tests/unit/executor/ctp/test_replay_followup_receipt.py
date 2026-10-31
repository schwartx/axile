"""真实 SPI 后续关联与原生返回沿入口 receipt 交付，不跨录制补帧。"""

import threading
from types import SimpleNamespace

import pytest
from openctp_ctp import thosttraderapi as td

from axile.server.execution_replay.service import replay_window
from tests.unit.executor.ctp.test_replay_cancel_errors import working_order
from tests.unit.executor.ctp.test_replay_native_callbacks import finish_and_read
from tests.unit.executor.ctp.test_replay_native_callbacks import recording as recording
from tests.unit.executor.ctp.test_replay_query_epoch import starting_recording as starting_recording
from tests.unit.executor.ctp.test_replay_query_epoch import wait_until

SYMBOL = "ag2612"


def native_trade(recording, *, deferred=False):
    row = td.CThostFtdcTradeField()
    for field, value in vars(recording.broker.trade).items():
        setattr(row, field, value)
    row.TradeID = "BEFORE_BOUNDARY"
    row.Direction = td.THOST_FTDC_D_Buy
    row.OffsetFlag = td.THOST_FTDC_OF_Open
    row.TradeDate, row.TradeTime = "20260909", "09:00:00"
    if deferred:
        row.OrderSysID, row.OrderRef = "late-order", "20"
    return row


def disable(recording):
    recording.recorder.configure(False, 2)
    wait_until(lambda: recording.recorder.status()["state"] == "disabled")


def cross_boundary(recording, boundary):
    if boundary == "restart":
        disable(recording)
    if boundary in {"restart", "enable"}:
        recording.recorder.configure(True, 3)
        recording.recording_id = recording.recorder.status()["recording_id"]
    recording.now[0] += 1


def read_all(recording, start):
    state, _ = finish_and_read(recording)
    page = replay_window(
        recording.root,
        17,
        "E-early",
        {"recording_id": recording.recording_id, "start_us": start, "end_us": recording.now[0] + 1},
    )
    return state, page["messages"]


@pytest.mark.parametrize("boundary", ["restart", "enable", "none"])
@pytest.mark.parametrize("callback", ["trade", "reject"])
def test_waiting_native_callback_keeps_receipt_after_recording_changes(recording, monkeypatch, boundary, callback):
    executor, broker = recording.executor, recording.broker
    working_order(recording)
    if boundary == "enable":
        disable(recording)
    start = recording.now[0]
    waiting, errors = threading.Event(), []
    original_lock = executor._lock
    if callback == "trade":
        row = native_trade(recording)

        def deliver():
            broker.trader_spi.OnRtnTrade(row)
    else:
        row = td.CThostFtdcInputOrderField()
        row.InstrumentID, row.OrderRef = SYMBOL, "10"
        row.Direction, row.CombOffsetFlag = td.THOST_FTDC_D_Buy, td.THOST_FTDC_OF_Open
        row.OrderPriceType, row.LimitPrice, row.VolumeTotalOriginal = td.THOST_FTDC_OPT_LimitPrice, 9000, 1
        info = td.CThostFtdcRspInfoField()
        info.ErrorID, info.ErrorMsg = 31, "isolated rejection"

        def deliver():
            broker.trader_spi.OnRspOrderInsert(row, info, 99, True)

    def invoke():
        try:
            deliver()
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=invoke)

    class ObservedLock:
        def __enter__(self):
            if threading.current_thread() is worker:
                waiting.set()
            return original_lock.__enter__()

        def __exit__(self, *args):
            return original_lock.__exit__(*args)

        def __getattr__(self, name):
            return getattr(original_lock, name)

    monkeypatch.setattr(executor, "_lock", ObservedLock())
    with original_lock:
        worker.start()
        assert waiting.wait(2)
        cross_boundary(recording, boundary)
    worker.join(2)
    assert not worker.is_alive() and not errors, errors
    state, messages = read_all(recording, start)
    source = "ctp.trade_mapping" if callback == "trade" else "ctp.reject_mapping"
    mapped = [event for event in messages if event["data"].get("source") == source]
    assert bool(mapped) is (boundary == "none")
    if boundary != "none":
        assert not state["trade_accounting"]["own_seen_keys"]
        assert not state["orders"]


@pytest.mark.parametrize("boundary", ["restart", "enable", "none"])
def test_deferred_trade_preserves_receipt_until_order_arrives(recording, boundary):
    if boundary == "enable":
        disable(recording)
    start = recording.now[0]
    recording.broker.trader_spi.OnRtnTrade(native_trade(recording, deferred=True))
    assert len(recording.executor._unassociated_trades) == 1
    cross_boundary(recording, boundary)
    order = td.CThostFtdcOrderField()
    for field, value in vars(recording.broker.order).items():
        setattr(order, field, value)
    order.OrderSysID, order.OrderRef = "late-order", "20"
    recording.broker.trader_spi.OnRtnOrder(order)
    assert not recording.executor._unassociated_trades
    state, messages = read_all(recording, start)
    mapped = [event for event in messages if event["data"].get("source") == "ctp.trade_mapping"]
    assert bool(mapped) is (boundary == "none")
    assert bool(state["trade_accounting"]["own_seen_keys"]) is (boundary == "none")


@pytest.mark.parametrize("boundary", ["restart", "enable", "none"])
def test_startup_trade_preserves_receipt_until_reconciliation(starting_recording, boundary):
    recording = starting_recording
    if boundary == "enable":
        disable(recording)
    start = recording.now[0]

    def before_order_snapshot():
        recording.broker.trader_spi.OnRtnTrade(native_trade(recording))
        cross_boundary(recording, boundary)

    recording.broker.hooks["ReqQryOrder"] = before_order_snapshot
    recording.broker.start()
    assert recording.executor._ready and not recording.executor._startup_trades
    state, messages = read_all(recording, start)
    mapped = [event for event in messages if event["data"].get("source") == "ctp.trade_mapping"]
    assert bool(mapped) is (boundary == "none")
    assert bool(state["trade_accounting"]["own_seen_keys"]) is (boundary == "none")


@pytest.mark.parametrize("boundary", ["restart", "enable", "none"])
@pytest.mark.parametrize("submission_recorder", [False, True])
@pytest.mark.parametrize("blocked_at", ["sdk", "public_lock"])
def test_native_request_return_uses_the_request_receipt(
    recording, monkeypatch, boundary, submission_recorder, blocked_at
):
    executor, broker = recording.executor, recording.broker
    _, order_id = working_order(recording)
    if boundary == "enable":
        disable(recording)
    if submission_recorder:
        monkeypatch.setattr(
            executor, "order_submission_recorder", SimpleNamespace(submit=lambda send, **_kw: send()), raising=False
        )
    start = recording.now[0]
    entered, release = threading.Event(), threading.Event()
    errors, results = [], []

    def send(_request, _rid):
        if blocked_at == "sdk":
            entered.set()
            assert release.wait(3)
        return 0

    def cancel():
        try:
            results.append(executor.cancel_order(SYMBOL, order_id))
        except BaseException as error:
            errors.append(error)

    broker.trader.ReqOrderAction.side_effect = send
    worker = threading.Thread(target=cancel)
    original_lock = executor._lock

    class ObservedLock:
        def __enter__(self):
            if threading.current_thread() is worker:
                entered.set()
            return original_lock.__enter__()

        def __exit__(self, *args):
            return original_lock.__exit__(*args)

        def __getattr__(self, name):
            return getattr(original_lock, name)

    if blocked_at == "public_lock":
        monkeypatch.setattr(executor, "_lock", ObservedLock())
        original_lock.acquire()
    try:
        worker.start()
        assert entered.wait(2)
        cross_boundary(recording, boundary)
    finally:
        release.set()
        if blocked_at == "public_lock":
            original_lock.release()
        worker.join(2)
    assert not worker.is_alive() and not errors, errors
    assert results == [True] and broker.trader.ReqOrderAction.call_count == 1
    _, messages = read_all(recording, start)
    commands = [event["data"] for event in messages if event["topic"] == "/command"]
    if boundary == "none":
        assert [event["phase"] for event in commands] == [
            "request",
            "native_request",
            "native_return",
            "returned",
            "result",
        ]
    else:
        assert not commands


@pytest.mark.parametrize("boundary", ["restart", "enable", "none"])
@pytest.mark.parametrize("entry", ["orders", "trades", "recent_trades", "assets"])
def test_query_results_keep_the_query_entry_receipt(recording, boundary, entry):
    if boundary == "enable":
        disable(recording)
    start = recording.now[0]
    broker, executor = recording.broker, recording.executor
    crossed = False

    def response(name, row):
        def respond(_request, rid):
            nonlocal crossed
            getattr(broker.trader_spi, name.replace("Req", "OnRsp", 1))(row, None, rid, True)
            if not crossed:
                crossed = True
                cross_boundary(recording, boundary)
            return 0

        getattr(broker.trader, name).side_effect = respond

    order = td.CThostFtdcOrderField()
    for field, value in vars(broker.order).items():
        setattr(order, field, value)
    account = td.CThostFtdcTradingAccountField()
    account.TradingDay, account.Balance, account.Available = "20260909", 100000, 90000
    response("ReqQryOrder", order)
    response("ReqQryTrade", native_trade(recording))
    response("ReqQryTradingAccount", account)
    response("ReqQryInvestorPosition", None)
    if entry == "orders":
        assert executor.query_recent_orders(SYMBOL)
    elif entry == "trades":
        assert executor.query_trades(SYMBOL, "20260909:7:1:10")
    elif entry == "recent_trades":
        assert executor.query_recent_trades(SYMBOL)
    else:
        assert executor.get_account_assets().available_cash == 90000
    _, messages = read_all(recording, start)
    observations = [event for event in messages if event["topic"] in {"/order_set", "/trade", "/account_observation"}]
    assert bool(observations) is (boundary == "none")
