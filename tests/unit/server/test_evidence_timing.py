"""计数时间对账不能混淆作用域、来源、分钟边界或重复回报。"""

from datetime import datetime

import pytest

from axile.server.evidence.models import ExecutionEvidence
from axile.server.evidence.timing import counter_time_evidence

STAMP = int(datetime.fromisoformat("2026-10-15T09:00:01+08:00").timestamp() * 1000)


def evidence(**changes):
    event = dict(
        event_uid="op-1",
        counted=True,
        occurred_at_ms=STAMP,
        control_date="2026-10-15",
        operation="place_order",
        source="algorithm",
        symbol="rb2701",
    )
    bucket = dict(
        control_date="2026-10-15",
        operation="place_order",
        source="algorithm",
        scope_type="symbol",
        symbol="rb2701",
        bucket_type="minute",
        bucket_start="2026-10-15T09:00:00+08:00",
        delta_count=1,
    )
    bucket.update(changes)
    return ExecutionEvidence(key="exec", execution_id="exec", controls=[event], counters=[bucket])


@pytest.mark.parametrize(
    "changes",
    [
        {"symbol": "ag2701"},
        {"source": "manual_cancel"},
        {"operation": "cancel_order"},
        {"control_date": "2026-10-16"},
        {"bucket_start": "2026-10-15T09:01:00+08:00"},
        {"delta_count": 2},
        {"scope_type": "future"},
        {"bucket_start": "unknown"},
    ],
)
def test_unmatched_counter_bucket_remains_unknown(changes):
    report = counter_time_evidence(evidence(**changes))
    assert report["status"] == "unknown"
    assert report["unmatched_buckets"] == 1


def test_duplicate_events_and_account_symbol_buckets_do_not_inflate_counts():
    item = evidence()
    item.controls.append(dict(item.controls[0]))
    item.counters.append({**item.counters[0], "scope_type": "account", "symbol": None})
    item.counters.append({**item.counters[0], "bucket_type": "day", "bucket_start": "2026-10-15T00:00:00+08:00"})
    assert counter_time_evidence(item)["status"] == "reconciled"


@pytest.mark.parametrize("changes", [{"counted": False}, {"occurred_at_ms": None}, {"event_uid": None}])
def test_missing_or_uncounted_event_cannot_supply_counter_time(changes):
    item = evidence()
    item.controls[0].update(changes)
    assert counter_time_evidence(item)["status"] == "unknown"
