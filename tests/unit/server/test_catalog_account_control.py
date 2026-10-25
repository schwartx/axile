"""目录与交易共享限额，独立刷盘及后续基线读取不重复计入同一操作。"""

import asyncio
from contextlib import closing

import pytest

from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.exceptions import AccountControlBlockedError
from axile.executor.account_control.models import AccountControlBucketType
from axile.executor.ctp.ctp_execute import CTPExecutor
from axile.server.account_control import providers
from axile.server.db.models.account import AccountSnapshot
from axile.server.execution import execution_account_control as persistence
from axile.server.execution.catalog_control import catalog_control_scope
from tests.sqlite_oracle import connect as oracle_connect
from tests.unit.server._business_facts_test_support import rows
from tests.unit.server._execution_test_support import build_account, insert_account


def test_independent_catalog_flush_preserves_running_usage_and_audit_ownership(native_database, monkeypatch):
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    for name in ("_guards", "_final", "_updates"):
        monkeypatch.setattr(providers, name, {})

    async def scenario():
        account = build_account(
            portfolio_id=None,
            account_control_preset="ctp",
            account_control_override={
                "groups": {
                    "ctp_td_global": {
                        "per_day": {"limit": 4, "on_trigger": "block"},
                        "min_interval_ms": {"limit": 1, "on_trigger": "wait"},
                    }
                }
            },
        )
        with closing(oracle_connect(native_database)) as connection, connection:
            insert_account(connection, account)
        snapshot = AccountSnapshot.model_validate(account.model_dump())
        executor = CTPExecutor(TradeChannel.CTP)
        active = await persistence.build_account_control_guard(snapshot, "active-execution")
        executor.set_account_control_guard(active)
        active.begin_operation("query_orders").record_outcome("fetched")

        def query_catalog():
            with catalog_control_scope(snapshot, lambda: None) as catalog:
                catalog.share_quota_state(executor._control_quota_state)
                catalog.begin_operation("query_instruments").record_outcome("fetched")

        try:
            await asyncio.to_thread(query_catalog)
            # 第一次目录操作已落库，第二次构建会读到它，额度总量仍只增加一次。
            await asyncio.to_thread(query_catalog)
            active.begin_operation("query_positions").record_outcome("fetched")
            with pytest.raises(AccountControlBlockedError):
                await asyncio.to_thread(query_catalog)
            assert providers.active_guard(1) is active
            deltas = rows(native_database, "account_control_counter_delta")
            daily = [row for row in deltas if row["bucket_type"] == AccountControlBucketType.DAY.name]
            assert sum(row["delta_count"] for row in daily) == 2
            assert {row["source"] for row in daily} == {"instrument_catalog"}
            await persistence.flush_account_control_records(executor)
            await persistence.flush_account_control_records(executor)
            deltas = rows(native_database, "account_control_counter_delta")
            daily = [row for row in deltas if row["bucket_type"] == AccountControlBucketType.DAY.name]
            assert sum(row["delta_count"] for row in daily) == 4
            assert {row["source"] for row in daily} == {"algorithm", "instrument_catalog"}
            events = rows(native_database, "account_control_event")
            assert len(events) == 5 and sum(row["counted"] for row in events) == 4
            execution_events = [row for row in events if row["execution_id"] == "active-execution"]
            assert {row["operation"] for row in execution_events} == {"query_orders", "query_positions"}
            assert all(row["context_kind"] == "execution" for row in execution_events)
            assert all(row["context_kind"] == "account_operation" for row in events if row not in execution_events)
            next_guard = await persistence.build_account_control_guard(snapshot, "next-execution")
            executor.set_account_control_guard(next_guard)
            with pytest.raises(AccountControlBlockedError):
                next_guard.begin_operation("query_instruments")
            await persistence.flush_account_control_records(executor)
        finally:
            providers.finish(active, committed=True)
            executor.close()

    asyncio.run(scenario())
