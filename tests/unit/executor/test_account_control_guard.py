"""账户控制防护层测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier, BrokenBarrierError, Event, Lock
from zoneinfo import ZoneInfo

import pytest

from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.exceptions import AccountControlBlockedError
from axile.executor.account_control.guard import (
    AccountControlEnforcementDecision,
    AccountControlGuard,
    AccountControlResolvedAttempt,
)
from axile.executor.account_control.models import (
    AccountControlBucketType,
    AccountControlDecision,
    AccountControlOverride,
    AccountControlScopeType,
)
from axile.executor.account_control.presets import resolve_account_control_policy
from axile.executor.account_control.registry import (
    ensure_default_account_control_registry_bootstrapped,
    reset_default_account_control_registry_for_tests,
)
from axile.executor.account_control.snapshot import (
    AccountControlCounterSnapshot,
    AccountControlRecentAllowedSnapshot,
)
from axile.executor.algorithms.utils.clock import get_default_clock, set_default_clock
from tests.unit.executor._account_control_test_support import normalize_account_control_override


@pytest.fixture(autouse=True)
def _seed_account_control_registry() -> None:
    registry = reset_default_account_control_registry_for_tests()
    ensure_default_account_control_registry_bootstrapped()
    registry.freeze()


class _AdvancingClock:
    def __init__(self, start_time: datetime) -> None:
        self._current_ts = start_time.timestamp()
        self.sleep_calls: list[float] = []

    def time(self) -> float:
        return self._current_ts

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self._current_ts += seconds

    def event_wait(self, _event: object, timeout: float) -> bool:
        if timeout > 0:
            self._current_ts += timeout
        return False


def _clock() -> datetime:
    return datetime(2026, 3, 22, 9, 31, 15)


def _clock_from(*moments: datetime):
    sequence = iter(moments)

    def _inner() -> datetime:
        return next(sequence)

    return _inner


def _to_ms(moment: datetime, timezone_name: str = "Asia/Shanghai") -> int:
    tz = ZoneInfo(timezone_name)
    localized = moment.replace(tzinfo=tz) if moment.tzinfo is None else moment.astimezone(tz)
    return int(localized.timestamp() * 1000)


def _override_payload(operations: dict[str, object], **extra: object) -> dict[str, object]:
    return normalize_account_control_override({**extra, "operations": operations})


def test_guard_uses_loaded_baseline_plus_in_memory_deltas() -> None:
    """防护层应只依赖启动时基线和本次 execution 内存增量。"""
    guard = AccountControlGuard(
        account_id=1,
        execution_id="exec-1",
        channel=TradeChannel("external"),
        policy=resolve_account_control_policy(
            "default",
            AccountControlOverride.model_validate(_override_payload({"place_order": {"per_day": 1}})),
        ),
        baseline=AccountControlCounterSnapshot(),
        clock=_clock,
    )

    attempt = guard.begin_operation("place_order", symbol="rb2610")
    attempt.record_outcome("submitted", metadata={"order_id": "oid-1"})

    with pytest.raises(AccountControlBlockedError, match="rb2610"):
        guard.begin_operation("place_order", symbol="rb2610")

    counter_deltas, events = guard.flush_records()

    assert len(events) == 2
    assert events[0].decision == AccountControlDecision.ALLOWED
    assert events[0].counted is True
    assert events[1].decision == AccountControlDecision.BLOCKED
    assert events[1].counted is False
    assert {delta.scope_type for delta in counter_deltas} == {
        AccountControlScopeType.ACCOUNT,
        AccountControlScopeType.SYMBOL,
    }


def test_guard_keeps_its_policy_when_configuration_changes() -> None:
    """新配置只影响后续 guard；放行、阻断及计数不修改现有规则。"""
    override = AccountControlOverride.model_validate(
        {"operations": {"query_account": {"symbol": {"per_day": {"limit": 1, "on_trigger": "block"}}}}}
    )
    policy = resolve_account_control_policy("default", override)
    before = policy.model_dump(mode="json")
    guard = AccountControlGuard(
        account_id=1,
        execution_id="old-config",
        channel=TradeChannel("external"),
        policy=policy,
        baseline=AccountControlCounterSnapshot(),
        clock=_clock,
    )
    guard.begin_operation("query_account", symbol="rb2610")

    override = override.model_dump(mode="json")
    override["operations"]["query_account"]["symbol"]["per_day"]["limit"] = 2
    next_policy = resolve_account_control_policy("default", override)
    next_before = next_policy.model_dump(mode="json")
    next_guard = AccountControlGuard(
        account_id=1,
        execution_id="new-config",
        channel=TradeChannel("external"),
        policy=next_policy,
        baseline=AccountControlCounterSnapshot(),
        clock=_clock,
    )
    with pytest.raises(AccountControlBlockedError):
        guard.begin_operation("query_account", symbol="rb2610")
    next_guard.begin_operation("query_account", symbol="rb2610")
    next_guard.begin_operation("query_account", symbol="rb2610")
    with pytest.raises(AccountControlBlockedError):
        next_guard.begin_operation("query_account", symbol="rb2610")

    assert policy.model_dump(mode="json") == before
    assert next_policy.model_dump(mode="json") == next_before


def test_guard_atomically_checks_and_reserves_concurrent_account_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发请求不能在写入内存增量前同时穿透同一份账户额度。"""
    guard = AccountControlGuard(
        account_id=11,
        execution_id="exec-concurrent-quota",
        channel=TradeChannel("external"),
        policy=resolve_account_control_policy(
            "default",
            AccountControlOverride.model_validate(
                _override_payload(
                    {
                        "query_account": {
                            "account": {
                                "per_day": {"limit": 1, "on_trigger": "block"},
                            }
                        }
                    }
                )
            ),
        ),
        baseline=AccountControlCounterSnapshot(),
        clock=_clock,
    )
    original_resolve = guard._resolve_operation_attempt
    concurrent_resolutions = Barrier(2)

    def resolve_with_old_race_window(*, operation: str, symbol: str | None):
        resolved = original_resolve(operation=operation, symbol=symbol)
        if resolved.decision.kind == "allow":
            try:
                concurrent_resolutions.wait(timeout=0.1)
            except BrokenBarrierError:
                pass
        return resolved

    monkeypatch.setattr(guard, "_resolve_operation_attempt", resolve_with_old_race_window)

    def begin() -> str:
        try:
            attempt = guard.begin_operation("query_account")
        except AccountControlBlockedError:
            return "blocked"
        attempt.record_outcome("fetched")
        return "allowed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [future.result() for future in [pool.submit(begin), pool.submit(begin)]]

    counter_deltas, events = guard.flush_records()

    assert sorted(outcomes) == ["allowed", "blocked"]
    assert {delta.bucket_type for delta in counter_deltas} == {
        AccountControlBucketType.DAY,
        AccountControlBucketType.MINUTE,
    }
    assert all(delta.delta_count == 1 for delta in counter_deltas)
    assert [event.decision for event in events].count(AccountControlDecision.ALLOWED) == 1
    assert [event.decision for event in events].count(AccountControlDecision.BLOCKED) == 1
    assert len({event.event_uid for event in events}) == 2


