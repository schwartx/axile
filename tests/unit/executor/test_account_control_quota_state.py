"""账户操作共享额度，审计增量独立，跨上下文、跨桶及并发均不超额。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Event, RLock
from zoneinfo import ZoneInfo

import pytest

from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.exceptions import AccountControlBlockedError
from axile.executor.account_control.guard import AccountControlGuard, resolve_control_window
from axile.executor.account_control.models import AccountControlBucketType, AccountControlRule
from axile.executor.account_control.presets import resolve_account_control_policy
from axile.executor.account_control.quota_state import AccountControlQuotaState
from axile.executor.account_control.snapshot import AccountControlCounterSnapshot

BASE = datetime(2026, 10, 15, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def guard(state, name, *, baseline=None, now=None, group_rule=None, account_limit=None, symbol_limit=None, sleep=None):
    policy = resolve_account_control_policy("ctp")
    for operation in policy.operations.values():
        operation.account.min_interval_ms = None
        operation.symbol = None
    scope = policy.groups["ctp_td_global"]
    scope.min_interval_ms = None
    if group_rule is not None:
        setattr(scope, group_rule[0], AccountControlRule(limit=group_rule[1], on_trigger="block"))
    if account_limit is not None:
        policy.operations["query_instruments"].account.per_day = AccountControlRule(
            limit=account_limit, on_trigger="block"
        )
    if symbol_limit is not None:
        policy.operations["place_order"].symbol = policy.operations["place_order"].account.model_copy(deep=True)
        policy.operations["place_order"].symbol.per_day = AccountControlRule(limit=symbol_limit, on_trigger="block")
    current = AccountControlGuard(
        account_id=1,
        execution_id=name,
        channel=TradeChannel.CTP,
        policy=policy,
        baseline=baseline or AccountControlCounterSnapshot(),
        clock=lambda: now[0] if now is not None else BASE,
        sleep=sleep,
    )
    current.share_quota_state(state)
    return current


def waiter_sleep(waiting, release):
    def sleep(_seconds):
        waiting.set()
        assert release.wait(2)

    return sleep


@pytest.mark.parametrize("rule", ["per_minute", "per_day"])
def test_group_limit_counts_unflushed_operations_from_other_contexts(rule):
    state = AccountControlQuotaState()
    execution = guard(state, "execution", group_rule=(rule, 1))
    catalog = guard(state, "catalog", group_rule=(rule, 1))
    execution.begin_operation("query_orders").record_outcome("fetched")
    with pytest.raises(AccountControlBlockedError):
        catalog.begin_operation("query_instruments")
    assert [event.operation for event in execution.flush_records()[1]] == ["query_orders"]
    deltas, events = catalog.flush_records()
    assert deltas == []
    assert len(events) == 1 and not events[0].counted


def test_account_limit_is_atomic_across_concurrent_guards():
    state = AccountControlQuotaState()
    guards = [guard(state, str(index), account_limit=1) for index in range(8)]

    def attempt(current):
        try:
            current.begin_operation("query_instruments").record_outcome("fetched")
        except AccountControlBlockedError:
            return False
        return True

    with ThreadPoolExecutor(8) as pool:
        assert sum(pool.map(attempt, guards)) == 1
    assert state.wait_queue == [] and state.monitor_waits == {}
    assert sum(event.counted for current in guards for event in current.flush_records()[1]) == 1


def test_symbol_limit_does_not_mix_other_symbols_or_other_contexts():
    state = AccountControlQuotaState()
    execution = guard(state, "execution", symbol_limit=1)
    manual = guard(state, "manual", symbol_limit=1)
    execution.begin_operation("place_order", symbol="rb2701")
    with pytest.raises(AccountControlBlockedError):
        manual.begin_operation("place_order", symbol="rb2701")
    manual.begin_operation("place_order", symbol="ag2701")
    assert [event.counted for event in manual.flush_records()[1]] == [False, True]


def test_reloading_committed_baseline_keeps_unflushed_usage_without_double_counting():
    state = AccountControlQuotaState()
    execution = guard(state, "execution", group_rule=("per_day", 4))
    catalog = guard(state, "catalog", group_rule=("per_day", 4))
    execution.begin_operation("query_orders")
    catalog.begin_operation("query_instruments")
    window = resolve_control_window("Asia/Shanghai", now=BASE)
    baseline = AccountControlCounterSnapshot()
    baseline.add(
        bucket_type=AccountControlBucketType.DAY,
        bucket_start=window.day_bucket_start,
        operation="query_instruments",
        delta_count=1,
    )
    next_catalog = guard(state, "next-catalog", baseline=baseline, group_rule=("per_day", 4))
    next_catalog.begin_operation("query_instruments")
    execution.begin_operation("query_positions")
    with pytest.raises(AccountControlBlockedError):
        next_catalog.begin_operation("query_instruments")
    for current, expected in ((execution, 2), (catalog, 1), (next_catalog, 1)):
        deltas, _ = current.flush_records()
        assert sum(row.delta_count for row in deltas if row.bucket_type == AccountControlBucketType.DAY) == expected
    snapshot = execution.monitor_snapshot()
    group = next(row for row in snapshot["rules"] if row["id"] == "group:ctp_td_global:per_day")
    assert group["value"] == 4


@pytest.mark.parametrize("rule,advance", [("per_minute", timedelta(minutes=1)), ("per_day", timedelta(days=1))])
def test_shared_quota_resets_by_bucket_and_keeps_original_audit(rule, advance):
    state = AccountControlQuotaState()
    now = [BASE]
    execution = guard(state, "execution", now=now, group_rule=(rule, 1))
    catalog = guard(state, "catalog", now=now, group_rule=(rule, 1))
    execution.begin_operation("query_orders")
    with pytest.raises(AccountControlBlockedError):
        catalog.begin_operation("query_instruments")
    now[0] += advance
    catalog.begin_operation("query_instruments")
    with pytest.raises(AccountControlBlockedError):
        execution.begin_operation("query_positions")
    assert sum(event.counted for event in execution.flush_records()[1]) == 1
    assert sum(event.counted for event in catalog.flush_records()[1]) == 1


def test_higher_priority_cancel_precedes_catalog_waiter_across_guards():
    state = AccountControlQuotaState()
    now = [BASE]
    state.group_timestamps["ctp_td_global",] = int(BASE.timestamp() * 1000)
    query_waiting, cancel_waiting, release = Event(), Event(), Event()

    catalog = guard(state, "catalog", now=now, sleep=waiter_sleep(query_waiting, release))
    manual = guard(state, "manual", now=now, sleep=waiter_sleep(cancel_waiting, release))
    for current in (catalog, manual):
        current.policy.groups["ctp_td_global"].min_interval_ms = AccountControlRule(limit=1000, on_trigger="wait")
    sent = []

    def send(current, operation):
        current.begin_operation(operation).record_outcome("accepted")
        sent.append(operation)

    with ThreadPoolExecutor(2) as pool:
        query = pool.submit(send, catalog, "query_instruments")
        cancel = None
        try:
            assert query_waiting.wait(1)
            cancel = pool.submit(send, manual, "cancel_order_ctp")
            assert cancel_waiting.wait(1)
            now[0] += timedelta(seconds=1)
            release.set()
            cancel.result(1)
            now[0] += timedelta(seconds=1)
            query.result(1)
        finally:
            now[0] += timedelta(seconds=10)
            release.set()
    assert sent == ["cancel_order_ctp", "query_instruments"]
    assert state.wait_queue == [] and state.monitor_waits == {}


def test_canceling_one_context_removes_only_its_own_waiter():
    state = AccountControlQuotaState()
    now = [BASE]
    first_waiting, second_waiting, release, cancelled = Event(), Event(), Event(), Event()
    first = guard(state, "cancelled", now=now, group_rule=("per_minute", 1), sleep=waiter_sleep(first_waiting, release))
    second = guard(
        state, "surviving", now=now, group_rule=("per_minute", 1), sleep=waiter_sleep(second_waiting, release)
    )
    for current in (first, second):
        current.policy.groups["ctp_td_global"].per_minute.on_trigger = "wait"
    first.begin_operation("query_orders")

    def cancel(_symbol):
        if cancelled.is_set():
            raise RuntimeError("cancelled")

    first.set_termination_checkpoint(cancel)
    with ThreadPoolExecutor(2) as pool:
        ending = pool.submit(first.begin_operation, "query_instruments")
        try:
            assert first_waiting.wait(1)
            surviving = pool.submit(second.begin_operation, "query_positions")
            assert second_waiting.wait(1)
            cancelled.set()
            release.set()
            with pytest.raises(RuntimeError, match="cancelled"):
                ending.result(1)
            with state.lock:
                assert [request.operation for request in state.wait_queue] == ["query_positions"]
            now[0] += timedelta(minutes=1)
            surviving.result(1).record_outcome("fetched")
        finally:
            now[0] += timedelta(minutes=2)
            release.set()
    assert state.wait_queue == [] and state.monitor_waits == {}
    assert len(first.flush_records()[1]) == len(second.flush_records()[1]) == 1


def test_monitor_uses_a_frozen_copy_of_shared_usage(monkeypatch):
    from axile.executor.account_control import monitor

    state = AccountControlQuotaState()
    execution = guard(state, "execution")
    catalog = guard(state, "catalog")
    execution.begin_operation("query_orders")
    render = monitor.render_guard_snapshot

    def concurrent_render(frozen):
        catalog.begin_operation("query_instruments")
        return render(frozen)

    monkeypatch.setattr(monitor, "render_guard_snapshot", concurrent_render)
    frame = execution.monitor_snapshot()
    group = next(row for row in frame["rules"] if row["id"] == "group:ctp_td_global:per_day")
    assert group["value"] == 1
    monkeypatch.setattr(monitor, "render_guard_snapshot", render)
    frame = execution.monitor_snapshot()
    group = next(row for row in frame["rules"] if row["id"] == "group:ctp_td_global:per_day")
    assert group["value"] == 2


def test_shared_quota_cannot_be_rebound_to_another_account_or_after_first_operation():
    state = AccountControlQuotaState()
    first = guard(state, "first")
    other = AccountControlGuard(
        account_id=2,
        execution_id="other",
        channel=TradeChannel.CTP,
        policy=resolve_account_control_policy("ctp"),
        baseline=AccountControlCounterSnapshot(),
    )
    with pytest.raises(ValueError, match="跨账户"):
        other.share_quota_state(state)
    first.begin_operation("query_orders")
    with pytest.raises(ValueError, match="首个操作前"):
        first.share_quota_state(AccountControlQuotaState())
    with pytest.raises(ValueError, match="仅共享发送间隔"):
        first.share_group_timestamps({}, RLock())
