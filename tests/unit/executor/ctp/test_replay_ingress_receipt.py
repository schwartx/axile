"""真实 SDK 请求持锁时，SPI 订单帧不能借用等待之后的录制身份。"""

import threading
from types import SimpleNamespace

from tests.unit.executor.ctp.test_replay_native_callbacks import recording as recording

from axile.executor.ctp import replay as ctp_replay
from axile.executor.replay import capture_receipt

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
