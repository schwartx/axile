"""控制上下文分类不改变账户额度，调用来源在线程及异常边界内隔离。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.context import control_source_scope, controlled_source
from axile.executor.account_control.exceptions import AccountControlBlockedError
from axile.executor.account_control.guard import AccountControlGuard
from axile.executor.account_control.models import (
    AccountControlContextKind,
    AccountControlOperationPolicy,
    AccountControlPolicy,
    AccountControlRule,
    AccountControlScopePolicy,
    AccountControlSource,
)
from axile.executor.account_control.snapshot import AccountControlCounterSnapshot


def _guard(kind=AccountControlContextKind.EXECUTION, source=AccountControlSource.ALGORITHM):
    return AccountControlGuard(
        account_id=1,
        execution_id="context-1",
        channel=TradeChannel.CTP,
        context_kind=kind,
        source=source,
        baseline=AccountControlCounterSnapshot(),
        policy=AccountControlPolicy(
            timezone="UTC",
            operations={
                "query_order": AccountControlOperationPolicy(
                    account=AccountControlScopePolicy(per_day=AccountControlRule(limit=2, on_trigger="block")),
                )
            },
        ),
        clock=lambda: datetime(2026, 10, 15, tzinfo=UTC),
    )


def test_account_operation_guard_still_limits_queries_and_emits_typed_records():
    guard = _guard(AccountControlContextKind.ACCOUNT_OPERATION, AccountControlSource.ORDER_QUERY)
    guard.begin_operation("query_order").record_outcome("fetched")
    guard.begin_operation("query_order").record_outcome("fetched")
    with pytest.raises(AccountControlBlockedError):
        guard.begin_operation("query_order")
    deltas, events = guard.flush_records()
    assert len(events) == 3 and events[-1].counted is False
    assert {row.context_kind for row in [*events, *deltas]} == {AccountControlContextKind.ACCOUNT_OPERATION}
    assert {row.source for row in [*events, *deltas]} == {AccountControlSource.ORDER_QUERY}
    assert {row.delta_count for row in deltas} == {2}


def test_manual_source_shares_the_algorithm_quota_and_partitions_persisted_deltas():
    guard = _guard()
    guard.begin_operation("query_order").record_outcome("fetched")
    with control_source_scope(guard, AccountControlSource.ORDER_QUERY):
        guard.begin_operation("query_order").record_outcome("fetched")
    with pytest.raises(AccountControlBlockedError):
        guard.begin_operation("query_order")
    deltas, events = guard.flush_records()
    assert [row.source for row in events] == [
        AccountControlSource.ALGORITHM,
        AccountControlSource.ORDER_QUERY,
        AccountControlSource.ALGORITHM,
    ]
    assert {row.context_kind for row in [*events, *deltas]} == {AccountControlContextKind.EXECUTION}
    for bucket in {row.bucket_type for row in deltas}:
        assert {row.source: row.delta_count for row in deltas if row.bucket_type == bucket} == {
            "algorithm": 1,
            "order_query": 1,
        }
    assert len({row.delta_uid for row in deltas}) == len(deltas)
    assert guard.flush_records() == (deltas, events)


def test_source_scope_does_not_leak_to_another_guard_or_thread_and_restores_after_error():
    guard, other = _guard(), _guard()
    with pytest.raises(ValueError), control_source_scope(guard, AccountControlSource.MANUAL_CANCEL):
        assert controlled_source(guard, guard.source) == AccountControlSource.MANUAL_CANCEL
        assert controlled_source(other, other.source) == AccountControlSource.ALGORITHM
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(controlled_source, guard, guard.source).result() == AccountControlSource.ALGORITHM
        raise ValueError("退出本次操作")
    assert controlled_source(guard, guard.source) == AccountControlSource.ALGORITHM