def test_guard_allows_high_priority_operation_ahead_of_queued_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """高优先级撤单应越过已经排队但尚未获准的普通查询。"""
    guard = AccountControlGuard(
        account_id=12,
        execution_id="exec-priority",
        channel=TradeChannel.CTP,
        policy=resolve_account_control_policy(
            "default",
            AccountControlOverride.model_validate(
                {
                    "operations": {
                        "query_account": {"priority": 100},
                        "cancel_order": {"priority": 0},
                    }
                }
            ),
        ),
        baseline=AccountControlCounterSnapshot(),
        clock=_clock,
    )
    original_resolve = guard._resolve_operation_attempt
    delayed_first_query = False

    def delay_first_query(*, operation: str, symbol: str | None) -> AccountControlResolvedAttempt:
        nonlocal delayed_first_query
        resolved = original_resolve(operation=operation, symbol=symbol)
        if operation == "query_account" and not delayed_first_query:
            delayed_first_query = True
            return AccountControlResolvedAttempt(
                context=resolved.context,
                decision=AccountControlEnforcementDecision.wait(1),
            )
        return resolved

    release_waiters = Event()
    queries_waiting = Event()
    waiter_lock = Lock()
    waiter_count = 0

    def block_waiters(_seconds: float) -> None:
        nonlocal waiter_count
        if release_waiters.is_set():
            Event().wait(0.001)
            return
        with waiter_lock:
            waiter_count += 1
            if waiter_count == 2:
                queries_waiting.set()
        assert release_waiters.wait(timeout=1)

    monkeypatch.setattr(guard, "_resolve_operation_attempt", delay_first_query)
    monkeypatch.setattr(guard, "_sleep", block_waiters)
    completion_order: list[str] = []

    def begin(operation: str) -> None:
        guard.begin_operation(operation).record_outcome("done")
        completion_order.append(operation)

    with ThreadPoolExecutor(max_workers=3) as pool:
        first_query = pool.submit(begin, "query_account")
        while not delayed_first_query:
            Event().wait(0.001)
        second_query = pool.submit(begin, "query_account")
        cancel = pool.submit(begin, "cancel_order")
        try:
            cancel.result(timeout=1)
            assert completion_order == ["cancel_order"]
            assert queries_waiting.wait(timeout=1)
        finally:
            release_waiters.set()
        first_query.result()
        second_query.result()

    assert completion_order == ["cancel_order", "query_account", "query_account"]


