"""真实 CTP 查询结果只能沿原查询证明进入回放。"""

import threading
from types import SimpleNamespace

import pytest
from openctp_ctp import thosttraderapi as td

from axile.channels.builtins import _ctp_plugin
from axile.executor.ctp import replay as replay_capture
from axile.executor.ctp.ctp_execute import CtpRequestError
from axile.server.execution_replay.recorder import ReplayRecorder
from axile.server.execution_replay.service import replay_window
from tests.unit.executor.ctp.test_connection_recovery import ScriptedBroker
from tests.unit.executor.ctp.test_replay_native_callbacks import finish_and_read
from tests.unit.executor.ctp.test_replay_native_callbacks import recording as recording

SYMBOL = "ag2612"
ORDER_ID = "20260909:7:1:10"
ORDER_KEY = f'["{SYMBOL}","{ORDER_ID}"]'


def working_order(recording, **changes):
    return SimpleNamespace(
        **(
            vars(recording.broker.order)
            | {
                "OrderStatus": td.THOST_FTDC_OST_NoTradeQueueing,
                "VolumeTotalOriginal": 4,
                "VolumeTraded": 0,
                "LimitPrice": 9000,
            }
            | changes
        )
    )


def configure_query(recording, row):
    def response(_request, rid):
        recording.broker.trader_spi.OnRspQryOrder(row, None, rid, True)
        return 0

    recording.broker.trader.ReqQryOrder.side_effect = response


def query(recording, entry):
    executor = recording.executor
    if entry == "pending":
        return executor.get_pending_orders(SYMBOL)
    if entry == "recent":
        return executor.query_recent_orders(SYMBOL)
    return executor.reconcile_terminal_order(SYMBOL, ORDER_ID)


def read_recording(recording, start):
    state, _ = finish_and_read(recording)
    window = replay_window(
        recording.root,
        17,
        "E-early",
        {
            "recording_id": recording.recording_id,
            "start_us": start,
            "end_us": recording.now[0] + 1,
        },
    )
    return state, window["messages"]


def wait_until(predicate):
    wake = threading.Event()
    for _ in range(300):
        if predicate():
            return
        wake.wait(0.01)
    assert predicate()


@pytest.fixture
def starting_recording(monkeypatch, tmp_path):
    broker = ScriptedBroker(monkeypatch)
    broker.rows["ReqQryOrder"] = [working_order(SimpleNamespace(broker=broker))]
    broker.rows["ReqQryTrade"] = []
    context = _ctp_plugin().descriptor.replay.model_dump(mode="json")
    context["stream_defaults"] = context.pop("streams")
    context["channel"] = "ctp"
    now = [1_793_404_800_000_000]
    root = tmp_path / "replay"
    recorder = ReplayRecorder(17, root, context, now_us=lambda: now[0])
    recorder.attach(broker.executor)
    recorder.configure(True, 1)
    recorder.begin_execution("E-early", [SYMBOL])
    yield SimpleNamespace(
        broker=broker,
        executor=broker.executor,
        recorder=recorder,
        root=root,
        now=now,
        recording_id=recorder.status()["recording_id"],
    )
    recorder.close()
    broker.executor.close()


@pytest.mark.parametrize("entry", ["pending", "recent", "terminal"])
def test_query_return_followed_by_loss_cannot_reconfirm_old_rows(recording, monkeypatch, entry):
    start = recording.now[0]
    row = working_order(recording)
    recording.broker.trader_spi.OnRtnOrder(row)
    configure_query(recording, row)
    original = recording.executor._query

    def lose_after_response(*args, **kwargs):
        rows = original(*args, **kwargs)
        recording.now[0] += 1
        recording.executor._invalidate_connection("isolated loss after complete query")
        return rows

    monkeypatch.setattr(recording.executor, "_query", lose_after_response)
    query(recording, entry)
    state, messages = read_recording(recording, start)
    order = state["orders"][ORDER_KEY]
    assert recording.executor._ready is False
    assert order["confirmed_book"] is False
    assert order["sync_generation"] == 1
    sets = [event["data"] for event in messages if event["topic"] == "/order_set"]
    assert len(sets) == 1 and sets[0]["sync_generation"] == 1
    assert sets[0]["synchronized"] is False and sets[0]["query_basis"] is None
    assert len([event for event in messages if event["topic"] == "/order"]) == 1


