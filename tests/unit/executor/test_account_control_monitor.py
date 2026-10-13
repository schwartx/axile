"""监控读取不得改变放行与统计口径。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Event
from zoneinfo import ZoneInfo

import pytest

from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.exceptions import AccountControlBlockedError
from axile.executor.account_control.guard import AccountControlGuard, resolve_control_window
from axile.executor.account_control.models import (
    AccountControlBucketType,
    AccountControlOverride,
    AccountControlTriggerBehavior,
)
from axile.executor.account_control.presets import resolve_account_control_policy
from axile.executor.account_control.snapshot import AccountControlCounterSnapshot


def make_guard(*, baseline_count=120, limit=500, clock=None, sleep=None):
    clock = clock or (lambda: datetime(2026, 10, 13, 10, 30, 0, tzinfo=ZoneInfo("Asia/Shanghai")))
    window = resolve_control_window("Asia/Shanghai", now=clock())
    baseline = AccountControlCounterSnapshot()
    baseline.add(
        bucket_type=AccountControlBucketType.DAY,
        bucket_start=window.day_bucket_start,
        operation="place_order",
        delta_count=baseline_count,
    )
    policy = resolve_account_control_policy(
        "default",
        AccountControlOverride.model_validate(
            {"operations": {"place_order": {"account": {"per_day": {"limit": limit, "on_trigger": "block"}}}}}
        ),
    )
    policy.operations["place_order"].account.min_interval_ms = None
    policy.operations["place_order"].symbol.min_interval_ms = None
    return AccountControlGuard(
        account_id=1,
        execution_id="exec-monitor",
        channel=TradeChannel.CTP,
        policy=policy,
        baseline=baseline,
        clock=clock,
        sleep=sleep,
    )


def used(frame):
    return next(m["value"] for m in frame["metrics"] if m["operation"] == "place_order")


def test_snapshot_replaces_values_and_never_consumes_quota():
    guard = make_guard()
    for _ in range(3):
        guard.begin_operation("place_order", symbol="rb2610").record_outcome("submitted")
    before = guard.flush_records()
    with ThreadPoolExecutor(max_workers=8) as pool:
        frames = list(pool.map(lambda _: guard.monitor_snapshot(), range(40)))
    assert all(used(frame) == 123 for frame in frames)
    assert guard.flush_records() == before
    assert len({frame["seq"] for frame in frames}) == 40
    assert all(frame["waiting_total"] == 0 for frame in frames)


def test_blocked_and_downstream_failure_keep_local_allowance_semantics():
    guard = make_guard(limit=121)
    guard.begin_operation("place_order").record_outcome("downstream_error")
    with pytest.raises(AccountControlBlockedError):
        guard.begin_operation("place_order")
    frame = guard.monitor_snapshot()
    assert used(frame) == 121
    assert frame["waiting_total"] == 0
    assert frame["records"][0]["decision"] == "blocked"
    assert frame["records"][1]["outcome"] == "downstream_error"


def test_rebinding_same_execution_has_distinct_persistence_uids():
    first, second = make_guard(), make_guard()
    for guard in (first, second):
        guard.begin_operation("place_order")
    a_delta, a_events = first.flush_records()
    b_delta, b_events = second.flush_records()
    assert {d.delta_uid for d in a_delta}.isdisjoint(d.delta_uid for d in b_delta)
    assert a_events[0].event_uid != b_events[0].event_uid


def test_minute_and_natural_day_switch_do_not_reuse_previous_bucket():
    current = [datetime(2026, 10, 13, 23, 59, 59, tzinfo=ZoneInfo("Asia/Shanghai"))]
    guard = make_guard(clock=lambda: current[0])
    guard.begin_operation("place_order")
    assert used(guard.monitor_snapshot()) == 121
    current[0] = datetime(2026, 10, 14, 0, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert used(guard.monitor_snapshot()) == 0
    guard.begin_operation("place_order")
    assert used(guard.monitor_snapshot()) == 1


def test_async_reject_before_sync_return_is_not_overwritten():
    guard = make_guard()
    attempt = guard.begin_operation("place_order")
    indices = guard.active_attempt_indices()
    assert len(indices) == 1
    guard.update_event_outcome(indices[0], outcome="broker_rejected", metadata={"error_id": 31})
    attempt.record_outcome("submitted", metadata={"order_id": "order-1"})
    frame = guard.monitor_snapshot()
    assert frame["records"][0]["outcome"] == "broker_rejected"
    assert frame["records"][0]["metadata"]["order_id"] == "order-1"
    assert used(frame) == 121
    assert guard.active_attempt_indices() == ()


def test_waiting_snapshot_has_identity_and_cleans_up_on_termination():
    entered, release = Event(), Event()

    def sleep(_seconds):
        entered.set()
        assert release.wait(3)
        raise RuntimeError("terminated")

    guard = make_guard(sleep=sleep)
    guard.policy.operations["place_order"].account.per_day.on_trigger = AccountControlTriggerBehavior.WAIT
    guard.policy.operations["place_order"].account.per_day.limit = 120
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(guard.begin_operation, "place_order", symbol="rb2610")
        try:
            assert entered.wait(3)
            frame = guard.monitor_snapshot()
            assert used(frame) == 120
            assert frame["waiting_total"] == 1
            assert frame["waiting"][0]["kind"] == "quota"
            assert frame["waiting"][0]["symbol"] == "rb2610"
            assert frame["waiting"][0]["hits"][0]["rule_kind"] == "per_day"
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="terminated"):
            future.result(timeout=3)
    assert guard.monitor_snapshot()["waiting_total"] == 0


def test_snapshot_uses_one_time_context_for_all_rules():
    calls = []

    def clock():
        calls.append(True)
        return datetime(2026, 10, 13, 23, 59, 59, tzinfo=ZoneInfo("Asia/Shanghai"))

    guard = make_guard(clock=clock)
    calls.clear()
    frame = guard.monitor_snapshot()
    assert len(calls) == 1
    assert frame["control_date"] == "2026-10-13"
    assert used(frame) == 120
