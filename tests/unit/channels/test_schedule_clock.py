"""渠道市场钟与节奏落点。"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from axile.channels.schedule_clock import (
    CN_FUTURES_WINDOWS,
    CN_STOCK_WINDOWS,
    bar_windows,
    close_lead_hhmm,
    day_windows,
    is_within_schedule_windows,
    last_tradable_hhmm,
    overnight_windows,
    rhythm_hhmm,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 9, 18, 11, 29, tzinfo=_SHANGHAI), True),
        (datetime(2026, 9, 18, 11, 30, tzinfo=_SHANGHAI), False),
        (datetime(2026, 9, 18, 12, 0, tzinfo=_SHANGHAI), False),
        (datetime(2026, 9, 18, 13, 0, tzinfo=_SHANGHAI), True),
        (datetime(2026, 9, 18, 10, 20, tzinfo=_SHANGHAI), True),
        (datetime(2026, 9, 18, 15, 14, tzinfo=_SHANGHAI), True),
        (datetime(2026, 9, 18, 15, 15, tzinfo=_SHANGHAI), False),
        (datetime(2026, 9, 18, 21, 0, tzinfo=_SHANGHAI), True),
        (datetime(2026, 9, 18, 20, 34, tzinfo=_SHANGHAI), False),
    ],
)
def test_futures_clock_punches_lunch_and_keeps_tea_break(moment: datetime, expected: bool) -> None:
    assert is_within_schedule_windows(moment, CN_FUTURES_WINDOWS) is expected


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 9, 18, 9, 14, tzinfo=_SHANGHAI), False),
        (datetime(2026, 9, 18, 9, 15, tzinfo=_SHANGHAI), True),
        (datetime(2026, 9, 18, 9, 20, tzinfo=_SHANGHAI), True),
        (datetime(2026, 9, 18, 9, 24, tzinfo=_SHANGHAI), True),
        (datetime(2026, 9, 18, 9, 25, tzinfo=_SHANGHAI), False),
        (datetime(2026, 9, 18, 9, 29, tzinfo=_SHANGHAI), False),
        (datetime(2026, 9, 18, 9, 30, tzinfo=_SHANGHAI), True),
        (datetime(2026, 9, 18, 11, 30, tzinfo=_SHANGHAI), False),
        (datetime(2026, 9, 18, 12, 0, tzinfo=_SHANGHAI), False),
        (datetime(2026, 9, 18, 13, 0, tzinfo=_SHANGHAI), True),
        (datetime(2026, 9, 18, 15, 0, tzinfo=_SHANGHAI), False),
        (datetime(2026, 9, 18, 9, 0, tzinfo=_SHANGHAI), False),
    ],
)
def test_stock_clock_matches_a_share_sessions(moment: datetime, expected: bool) -> None:
    assert is_within_schedule_windows(moment, CN_STOCK_WINDOWS) is expected


def test_empty_windows_are_always_open() -> None:
    assert is_within_schedule_windows(datetime(2026, 9, 18, 12, 0, tzinfo=_SHANGHAI), ()) is True


def test_stock_m15_clamps_session_close_and_keeps_lunch_out() -> None:
    times = rhythm_hhmm(day_windows(CN_STOCK_WINDOWS), 15)
    assert times[:3] == ["09:15", "09:30", "09:45"]
    assert "09:25" not in times
    assert "11:15" in times
    assert "11:29" in times
    assert "11:30" not in times
    assert "13:00" in times
    assert "13:15" in times
    assert "14:59" in times
    assert "15:00" not in times


def test_futures_m15_fires_at_session_open() -> None:
    bars = bar_windows("cn_futures", day_windows(CN_FUTURES_WINDOWS))
    times = rhythm_hhmm(bars, 15, clock_windows=day_windows(CN_FUTURES_WINDOWS))
    assert times[:4] == ["09:00", "09:15", "09:30", "09:45"]
    assert "11:29" in times
    assert "11:30" not in times
    assert "13:00" in times
    assert "13:15" in times
    assert "13:30" in times
    assert "15:00" in times
    assert "15:14" not in times


def test_replenish_offsets_do_not_cross_lunch() -> None:
    times = rhythm_hhmm(day_windows(CN_STOCK_WINDOWS), 15, offsets=(0, 1, 2, 3, 4))
    assert "11:19" in times
    assert "11:30" not in times
    assert "11:31" not in times
    assert "11:33" not in times


def test_stock_m120_uses_open_and_last_open_minute() -> None:
    assert rhythm_hhmm(day_windows(CN_STOCK_WINDOWS), 120) == ["09:15", "09:30", "11:29", "13:00", "14:59"]


def test_night_close_is_last_open_minute() -> None:
    night = overnight_windows(CN_FUTURES_WINDOWS)
    assert last_tradable_hhmm(*night[0]) == "02:29"
    assert close_lead_hhmm("13:00", "15:00") == "14:55"
    assert close_lead_hhmm(*night[0]) == "02:25"
    assert rhythm_hhmm(night, 15)[0] == "21:00"
    assert rhythm_hhmm(night, 15)[-1] == "02:29"
    assert rhythm_hhmm(night, 60)[0] == "21:00"
    assert "02:30" not in rhythm_hhmm(night, 60)