@pytest.mark.parametrize("entry", ["pending", "recent", "terminal"])
def test_query_prefix_does_not_overwrite_newer_realtime_return(recording, monkeypatch, entry):
    start = recording.now[0]
    row = working_order(recording)
    recording.broker.trader_spi.OnRtnOrder(row)
    configure_query(recording, row)
    original = recording.executor._query

    def update_after_response(*args, **kwargs):
        rows = original(*args, **kwargs)
        recording.now[0] += 1
        recording.broker.trader_spi.OnRtnOrder(
            working_order(
                recording,
                OrderStatus=td.THOST_FTDC_OST_PartTradedQueueing,
                VolumeTraded=1,
                LimitPrice=9002,
            )
        )
        return rows

    monkeypatch.setattr(recording.executor, "_query", update_after_response)
    query(recording, entry)
    state, messages = read_recording(recording, start)
    order = state["orders"][ORDER_KEY]
    assert order["confirmed_book"] is True
    assert order["price"] == 9002
    assert order["filled_quantity"] == 1 and order["remaining_quantity"] == 3
    assert len([event for event in messages if event["topic"] == "/order"]) == 2


@pytest.mark.parametrize("entry", ["pending", "recent", "terminal"])
def test_normal_query_delivers_one_proven_set_without_synthetic_returns(recording, entry):
    start = recording.now[0]
    configure_query(recording, working_order(recording))
    query(recording, entry)
    state, messages = read_recording(recording, start)
    order = state["orders"][ORDER_KEY]
    assert order["confirmed_book"] is True and order["price"] == 9000
    assert not [event for event in messages if event["topic"] == "/order"]
    sets = [event["data"] for event in messages if event["topic"] == "/order_set"]
    assert len(sets) == 1 and sets[0]["query_basis"] is not None and sets[0]["synchronized"] is True
    assert recording.broker.trader.ReqQryOrder.call_count == 2


@pytest.mark.parametrize("entry", ["pending", "recent", "terminal"])
def test_query_error_releases_unpublished_proof_without_changing_error(recording, monkeypatch, entry):
    failure = RuntimeError("isolated query failure")

    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(recording.executor, "_query", fail)
    with pytest.raises(RuntimeError) as caught:
        query(recording, entry)
    assert caught.value is failure
    assert recording.recorder._session.queries == {}
    assert recording.recorder.status()["state"] != "error"


@pytest.mark.parametrize("concurrent_return", [False, True])
def test_startup_confirms_original_query_only_at_ready_and_preserves_newer_returns(
    starting_recording, concurrent_return
):
    recording = starting_recording
    start = recording.now[0]
    if concurrent_return:

        def update_during_reconciliation():
            recording.now[0] += 1
            recording.broker.trader_spi.OnRtnOrder(
                working_order(
                    recording,
                    OrderStatus=td.THOST_FTDC_OST_PartTradedQueueing,
                    VolumeTraded=1,
                    LimitPrice=9002,
                )
            )

        recording.broker.hooks["ReqQryTradingAccount"] = update_during_reconciliation
    recording.broker.start()
    assert recording.executor._ready is True
    assert recording.recorder._session.queries == {}
    state, messages = read_recording(recording, start)
    order = state["orders"][ORDER_KEY]
    assert order["confirmed_book"] is True
    assert order["price"] == (9002 if concurrent_return else 9000)
    assert order["remaining_quantity"] == (3 if concurrent_return else 4)
    sets = [event["data"] for event in messages if event["topic"] == "/order_set"]
    assert len(sets) == 2
    assert sets[0]["synchronized"] is False and sets[1]["synchronized"] is True
    assert sets[0]["query_basis"] == sets[1]["query_basis"] and sets[0]["query_basis"] is not None
    assert len([event for event in messages if event["topic"] == "/order"]) == int(concurrent_return)
    assert recording.broker.events.count("ReqQryOrder") == 1


