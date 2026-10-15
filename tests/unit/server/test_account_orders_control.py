"""人工委托操作保持 guard 隔离，执行中的计数在最终收尾时完整落库。"""

import asyncio
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

from axile.executor.account_control.guard import AccountControlGuard
from axile.executor.account_control.presets import resolve_account_control_policy
from axile.executor.account_control.scope import account_control_scope, scoped_guard
from axile.executor.account_control.snapshot import AccountControlCounterSnapshot
from axile.server import account_orders_runtime
from axile.server.account_control import providers
from axile.server.db.models.account_control import AccountControlCounterDelta, AccountControlEvent
from axile.server.execution import execution_account_control
from tests.unit.server._execution_test_support import build_account
from tests.unit.server.test_evidence_export import _account
from tests.unit.server.test_evidence_export import database as database


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


def test_borrowed_guard_does_not_freeze_cumulative_deltas_before_execution_ends(monkeypatch, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'orders.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def setup():
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)

    asyncio.run(setup())
    monkeypatch.setattr(account_orders_runtime, "SessionLocal", factory)
    monkeypatch.setattr(execution_account_control, "SessionLocal", factory)
    account = build_account()
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

        async def verify():
            async with factory() as session:
                assert list((await session.execute(select(AccountControlCounterDelta))).scalars()) == []
                assert len(list((await session.execute(select(AccountControlEvent))).scalars())) == 2
            await execution_account_control.flush_account_control_records(adapter)
            async with factory() as session:
                rows = list((await session.execute(select(AccountControlCounterDelta))).scalars())
                for bucket in {row.bucket_type for row in rows}:
                    selected = [row for row in rows if row.bucket_type == bucket]
                    assert {row.source: row.delta_count for row in selected} == {"algorithm": 2, "manual_cancel": 1}
                assert {row.context_kind for row in rows} == {"execution"}
                events = list((await session.execute(select(AccountControlEvent))).scalars())
                assert [row.source for row in events] == ["algorithm", "manual_cancel", "algorithm"]
                baseline = await execution_account_control.AccountControlStore(session).load_daily_counters(
                    1, rows[0].control_date
                )
                assert (
                    baseline.get_count(
                        bucket_type=rows[0].bucket_type, bucket_start=rows[0].bucket_start, operation="cancel_order"
                    )
                    == 3
                )
            assert providers.active_guard(1) is None

        asyncio.run(verify())
    finally:
        providers.finish(guard, committed=True)
        asyncio.run(engine.dispose())


def test_idle_commands_keep_typed_sessions_and_reload_shared_counters(database, monkeypatch):
    from axile.server.db.models.account_control_session import AccountControlSession

    monkeypatch.setattr(execution_account_control, "SessionLocal", database)
    account, executor = _account().snapshot(), SimpleNamespace()
    for command, operation in (("query", "query_order"), ("cancel", "cancel_order"), ("query", "query_order")):
        with account_orders_runtime.order_control_scope(executor, account, command=command):
            guard = scoped_guard(executor)
            assert guard is not None
            guard.begin_operation(operation).record_outcome("fetched" if command == "query" else "submitted")

    async def verify():
        async with database() as session:
            sessions = list((await session.execute(select(AccountControlSession))).scalars())
            assert len({row.execution_id for row in sessions}) == 3
            assert {row.context_kind for row in sessions} == {"account_operation"}
            assert [row.source for row in sessions] == ["order_query", "manual_cancel", "order_query"]
            assert {row.status for row in sessions} == {"closed"}
            events = list((await session.execute(select(AccountControlEvent))).scalars())
            assert [row.source for row in events] == ["order_query", "manual_cancel", "order_query"]
            baseline = await execution_account_control.AccountControlStore(session).load_daily_counters(
                1, events[-1].control_date
            )
            # 查询可跨分钟边界；按窗口类型汇总，验证共享计数而非假定三次操作在同一分钟。
            buckets = {(bucket_type, operation) for bucket_type, _, operation in baseline.account_counts}
            assert all(
                sum(
                    value
                    for (kind, _, op), value in baseline.account_counts.items()
                    if (kind, op) == (bucket_type, operation)
                )
                == (2 if operation == "query_order" else 1)
                for bucket_type, operation in buckets
            )

    asyncio.run(verify())