def test_guard_records_account_execution_channel_symbol_and_operation() -> None:
    """防护层记录的事件应带上评估所需上下文。"""
    baseline = AccountControlCounterSnapshot()
    baseline.add(
        bucket_type=AccountControlBucketType.DAY,
        bucket_start="2026-03-22T00:00:00",
        operation="query_order",
        delta_count=1,
    )
    guard = AccountControlGuard(
        account_id=9,
        execution_id="exec-ctx",
        channel=TradeChannel("external"),
        policy=resolve_account_control_policy(
            "default",
            AccountControlOverride.model_validate(_override_payload({"query_order": {"per_day": 1}})),
        ),
        baseline=baseline,
        clock=_clock,
    )

    with pytest.raises(AccountControlBlockedError):
        guard.begin_operation("query_order", symbol="ag2612")

    counter_deltas, events = guard.flush_records()

    assert counter_deltas == []
    assert len(events) == 1
    event = events[0]
    assert event.account_id == 9
    assert event.execution_id == "exec-ctx"
    assert event.channel == TradeChannel("external")
    assert event.operation == "query_order"
    assert event.symbol == "ag2612"
    assert event.decision == AccountControlDecision.BLOCKED


def test_guard_is_noop_without_account_id() -> None:
    """缺失 account_id 时应稳定降级为 no-op。"""
    guard = AccountControlGuard(
        account_id=None,
        execution_id="exec-noop",
        channel=TradeChannel("external"),
        policy=resolve_account_control_policy("default"),
        baseline=AccountControlCounterSnapshot(),
        clock=_clock,
    )

    attempt = guard.begin_operation("cancel_order", symbol="rb2610")
    attempt.record_outcome("submitted", metadata={"order_id": "oid-noop"})

    counter_deltas, events = guard.flush_records()

    assert counter_deltas == []
    assert events == []


def test_guard_waits_until_min_interval_ms_elapsed() -> None:
    """最小间隔未到时应等待到达阈值后再放行。"""
    first_moment = datetime(2026, 3, 22, 9, 31, 15, 100000)
    second_moment = datetime(2026, 3, 22, 9, 31, 15, 300000)
    third_moment = datetime(2026, 3, 22, 9, 31, 15, 600000)
    sleep_calls: list[float] = []
    guard = AccountControlGuard(
        account_id=3,
        execution_id="exec-interval",
        channel=TradeChannel("external"),
        policy=resolve_account_control_policy(
            "default",
            AccountControlOverride.model_validate(
                _override_payload({"place_order": {"per_day": 5, "min_interval_ms": 500}})
            ),
        ),
        baseline=AccountControlCounterSnapshot(),
        clock=_clock_from(first_moment, first_moment, second_moment, third_moment),
        sleep=sleep_calls.append,
        wait_poll_interval_ms=500,
    )

    attempt = guard.begin_operation("place_order", symbol="rb2610")
    attempt.record_outcome("submitted", metadata={"order_id": "oid-1"})
    second_attempt = guard.begin_operation("place_order", symbol="rb2610")
    second_attempt.record_outcome("submitted", metadata={"order_id": "oid-2"})

    counter_deltas, events = guard.flush_records()

    assert sleep_calls == pytest.approx([0.3])
    assert {delta.scope_type for delta in counter_deltas} == {
        AccountControlScopeType.ACCOUNT,
        AccountControlScopeType.SYMBOL,
    }
    assert [event.decision for event in events] == [
        AccountControlDecision.ALLOWED,
        AccountControlDecision.ALLOWED,
    ]
    assert events[0].occurred_at_ms == _to_ms(first_moment)
    assert events[1].occurred_at_ms == _to_ms(third_moment)
    wait_details = events[1].metadata["account_control_wait_hits"]
    assert len(wait_details) == 1
    assert wait_details[0]["current_value"] == 200
    assert wait_details[0]["retry_after_ms"] == 300
    assert {key: value for key, value in events[1].metadata.items() if key != "account_control_wait_hits"} == {
        "order_id": "oid-2",
        "waited_ms": 300,
        "priority": 100,
        "groups": [],
        "rules": ["place_order.min_interval_ms"],
    }