def test_failed_startup_keeps_query_observation_unconfirmed(starting_recording):
    recording = starting_recording
    start = recording.now[0]
    recording.broker.errors["ReqQryTradingAccount"] = SimpleNamespace(ErrorID=31, ErrorMsg="isolated startup failure")
    with pytest.raises(CtpRequestError):
        recording.broker.start()
    assert recording.recorder._session.queries == {}
    state, messages = read_recording(recording, start)
    assert not state["orders"]
    sets = [event["data"] for event in messages if event["topic"] == "/order_set"]
    assert len(sets) == 1 and sets[0]["synchronized"] is False
    assert not [event for event in messages if event["topic"] == "/order"]
    assert not [event for event in messages if event["topic"] == "/connection" and event["data"].get("ready") is True]


def test_cancel_missing_cached_key_uses_query_proof_without_extra_returns(recording):
    start = recording.now[0]
    recording.executor._order_keys.clear()
    configure_query(recording, working_order(recording))
    recording.broker.trader.ReqOrderAction.return_value = 0
    assert recording.executor.cancel_order(SYMBOL, ORDER_ID) is True
    state, messages = read_recording(recording, start)
    assert ORDER_ID in recording.executor._order_keys
    assert state["orders"][ORDER_KEY]["confirmed_book"] is True
    assert not [event for event in messages if event["topic"] == "/order"]
    assert len([event for event in messages if event["topic"] == "/order_set"]) == 1
    assert recording.broker.trader.ReqOrderAction.call_count == 1


@pytest.mark.parametrize("boundary", ["close", "invalidate"])
def test_connection_teardown_and_startup_publish_in_the_same_lock_order(starting_recording, monkeypatch, boundary):
    recording = starting_recording
    start = recording.now[0]
    executor = recording.executor
    ready_barrier, allow_ready, teardown_waits, teardown_observed = (threading.Event() for _ in range(4))
    original_lock, original_capture = executor._lock, replay_capture.capture
    failures, release_lock_states = [], []
    recording.broker.market.Release.side_effect = lambda: release_lock_states.append(original_lock._is_owned())
    recording.broker.trader.Release.side_effect = lambda: release_lock_states.append(original_lock._is_owned())
    final_status = "closed" if boundary == "close" else "error"

    def finish_connection():
        if boundary == "close":
            executor.close()
        else:
            executor._invalidate_connection("isolated direct invalidation during startup")

    finisher = threading.Thread(target=finish_connection)

    class ObservedLock:
        def __enter__(self):
            if threading.current_thread() is finisher:
                teardown_waits.set()
            return original_lock.__enter__()

        def __exit__(self, *args):
            return original_lock.__exit__(*args)

        def __getattr__(self, name):
            return getattr(original_lock, name)

    def capture(owner, topic, build, **kwargs):
        data = build()
        if owner is executor and topic == "/order_set" and data.get("synchronized") is True:
            ready_barrier.set()
            assert allow_ready.wait(2)
        original_capture(owner, topic, lambda: data, **kwargs)
        if owner is executor and topic == "/connection" and data.get("status") == final_status:
            teardown_observed.set()

    def startup():
        try:
            recording.broker.start()
        except BaseException as error:
            failures.append(error)

    monkeypatch.setattr(executor, "_lock", ObservedLock())
    monkeypatch.setattr(replay_capture, "capture", capture)
    starter = threading.Thread(target=startup)
    starter.start()
    try:
        assert ready_barrier.wait(2)
        finisher.start()
        assert teardown_waits.wait(2)
        teardown_before_ready = teardown_observed.is_set()
    finally:
        allow_ready.set()
        starter.join(2)
        if finisher.ident is not None:
            finisher.join(2)
    assert not starter.is_alive() and not finisher.is_alive()
    assert not failures
    assert executor._ready is False
    if boundary == "close":
        assert executor._closed is True
        executor.close()
    else:
        assert executor._invalid_reason and not executor._closed
    state, messages = read_recording(recording, start)
    connection = state["connections"][SYMBOL]
    assert teardown_before_ready is False
    assert connection["connected"] is False and connection["ready"] is False
    transitions = [event["data"]["status"] for event in messages if event["topic"] == "/connection"]
    assert transitions[-2:] == ["connected", final_status]
    assert state["orders"][ORDER_KEY]["confirmed_book"] is False
    assert release_lock_states == ([False, False] if boundary == "close" else [])
    assert recording.broker.market.Release.call_count == int(boundary == "close")
    assert recording.broker.trader.Release.call_count == int(boundary == "close")


