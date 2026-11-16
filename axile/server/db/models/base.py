"""服务端数据库模型共享基础定义."""

from __future__ import annotations

import uuid
from datetime import UTC

from axile.executor.algorithms.utils.clock import clock_now, clock_now_ms


def now_str() -> str:
    """返回保留微秒的 UTC ISO 字符串。"""
    return clock_now(tz=UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def now_ms() -> int:
    """返回当前时间的 Unix 毫秒时间戳."""
    return clock_now_ms()


def new_execution_id() -> str:
    """返回一个随机 execution 标识."""
    return uuid.uuid4().hex