def test_guard_uses_default_clock_for_waiting_when_no_sleep_or_clock_is_injected() -> None:
    """默认等待应走 clock 抽象，而不是直接调用真实 time.sleep。"""
    fake_clock = _AdvancingClock(datetime(2026, 3, 22, 9, 31, 15))
    old_clock = get_default_clock()
    set_default_clock(fake_clock)
    try:
        guard = AccountControlGuard(
            account_id=31,
            execution_id="exec-default-clock",
            channel=TradeChannel("external"),
            policy=resolve_account_control_policy(
                "default",
                AccountControlOverride.model_validate(
                    _override_payload({"place_order": {"per_day": 5, "min_interval_ms": 500}})
                ),
            ),
            baseline=AccountControlCounterSnapshot(),
            wait_poll_interval_ms=500,
        )

        first_attempt = guard.begin_operation("place_order", symbol="rb2610")
        first_attempt.record_outcome("submitted", metadata={"order_id": "oid-1"})
        second_attempt = guard.begin_operation("place_order", symbol="rb2610")
        second_attempt.record_outcome("submitted", metadata={"order_id": "oid-2"})

        _, events = guard.flush_records()

        assert fake_clock.sleep_calls == pytest.approx([0.5])
        assert [event.decision for event in events] == [
            AccountControlDecision.ALLOWED,
            AccountControlDecision.ALLOWED,
        ]
        assert events[1].occurred_at_ms - events[0].occurred_at_ms == 500
    finally:
        set_default_clock(old_clock)


def test_guard_does_not_persist_symbol_counters_without_operation_symbol_policy() -> None:
    """未配置 operations.symbol 时，不应写入 symbol 维度计数。"""
    guard = AccountControlGuard(
        account_id=4,
        execution_id="exec-no-symbol-scope",
        channel=TradeChannel("external"),
        policy=resolve_account_control_policy(
            "default",
            AccountControlOverride.model_validate(_override_payload({"query_account": {"account": {"per_day": 10}}})),
        ),
        baseline=AccountControlCounterSnapshot(),
        clock=_clock,
    )

    attempt = guard.begin_operation("query_account", symbol="rb2610")
    attempt.record_outcome("fetched")

    counter_deltas, events = guard.flush_records()

    assert len(events) == 1
    assert events[0].symbol == "rb2610"
    assert {delta.scope_type for delta in counter_deltas} == {AccountControlScopeType.ACCOUNT}


def test_guard_waits_until_next_minute_when_per_minute_quota_is_exhausted() -> None:
    """分钟额度耗尽时应等待到下一个分钟桶，而不是立即阻断。"""
    current = datetime(2026, 3, 22, 9, 31, 59, 800000)
    next_minute = datetime(2026, 3, 22, 9, 32, 0, 0)
    baseline = AccountControlCounterSnapshot()
    baseline.add(
        bucket_type=AccountControlBucketType.MINUTE,
        bucket_start="2026-03-22T09:31:00",
        operation="query_order",
        delta_count=1,
    )
    sleep_calls: list[float] = []
    guard = AccountControlGuard(
        account_id=6,
        execution_id="exec-minute-quota",
        channel=TradeChannel("external"),
        policy=resolve_account_control_policy(
            "default",
            AccountControlOverride.model_validate(_override_payload({"query_order": {"per_minute": 1, "per_day": 5}})),
        ),
        baseline=baseline,
        clock=_clock_from(current, current, next_minute),
        sleep=sleep_calls.append,
        wait_poll_interval_ms=1000,
    )

    attempt = guard.begin_operation("query_order", symbol="rb2610")
    attempt.record_outcome("queried")

    counter_deltas, events = guard.flush_records()

    assert sleep_calls == pytest.approx([0.2])
    assert len(events) == 1
    assert events[0].decision == AccountControlDecision.ALLOWED
    assert events[0].occurred_at_ms == _to_ms(next_minute)
    assert events[0].outcome == "queried"


