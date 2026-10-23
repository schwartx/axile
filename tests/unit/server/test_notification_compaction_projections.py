"""通知上下文去重不得丢失已冻结的分析与活动列表投影。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.domain.execution import ExecutionKind, ExecutionTaskStatus
from axile.server.business_facts.extensions import execution_extension
from axile.server.business_facts.notification_context import (
    compact_record_notification_context,
    is_notification_context_reference,
)
from axile.server.business_facts.repository import read_execution_result, store_execution_facts
from axile.server.db.models import ExecuteRecord
from axile.server.db.models.analysis import analysis_state
from axile.server.db.models.execution_intent import ExecutionIntent
from axile.server.db.models.supplement import NotificationEvent, SupplementGroup, SupplementStep
from axile.server.execution.supplement_notifications import finish_supplement_execution
from tests.unit.server._business_analysis_support import install_analysis_triggers
from tests.unit.server._execution_read_oracle import read_execution_summaries, read_projected_trades
from tests.unit.server._execution_test_support import build_account
from tests.unit.server._journal_oracle import _compact_execution_activity

_EXECUTION = "notification-compaction-projections"
_CREATED = "2026-10-20T09:00:10+08:00"


def _result():
    return {
        "status": "PARTIAL",
        "success": False,
        "execution_time": 0.0,
        "account_assets": {
            "available_cash": 0.0,
            "total_asset": 1000.0,
            "market_value": None,
            "source": "real",
            "positions": [{"symbol": "A", "volume": 2, "available_volume": 0}],
        },
        "symbol_results": {
            "A": {
                "status": "PARTIAL",
                "first_tick": {"last_price": 100.0, "bid_price": 100.0, "ask_price": 100.0},
                "sizing": {"current_quantity": 0, "target_quantity": 3, "unit_multiplier": 10},
                "orders": [{"order_id": "o1", "direction": "BUY", "filled_volume": 2}],
                "trades": [
                    {
                        "trade_id": "t1",
                        "order_id": "o1",
                        "trade_price": 101.0,
                        "trade_volume": 2,
                        "trade_time": _CREATED,
                        "extra": {"commission": 0, "commission_asset": "CNY"},
                    }
                ],
            }
        },
        "extra": {
            "server_notification_context": {
                "account": {"id": 1, "name": "frozen-name"},
                "execution": {"status": "PARTIAL", "execution_time": 0.0},
                "orders": [{"order_id": "o1", "filled_volume": 2}],
                "assets": {"total_assets": None, "available_cash": 0.0},
            },
            "plugin_diagnostic": "unchanged native evidence",
        },
    }


@asynccontextmanager
async def _database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'notification-projections.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
            await connection.run_sync(install_analysis_triggers)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            session.add(build_account(portfolio_id=None))
            await session.commit()
        yield factory
    finally:
        await engine.dispose()


async def _seed(factory):
    payload = _result()
    async with factory() as session:
        record = ExecuteRecord(account_id=1, execution_id=_EXECUTION, created_at=_CREATED, is_success=0)
        session.add(record)
        await session.flush()
        await store_execution_facts(session, record, {"curr_target": {"A": 0.25}}, payload)
        await session.commit()
        return record.id


async def _projection_rows(session, record_id):
    return {
        table.name: list(
            (
                await session.execute(
                    sa.select(table).where(table.c.record_id == record_id).order_by(*table.primary_key.columns)
                )
            ).mappings()
        )
        for table in (
            SQLModel.metadata.tables[name]
            for name in ("execution_symbol_result", "execution_symbol_sizing", "business_trade")
        )
    }


async def _activity(session, record):
    summary = (await read_execution_summaries(session, [record]))[record.id]
    trades = (await read_projected_trades(session, [record.id])).get(record.id, [])
    return _compact_execution_activity(record, summary, trades, 1000.0).record


def _event():
    context = deepcopy(_result()["extra"]["server_notification_context"])
    context.update(event_id=f"execution:{_EXECUTION}", event_type="execution.finished")
    context["execution"].update(id=_EXECUTION, notified_at=_CREATED)
    return NotificationEvent(
        id=f"execution:{_EXECUTION}", account_id=1, event_type="execution.finished", context=context
    )


def test_compaction_preserves_projections_and_activity_without_requested_analysis(tmp_path):
    """未请求分析且没有快照时，压缩后成交数和成本也必须立即可读。"""

    async def scenario():
        async with _database(tmp_path) as factory:
            record_id = await _seed(factory)
            async with factory() as session:
                record = await session.get(ExecuteRecord, record_id)
                before = await _projection_rows(session, record_id)
                result_before = await read_execution_result(session, record)
                assert all(before.values())
                activity = await _activity(session, record)
                assert activity.trade_count == 1
                assert activity.summary is not None and activity.summary.cost is not None
                event = _event()
                session.add(event)
                await session.flush()
                assert await compact_record_notification_context(session, record, event)
                assert await _projection_rows(session, record_id) == before
                extra = await session.scalar(
                    sa.select(execution_extension.c.extra).where(execution_extension.c.record_id == record_id)
                )
                assert is_notification_context_reference(extra["server_notification_context"])
                assert not await compact_record_notification_context(session, record, event)
                await session.commit()
            async with factory() as session:
                state = (await session.execute(sa.select(analysis_state))).mappings().one()
                assert state["current_snapshot"] is None and not state["requested"]
                record = await session.get(ExecuteRecord, record_id)
                after_activity = await _activity(session, record)
                assert after_activity.trade_count == activity.trade_count
                assert after_activity.summary == activity.summary
                assert await _projection_rows(session, record_id) == before
                assert await read_execution_result(session, record) == result_before

    asyncio.run(scenario())


def test_real_extension_evidence_change_still_invalidates_projections(tmp_path):
    """压缩后的真实扩展变更仍清除全部派生事实，不恢复旧投影。"""

    async def scenario():
        async with _database(tmp_path) as factory:
            record_id = await _seed(factory)
            async with factory() as session:
                record = await session.get(ExecuteRecord, record_id)
                event = _event()
                session.add(event)
                await session.flush()
                assert await compact_record_notification_context(session, record, event)
                assert all((await _projection_rows(session, record_id)).values())
                version = await session.scalar(sa.select(analysis_state.c.source_version))
                extra = await session.scalar(
                    sa.select(execution_extension.c.extra).where(execution_extension.c.record_id == record_id)
                )
                await session.execute(
                    execution_extension.update()
                    .where(execution_extension.c.record_id == record_id)
                    .values(extra={**extra, "plugin_diagnostic": "changed native evidence"})
                )
                await session.commit()
            async with factory() as session:
                assert all((await _projection_rows(session, record_id)).values())
                assert await session.scalar(sa.select(analysis_state.c.source_version)) > version

    asyncio.run(scenario())


def test_distinct_business_context_is_not_compacted_or_reprojected(tmp_path):
    """outbox 的业务差异不允许引用，也不应改动原始正文与投影。"""

    async def scenario():
        async with _database(tmp_path) as factory:
            record_id = await _seed(factory)
            async with factory() as session:
                record = await session.get(ExecuteRecord, record_id)
                before = await _projection_rows(session, record_id)
                result_before = await read_execution_result(session, record)
                event = _event()
                event.context["orders"][0]["filled_volume"] = 1
                session.add(event)
                await session.flush()
                version = await session.scalar(sa.select(analysis_state.c.source_version))
                assert not await compact_record_notification_context(session, record, event)
                assert await _projection_rows(session, record_id) == before
                assert await read_execution_result(session, record) == result_before
                assert await session.scalar(sa.select(analysis_state.c.source_version)) == version

    asyncio.run(scenario())


def test_compaction_rolls_back_with_outbox(tmp_path):
    """外层失败回滚 outbox、引用及业务事实，不内部提交。"""

    async def scenario():
        async with _database(tmp_path) as factory:
            record_id = await _seed(factory)
            async with factory() as session:
                record = await session.get(ExecuteRecord, record_id)
                before = await _projection_rows(session, record_id)
                result_before = await read_execution_result(session, record)
                state_before = (await session.execute(sa.select(analysis_state))).mappings().one()
            with pytest.raises(RuntimeError, match="synthetic"):
                async with factory() as session:
                    record = await session.get(ExecuteRecord, record_id)
                    event = _event()
                    session.add(event)
                    await session.flush()
                    assert await compact_record_notification_context(session, record, event)
                    assert await _projection_rows(session, record_id) == before
                    raise RuntimeError("synthetic outer transaction failure")
            async with factory() as session:
                record = await session.get(ExecuteRecord, record_id)
                assert await session.scalar(sa.select(NotificationEvent)) is None
                assert await _projection_rows(session, record_id) == before
                assert await read_execution_result(session, record) == result_before
                assert (await session.execute(sa.select(analysis_state))).mappings().one() == state_before

    asyncio.run(scenario())


@pytest.mark.parametrize("existing_event", [False, True])
def test_supplement_finish_keeps_projections_in_real_caller_transaction(tmp_path, existing_event):
    """补发完成路径创建或复用通知时，投影在事务前后都保持完整。"""

    async def scenario():
        async with _database(tmp_path) as factory:
            record_id = await _seed(factory)
            async with factory() as session:
                intent = ExecutionIntent(
                    account_id=1,
                    execution_id=_EXECUTION,
                    kind=ExecutionKind.REBALANCE,
                    trigger_source="manual",
                    status=ExecutionTaskStatus.PARTIAL,
                    channel="ctp",
                    started_at=_CREATED,
                    finished_at=_CREATED,
                )
                group = SupplementGroup(
                    id="notification-compaction-group",
                    account_id=1,
                    fingerprint="notification-compaction-fingerprint",
                    base_scheduled_at=_CREATED,
                    expires_at="2026-10-20T23:00:00+08:00",
                    configured_count=1,
                    effective_count=1,
                )
                step = SupplementStep(
                    group_id=group.id, index=1, scheduled_at=_CREATED, status="running", execution_id=_EXECUTION
                )
                session.add_all([intent, group])
                await session.flush()
                session.add(step)
                if existing_event:
                    session.add(_event())
                await session.commit()
                before = await _projection_rows(session, record_id)
                await finish_supplement_execution(session, intent)
                assert step.status == "finished" and group.status == "completed"
                assert await _projection_rows(session, record_id) == before
                await session.commit()
            async with factory() as session:
                record = await session.get(ExecuteRecord, record_id)
                activity = await _activity(session, record)
                assert activity.trade_count == 1
                assert activity.summary is not None and activity.summary.cost == 20.0
                assert await _projection_rows(session, record_id) == before

    asyncio.run(scenario())
