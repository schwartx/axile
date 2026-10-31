"""共享时钟在仿真时间下的时间戳与等待行为。"""

import asyncio
import os
import subprocess
import sys
import time

import pytest

from axile.executor.algorithms.utils import clock as clock_module


class AdvancingClock:
    """等待时推进时间的轻量时钟。"""

    def __init__(self, timestamp: float) -> None:
        self.timestamp = timestamp

    def time(self) -> float:
        return self.timestamp

    def sleep(self, seconds: float) -> None:
        self.timestamp += seconds


def test_virtual_deadlines_and_waits_advance_without_wall_clock(monkeypatch) -> None:
    clock = AdvancingClock(1000)
    monkeypatch.setattr(clock_module, "_default_clock", clock)

    deadline = clock_module.clock_monotonic() + 5
    asyncio.run(clock_module.clock_async_sleep(5))

    assert clock_module.clock_monotonic() == deadline
    assert clock_module.clock_now_ms() == 1_005_000


def test_integer_microseconds_preserve_negative_epoch_and_source_precision() -> None:
    class IntegerClock(clock_module.RealClock):
        def time_us(self) -> int:
            return -1

        def time(self) -> float:
            raise AssertionError("integer source must not pass through float seconds")

    clock = IntegerClock()
    assert clock_module.clock_now_us(clock) == -1
    assert (
        clock_module.clock_now(clock, tz=__import__("datetime").UTC).isoformat() == "1969-12-31T23:59:59.999999+00:00"
    )


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="requires POSIX local timezone")
def test_local_clock_preserves_dst_fold() -> None:
    program = """
from axile.executor.algorithms.utils.clock import RealClock, clock_now
class IntegerClock(RealClock):
    def time_us(self):
        return 1762065000123456
now = clock_now(IntegerClock())
assert now.isoformat() == '2025-11-02T01:30:00.123456'
assert now.fold == 1
assert now.timestamp() == 1762065000.123456
"""
    subprocess.run(
        [sys.executable, "-B", "-c", program], env={**os.environ, "TZ": "America/New_York"}, check=True, timeout=10
    )