def test_ctp_order_waits_on_shared_group_after_query() -> None:
    """CTP 查询后下单应共享 TraderApi 全局间隔。"""
    sleep_calls: list[float] = []
    guard = AccountControlGuard(
        account_id=66,
        execution_id="exec-shared-group",
        channel=TradeChannel.CTP,
        policy=resolve_account_control_policy("ctp"),
        baseline=AccountControlCounterSnapshot(),
        clock=_clock_from(
            datetime(2026, 3, 22, 9, 31, 15, 0),
            datetime(2026, 3, 22, 9, 31, 15, 0),
            datetime(2026, 3, 22, 9, 31, 15, 200000),
            datetime(2026, 3, 22, 9, 31, 16, 500000),
        ),
        sleep=sleep_calls.append,
        wait_poll_interval_ms=1500,
    )

    first_attempt = guard.begin_operation("query_account")
    first_attempt.record_outcome("fetched")
    second_attempt = guard.begin_operation("place_order", symbol="rb2610")
    second_attempt.record_outcome("submitted")

    counter_deltas, events = guard.flush_records()

    assert sleep_calls == pytest.approx([1.3])
    assert [event.operation for event in events] == ["query_account", "place_order"]
    assert [delta.operation for delta in counter_deltas].count("query_account") == 2
    assert [delta.operation for delta in counter_deltas].count("place_order") == 4


def test_default_place_order_does_not_use_ctp_shared_group() -> None:
    """default preset 下单不应被 CTP TraderApi 分组污染。"""
    sleep_calls: list[float] = []
    guard = AccountControlGuard(
        account_id=69,
        execution_id="exec-default-order",
        channel=TradeChannel("external"),
        policy=resolve_account_control_policy(
            "default",
            AccountControlOverride.model_validate(
                {
                    "operations": {
                        "place_order": {
                            "account": {"min_interval_ms": {"limit": 1, "on_trigger": "wait"}},
                            "symbol": {"min_interval_ms": {"limit": 1, "on_trigger": "wait"}},
                        }
                    },
                    "groups": {
                        "ctp_td_global": {
                            "min_interval_ms": {"limit": 500, "on_trigger": "wait"},
                        }
                    },
                }
            ),
        ),
        baseline=AccountControlCounterSnapshot(),
        clock=_clock_from(
            datetime(2026, 3, 22, 9, 31, 15, 0),
            datetime(2026, 3, 22, 9, 31, 15, 0),
            datetime(2026, 3, 22, 9, 31, 15, 200000),
        ),
        sleep=sleep_calls.append,
    )

    first_attempt = guard.begin_operation("place_order", symbol="rb2610")
    first_attempt.record_outcome("submitted")
    second_attempt = guard.begin_operation("place_order", symbol="rb2610")
    second_attempt.record_outcome("submitted")

    _, events = guard.flush_records()

    assert sleep_calls == []
    assert [event.operation for event in events] == ["place_order", "place_order"]
    assert all(event.metadata.get("groups") is None for event in events)


def test_guard_applies_group_per_minute_limit_across_baseline_operations() -> None:
    """共享 group 的分钟额度应聚合历史基线中的不同 operation。"""
    current = datetime(2026, 3, 22, 9, 31, 15)
    baseline = AccountControlCounterSnapshot()
    baseline.add(
        bucket_type=AccountControlBucketType.MINUTE,
        bucket_start="2026-03-22T09:31:00",
        operation="query_account",
        delta_count=1,
    )
    guard = AccountControlGuard(
        account_id=67,
        execution_id="exec-shared-group-baseline",
        channel=TradeChannel.CTP,
        policy=resolve_account_control_policy(
            "ctp",
            AccountControlOverride.model_validate(
                {
                    "groups": {
                        "ctp_td_global": {
                            "per_minute": {"limit": 1, "on_trigger": "block"},
                        }
                    }
                }
            ),
        ),
        baseline=baseline,
        clock=lambda: current,
    )

    with pytest.raises(AccountControlBlockedError, match="CTP 交易柜台共享操作组") as caught:
        guard.begin_operation("query_positions")
    detail = caught.value.details
    assert detail is not None
    assert detail.scope_type == "group"
    assert detail.scope_key == "ctp_td_global"
    assert detail.operation == "query_positions"
    assert detail.current_value == detail.limit == 1
    assert "共享操作次数" in str(caught.value)

    counter_deltas, events = guard.flush_records()

    assert counter_deltas == []
    assert len(events) == 1
    assert events[0].operation == "query_positions"
    assert events[0].decision == AccountControlDecision.BLOCKED
    assert events[0].outcome == "policy_blocked"


