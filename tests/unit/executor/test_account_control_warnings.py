"""额度预警使用真实控制计数，保持额度保护与统计口径。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.exceptions import AccountControlBlockedError
from axile.executor.account_control.guard import AccountControlGuard, resolve_control_window
from axile.executor.account_control.models import AccountControlBucketType, AccountControlOverride
from axile.executor.account_control.presets import resolve_account_control_policy
from axile.executor.account_control.snapshot import AccountControlCounterSnapshot

BASE = datetime(2026, 10, 15, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def guard(*, limit=5, percent=80, baseline=None, now=None, operation="place_order"):
    override = AccountControlOverride.model_validate(
        {
            "operations": {
                operation: {
                    "daily_warning_percent": percent,
                    "account": {
                        "per_day": {"unlimited": True} if limit is None else {"limit": limit, "on_trigger": "block"},
                    },
                }
            }
        }
    )
    policy = resolve_account_control_policy("default", override)
    for item in policy.operations.values():
        item.account.min_interval_ms = None
        item.account.per_minute = None
        item.symbol = None
    return AccountControlGuard(
        account_id=1,
        execution_id="warning-test",
        channel=TradeChannel.CTP,
        policy=policy,
        baseline=baseline or AccountControlCounterSnapshot(),
        clock=lambda: now[0] if now else BASE,
    )


@pytest.mark.parametrize("percent", [0, 100, 80.5, True, "80"])
def test_invalid_warning_percent_is_rejected(percent):
    with pytest.raises(ValidationError):
        guard(percent=percent)


def test_threshold_freezes_exact_value_and_keeps_hard_quota():
    current = guard()
    for _ in range(3):
        current.begin_operation("place_order").record_outcome("broker_sync_rejected")
    assert current.monitor_snapshot()["quota_warnings"] == []
    current.begin_operation("place_order").record_outcome("submitted")
    warning = current.monitor_snapshot()["quota_warnings"][0]
    assert (warning["used"], warning["threshold_count"], warning["remaining"]) == (4, 4, 1)
    current.begin_operation("place_order").record_outcome("broker_rejected")
    with pytest.raises(AccountControlBlockedError):
        current.begin_operation("place_order")
    assert current.monitor_snapshot()["quota_warnings"] == [warning]
    deltas, events = current.flush_records()
    assert sum(event.counted for event in events) == 5
    assert sum("quota_warning" in event.metadata for event in events) == 1
    assert sum(row.delta_count for row in deltas if row.bucket_type == AccountControlBucketType.DAY) == 5


@pytest.mark.parametrize("limit,percent", [(None, 80), (0, 80), (5, None)])
def test_disabled_or_inapplicable_warning_creates_no_event(limit, percent):
    current = guard(limit=limit, percent=percent)
    if limit == 0:
        with pytest.raises(AccountControlBlockedError):
            current.begin_operation("place_order")
    else:
        for _ in range(5):
            current.begin_operation("place_order")
    assert current.monitor_snapshot()["quota_warnings"] == []


def test_rounding_cancellation_and_concurrent_attempts():
    current = guard(limit=7, percent=80, operation="cancel_order")
    with ThreadPoolExecutor(max_workers=7) as pool:
        list(pool.map(lambda _: current.begin_operation("cancel_order"), range(7)))
    warning = current.monitor_snapshot()["quota_warnings"]
    assert len(warning) == 1
    assert warning[0]["operation"] == "cancel_order"
    assert warning[0]["threshold_count"] == warning[0]["used"] == 6


def test_date_rollover_keeps_each_original_warning_after_more_than_100_events():
    now = [BASE]
    current = guard(limit=200, percent=2, now=now)
    for _ in range(110):
        current.begin_operation("place_order")
    now[0] += timedelta(days=1)
    for _ in range(5):
        current.begin_operation("place_order")
    warnings = current.monitor_snapshot()["quota_warnings"]
    assert len(warnings) == 2
    assert [warning["used"] for warning in warnings] == [4, 4]
    assert [warning["control_date"] for warning in warnings] == ["2026-10-15", "2026-10-16"]


def test_loaded_usage_already_at_limit_is_reported_without_increment():
    baseline = AccountControlCounterSnapshot()
    baseline.add(
        bucket_type=AccountControlBucketType.DAY,
        bucket_start=resolve_control_window("Asia/Shanghai", now=BASE).day_bucket_start,
        operation="place_order",
        delta_count=5,
    )
    current = guard(baseline=baseline)
    with pytest.raises(AccountControlBlockedError):
        current.begin_operation("place_order")
    assert current.monitor_snapshot()["quota_warnings"][0]["used"] == 5
    assert current.flush_records()[0] == []


def test_caller_cannot_inject_warning_metadata_or_enable_native_cancel_warning():
    current = guard()
    current.begin_operation("place_order", metadata={"quota_warning": {"used": 999}})
    assert "quota_warning" not in current.flush_records()[1][0].metadata
    with pytest.raises(ValidationError, match="仅支持"):
        guard(operation="cancel_order_ctp")
