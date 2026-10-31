"""
时钟抽象模块.

提供统一的时钟接口，支持真实时钟和仿真时钟。
算法代码通过 Clock 接口获取时间和等待，在仿真环境下可以自动加速。
"""

import asyncio
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Protocol, cast


class Clock(Protocol):
    """
    时钟协议.

    定义时间相关操作的统一接口。生产环境使用 ``RealClock``，
    仿真环境使用 ``SimulatedClock``。
    """

    def time(self) -> float:
        """
        获取当前时间戳.

        Returns
        -------
        float
            当前 Unix 时间戳，单位为秒。
        """
        ...

    def sleep(self, seconds: float) -> None:
        """
        等待指定秒数.

        Parameters
        ----------
        seconds : float
            需要等待的秒数。
        """
        ...

    def event_wait(self, event: threading.Event, timeout: float) -> bool:
        """
        等待事件触发或超时.

        Returns
        -------
        bool
            事件已触发时返回 ``True``，超时时返回 ``False``。

        Parameters
        ----------
        event : threading.Event
            要等待的事件对象。
        timeout : float
            最大等待时长，单位为秒。
        """
        ...


class RealClock:
    """
    真实时钟.

    使用系统时间和真实等待，用于生产环境。
    """

    def time(self) -> float:
        """
        获取当前时间戳.

        Returns
        -------
        float
            当前 Unix 时间戳，单位为秒。
        """
        return time.time()

    def monotonic(self) -> float:
        """返回不受系统时钟校准影响的超时计时值。"""
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        """
        等待指定秒数.

        Parameters
        ----------
        seconds : float
            需要等待的秒数。
        """
        time.sleep(seconds)

    def event_wait(self, event: threading.Event, timeout: float) -> bool:
        """
        等待事件触发或超时.

        Returns
        -------
        bool
            事件已触发时返回 ``True``，超时时返回 ``False``。

        Parameters
        ----------
        event : threading.Event
            要等待的事件对象。
        timeout : float
            最大等待时长，单位为秒。
        """
        return event.wait(timeout=timeout)


# 全局默认时钟
_default_clock: Clock = RealClock()


def _resolve_clock(clock: Clock | None = None) -> Clock:
    """
    解析要使用的时钟实例.

    Parameters
    ----------
    clock : Clock | None, optional
        显式传入的时钟；为空时使用全局默认时钟。

    Returns
    -------
    Clock
        解析后的时钟实例。
    """
    return get_default_clock() if clock is None else clock


def get_default_clock() -> Clock:
    """
    获取默认时钟.

    Returns
    -------
    Clock
        当前设置的默认时钟实例。
    """
    return _default_clock


def set_default_clock(clock: Clock) -> None:
    """
    设置默认时钟.

    Parameters
    ----------
    clock : Clock
        要设置的时钟实例。
    """
    global _default_clock
    _default_clock = clock


def clock_monotonic(clock: Clock | None = None) -> float:
    """读取超时计时值；旧版自定义时钟沿用其仿真时间戳。"""
    resolved = _resolve_clock(clock)
    monotonic = getattr(resolved, "monotonic", None)
    if callable(monotonic):
        return cast(Callable[[], float], monotonic)()
    return resolved.time()


async def clock_async_sleep(seconds: float, clock: Clock | None = None) -> None:
    """异步等待；仿真时推进时钟并让出事件循环。"""
    resolved = _resolve_clock(clock)
    if isinstance(resolved, RealClock):
        await asyncio.sleep(seconds)
        return
    resolved.sleep(seconds)
    await asyncio.sleep(0)


def clock_now_us(clock: Clock | None = None) -> int:
    """读取整数 Unix 微秒；自定义时钟可提供 ``time_us`` 保留源精度。"""
    resolved = _resolve_clock(clock)
    if type(resolved) is RealClock:
        return time.time_ns() // 1_000
    integer_time = getattr(resolved, "time_us", None)
    if callable(integer_time):
        return cast(Callable[[], int], integer_time)()
    # shortcut: 旧自定义时钟仅有浮点秒，需要精确微秒时实现 time_us。
    return round(resolved.time() * 1_000_000)


def clock_now(clock: Clock | None = None, tz: tzinfo | None = None) -> datetime:
    """
    读取当前 ``datetime``.

    Parameters
    ----------
    clock : Clock | None, optional
        显式传入的时钟；为空时使用全局默认时钟。
    tz : tzinfo | None, optional
        目标时区；为空时返回本地时区语义下的 ``datetime``。

    Returns
    -------
    datetime
        基于当前时钟时间戳转换得到的时间对象。
    """
    microseconds = clock_now_us(clock)
    if tz is None:
        seconds, fraction = divmod(microseconds, 1_000_000)
        return datetime.fromtimestamp(seconds).replace(microsecond=fraction)
    current = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=microseconds)
    return current.astimezone(tz)


def clock_now_iso(clock: Clock | None = None, tz: tzinfo | None = None) -> str:
    """
    读取当前 ISO 格式时间字符串.

    Parameters
    ----------
    clock : Clock | None, optional
        显式传入的时钟；为空时使用全局默认时钟。
    tz : tzinfo | None, optional
        目标时区；为空时返回本地时区语义下的字符串。

    Returns
    -------
    str
        当前时间对应的 ISO 8601 字符串。
    """
    return clock_now(clock=clock, tz=tz).isoformat()


def clock_now_ms(clock: Clock | None = None) -> int:
    """
    读取当前毫秒时间戳.

    Parameters
    ----------
    clock : Clock | None, optional
        显式传入的时钟；为空时使用全局默认时钟。

    Returns
    -------
    int
        当前 Unix 时间戳，单位为毫秒。
    """
    return clock_now_us(clock) // 1_000
