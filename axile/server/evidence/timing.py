"""分开上下文时间与计数桶的逐笔时间证据，不从发现来源推断材料缺失。"""

from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from axile.server.evidence.models import ExecutionEvidence, parse_time


def context_time_known(item: ExecutionEvidence) -> bool:
    """意图、结果、事件或会话已有时间时，上下文不只依赖计数桶。"""
    timed = [*(item.events), item.intent or {}, item.record or {}]
    if any(
        parse_time(row.get(key)) is not None
        for row in timed
        for key in ("created_at", "started_at", "finished_at", "ts_local_created")
    ):
        return True
    return any(
        isinstance(row.get(key), int)
        for row in [*item.controls, *item.sessions]
        for key in ("occurred_at_ms", "started_at_ms")
    )


def selection_time_evidence(item: ExecutionEvidence) -> str:
    """保留日期选入的精度，与整次执行材料及计数对账分别表达。"""
    reasons = set(item.selection_reasons)
    if reasons & {"explicit_execution", "explicit_execution_ids"}:
        return "explicit_id"
    if reasons & {
        "intent_overlaps_window",
        "record_created_in_window",
        "execution_event_in_window",
        "control_event_in_window",
        "control_session_overlaps_window",
    }:
        return "recorded_activity"
    if "counter_bucket_context_time_assumed" in reasons:
        return "bucket_overlap_assumed"
    return "unknown"


def _event_times(item: ExecutionEvidence) -> dict[tuple, list[int]]:
    """按账户／合约、操作与来源索引逐笔时间，避免逐桶扫描全部事件。"""
    times = defaultdict(list)
    seen = set()
    for row in item.controls:
        uid = row.get("event_uid")
        stamp = row.get("occurred_at_ms")
        if not row.get("counted") or not uid or uid in seen or not isinstance(stamp, int):
            continue
        seen.add(uid)
        base = (row.get("control_date"), row.get("operation"), row.get("source", "unknown"))
        times[(*base, "account", None)].append(stamp)
        if row.get("symbol") is not None:
            times[(*base, "symbol", row["symbol"])].append(stamp)
    for values in times.values():
        values.sort()
    return times


def _bucket_bounds(item: ExecutionEvidence, raw_start: object, bucket: str) -> tuple[float, float] | None:
    """本地桶必须有会话时区依据；自然日用该时区计算夏令时边界。"""
    if not isinstance(raw_start, str):
        return None
    try:
        start = datetime.fromisoformat(raw_start)
        zones = {row.get("timezone") for row in item.sessions if row.get("timezone")}
        zone = ZoneInfo(next(iter(zones))) if len(zones) == 1 else None
        if start.tzinfo is None:
            if zone is None:
                return None
            start = start.replace(tzinfo=zone)
        elif zone is not None:
            start = start.astimezone(zone)
        end = start + (timedelta(days=1) if bucket == "day" else timedelta(minutes=1))
        return start.timestamp() * 1000, end.timestamp() * 1000
    except (ValueError, TypeError, ZoneInfoNotFoundError, OverflowError, OSError):
        return None


def counter_time_evidence(item: ExecutionEvidence) -> dict[str, Any]:
    """对账桶增量与已保存事件；匹配不等于柜台回报完整或交易成功。"""
    if not item.counters:
        return {"status": "not_recorded", "basis": "persisted_control_events", "unmatched_buckets": 0}
    counts = Counter()
    for row in item.counters:
        key = (
            row.get("control_date"),
            row.get("operation"),
            row.get("source", "unknown"),
            row.get("scope_type"),
            row.get("symbol"),
            row.get("bucket_type"),
            row.get("bucket_start"),
        )
        counts[key] += row.get("delta_count", 0)
    times = _event_times(item)
    unmatched = 0
    for key, count in counts.items():
        base, bucket, raw_start = key[:5], key[5], key[6]
        bounds = _bucket_bounds(item, raw_start, bucket)
        if bounds is None or bucket not in {"day", "minute"} or base[3] not in {"account", "symbol"}:
            unmatched += 1
            continue
        values = times.get(base, [])
        actual = bisect_left(values, bounds[1]) - bisect_left(values, bounds[0])
        unmatched += actual != count
    return {
        "status": "unknown" if unmatched else "reconciled",
        "basis": "persisted_control_events",
        "unmatched_buckets": unmatched,
    }
