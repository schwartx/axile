"""CTP 流式响应、初始化进度与复制失败边界。"""

import threading
from types import SimpleNamespace

from axile.common.trade_channel import TradeChannel
from axile.executor.ctp.ctp_execute import CTPExecutor, _PendingQuery
from axile.executor.ctp_query_wait import QueryIdleClock


def test_idle_timeout_excludes_copy_time_and_accepts_long_stream():
    now = [0.0]
    idle = QueryIdleClock(lambda: now[0])
    done = threading.Event()
    now[0] = 100  # 模拟发送前限流等待。
    idle.sent()
    for _ in range(10):
        now[0] += 14
        assert not idle.timed_out(15, done)
        assert idle.enter()
        now[0] += 100  # 本地复制再慢也不是网络空闲。
        assert not idle.timed_out(15, done)
        idle.leave()
    assert idle.processing_seconds == 1000
    assert idle.max_idle == 14
    now[0] += 15
    assert idle.timed_out(15, done)
    assert not idle.enter()


def test_first_callback_timeout_and_completed_last_callback():
    now = [0.0]
    done = threading.Event()
    idle = QueryIdleClock(lambda: now[0])
    now[0] = 15
    assert idle.timed_out(15, done)
    idle = QueryIdleClock(lambda: now[0])
    assert idle.enter()
    done.set()
    idle.leave()
    now[0] += 100
    assert not idle.timed_out(15, done)


def test_late_last_callback_cannot_win_against_timeout():
    now = [0.0]
    idle = QueryIdleClock(lambda: now[0])
    now[0] = 16
    assert not idle.enter(15)
    assert idle.timed_out(15, threading.Event())


def test_record_progress_is_throttled_and_ready_executor_stops_reporting(monkeypatch):
    now = [10.0]
    monkeypatch.setattr("axile.executor.ctp.ctp_execute.time.monotonic", lambda: now[0])
    executor = CTPExecutor(TradeChannel.CTP)
    phases = []
    executor.set_instrument_catalog_provider(SimpleNamespace(progress=phases.append))
    for _ in range(20):
        executor._catalog_progress("对账记录", throttled=True)
        now[0] += 0.25
    assert len(phases) == 5
    executor._ready = True
    now[0] += 60
    executor._catalog_progress("对账记录", throttled=True)
    assert len(phases) == 5


def test_copy_exception_finishes_query_and_ignores_late_success(monkeypatch):
    executor = CTPExecutor(TradeChannel.CTP)
    pending = _PendingQuery([], threading.Event(), idle=QueryIdleClock())
    executor._pending_queries[7] = pending

    def broken(_row):
        raise ValueError("copy failed")

    monkeypatch.setattr("axile.executor.ctp.ctp_execute._copy_native_row", broken)
    executor._query_response(SimpleNamespace(InstrumentID="ag2612"), None, 7, False)
    assert pending.done.is_set()
    assert str(pending.error) == "copy failed"
    assert pending.idle.processing == 0
    executor._query_response(None, None, 7, True)
    assert str(pending.error) == "copy failed"