def test_guard_uses_recent_allowed_snapshot_for_shared_group_min_interval() -> None:
    """共享 group 的最小间隔应吃到历史 execution 的最近 allowed 时间。"""
    previous_allowed = datetime(2026, 3, 22, 9, 31, 15, 400000)
    current = datetime(2026, 3, 22, 9, 31, 15, 600000)
    allowed_after_wait = datetime(2026, 3, 22, 9, 31, 15, 900000)
    recent_snapshot = AccountControlRecentAllowedSnapshot()
    recent_snapshot.add(
        operation="query_account",
        occurred_at_ms=_to_ms(previous_allowed),
    )
    sleep_calls: list[float] = []
    guard = AccountControlGuard(
        account_id=68,
        execution_id="exec-shared-group-recent",
        channel=TradeChannel.CTP,
        policy=resolve_account_control_policy(
            "ctp",
            AccountControlOverride.model_validate(
                {
                    "groups": {
                        "ctp_td_global": {
                            "min_interval_ms": {"limit": 500, "on_trigger": "wait"},
                        }
                    }
                }
            ),
        ),
        baseline=AccountControlCounterSnapshot(),
        recent_allowed_snapshot=recent_snapshot,
        clock=_clock_from(current, current, allowed_after_wait),
        sleep=sleep_calls.append,
        wait_poll_interval_ms=500,
    )

    attempt = guard.begin_operation("query_positions")
    attempt.record_outcome("fetched")

    counter_deltas, events = guard.flush_records()

    assert sleep_calls == pytest.approx([0.3])
    assert len(events) == 1
    assert events[0].operation == "query_positions"
    assert events[0].decision == AccountControlDecision.ALLOWED
    assert events[0].occurred_at_ms == _to_ms(allowed_after_wait)
    assert [delta.operation for delta in counter_deltas].count("query_positions") == 2


def test_guard_waits_across_control_date_boundary_for_min_interval() -> None:
    """跨 control_date 时最小间隔不应因为日切而被重置。"""
    last_allowed = datetime(2026, 3, 21, 23, 59, 59, 900000)
    current = datetime(2026, 3, 22, 0, 0, 0, 100000)
    allowed_after_wait = datetime(2026, 3, 22, 0, 0, 0, 200000)
    recent_snapshot = AccountControlRecentAllowedSnapshot()
    recent_snapshot.add(
        operation="place_order",
        occurred_at_ms=_to_ms(last_allowed),
    )
    sleep_calls: list[float] = []
    guard = AccountControlGuard(
        account_id=5,
        execution_id="exec-midnight",
        channel=TradeChannel("external"),
        policy=resolve_account_control_policy(
            "default",
            AccountControlOverride.model_validate(_override_payload({"place_order": {"min_interval_ms": 300}})),
        ),
        baseline=AccountControlCounterSnapshot(),
        recent_allowed_snapshot=recent_snapshot,
        clock=_clock_from(current, current, allowed_after_wait),
        sleep=sleep_calls.append,
        wait_poll_interval_ms=1000,
    )

    attempt = guard.begin_operation("place_order", symbol="rb2610")
    attempt.record_outcome("submitted")

    counter_deltas, events = guard.flush_records()

    assert sleep_calls == pytest.approx([0.1])
    assert {delta.scope_type for delta in counter_deltas} == {
        AccountControlScopeType.ACCOUNT,
        AccountControlScopeType.SYMBOL,
    }
    assert len(events) == 1
    assert events[0].decision == AccountControlDecision.ALLOWED
    assert events[0].occurred_at_ms == _to_ms(allowed_after_wait)
