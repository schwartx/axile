"""人工委托操作保持 guard 隔离，累计计数只在执行收尾时写入。"""

import asyncio
import sqlite3
from contextlib import closing
from types import SimpleNamespace

from axile.executor.account_control.guard import AccountControlGuard
from axile.executor.account_control.presets import resolve_account_control_policy
from axile.executor.account_control.scope import account_control_scope, scoped_guard
from axile.executor.account_control.snapshot import AccountControlCounterSnapshot
from axile.server import account_orders_runtime
from axile.server.account_control import providers
from axile.server.account_control.store import AccountControlStore
from axile.server.execution import execution_account_control
from tests.unit.server.test_account_control_store import control_database as control_database


def _account():
    return SimpleNamespace(id=1, trade_channel="ctp", account_control_preset="default", account_control_override=None)


def test_scoped_guard_is_owner_and_context_local_and_restores_after_error():
    owner, other = object(), object()
    guard = AccountControlGuard(
        account_id=1, execution_id="manual", channel="ctp", policy=None, baseline=AccountControlCounterSnapshot()
    )
    assert scoped_guard(owner) is None
    try:
        with account_control_scope(owner, guard):
            assert scoped_guard(owner) is guard
            assert scoped_guard(other) is None
            raise ValueError("中断")
    except ValueError:
        pass
    assert scoped_guard(owner) is None


def test_borrowed_guard_does_not_freeze_cumulative_deltas_before_execution_ends(control_database):
    account = _account()
    guard = AccountControlGuard(
        account_id=1,
        execution_id="active-execution",
        channel=account.trade_channel,
        policy=resolve_account_control_policy("default"),
        baseline=AccountControlCounterSnapshot(),
    )
    adapter = SimpleNamespace(
        get_account_control_guard=lambda: guard, export_account_control_records=guard.flush_records
    )
    providers.register(guard)
    try:
        guard.begin_operation("cancel_order").record_outcome("submitted")
        with account_orders_runtime.order_control_scope(adapter, account, guard, command="cancel"):
            guard.begin_operation("cancel_order").record_outcome("submitted")
        assert providers.active_guard(1) is guard
        guard.begin_operation("cancel_order").record_outcome("submitted")
        with closing(sqlite3.connect(control_database)) as connection, connection:
            assert connection.execute("SELECT COUNT(*) FROM account_control_counter_delta").fetchone() == (0,)
            assert connection.execute("SELECT COUNT(*) FROM account_control_event").fetchone() == (2,)
        asyncio.run(execution_account_control.flush_account_control_records(adapter))
        with closing(sqlite3.connect(control_database)) as connection, connection:
            for bucket in ("DAY", "MINUTE"):
                assert dict(
                    connection.execute(
                        "SELECT source,SUM(delta_count) FROM account_control_counter_delta "
                        "WHERE bucket_type=? GROUP BY source",
                        (bucket,),
                    )
                ) == {"algorithm": 2, "manual_cancel": 1}
            assert list(connection.execute("SELECT DISTINCT context_kind FROM account_control_counter_delta")) == [
                ("execution",)
            ]
            assert list(connection.execute("SELECT source FROM account_control_event ORDER BY id")) == [
                ("algorithm",),
                ("manual_cancel",),
                ("algorithm",),
            ]
        assert providers.active_guard(1) is None
    finally:
        providers.finish(guard, committed=True)


def test_idle_commands_keep_typed_sessions_and_reload_shared_counters(control_database):
    account, executor = _account(), SimpleNamespace()
    for command, operation in (("query", "query_order"), ("cancel", "cancel_order"), ("query", "query_order")):
        with account_orders_runtime.order_control_scope(executor, account, command=command):
            guard = scoped_guard(executor)
            assert guard is not None
            guard.begin_operation(operation).record_outcome("fetched" if command == "query" else "submitted")
    with closing(sqlite3.connect(control_database)) as connection, connection:
        sessions = list(
            connection.execute(
                "SELECT execution_id,context_kind,source,status FROM account_control_session ORDER BY id"
            )
        )
        assert len({row[0] for row in sessions}) == 3
        assert {row[1] for row in sessions} == {"account_operation"}
        assert [row[2] for row in sessions] == ["order_query", "manual_cancel", "order_query"]
        assert {row[3] for row in sessions} == {"closed"}
        date = connection.execute("SELECT control_date FROM account_control_event LIMIT 1").fetchone()[0]
    baseline, _, incomplete = asyncio.run(AccountControlStore().load_baseline(1, date, "Asia/Shanghai"))
    assert not incomplete
    for operation, expected in (("query_order", 2), ("cancel_order", 1)):
        assert (
            sum(
                value
                for (kind, _, op), value in baseline.account_counts.items()
                if kind.value == "day" and op == operation
            )
            == expected
        )


def test_late_idle_cancel_rejection_preserves_existing_monitor_and_updates_original_event(control_database):
    account, executor = _account(), SimpleNamespace()
    resident = AccountControlGuard(
        account_id=1, execution_id="resident", channel="ctp", policy=None, baseline=AccountControlCounterSnapshot()
    )
    providers.register(resident)
    providers.finish(resident, committed=True)
    with account_orders_runtime.order_control_scope(executor, account, command="cancel"):
        manual = scoped_guard(executor)
        assert manual is not None
        attempt = manual.begin_operation("cancel_order", symbol="rb2610")
        attempt.record_outcome("submitted")
    assert providers.active_guard(1) is None
    attempt.record_outcome("broker_rejected", metadata={"error_msg": "柜台拒绝撤单"})
    frame = providers.snapshot(1)
    assert frame["instance_id"] == resident.instance_id
    updates = [row for row in frame["outcome_updates"] if f":{manual.instance_id}:" in row["event_uid"]]
    assert updates[-1]["outcome"] == "broker_rejected"
    asyncio.run(account_orders_runtime._persist_cancel_outcomes(updates))
    with closing(sqlite3.connect(control_database)) as connection, connection:
        assert connection.execute("SELECT COUNT(*),outcome FROM account_control_event").fetchone() == (
            1,
            "broker_rejected",
        )
