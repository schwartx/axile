"""真实 SDK 请求持锁时，SPI 订单帧不能借用等待之后的录制身份。"""

import json
import threading
from types import SimpleNamespace

import pytest

from axile.executor.ctp import replay as ctp_replay
from axile.executor.replay import capture_receipt
from axile.server.execution_replay.service import replay_window
from tests.unit.executor.ctp.test_replay_cancel_errors import working_order
from tests.unit.executor.ctp.test_replay_native_callbacks import finish_and_read
from tests.unit.executor.ctp.test_replay_native_callbacks import recording as recording

SYMBOL = "ag2612"


def wait_until(predicate):
    wake = threading.Event()
    for _ in range(300):
        if predicate():
            return
        wake.wait(0.01)
    assert predicate()


def invoke(errors, function, *args):
    try:
        return function(*args)
    except BaseException as error:
        errors.append(error)


def assert_captured_orders(recording, boundary, start, order_id, fresh, generation):
    fresh_id = f"20260909:7:{recording.executor._session_id}:{fresh.OrderRef}"
    if boundary != "disconnect":
        recording.broker.trader_spi.OnRtnOrder(fresh)
    state, _ = finish_and_read(recording)
    window = replay_window(
        recording.root,
        17,
        "E-early",
        {"recording_id": recording.recording_id, "start_us": start, "end_us": recording.now[0] + 1},
    )
    frames = [message for message in window["messages"] if message["topic"] == "/order"]
    old_frames = [message for message in frames if message["data"].get("order_id") == order_id]
    key = json.dumps([SYMBOL, order_id], separators=(",", ":"))
    if boundary in {"restart", "enable"}:
        assert not old_frames and key not in state["orders"]
    elif boundary == "disconnect":
        assert len(old_frames) == 1
        assert old_frames[0]["data"]["sync_generation"] == generation
        assert old_frames[0]["data"]["confirmed_book"] is False
        before_invalidation = replay_window(
            recording.root,
            17,
            "E-early",
            {"recording_id": recording.recording_id, "start_us": start + 3, "end_us": start + 4},
        )["state_before_start"]
        assert not before_invalidation["orders"].get(key, {}).get("confirmed_book", False)
    else:
        assert state["orders"][key]["confirmed_book"] is True
    if boundary != "disconnect":
        fresh_key = json.dumps([SYMBOL, fresh_id], separators=(",", ":"))
        assert state["orders"][fresh_key]["confirmed_book"] is True


@pytest.mark.parametrize("boundary", ["restart", "enable", "disconnect", "none"])
def test_waiting_spi_order_keeps_receipt_across_a_real_native_request(recording, monkeypatch, boundary):
    recorder, executor, broker = recording.recorder, recording.executor, recording.broker
    start, original_id = recording.now[0], recording.recording_id
    row, cancel_id = working_order(recording)
    queued = SimpleNamespace(**(vars(row) | {"OrderRef": "12", "OrderSysID": "queued-before-boundary"}))
    fresh = SimpleNamespace(**(vars(row) | {"OrderRef": "14", "OrderSysID": "fresh-after-boundary"}))
    order_id = f"20260909:7:{executor._session_id}:12"
    generation = executor._replay_sync_generation
    sdk_entered, release_sdk, waiting, loss_waiting, allow_loss = (threading.Event() for _ in range(5))
    errors, results, delivered = [], [], []
    executor.register_order_callback(delivered.append)
    if boundary == "enable":
        recorder.configure(False, 2)
        wait_until(lambda: recorder.status()["state"] == "disabled")

    def send(_request, _rid):
        sdk_entered.set()
        assert release_sdk.wait(3)
        return 0

    def cancel():
        results.append(executor.cancel_order(SYMBOL, cancel_id))

    broker.trader.ReqOrderAction.side_effect = send
    sender = threading.Thread(target=invoke, args=(errors, cancel))
    delivery = threading.Thread(target=invoke, args=(errors, broker.trader_spi.OnRtnOrder, queued))
    loss = threading.Thread(target=invoke, args=(errors, broker.trader_spi.OnFrontDisconnected, 1))
    original_lock = executor._lock

    class ObservedLock:
        def __enter__(self):
            if threading.current_thread() is delivery:
                waiting.set()
            if threading.current_thread() is loss:
                loss_waiting.set()
                assert allow_loss.wait(3)
            return original_lock.__enter__()

        def __exit__(self, *args):
            return original_lock.__exit__(*args)

        def __getattr__(self, name):
            return getattr(original_lock, name)

    monkeypatch.setattr(executor, "_lock", ObservedLock())
    try:
        sender.start()
        assert sdk_entered.wait(2)
        delivery.start()
        assert waiting.wait(2)
        recording.now[0] += 1
        if boundary in {"restart", "enable"}:
            if boundary == "restart":
                recorder.configure(False, 2)
                wait_until(lambda: recorder.status()["state"] == "disabled")
            recorder.configure(True, 3)
            recording.recording_id = recorder.status()["recording_id"]
            assert recording.recording_id != original_id
        elif boundary == "disconnect":
            loss.start()
            assert loss_waiting.wait(2)
            assert executor._replay_sync_generation > generation
        recording.now[0] += 1
        release_sdk.set()
        sender.join(2)
        delivery.join(2)
        assert not sender.is_alive() and not delivery.is_alive()
        recording.now[0] += 1
        allow_loss.set()
        if loss.ident is not None:
            loss.join(2)
        assert not loss.is_alive() and not errors, errors
        assert results == [True]
        assert order_id in [order.order_id for order in delivered]
        assert_captured_orders(recording, boundary, start, order_id, fresh, generation)
    finally:
        release_sdk.set()
        allow_loss.set()
        for thread in (sender, delivery, loss):
            if thread.ident is not None:
                thread.join(2)


def test_explicit_disabled_receipt_does_not_construct_after_recording_is_enabled():
    observations, constructed = [], []
    owner = SimpleNamespace(_replay_observer=None)
    receipt = capture_receipt(owner)
    owner._replay_observer = lambda topic, data: observations.append((topic, data))
    owner._replay_sync_generation = 5
    ctp_replay.capture(owner, "/order", lambda: constructed.append(True) or {}, receipt=receipt)
    assert constructed == observations == []


def test_unknown_receipt_generation_does_not_borrow_a_later_generation():
    observations = []
    owner = SimpleNamespace(_replay_observer=lambda topic, data: observations.append((topic, data)))
    receipt = capture_receipt(owner)
    owner._replay_sync_generation = 9
    ctp_replay.capture(owner, "/order", lambda: {}, receipt=receipt)
    assert observations == [("/order", {"sync_generation": None})]


def test_normalization_error_uses_the_original_receipt():
    old, current = [], []
    owner = SimpleNamespace(_replay_observer=lambda topic, data: old.append((topic, data)), _replay_sync_generation=1)
    receipt = capture_receipt(owner)
    owner._replay_observer = lambda topic, data: current.append((topic, data))
    owner._replay_sync_generation = 2

    def fail():
        raise ValueError("isolated old-frame normalization error")

    ctp_replay.capture(owner, "/order", fail, receipt=receipt)
    assert current == []
    assert len(old) == 1 and old[0][0] == "/connection"
    assert old[0][1]["sync_generation"] == 1