@pytest.mark.parametrize("boundary", ["restart", "restart_normalization_error", "disconnect"])
def test_order_normalization_keeps_the_original_recording_and_generation(recording, monkeypatch, boundary):
    recorder, executor = recording.recorder, recording.executor
    start = recording.now[0]
    recorder.observe("/command", {"command_id": "seed-barrier", "phase": "result"})
    wait_until(lambda: not recorder._session.queue)
    original, normalized, release = replay_capture.order, threading.Event(), threading.Event()
    generation = executor._replay_sync_generation
    errors = []
    calls = []

    def normalize(row, **kwargs):
        data = original(row, **kwargs)
        if not calls:
            calls.append(True)
            normalized.set()
            assert release.wait(5)
            if boundary == "restart_normalization_error":
                raise ValueError("isolated normalization failure from the old recording")
        return data

    def deliver():
        try:
            recording.broker.trader_spi.OnRtnOrder(working_order(recording))
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(replay_capture, "order", normalize)
    worker = threading.Thread(target=deliver)
    loss = None
    worker.start()
    try:
        assert normalized.wait(2)
        recording.now[0] += 1
        if boundary.startswith("restart"):
            old_recording_id = recording.recording_id
            recorder.configure(False, 2)
            wait_until(lambda: recorder.status()["state"] == "disabled")
            recorder.configure(True, 3)
            recording.recording_id = recorder.status()["recording_id"]
            assert recording.recording_id != old_recording_id
        else:
            loss = threading.Thread(target=lambda: executor._disconnected("交易", 1))
            loss.start()
            wait_until(lambda: executor._replay_sync_generation > generation)
        recording.now[0] += 1
    finally:
        release.set()
        worker.join(2)
        if loss is not None:
            loss.join(2)
    assert not worker.is_alive() and (loss is None or not loss.is_alive()) and not errors
    if boundary.startswith("restart"):
        recording.now[0] += 1
        recording.broker.trader_spi.OnRtnOrder(working_order(recording, OrderRef="11", OrderSysID="fresh-system-id"))
    state, messages = read_recording(recording, start)
    if boundary.startswith("restart"):
        assert ORDER_KEY not in state["orders"]
        assert state["orders"]['["ag2612","20260909:7:1:11"]']["confirmed_book"] is True
        assert not [event for event in messages if event["data"].get("reason") == "normalization_failed"]
    else:
        old_return = next(event["data"] for event in messages if event["topic"] == "/order")
        assert old_return["sync_generation"] == generation and old_return["confirmed_book"] is False
        assert state["orders"][ORDER_KEY]["confirmed_book"] is False


def test_quote_normalization_cannot_revalidate_a_pre_disconnect_book(recording):
    normalized, release = threading.Event(), threading.Event()
    generation = recording.executor._replay_sync_generation
    start = recording.now[0]

    def normalize_valid_depth():
        normalized.set()
        assert release.wait(2)
        return {
            "symbol": SYMBOL,
            "source": "isolated.normalized_depth",
            "book_valid": True,
            "valid_until_us": start + 10_000_000,
            "bids": [{"price": 9000, "quantity": 2}],
            "asks": [{"price": 9001, "quantity": 3}],
        }

    worker = threading.Thread(
        target=lambda: replay_capture.capture(recording.executor, f"/quote/{SYMBOL}", normalize_valid_depth)
    )
    worker.start()
    try:
        assert normalized.wait(2)
        recording.now[0] += 1
        recording.broker.trader_spi.OnFrontDisconnected(1)
        recording.now[0] += 1
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    state, messages = read_recording(recording, start)
    quote = next(event["data"] for event in messages if event["topic"].startswith("/quote/"))
    assert quote["sync_generation"] == generation and quote["book_valid"] is False
    assert state["quotes"][SYMBOL]["book_valid"] is False


def test_capture_keeps_the_query_token_generation_instead_of_the_current_generation():
    observations = []
    owner = SimpleNamespace(
        _replay_observer=lambda topic, data: observations.append((topic, data)), _replay_sync_generation=8
    )
    replay_capture.capture(owner, "/order_set", lambda: {"sync_generation": 3})
    assert observations == [("/order_set", {"sync_generation": 3})]
