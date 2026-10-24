"""账户图表的本地日期和日末权益读取。"""

from __future__ import annotations

import math
from datetime import datetime
from zoneinfo import ZoneInfo

_TIMEZONE = ZoneInfo("Asia/Shanghai")


def local_time(value: str) -> datetime:
    """将有时区和历史无时区时间统一为上海本地时间."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(_TIMEZONE).replace(tzinfo=None)


def _point_fields(point: dict[str, object]) -> tuple[str, float | None]:
    date = str(point.get("date") or "")
    raw = point.get("account_equity")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return date, None
    value = float(raw)
    return date, value if math.isfinite(value) and value > 0 else None


def close_before(points: list[dict[str, object]], day: str) -> float | None:
    """``day`` 之前最近一日末权益；没有更早日则用基准点。"""
    last_before = None
    baseline = None
    for point in points:
        date, equity = _point_fields(point)
        if equity is None:
            continue
        # 日点 date 是日历日；基准点 date 是完整时间，原生回放保留此契约。
        if "T" in date:
            baseline = equity
            continue
        if date < day:
            last_before = equity
    return last_before if last_before is not None else baseline
